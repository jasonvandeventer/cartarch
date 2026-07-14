"""Per-user drawer-sorter rules (#104).

A rule is a collection-search ``query`` string -> a ``target_location``. Rules are
evaluated ascending ``position``, first match wins; unmatched cards fall through
to the legacy drawer sort (or Pending). The query grammar is the SAME parser the
collection search uses (reused verbatim — no DSL, no drift). CRUD/reorder mirror
the DeckGoal (#46) ordered-list pattern.
"""

from __future__ import annotations

from collections.abc import Iterator

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
    or ≥1 drawer location. Replaces the DRAWER_SORTER_USERNAMES username gate (#104):
    the sorter is now open to anyone who sets one up."""
    from app.location_service import user_has_drawers

    if session.query(SorterRule.id).filter(SorterRule.user_id == user_id).first() is not None:
        return True
    return user_has_drawers(session, user_id)


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
