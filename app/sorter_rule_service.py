"""Per-user drawer-sorter rules (#104).

A rule is a collection-search ``query`` string -> a ``target_location``. Rules are
evaluated ascending ``position``, first match wins; unmatched cards fall through
to the legacy drawer sort (or Pending). The query grammar is the SAME parser the
collection search uses (reused verbatim — no DSL, no drift). CRUD/reorder mirror
the DeckGoal (#46) ordered-list pattern.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import func
from sqlalchemy.orm import Session

# The search parser lives in inventory_service; import the pieces we reuse.
# inventory_service imports evaluate_rules LAZILY (inside resort_collection) to
# avoid a cycle.
from app.inventory_service import (
    _term_to_clause,
    _tokenize_search,
    apply_collection_search_filters,
)
from app.models import Card, InventoryRow, SorterRule, StorageLocation

_PARAM_CHUNK = 900  # stay under SQLite's 999 bound-parameter limit


def _chunks(items: list, size: int = _PARAM_CHUNK) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def has_sortable_setup(session: Session, user_id: int) -> bool:
    """True if the user participates in the auto-sorter — i.e. has ≥1 sorter rule
    or ≥1 drawer location. Replaces the old username-frozenset gate (#104):
    the sorter is now open to anyone who sets one up."""
    from app.location_service import user_has_drawers

    if session.query(SorterRule.id).filter(SorterRule.user_id == user_id).first() is not None:
        return True
    return user_has_drawers(session, user_id)


def first_sweep_preview(session: Session, user_id: int) -> list[dict]:
    """Locations whose contents the sorter would sweep on its FIRST run.

    ``StorageLocation.mode`` defaults to ``managed``, and ``resort_collection``
    treats any non-deck ``managed``/``sink`` location as a sortable SOURCE over
    the user's WHOLE collection (``row_ids=None``). So a user who has quietly
    built boxes and binders through the normal UI turns all of them into sorter
    input the moment they create their first rule — a bulk relocation of every
    card they had deliberately filed by hand.

    Returns ``[{location, rows, cards}, ...]`` for the non-empty ones, biggest
    first. Empty list = nothing to warn about. Mirrors ``resort_collection``'s
    source predicate; a location holding no rows is not a warning.
    """
    from app.location_service import SORTABLE_SOURCE_MODES

    rows = (
        session.query(
            StorageLocation,
            func.count(InventoryRow.id),
            func.coalesce(func.sum(InventoryRow.quantity), 0),
        )
        .join(InventoryRow, InventoryRow.storage_location_id == StorageLocation.id)
        .filter(
            StorageLocation.user_id == user_id,
            StorageLocation.type != "deck",
            StorageLocation.mode.in_(SORTABLE_SOURCE_MODES),
            InventoryRow.user_id == user_id,
        )
        .group_by(StorageLocation.id)
        .all()
    )
    return sorted(
        ({"location": loc, "rows": n, "cards": int(qty)} for loc, n, qty in rows),
        key=lambda d: d["cards"],
        reverse=True,
    )


def validate_query(query: str) -> str | None:
    """Return an error message for a bad rule query, or None if valid. Empty is
    valid (matches everything → a catch-all/default rule). Fills the gap that the
    live search silently drops unknown terms."""
    q = (query or "").strip()
    if not q:
        return None
    try:
        tokens = _tokenize_search(q)
    except Exception:
        return "Could not parse the rule query."
    for tok in tokens:
        if tok[0] != "TERM":
            continue
        _, key, value, _neg = tok
        if _term_to_clause(key, value) is None:
            shown = f"{key}:{value}" if key else value
            return f"Unknown or invalid search term: {shown}"
    return None


def evaluate_rules(session: Session, user_id: int, row_ids: set[int] | list[int]) -> dict[int, int]:
    """Map ``row_id -> target_location_id`` for the rows a user's active rules
    claim, first-match-wins in ascending ``position``. One batched query per rule
    (chunked under the SQLite param limit), so cost is O(rules), not O(rows*rules).
    Rows matching no rule are absent from the result (they fall through)."""
    unassigned = set(row_ids)
    if not unassigned:
        return {}
    rules = (
        session.query(SorterRule)
        .filter(SorterRule.user_id == user_id, SorterRule.is_active.is_(True))
        .order_by(SorterRule.position, SorterRule.id)
        .all()
    )
    assigned: dict[int, int] = {}
    for rule in rules:
        if not unassigned:
            break
        matched: set[int] = set()
        for chunk in _chunks(list(unassigned)):
            base = (
                session.query(InventoryRow.id)
                .join(Card, InventoryRow.card_id == Card.id)
                .filter(InventoryRow.id.in_(chunk))
            )
            q = apply_collection_search_filters(base, rule.query or "")
            matched |= {rid for (rid,) in q.all()}
        for rid in matched:
            assigned[rid] = rule.target_location_id
        unassigned -= matched
    return assigned


# ── CRUD + reorder (mirrors deck_service DeckGoal helpers) ───────────────────


def _owned_rule(session: Session, user_id: int, rule_id: int) -> SorterRule | None:
    return (
        session.query(SorterRule)
        .filter(SorterRule.id == rule_id, SorterRule.user_id == user_id)
        .first()
    )


def _owned_location(session: Session, user_id: int, location_id: int) -> StorageLocation | None:
    return (
        session.query(StorageLocation)
        .filter(StorageLocation.id == location_id, StorageLocation.user_id == user_id)
        .first()
    )


def list_sorter_rules(session: Session, user_id: int) -> list[SorterRule]:
    return (
        session.query(SorterRule)
        .filter(SorterRule.user_id == user_id)
        .order_by(SorterRule.position, SorterRule.id)
        .all()
    )


def create_sorter_rule(
    session: Session, user_id: int, query: str, target_location_id: int
) -> SorterRule:
    err = validate_query(query)
    if err:
        raise ValueError(err)
    if _owned_location(session, user_id, target_location_id) is None:
        raise ValueError("Target location not found")
    max_pos = (
        session.query(SorterRule.position)
        .filter(SorterRule.user_id == user_id)
        .order_by(SorterRule.position.desc())
        .limit(1)
        .scalar()
    )
    rule = SorterRule(
        user_id=user_id,
        query=(query or "").strip(),
        target_location_id=target_location_id,
        position=(max_pos or 0) + 1,
        is_active=True,
    )
    session.add(rule)
    session.commit()
    return rule


def edit_sorter_rule(
    session: Session, user_id: int, rule_id: int, query: str, target_location_id: int
) -> SorterRule:
    rule = _owned_rule(session, user_id, rule_id)
    if rule is None:
        raise ValueError("Rule not found")
    err = validate_query(query)
    if err:
        raise ValueError(err)
    if _owned_location(session, user_id, target_location_id) is None:
        raise ValueError("Target location not found")
    rule.query = (query or "").strip()
    rule.target_location_id = target_location_id
    session.commit()
    return rule


def move_sorter_rule(session: Session, user_id: int, rule_id: int, direction: str) -> None:
    """Swap this rule's position with its neighbour ('up' = earlier/higher
    priority). Ordered by (position, id); the id tiebreaker handles tied
    positions deterministically (same as DeckGoal)."""
    rule = _owned_rule(session, user_id, rule_id)
    if rule is None:
        raise ValueError("Rule not found")
    ordered = list_sorter_rules(session, user_id)
    idx = next((i for i, r in enumerate(ordered) if r.id == rule.id), None)
    if idx is None:
        return
    swap = idx - 1 if direction == "up" else idx + 1
    if swap < 0 or swap >= len(ordered):
        return
    other = ordered[swap]
    rule.position, other.position = other.position, rule.position
    session.commit()


def set_sorter_rule_active(session: Session, user_id: int, rule_id: int, active: bool) -> None:
    rule = _owned_rule(session, user_id, rule_id)
    if rule is None:
        raise ValueError("Rule not found")
    rule.is_active = bool(active)
    session.commit()


def delete_sorter_rule(session: Session, user_id: int, rule_id: int) -> None:
    rule = _owned_rule(session, user_id, rule_id)
    if rule is None:
        raise ValueError("Rule not found")
    session.delete(rule)
    session.commit()


TWELVE_DRAWER_LABELS = (
    "A1 · Numbers + A–B",
    "A2 · C",
    "A3 · D–E",
    "A4 · F–H",
    "A5 · I–L",
    "A6 · M",
    "B1 · N–R",
    "B2 · S",
    "B3 · T–V",
    "B4 · W–Z",
    "B5 · Basic lands",
    "B6 · Tokens & proxies",
)


def configure_twelve_drawers(session: Session, user_id: int) -> None:
    """Stage the two-catalog layout without moving or verifying any inventory.

    Caller commits. Existing native locations retain their IDs and contents;
    existing custom rules require manual review rather than silent replacement.
    """
    if list_sorter_rules(session, user_id):
        raise ValueError(
            "You already have sorter rules. Edit them on Locations before changing layouts."
        )
    locations = {
        loc.name: loc
        for loc in session.query(StorageLocation).filter(StorageLocation.user_id == user_id)
    }
    targets = []
    for number, label in enumerate(TWELVE_DRAWER_LABELS, 1):
        name = f"Drawer {number}"
        loc = locations.get(name)
        if loc is not None and loc.type != "drawer":
            raise ValueError(f"{name} already exists as a different location type.")
        if loc is None:
            loc = StorageLocation(user_id=user_id, name=name, type="drawer")
            session.add(loc)
        loc.mode = "managed"
        loc.note = label
        loc.sort_order = number
        targets.append(loc)
    oversized = locations.get("Oversized cards")
    if oversized is not None and oversized.type in ("deck", "considering", "root", "drawer"):
        raise ValueError("Oversized cards already exists as an incompatible location type.")
    if oversized is None:
        oversized = StorageLocation(
            user_id=user_id, name="Oversized cards", type="other", mode="managed"
        )
        session.add(oversized)
    oversized.mode = "managed"
    session.flush()
    rules = [
        ('t:"plane —" or t:phenomenon or t:scheme or t:vanguard', oversized),
        ("is:proxy or t:token or settype:token", targets[11]),
        ("t:basic t:land", targets[10]),
    ]
    for letters, target in zip(
        ("0123456789ab", "c", "de", "fgh", "ijkl", "m", "nopqr", "s", "tuv", "wxyz"),
        targets[:10],
        strict=True,
    ):
        rules.append((" or ".join(f"setprefix:{letter}" for letter in letters), target))
    # An unknown/empty set goes to the first drawer for review, never the
    # legacy $5 value drawer. This also covers future catalog set codes.
    rules.append(("", targets[0]))
    for position, (query, target) in enumerate(rules, 1):
        error = validate_query(query)
        if error:
            raise ValueError(error)
        session.add(
            SorterRule(
                user_id=user_id,
                query=query,
                target_location_id=target.id,
                position=position,
                is_active=True,
            )
        )
    session.flush()
