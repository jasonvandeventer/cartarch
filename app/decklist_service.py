"""Decklist collection check + owned-count aggregation (v3.27.19).

Shared infrastructure for two consumers:

* the **decklist checker** at ``/decklist`` (paste a decklist, see what you
  own and where — wantlist matcher);
* the **count-sorted collection view** (the existing ``/collection`` page
  gains a ``sort=count`` option so all printings of a high-count name
  cluster together by total-owned).

Both consumers route through :func:`name_owned_counts` for the
name-level total. The aggregation is the single ``GROUP BY`` query the
v3.27.10 spec required — see :func:`name_owned_counts` for the
performance contract (single indexed aggregate per page load; no
materialized counts column; no caching).

Request-path network invariant: **nothing here calls Scryfall**. The
checker matches against local data only — a name that does not resolve
becomes a Missing result, never a live fetch. The v3.23.x import-outage
lesson stays enforced.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.import_service import _SECTION_HEADERS, _SET_SUFFIX_RE, _parse_list_line
from app.inventory_service import get_drawer_label
from app.models import Card, InventoryRow, StorageLocation

# ---------------------------------------------------------------------------
# Basic land detection — bucket these into their own section in the
# checker so they don't dominate the "Missing" list. ("Do you have 10
# Forests?" is not a meaningful trade question, but dropping basics
# silently is worse than setting them aside visibly.)
# ---------------------------------------------------------------------------

_BASIC_LAND_NAMES: frozenset[str] = frozenset(
    {
        "plains",
        "island",
        "swamp",
        "mountain",
        "forest",
        "wastes",
        "snow-covered plains",
        "snow-covered island",
        "snow-covered swamp",
        "snow-covered mountain",
        "snow-covered forest",
        "snow-covered wastes",
    }
)


def _is_basic_land(name: str) -> bool:
    return name.strip().lower() in _BASIC_LAND_NAMES


# Quantity-suffix regex for the bare-name fallback below — matches a
# trailing standalone "x4" or "×4" so paste lines like "Sol Ring x4"
# resolve to ("Sol Ring", 4) instead of ("Sol Ring x4", 1). Conservative:
# the suffix has to be the literal LAST token, not embedded.
_TRAILING_XQTY_RE = re.compile(r"\s+[xX×](\d+)\s*$")


def _bare_name_fallback(line: str) -> dict[str, Any] | None:
    """Recognize bare card-name paste lines that lack a leading quantity.

    The import flow's ``_parse_list_line`` requires every card line to
    start with a digit (the import is committing N copies; quantity is
    mandatory). The decklist checker is the opposite contract: "do I
    have a Sol Ring?" should work with a bare ``Sol Ring`` paste —
    quantity defaults to 1.

    This fallback handles the lines ``_parse_list_line`` returns None
    on. It still applies the matching-parity invariant from the spec:
    the extracted ``name`` is the same string the import flow would
    have extracted from ``1 Sol Ring [optional (SET) COLLECTOR]`` — so
    a list that imports cleanly matches cleanly here, AND a list that
    is checker-only (no leading qty) also matches.

    Returns ``None`` for empty / header / comment lines and for lines
    that look like quantities-only or section noise so the caller
    keeps skipping those silently. Always returns ``name`` non-empty
    when not None.
    """
    rest = line.strip()
    if not rest:
        return None

    # Trailing "x4" or "×4" quantity suffix (alternative paste shape:
    # "Sol Ring x4" rather than "4 Sol Ring"). Strip + capture.
    quantity = 1
    m_qty = _TRAILING_XQTY_RE.search(rest)
    if m_qty:
        quantity = max(1, int(m_qty.group(1)))
        rest = rest[: m_qty.start()].strip()

    # Trailing (SET) [COLLECTOR] suffix — same regex the import parser
    # uses. Strip and capture set/collector so the caller can still
    # use them downstream if useful. The matching path is name-based
    # so the suffix is informational; it is NOT used to disqualify a
    # line that otherwise looks like a card name.
    set_code = ""
    collector_number = ""
    set_match = _SET_SUFFIX_RE.search(rest)
    if set_match:
        set_code = (set_match.group(1) or "").lower()
        collector_number = set_match.group(2) or ""
        rest = rest[: set_match.start()].strip()

    if not rest:
        return None

    # Skip pure-numeric strings (quantity-only lines, accidental noise)
    # and lines containing characters that look like JSON / markup — a
    # bare ``Sol Ring`` is fine; ``{"deck":...}`` is not.
    if rest.isdigit():
        return None
    if rest[0] in "{[<\"'":
        return None

    return {
        "name": rest,
        "set_code": set_code,
        "collector_number": collector_number,
        "quantity": quantity,
        "finish": "normal",
        "language": "en",
    }


# ---------------------------------------------------------------------------
# Paste parser — reuses the import flow's ``_parse_list_line`` so a list
# that imports cleanly via /import matches cleanly here (the v3.27.19
# spec's matching-parity requirement). Aggregates duplicate lines (e.g.
# multiple sideboard entries for the same card) into a single decklist
# entry with the summed quantity.
# ---------------------------------------------------------------------------


def parse_decklist_text(
    text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse a pasted decklist into name-aggregated entries + short-form
    entries.

    Returns ``(name_entries, short_entries)``:

    * ``name_entries`` — list of ``{"name", "quantity", "is_basic",
      "line_numbers"}`` dicts, ordered by first appearance in the
      paste. Duplicate lines for the same card name (case-insensitive)
      are aggregated into one entry with the summed quantity.
    * ``short_entries`` — list of ``{"line_number", "set_code",
      "collector_number", "quantity"}`` dicts for paste lines that
      carried only a ``SET COLLECTOR [qty]`` triple with no name. The
      caller resolves these via a local ``Card`` lookup (see
      :func:`resolve_short_form_lines`).

    Section headers (``Deck``, ``Sideboard``, ``Commander``…) and
    comments are skipped silently — same set the import paste-list
    path skips, so a list that imports cleanly produces the same
    entry set here (matching parity with the import flow per the
    v3.27.19 spec).

    Pure parsing — no DB access, no network.
    """
    # Use a dict keyed by lowercased name for in-order aggregation. The
    # 3.7+ insertion-order guarantee gives the caller stable display
    # order (first appearance in the paste wins).
    by_name: dict[str, dict[str, Any]] = {}
    short_form: list[dict[str, Any]] = []

    for line_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower() in _SECTION_HEADERS:
            continue
        parsed = _parse_list_line(stripped)
        if not parsed:
            # v3.27.19 — bare-name fallback. The import parser
            # requires a leading quantity (commits N copies); the
            # checker is "do I have a Sol Ring?" so bare names with
            # no leading qty default to qty=1. Same name extraction
            # semantics — matching parity preserved.
            parsed = _bare_name_fallback(stripped)
            if not parsed:
                continue
        name = (parsed.get("name") or "").strip()
        qty = max(1, int(parsed.get("quantity") or 1))
        if not name:
            short_form.append(
                {
                    "line_number": line_number,
                    "set_code": (parsed.get("set_code") or "").lower(),
                    "collector_number": parsed.get("collector_number") or "",
                    "quantity": qty,
                }
            )
            continue
        key = name.lower()
        if key in by_name:
            by_name[key]["quantity"] += qty
            by_name[key]["line_numbers"].append(line_number)
        else:
            by_name[key] = {
                "name": name,
                "quantity": qty,
                "is_basic": _is_basic_land(name),
                "line_numbers": [line_number],
            }

    return list(by_name.values()), short_form


def resolve_short_form_lines(
    session: Session, short_entries: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve ``SET COLLECTOR`` short-form lines to canonical card names
    via a single local ``Card`` lookup. NEVER calls Scryfall.

    Returns ``(resolved_name_entries, unresolved)`` — resolved entries
    are dropped into the standard name-entry pipeline alongside paste
    lines that carried names; unresolved entries surface to the user
    as "couldn't match (set, collector) locally". Quantities for
    multiple short-form lines that resolve to the same name are
    aggregated (matches the name-entry aggregation in
    :func:`parse_decklist_text`).
    """
    if not short_entries:
        return [], []

    # Single batched lookup keyed on (set_code, collector_number) tuples.
    # Both columns are ``index=True`` on the Card model so this resolves
    # against the existing composite index. tuple_().in_() expresses the
    # pair-wise IN ("WHERE (set_code, collector_number) IN ((:s1,:c1), ...)")
    # so we don't over-fetch by independent set/collector matches.
    keys: set[tuple[str, str]] = {(e["set_code"], e["collector_number"]) for e in short_entries}
    seen: dict[tuple[str, str], str] = {}
    if keys:
        rows = (
            session.query(Card.set_code, Card.collector_number, Card.name)
            .filter(tuple_(Card.set_code, Card.collector_number).in_(list(keys)))
            .all()
        )
        for set_code, collector, name in rows:
            seen[(set_code.lower() if set_code else "", collector or "")] = name

    resolved_by_name: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for entry in short_entries:
        key = (entry["set_code"], entry["collector_number"])
        canonical = seen.get(key)
        if canonical is None:
            unresolved.append(entry)
            continue
        name_key = canonical.lower()
        if name_key in resolved_by_name:
            resolved_by_name[name_key]["quantity"] += entry["quantity"]
            resolved_by_name[name_key]["line_numbers"].append(entry["line_number"])
        else:
            resolved_by_name[name_key] = {
                "name": canonical,
                "quantity": entry["quantity"],
                "is_basic": _is_basic_land(canonical),
                "line_numbers": [entry["line_number"]],
            }
    return list(resolved_by_name.values()), unresolved


# ---------------------------------------------------------------------------
# Owned-count aggregation — the v3.27.19 SHARED infrastructure.
#
# Performance contract (from the roadmap spec, reproduced verbatim):
#
#   * a SINGLE indexed aggregate query per page load (``GROUP BY`` name,
#     ``SUM(quantity)``), with the ``InventoryRow → Card`` join column
#     and the ``Card`` name column indexed;
#   * attached to rows via an application-side dict lookup — NEVER
#     computed per-``InventoryRow`` (the N+1 pattern is the failure
#     mode to avoid);
#   * measured against a realistic stress dataset before shipping.
#
# Existing indexes ALREADY cover the query plan: ``Card.name`` is
# ``index=True`` (models.py:65), ``InventoryRow.card_id`` and
# ``InventoryRow.user_id`` are ``index=True`` (models.py:121-122). No
# new index needed — the migration runner stays untouched.
#
# Caching is explicitly OUT OF SCOPE per the spec: "Do NOT build a
# materialized/cached count column or counts table. A cache introduces
# multi-writer correctness burden that the SQLite-until-v4 posture
# defers to the v4 Postgres migration."
# ---------------------------------------------------------------------------


def name_owned_counts(
    session: Session,
    user_id: int,
    names: Iterable[str] | None = None,
    exclude_location_id: int | None = None,
    exclude_proxies: bool = False,
) -> dict[str, int]:
    """Return ``{lowercased_card_name: total_owned_copies}`` for a user.

    When ``names`` is None, returns counts for EVERY name the user
    owns (the count-sorted collection-view path). When ``names`` is
    a set/list of name strings (case-insensitive), narrows the
    aggregate to just those names (the decklist-checker path).

    The query is a single ``GROUP BY lower(Card.name)`` against
    ``InventoryRow`` joined to ``Card``, filtered by ``user_id``.
    Returns a dict for O(1) application-side lookup — never iterate
    the result and refetch per row.

    "Count all" semantics: copies in built decks (decks are
    StorageLocations) and pending placements all count. Per the spec,
    "you own this" is the question — placement state isn't a filter.

    v3.37.0 — two optional exclusions for the Brew Mode buy-list, both
    default-off so the /decklist + collection-view callers are unchanged:
      * ``exclude_location_id`` drops rows physically in that location
        (the brew deck's own rows) — NULL-location rows still count;
      * ``exclude_proxies`` drops proxy rows.
    Together they answer "do I own a REAL copy OUTSIDE this deck?" — the
    owner-chosen brew "owned" semantics (2026-06-10).
    """
    query = (
        session.query(
            func.lower(Card.name).label("name_key"),
            func.sum(InventoryRow.quantity).label("total"),
        )
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == user_id)
        .group_by(func.lower(Card.name))
    )
    if exclude_location_id is not None:
        query = query.filter(
            (InventoryRow.storage_location_id != exclude_location_id)
            | (InventoryRow.storage_location_id.is_(None))
        )
    if exclude_proxies:
        query = query.filter(InventoryRow.is_proxy.is_(False))
    if names is not None:
        lowered = {n.strip().lower() for n in names if n and n.strip()}
        if not lowered:
            return {}
        query = query.filter(func.lower(Card.name).in_(lowered))
    return {row.name_key: int(row.total or 0) for row in query.all()}


# ---------------------------------------------------------------------------
# Owned-inventory fetch for the checker — for each matched name, return
# the per-printing / per-location detail rows the UI shows (alongside
# the name-level total from :func:`name_owned_counts`). The checker
# uses both: the count to bucket Have/Partial/Missing, the detail rows
# to render "you have these printings, in these locations."
# ---------------------------------------------------------------------------


def _build_full_location_label(
    loc: StorageLocation | None, parent_chain: dict[int, StorageLocation]
) -> str:
    """Compose a richer location label than the bare ``loc.name``.

    v3.27.19 — used by the decklist checker so the user sees:

    * **Drawer 2 – Sets A–D** for drawer-type locations (uses
      :func:`get_drawer_label` from inventory_service, the same labelling
      the existing collection / placement surfaces use).
    * **Deck · Frodo and Sam 2** for deck-type locations (the existing 🔒
      icon also marks these as manual-mode; the prefix makes the type
      explicit so the user doesn't confuse a deck-type location named
      "Frodo and Sam 2" with a binder named the same).
    * **Binder · Mythics** / **Box · Bulk** for binder/box locations —
      type prefix makes the storage shape obvious.
    * **Parent → Child** breadcrumb when ``parent_id`` is set on a non-
      typed (``other``) location, so nested setups read like a path.

    ``parent_chain`` is a pre-fetched ``{id: StorageLocation}`` map of
    every parent the caller might need, so this helper never issues
    its own queries — the caller batches the parent lookup once.
    """
    if loc is None:
        return "Unassigned"

    name = loc.name or "Unknown"
    loc_type = (loc.type or "other").lower()

    if loc_type == "drawer":
        # Existing get_drawer_label gives "Drawer 2 – Sets A–D" via the
        # DRAWER_LABELS dict in inventory_service. Fall back to just the
        # raw name if the drawer number can't be extracted.
        drawer_number = (name.replace("Drawer", "").strip()) if name else ""
        if drawer_number:
            return get_drawer_label(drawer_number)
        return name
    if loc_type == "deck":
        return f"Deck · {name}"
    if loc_type == "binder":
        return f"Binder · {name}"
    if loc_type == "box":
        return f"Box · {name}"

    # "other" (default) — walk the parent chain if it has one so nested
    # setups read as a breadcrumb. Stops at root or a missing parent.
    if loc.parent_id and loc.parent_id in parent_chain:
        parent = parent_chain[loc.parent_id]
        # parent might itself be typed (a deck has children? not today,
        # but be defensive); recurse once via this same helper rather
        # than reimplementing the type prefix logic.
        parent_label = _build_full_location_label(parent, parent_chain)
        return f"{parent_label} → {name}"
    return name


def owned_inventory_for_names(
    session: Session,
    user_id: int,
    names: Iterable[str],
    exclude_location_id: int | None = None,
    exclude_proxies: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch per-row inventory detail for a set of card names.

    Single batched query joining InventoryRow → Card → StorageLocation
    (LEFT JOIN — rows can have NULL ``storage_location_id`` for
    Unassigned items), plus ONE follow-up batched query to load any
    parent StorageLocations referenced by the returned locations (so
    the full-location label can render a breadcrumb without N+1
    parent walks). The result is bucketed in Python by lowercased
    card name; sort within each bucket pushes tradeable copies
    (``mode in {"managed", "sink"}``) ahead of "would-have-to-break-
    something" copies (``mode == "manual"``) so the checker's UI
    surfaces the actionable copies first per spec.

    v3.27.19 — each printing dict carries ``image_url`` (Scryfall card
    image, from the existing Card column) and ``storage_location_full_label``
    (richer than the bare ``loc.name`` — drawer/deck/binder/box type
    prefixes + parent breadcrumb).
    """
    lowered = {n.strip().lower() for n in names if n and n.strip()}
    if not lowered:
        return {}

    query = (
        session.query(InventoryRow, Card, StorageLocation)
        .join(Card, InventoryRow.card_id == Card.id)
        .outerjoin(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
        .filter(
            InventoryRow.user_id == user_id,
            func.lower(Card.name).in_(lowered),
        )
    )
    # v3.37.0 — mirror name_owned_counts' Brew Mode exclusions so the
    # per-printing detail matches the counts (default-off elsewhere).
    if exclude_location_id is not None:
        query = query.filter(
            (InventoryRow.storage_location_id != exclude_location_id)
            | (InventoryRow.storage_location_id.is_(None))
        )
    if exclude_proxies:
        query = query.filter(InventoryRow.is_proxy.is_(False))
    rows = query.all()

    # Batch-fetch parent locations for any rows whose location has a
    # parent_id. One query rather than N parent walks. The dict is
    # then passed into _build_full_location_label for in-Python
    # breadcrumb composition.
    parent_ids = {
        loc.parent_id for _, _, loc in rows if loc is not None and loc.parent_id is not None
    }
    parent_chain: dict[int, StorageLocation] = {}
    if parent_ids:
        parents = session.query(StorageLocation).filter(StorageLocation.id.in_(parent_ids)).all()
        parent_chain = {p.id: p for p in parents}

    # mode-tradeability sort key — lower = surfaced first.
    # managed/sink = easily-tradeable (loose, sortable); manual =
    # in a deck or display case (you'd have to break something).
    # ``ignored`` and any future modes fall to the back.
    _MODE_RANK = {"managed": 0, "sink": 1, "manual": 2, "ignored": 3}

    def _sort_key(row_tuple: tuple) -> tuple:
        row, card, loc = row_tuple
        mode = (loc.mode if loc else "managed").lower()
        return (
            _MODE_RANK.get(mode, 9),
            (card.set_code or ""),
            (card.collector_number or ""),
            row.id,
        )

    buckets: dict[str, list[dict[str, Any]]] = {}
    rows.sort(key=_sort_key)
    for row, card, loc in rows:
        name_key = card.name.lower()
        buckets.setdefault(name_key, []).append(
            {
                "row_id": row.id,
                "card_id": card.id,
                "name": card.name,
                "set_code": card.set_code,
                "collector_number": card.collector_number,
                # v3.27.19 enhancement — Scryfall card thumbnail (existing
                # Card column; no new schema). Rendered tiny in the
                # checker UI; the template swaps /normal/ → /small/ in JS
                # at render time the same way v3.26.1 commander art does.
                "image_url": card.image_url,
                "finish": row.finish,
                "is_proxy": bool(row.is_proxy),
                "is_pending": bool(row.is_pending),
                "quantity": int(row.quantity),
                "storage_location_id": row.storage_location_id,
                "storage_location_name": loc.name if loc else "Unassigned",
                "storage_location_mode": (loc.mode if loc else "managed"),
                # v3.27.19 enhancement — full location label (drawer label
                # / deck prefix / binder prefix / box prefix / parent
                # breadcrumb) rather than the bare loc.name.
                "storage_location_full_label": _build_full_location_label(loc, parent_chain),
                "is_tradeable": (loc.mode if loc else "managed") in ("managed", "sink"),
            }
        )
    return buckets


# ---------------------------------------------------------------------------
# Bucket helper — given a parsed decklist + the owned counts/detail,
# produce the four display sections the checker renders.
# ---------------------------------------------------------------------------


def bucket_decklist_results(
    decklist_entries: Sequence[dict[str, Any]],
    owned_counts: dict[str, int],
    owned_detail: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Group decklist entries into Have / Partial / Missing / Basics.

    Each entry input shape (from :func:`parse_decklist_text`):
        ``{"name", "quantity", "is_basic", "line_numbers"}``

    Output entry shape (each bucket is a list of these):
        ``{"name", "wanted", "owned", "is_basic", "printings": [...]}``

    Bucketing rules:
        * basics → ``"basics"`` regardless of owned vs wanted (do not
          fold "do you have 10 Forests" into Have/Partial — the
          v3.27.19 spec calls this out explicitly);
        * non-basic, ``owned >= wanted`` → ``"have"``;
        * non-basic, ``0 < owned < wanted`` → ``"partial"``;
        * non-basic, ``owned == 0`` → ``"missing"``.

    Within Have / Partial each entry carries the per-printing detail
    rows (sorted tradeable-first by :func:`owned_inventory_for_names`)
    so the template can render "you have these printings, in these
    locations".
    """
    have: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    basics: list[dict[str, Any]] = []

    for entry in decklist_entries:
        name = entry["name"]
        wanted = entry["quantity"]
        is_basic = entry["is_basic"]
        key = name.lower()
        owned = owned_counts.get(key, 0)
        printings = owned_detail.get(key, [])
        result = {
            "name": name,
            "wanted": wanted,
            "owned": owned,
            "is_basic": is_basic,
            "printings": printings,
        }
        if is_basic:
            basics.append(result)
        elif owned >= wanted:
            have.append(result)
        elif owned > 0:
            partial.append(result)
        else:
            missing.append(result)

    return {
        "have": have,
        "partial": partial,
        "missing": missing,
        "basics": basics,
    }


# ---------------------------------------------------------------------------
# Shared owned/missing comparison (v3.37.0) — the single orchestration of
# counts + per-printing detail + bucketing, consumed by BOTH the Decklist
# Check route (no exclusions) and the Brew Mode buy-list (exclude the brew's
# own rows + proxies). Extracted from main.py's /decklist handler so the two
# surfaces can never drift (the v3.36.12 filter-drift lesson).
# ---------------------------------------------------------------------------


def compare_entries_to_owned(
    session: Session,
    user_id: int,
    entries: Sequence[dict[str, Any]],
    *,
    exclude_location_id: int | None = None,
    exclude_proxies: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Compare wanted ``entries`` against the user's owned inventory.

    ``entries`` is the merged want-list shape ``bucket_decklist_results``
    consumes — each ``{"name", "quantity", "is_basic", "line_numbers"}``.
    Runs the one GROUP-BY count + one batched per-printing detail fetch,
    then buckets into Have / Partial / Missing / Basics.

    The two ``exclude_*`` kwargs thread straight through to both queries
    (default-off → byte-identical to the pre-extraction /decklist path).
    """
    names = [e["name"] for e in entries]
    owned_counts = name_owned_counts(
        session,
        user_id,
        names=names,
        exclude_location_id=exclude_location_id,
        exclude_proxies=exclude_proxies,
    )
    owned_detail = owned_inventory_for_names(
        session,
        user_id,
        names,
        exclude_location_id=exclude_location_id,
        exclude_proxies=exclude_proxies,
    )
    return bucket_decklist_results(entries, owned_counts, owned_detail)


def build_brew_buylist(
    session: Session,
    user_id: int,
    deck_rows: Sequence[Any],
    deck_location_id: int,
) -> dict[str, list[dict[str, Any]]]:
    """Owned/missing buy-list for a brew deck (semantics revised 2026-06-11).

    ``deck_rows`` are the brew deck's ``InventoryRow`` objects (``.card``
    eager-loaded). A brew's rows are **self-describing**: a *proxy* row is a
    card the user does NOT own (added as a placeholder), a *real* (non-proxy)
    row is backed by a physical copy that was pulled into the deck on import.
    So the buy-list is simply the deck's own rows bucketed by proxy status —
    *to buy* = proxy copies, *owned* = real copies — regardless of whether the
    physical copy sits inside or outside the deck (owner decision 2026-06-11).

    This SUPERSEDES the v3.37.0 "a real copy must exist OUTSIDE this deck"
    rule, which structurally false-flagged every singleton the user
    deliberately decked: pulling the only copy into the deck dropped its
    outside-count to zero, so a card you own showed as "to buy". Now a real
    deck row counts as owned no matter where the copy physically lives.

    ``session`` / ``user_id`` / ``deck_location_id`` ARE queried again (v3.38.x,
    Option B): a proxy row whose oracle card is owned but lives in ANOTHER deck
    ("owned in Buttercup") is split out of *to buy* into a separate
    **owned_elsewhere** bucket, so the buy-list never tells the owner to
    purchase a card that's sitting in a different deck (2026-06-11 amendment).
    The deck's own rows remain the source of truth for owned-vs-proxy; the only
    new query is a single name-IN lookup over deck-resident real copies. Basics
    are bucketed out (you don't buy basic lands).
    """
    # Aggregate the deck's own rows by card name: wanted = total copies,
    # owned = the non-proxy (real-copy-backed) portion.
    entries: dict[str, dict[str, Any]] = {}
    for row in deck_rows:
        name = row.card.name
        key = name.lower()
        qty = int(row.quantity)
        entry = entries.get(key)
        if entry is None:
            entry = entries[key] = {
                "name": name,
                "wanted": 0,
                "owned": 0,
                "is_basic": _is_basic_land(name),
                "printings": [],
            }
        entry["wanted"] += qty
        if not bool(row.is_proxy):
            entry["owned"] += qty

    # Option B — for entries that are fully proxy (owned == 0, non-basic), find
    # which ones the user actually owns as a REAL copy inside ANOTHER deck. One
    # scoped name-IN query (no N+1, no network); deck-resident rows of the same
    # Card.name, excluding this brew's own location and proxies.
    fully_proxy_names = [
        e["name"] for e in entries.values() if e["owned"] == 0 and not e["is_basic"]
    ]
    deck_resident: dict[str, str] = {}  # name.lower() -> a holding deck's name
    if fully_proxy_names and deck_location_id:
        rows = (
            session.query(Card.name, StorageLocation.name)
            .select_from(InventoryRow)
            .join(Card, InventoryRow.card_id == Card.id)
            .join(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.is_proxy.is_(False),
                StorageLocation.type == "deck",
                StorageLocation.id != deck_location_id,
                func.lower(Card.name).in_({n.lower() for n in fully_proxy_names}),
            )
            .all()
        )
        for card_name, loc_name in rows:
            deck_resident.setdefault(card_name.lower(), loc_name)

    have: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    owned_elsewhere: list[dict[str, Any]] = []
    basics: list[dict[str, Any]] = []
    for entry in entries.values():
        if entry["is_basic"]:
            basics.append(entry)
        elif entry["owned"] >= entry["wanted"]:
            have.append(entry)
        elif entry["owned"] > 0:
            partial.append(entry)
        elif entry["name"].lower() in deck_resident:
            entry["in_deck"] = deck_resident[entry["name"].lower()]
            owned_elsewhere.append(entry)
        else:
            missing.append(entry)
    return {
        "have": have,
        "partial": partial,
        "missing": missing,
        "owned_elsewhere": owned_elsewhere,
        "basics": basics,
    }
