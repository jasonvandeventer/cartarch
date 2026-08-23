"""Single source of truth for the user-facing card-list SORT control (v3.36.11).

Every card-listing/search surface (Collection, Decks, Locations, Showcase,
Share) sorts through the ONE spec defined here, so a sort key means the same
thing everywhere and equal-key rows never reshuffle between requests / HTMX
swaps. Sort is a query param that composes with the existing search + filter
pipeline — it is NOT a new mechanism and NOT client-side JS.

Two mechanisms, both driven by the constants in this module so they stay in
lockstep:

  * **SQL** — the paginated Collection keeps ORDER BY in the query (it must not
    fetch-all for large collections). It uses :func:`rarity_case` here for the
    Rarity sort and ``InventoryRow.quantity`` for Available, so the rank is
    defined once. Engine-agnostic: SQLite now and Postgres in v4 (plain CASE /
    ``.nulls_last()``, no SQLite-only casts).

  * **Python** — for the fetch-all surfaces (Decks, Locations — via
    :func:`sort_inventory_rows`) and the computed Showcase/Share item dicts (via
    :func:`sort_showcase_items`), and for any value unreachable by ORDER BY: the
    WUBRG Color set-order, the finish-aware Price, and the Showcase/Share
    ``available = min(offered, inventory qty)``. Python sorts load the full
    result set into memory; the live surfaces already materialize their rows, so
    this adds no fetch — noted as a perf consideration only for very large lists.

A stable deterministic tiebreaker (name → id) is appended to every spec so
equal-key rows keep a fixed order across swaps (this also fixes the Showcase's
previously-undefined order for bulk-added rows that share an ``added_at``).
"""

from __future__ import annotations

from sqlalchemy import case, func

from app.models import Card
from app.pricing import effective_price

# --- Canonical sort fields ---------------------------------------------------
# The seven fields the shared dropdown must offer, in display order. Surfaces
# may PREPEND their own surface-specific default (e.g. Showcase "Date Added",
# Collection "Date Added"/"Placement"/"Owned Count") — the shared partial just
# renders whatever (key, label) list it is handed. Keep this list to the seven.
CARD_SORT_OPTIONS: list[tuple[str, str]] = [
    ("name", "Name"),
    ("cmc", "Mana Value"),
    ("color", "Color"),
    ("set", "Set"),
    ("rarity", "Rarity"),
    ("price", "Price"),
    ("available", "Quantity Available"),
]

VALID_DIRECTIONS = ("asc", "desc")


def normalize_sort(sort: str | None, options: list[tuple[str, str]]) -> str:
    """Coerce an untrusted sort key to one this surface actually offers.

    An unknown key would otherwise reach ``sort_showcase_items``, which leaves
    the input order untouched — a control that silently does nothing rather than
    an obviously ignored one. Falls back to the first option.
    """
    keys = {k for k, _label in options}
    return sort if sort in keys else (options[0][0] if options else "name")


def normalize_direction(direction: str | None) -> str:
    """Coerce an untrusted direction param to ``asc``/``desc`` (default desc)."""
    return direction if direction in VALID_DIRECTIONS else "desc"


# --- Rarity rank (ascending: common -> ... -> bonus; unknown last) -----------
# Decision (b). Distinct from set_service._RARITY_ORDER, which is a *display*
# order (mythic first); this is the ascending SORT rank.
RARITY_RANK: dict[str, int] = {
    "common": 0,
    "uncommon": 1,
    "rare": 2,
    "mythic": 3,
    "special": 4,
    "bonus": 5,
}
RARITY_RANK_UNKNOWN = 99  # NULL / token / unrecognized rarity -> sorts last


def rarity_rank(rarity: str | None) -> int:
    return RARITY_RANK.get((rarity or "").lower(), RARITY_RANK_UNKNOWN)


def rarity_case():
    """SQLAlchemy CASE mapping ``Card.rarity`` -> the ascending rank above.

    The SQL counterpart of :func:`rarity_rank`, consumed by the live-query
    surfaces (Collection) so the rank is defined exactly once. NULL / unknown
    rarities fall to :data:`RARITY_RANK_UNKNOWN` (last in ascending order),
    matching the Python sorter. Engine-agnostic (plain CASE — SQLite + Postgres).
    """
    whens = [(func.lower(Card.rarity) == name, rank) for name, rank in RARITY_RANK.items()]
    return case(*whens, else_=RARITY_RANK_UNKNOWN)


# --- Color order (decision a) ------------------------------------------------
# Mono in WUBRG order, then multicolor, then colorless. Mirrors the existing
# collection color sort (inventory_service._color_sort_key) so adopting this as
# the shared definition keeps that surface's behavior identical.
COLOR_ORDER: dict[str, int] = {"W": 0, "U": 1, "B": 2, "R": 3, "G": 4}


def color_sort_value(card) -> tuple:
    """Sortable key for a card's printed colors (duck-typed: needs ``.colors``)."""
    colors = (card.colors or "").split()
    if not colors:
        return (6, "")
    if len(colors) > 1:
        return (5, " ".join(colors))
    return (COLOR_ORDER.get(colors[0], 7), colors[0])


# --- Per-surface dropdown option lists ---------------------------------------
# Each surface PREPENDS its surface-specific default/extra options (preserved so
# behavior is unchanged until the user picks a sort — decision e) then offers the
# canonical seven. The shared partial renders whatever (key, label) pairs it is
# given, so the differing internal key for Price ("price" on Showcase/Share,
# "value" on the live-query surfaces, matching each one's existing query branch)
# is invisible to the user — the field set is identical everywhere.
SHOWCASE_SORT_OPTIONS: list[tuple[str, str]] = [
    ("added", "Date Added"),
] + CARD_SORT_OPTIONS

# The trade picker's sort control (2026-08-23). The shared card keys WITHOUT
# "Date Added": the requested side has an added_at and the offered side (plain
# inventory rows) does not, and a control that silently does nothing on one of
# the two panes is worse than not offering it.
PICKER_SORT_OPTIONS: list[tuple[str, str]] = list(CARD_SORT_OPTIONS)

# Live-query surfaces use "value" for Price and keep their own extras.
_LIVE_CANONICAL: list[tuple[str, str]] = [
    ("name", "Name"),
    ("cmc", "Mana Value"),
    ("color", "Color"),
    ("set", "Set"),
    ("rarity", "Rarity"),
    ("value", "Price"),
    ("available", "Quantity Available"),
    ("type", "Type"),
]

COLLECTION_SORT_OPTIONS: list[tuple[str, str]] = (
    [("newest", "Date Added")]
    + _LIVE_CANONICAL
    + [("placement", "Placement"), ("count", "Owned Count")]
)

DECK_SORT_OPTIONS: list[tuple[str, str]] = list(_LIVE_CANONICAL)

LOCATION_SORT_OPTIONS: list[tuple[str, str]] = [("slot", "Slot")] + _LIVE_CANONICAL


# --- Generic stable sort core ------------------------------------------------


def _stable_sort(seq, primary, nulls_last, reverse, tiebreak):
    """Stable, deterministic in-place sort with optional nulls-last partition.

    The tiebreaker is applied first; Python's sort is stable — including with
    ``reverse=True``, which preserves the relative order of equal-key elements —
    so equal-key rows keep the tiebreaker order across requests / HTMX swaps.
    When ``nulls_last`` the None group is partitioned to the end regardless of
    direction (so missing mana values / 0-or-NULL prices never lead a desc sort).
    """
    seq.sort(key=tiebreak)
    if nulls_last:
        non_null = [x for x in seq if primary(x) is not None]
        null_rows = [x for x in seq if primary(x) is None]
        non_null.sort(key=primary, reverse=reverse)
        return non_null + null_rows
    seq.sort(key=primary, reverse=reverse)
    return seq


# --- Showcase / Share item sorting (Python) ----------------------------------
# Operates on the item dicts built by share_service.get_showcase_with_items
# (keys: card, finish, available, effective_price, added_at, id) and
# build_share_display_items (same, but available is under "quantity"). Price +
# available are already computed there, so a Python sort reaches all seven
# fields uniformly (plus the surface-default "added").


def _avail(it):
    # Showcase dicts carry "available"; the sanitized share dict carries the
    # computed available under "quantity" (raw InventoryRow.quantity is never
    # exposed there). Tolerate both so one sorter serves both surfaces.
    return it["available"] if "available" in it else it.get("quantity")


def _price_or_none(value):
    # Decision (c): 0/None price sorts last regardless of direction -> None.
    return value or None


def slot_sort_value(slot: str | None) -> tuple[int, int, str]:
    """Numeric-aware ordering for ``InventoryRow.slot``.

    A slot is a **physical position** — ``resort_collection`` writes
    ``str(index)`` for 1..N — so it is a number wearing a string's clothes, and
    comparing it lexically puts slot 10 immediately after slot 1. On a real
    drawer that is not cosmetic: Drawer 3 holds 1,042 slotted rows and ``slot``
    is the DEFAULT sort for a location page, so the most-used storage view in
    the app was ordered ``1, 10, 100, 1000, …, 2, 20`` — unusable for walking a
    physical drawer, which is the only thing that ordering is for.

    Non-numeric and empty slots sort AFTER every numbered one (leading 1),
    keeping unslotted rows out of the numbered run rather than interleaved with
    it. Distinct from ``inventory_service.collector_sort_key``, which parses a
    printed collector number and must handle suffixes like ``12a``; a slot is
    always an integer this app wrote.
    """
    text = (slot or "").strip()
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text.lower())


# (key_fn, nulls_last). See _stable_sort for null handling.
SHOWCASE_SORT_KEYS = {
    "added": (lambda it: it.get("added_at"), False),
    "name": (lambda it: (it["card"].name or "").lower(), False),
    "cmc": (lambda it: it["card"].cmc, True),
    "color": (lambda it: color_sort_value(it["card"]), False),
    # Collector tiebreaker stays lexical (decision d): cheap + portable.
    "set": (
        lambda it: ((it["card"].set_code or ""), (it["card"].collector_number or "")),
        False,
    ),
    "rarity": (lambda it: rarity_rank(it["card"].rarity), False),
    "price": (lambda it: _price_or_none(it["effective_price"]), True),
    "available": (lambda it: _avail(it), False),
}


def sort_showcase_items(items: list[dict], sort: str, direction: str) -> list[dict]:
    """Sort Showcase/Share item dicts by the shared spec. Unknown keys leave the
    input order untouched (the query's added_at desc)."""
    direction = normalize_direction(direction)
    spec = SHOWCASE_SORT_KEYS.get(sort)
    if spec is None:
        return items
    primary, nulls_last = spec
    return _stable_sort(
        items,
        primary,
        nulls_last,
        reverse=(direction == "desc"),
        # ``id`` is the ShowcaseItem id and is None for a MIRRORED row (#135 —
        # membership computed, no ShowcaseItem to carry an id), so two rows of
        # one card, one curated and one mirrored, compared None with an int and
        # 500'd the page. `or 0` keeps the tiebreak total; mirrored rows tie with
        # each other and the sort is stable, so the order still holds across
        # requests and HTMX swaps.
        tiebreak=lambda it: ((it["card"].name or "").lower(), it.get("id") or 0),
    )


# --- InventoryRow sorting (Python) -------------------------------------------
# For the fetch-all live surfaces that materialize ORM rows then build dicts
# (Decks, Locations). Duck-typed: needs r.card, r.finish, r.quantity, r.slot,
# r.id. Collection is NOT routed here — it is paginated, so it keeps SQL ORDER
# BY (with Python only for color/value/count/placement, as it already did).


def _row_price(r):
    return _price_or_none(effective_price(r.card, r.finish))


# (key_fn, nulls_last). "value" = finish-aware Price; "slot" = location order.
ROW_SORT_KEYS = {
    "name": (lambda r: (r.card.name or "").lower(), False),
    "cmc": (lambda r: r.card.cmc, True),
    "color": (lambda r: color_sort_value(r.card), False),
    "set": (
        lambda r: ((r.card.set_code or ""), (r.card.collector_number or "")),
        False,
    ),
    "rarity": (lambda r: rarity_rank(r.card.rarity), False),
    "value": (_row_price, True),
    "available": (lambda r: r.quantity, False),
    "type": (lambda r: (r.card.type_line or "").lower(), False),
    "slot": (lambda r: slot_sort_value(r.slot), False),
}


# --- Wishlist sorting (Python) -----------------------------------------------
# Operates on the dicts built by watchlist_service.list_watchlist (keys:
# display_name, current_min_price, target_price, placed_count, pending_count,
# added_at, id). A wishlist row is NOT a card list — it can be a name-only watch
# with no Card at all — so it gets its own key set rather than being forced
# through CARD_SORT_OPTIONS (no mana value / colour / rarity to sort by).
WISHLIST_SORT_OPTIONS: list[tuple[str, str]] = [
    ("added", "Date Added"),
    ("name", "Name"),
    ("price", "Current Price"),
    ("target", "Target Price"),
    ("owned", "Owned Count"),
]

WISHLIST_SORT_KEYS = {
    "added": (lambda it: it.get("added_at"), False),
    "name": (lambda it: (it.get("display_name") or "").lower(), False),
    # Same nulls-last rule as everywhere else: an un-priced card must not lead a
    # descending price sort just because its value reads as zero.
    "price": (lambda it: _price_or_none(it.get("current_min_price")), True),
    "target": (lambda it: _price_or_none(it.get("target_price")), True),
    "owned": (lambda it: (it.get("placed_count") or 0) + (it.get("pending_count") or 0), False),
}


def sort_wishlist_items(items: list[dict], sort: str, direction: str) -> list[dict]:
    """Sort watchlist item dicts. Unknown keys leave the input order untouched
    (the query's ``added_at desc``)."""
    direction = normalize_direction(direction)
    spec = WISHLIST_SORT_KEYS.get(sort)
    if spec is None:
        return items
    primary, nulls_last = spec
    return _stable_sort(
        items,
        primary,
        nulls_last,
        reverse=(direction == "desc"),
        tiebreak=lambda it: ((it.get("display_name") or "").lower(), it["id"]),
    )


def sort_inventory_rows(rows: list, sort: str, direction: str) -> list:
    """Sort a materialized list of InventoryRow objects by the shared spec.

    Unknown sort keys fall back to the deterministic tiebreaker (name, id) — a
    sane name-ascending default that also pins order across HTMX swaps.
    """
    direction = normalize_direction(direction)
    tiebreak = lambda r: ((r.card.name or "").lower(), r.id)  # noqa: E731
    spec = ROW_SORT_KEYS.get(sort)
    if spec is None:
        rows.sort(key=tiebreak)
        return rows
    primary, nulls_last = spec
    return _stable_sort(rows, primary, nulls_last, reverse=(direction == "desc"), tiebreak=tiebreak)
