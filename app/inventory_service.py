from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Float as SAFloat
from sqlalchemy import and_, case, cast, func, not_, or_, select, text, tuple_
from sqlalchemy.orm import Session, joinedload

from app import sort_spec
from app.audit_service import log_transaction
from app.import_service import coerce_language_code_strict, normalize_finish
from app.location_service import SORTABLE_SOURCE_MODES, get_location
from app.models import Card, InventoryRow, ShowcaseItem, StorageLocation, TransactionLog
from app.pricing import effective_price
from app.scryfall import card_constructor_kwargs, fetch_card_by_scryfall_id
from app.timeutil import utc_now

PRICE_STALE_DAYS = 7
VALUE_THRESHOLD = 5.0

_BASIC_LAND_NAMES = {"plains", "island", "swamp", "mountain", "forest", "wastes"}

DRAWER_LABELS = {
    "1": "Drawer 1 – Value ($5+)",
    "2": "Drawer 2 – Sets A–D",
    "3": "Drawer 3 – Sets E–L",
    "4": "Drawer 4 – Sets M–R",
    "5": "Drawer 5 – Sets S–Z",
    "6": "Drawer 6 – Numeric sets / basics",
}


def collector_sort_key(value: str | None) -> tuple[int, str, str]:
    text = (value or "").strip().lower()
    match = re.match(r"^(\d+)([a-z]*)$", text)
    if match:
        return (0, f"{int(match.group(1)):09d}", match.group(2))
    return (1, text, "")


def get_drawer_label(drawer: str | None) -> str:
    return DRAWER_LABELS.get(str(drawer or "").strip(), f"Drawer {drawer or '-'}")


def get_location_label(row: InventoryRow) -> str:
    if row.storage_location:
        location = row.storage_location

        if location.type == "drawer":
            drawer_number = location.name.replace("Drawer", "").strip()
            return get_drawer_label(drawer_number)

        return location.name

    return get_drawer_label(row.drawer)


def basic_land_type_sort_key(card: Card) -> tuple[int, str]:
    name = (card.name or "").strip().lower()
    order = {
        "plains": 0,
        "island": 1,
        "swamp": 2,
        "mountain": 3,
        "forest": 4,
        "wastes": 5,
    }
    return (order.get(name, 99), name)


def _is_basic_land_any_kind(card: Card) -> bool:
    """True for any basic-land variant — plain, snow, full-art, showcase, etc.

    Detected via the type_line carrying both a "Basic" supertype AND a basic
    land subtype (Plains/Island/Swamp/Mountain/Forest/Wastes). This handles
    snow basics (whose type_line reads "Basic Snow Land — Plains", *without*
    the "Basic Land" substring) alongside ordinary basics.
    """
    type_line = (card.type_line or "").lower()
    if "basic" not in type_line:
        return False
    return any(name in type_line for name in _BASIC_LAND_NAMES)


_CARD_TRAIT_KEYS = ("full_art", "frame_effects", "set_type", "layout")


def _apply_card_traits(card: Card, payload: dict) -> None:
    """Copy the Scryfall-only trait fields from a normalized payload onto a
    Card, skipping keys the payload doesn't carry so a partial dict never
    clobbers existing values with None."""
    for key in _CARD_TRAIT_KEYS:
        if key in payload:
            setattr(card, key, payload[key])


def card_traits(card: Card) -> dict[str, bool]:
    """Resolve printing traits for a Card — strictly local, never network.

    This runs ~11×/row inside ``assign_drawer``/``drawer_sort_key`` during
    a resort. It MUST NOT make a Scryfall call: a synchronous live fetch
    here turns a resort into minutes of throttled network I/O while
    holding a SQLite transaction, which blocks every other request
    (single-writer) and locks the pod (v3.23.8 incident).

    When ``set_type`` is backfilled, every trait is derived exactly from
    local columns. When it's still NULL (not yet backfilled), fall back
    to a type_line-only best-effort: substitute / empty-type_line-token
    detection is unavailable until the background trait-backfill loop
    populates ``set_type`` (self-heals within minutes of deploy — the
    documented v3.23.7 limitation, just sourced from a background loop
    instead of a per-row live fetch).
    """
    type_line = (card.type_line or "").lower()
    if card.set_type is None:
        return {
            "is_basic_land": "basic land" in type_line,
            "is_full_art": False,
            "is_snow": "snow" in type_line,
            "has_showcase_frame": False,
            "has_extended_art_frame": False,
            "is_token": "token" in type_line,
            "is_token_substitute": False,
            "is_token_set": False,
        }

    try:
        frame_effects = json.loads(card.frame_effects or "[]")
    except (ValueError, TypeError):
        frame_effects = []
    frame_effects_lc = {str(eff).lower() for eff in frame_effects}
    set_type = (card.set_type or "").lower()
    layout = (card.layout or "").lower()

    return {
        "is_basic_land": "basic land" in type_line,
        "is_full_art": bool(card.full_art),
        "is_snow": "snow" in type_line,
        "has_showcase_frame": "showcase" in frame_effects_lc,
        "has_extended_art_frame": "extendedart" in frame_effects_lc,
        "is_token": "token" in type_line,
        "is_token_substitute": (
            set_type == "token" and layout == "normal" and "token" not in type_line
        ),
        "is_token_set": set_type == "token",
    }


def is_basic_land_candidate(card: Card, finish: str) -> bool:
    """True for *plain* basic lands only — normal finish, non-full-art, non-snow.

    Premium basics (foil, full-art, snow, showcase, extended-art) are filtered
    out here and routed to drawer 6's "premium basics" section by
    ``is_premium_basic`` instead.
    """
    if (finish or "").strip().lower() != "normal":
        return False
    if not _is_basic_land_any_kind(card):
        return False
    traits = card_traits(card)
    return not (
        traits["is_full_art"]
        or traits["is_snow"]
        or traits["has_showcase_frame"]
        or traits["has_extended_art_frame"]
    )


def is_premium_basic(card: Card, finish: str) -> bool:
    """True for any non-plain basic land variant.

    Matches when the card is a basic land (any kind, including snow) AND
    at least one of: finish != normal, full_art, snow, showcase frame,
    extended-art frame. Mirror image of :func:`is_basic_land_candidate`.
    """
    if not _is_basic_land_any_kind(card):
        return False
    if (finish or "").strip().lower() != "normal":
        return True
    traits = card_traits(card)
    return bool(
        traits["is_full_art"]
        or traits["is_snow"]
        or traits["has_showcase_frame"]
        or traits["has_extended_art_frame"]
    )


# -----------------------------------------------------------------------------
# Drawer-vs-Bulk routing predicate (v3.38.0)
# -----------------------------------------------------------------------------
# One routing predicate, two call sites — the retroactive cull (Call site A) and
# intake routing (Call site B). See drawer-vs-bulk-routing-design-2026-06-08.md.
# Pure + offline: price is the cached ``Card.price_usd*`` columns, never a
# Scryfall fetch, so the request-path network invariant holds. Per-printing
# grain == ``(card_id, finish)`` — the v3.17.0 merge key (owner F2 decision,
# 2026-06-10; deliberately NO oracle_id grouping).
#
# ``should_keep_in_drawer`` covers the INTRINSIC-protection layers only —
# 1 (basic), 3 (in-deck), 4 (value). Layer 2 (the drawer-presence "always keep
# one findable copy" rule) is keep-exactly-one-of-N quantity arithmetic, not a
# per-row boolean, so both call sites apply it identically as a shared keep-one
# mechanic ON TOP of this predicate rather than folding it in here (one
# predicate + one shared keep-one rule, two call sites — no drift). Layer 5
# (manual keep-list) is Phase 4 / post-v4 schema and intentionally absent.

DRAWER_KEEP_PRICE_THRESHOLD = 1.0


def deck_member_card_ids(session: Session, user_id: int) -> set[int]:
    """Set of ``card_id`` values the user holds in ANY deck location
    (``StorageLocation.type == "deck"``). One query; both routing call sites
    precompute it once and pass it into ``should_keep_in_drawer`` so layer 3
    (in-deck protection) never fires a per-row query inside a loop.
    """
    rows = (
        session.query(InventoryRow.card_id)
        .join(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
        .filter(
            InventoryRow.user_id == user_id,
            StorageLocation.type == "deck",
        )
        .distinct()
        .all()
    )
    return {card_id for (card_id,) in rows}


def should_keep_in_drawer(
    session: Session,
    row: InventoryRow,
    *,
    user_id: int,
    price_threshold: float = DRAWER_KEEP_PRICE_THRESHOLD,
    deck_card_ids: set[int] | None = None,
) -> bool:
    """True when this printing has intrinsic drawer protection (it stays in the
    drawers); False when surplus copies are bulk-eligible.

    Layers, earliest match wins (per the routing design, minus the keeper and
    manual layers handled elsewhere):

      1. Basic land  → keep. Basics are stock you keep a pool of (any kind /
         finish / frame), and this leaves in-deck mana bases alone.
      3. In a deck   → keep. The ``card_id`` appears in one of the user's decks
         — a strong personal-staple signal (finish-agnostic: running any finish
         of a printing protects the printing).
      4. Value       → keep. Cached ``effective_price`` STRICTLY greater than
         ``price_threshold`` (default $1.00).

    Otherwise → False. Pure + offline; no Scryfall fetch. A row whose ``card``
    is not yet loaded keeps (conservative — never bulk something we cannot
    classify). ``deck_card_ids`` may be precomputed by the caller (see
    ``deck_member_card_ids``); when omitted it is resolved once here.
    """
    if deck_card_ids is None:
        deck_card_ids = deck_member_card_ids(session, user_id)
    return _is_intrinsically_protected(
        row.card,
        row.finish,
        row.card_id,
        deck_card_ids=deck_card_ids,
        price_threshold=price_threshold,
    )


def _is_intrinsically_protected(
    card: Card | None,
    finish: str,
    card_id: int,
    *,
    deck_card_ids: set[int],
    price_threshold: float,
) -> bool:
    """Shared core of the routing predicate (layers 1 / 3 / 4), keyed on the
    bare printing fields so BOTH ``should_keep_in_drawer`` (operates on a placed
    InventoryRow, the cull) AND ``split_intake_quantity`` (operates on rows
    being imported, the intake) read ONE source of truth — no drift between the
    two call sites. A None card cannot be classified → protected (never bulk
    something we can't see)."""
    if card is None:
        return True
    if _is_basic_land_any_kind(card):  # layer 1 — basic land stock
        return True
    if card_id in deck_card_ids:  # layer 3 — in any deck (personal staple)
        return True
    if effective_price(card, finish) > price_threshold:  # layer 4 — value floor
        return True
    return False


def _drawer_copy_exists(session: Session, user_id: int, card_id: int, finish: str) -> bool:
    """True when a PLACED copy of this exact printing ``(card_id, finish)`` already
    sits in a drawer-type location. The layer-2 keeper lookup for intake routing:
    if a findable drawer copy already exists, an incoming cheap copy has no keeper
    duty and the whole quantity is bulk-eligible; if none exists, one copy stays.
    Pending rows (the import being processed) are excluded — they aren't in a
    drawer yet."""
    return (
        session.query(InventoryRow.id)
        .join(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.card_id == card_id,
            InventoryRow.finish == finish,
            InventoryRow.is_pending.is_(False),
            StorageLocation.type == "drawer",
        )
        .first()
        is not None
    )


def split_intake_quantity(
    card: Card | None,
    finish: str,
    card_id: int,
    quantity: int,
    *,
    has_drawer_copy: bool,
    deck_card_ids: set[int],
    price_threshold: float = DRAWER_KEEP_PRICE_THRESHOLD,
) -> tuple[int, int]:
    """Split ``quantity`` freshly-acquired copies of one printing into
    ``(drawer_bound, bulk_bound)`` per the routing design (Call site B).

    Intrinsically protected (basic / in-deck / value) → all to drawers.
    Otherwise the layer-2 keeper rule: keep ONE findable drawer copy only when
    none exists yet (``has_drawer_copy`` False); the rest are bulk-bound. The
    single decision both the commit router and the preview summary use, so they
    can never disagree."""
    if quantity <= 0:
        return (0, 0)
    if _is_intrinsically_protected(
        card, finish, card_id, deck_card_ids=deck_card_ids, price_threshold=price_threshold
    ):
        return (quantity, 0)
    keep = 0 if has_drawer_copy else min(1, quantity)
    return (keep, quantity - keep)


def is_token_card(card: Card) -> bool:
    """True when the inventory row holds a token card (vs. a real spell).

    Tokens generally have ``type_line`` starting with "Token" (or containing
    "Token" plus the creature subtype). The separate ``token_inventory`` table
    holds tokens not tracked as collectibles; this helper covers the case
    where a token printing slipped into ``inventory_rows`` via CSV import.

    Most tokens carry "Token" in the type_line (fast path, no network).
    Token-set helper cards like "Day // Night" have an *empty* type_line,
    so fall back to the Scryfall ``set_type == "token"`` trait — but
    exclude substitute cards, which share that set_type yet belong in the
    separate substitutes section (see ``is_substitute_card``).
    """
    if "token" in (card.type_line or "").lower():
        return True
    traits = card_traits(card)
    return traits["is_token_set"] and not traits["is_token_substitute"]


def is_substitute_card(card: Card) -> bool:
    """True when the inventory row holds a Scryfall "substitute" printing.

    Substitute cards (set codes like ``sznr``, ``slci``) are physical
    standard-back cards that represent something else in play — most commonly
    used as proxies for DFC tokens in clear sleeves. Scryfall marks them as
    ``set_type=token`` with ``layout=normal`` and an MTG-style ``type_line``
    (no "token" supertype). They look like regular cards by type_line
    alone, so detection needs the backfilled ``set_type``/``layout``
    columns; unavailable until the background trait-backfill populates
    them (returns False in the interim — documented v3.23.7 limitation).
    """
    return card_traits(card)["is_token_substitute"]


def is_oversized_card(card: Card) -> bool:
    """True when the card is a physically oversized type that won't fit the
    normal card slots — Planechase ``Plane`` / ``Phenomenon`` and Archenemy
    ``Scheme`` / ``Vanguard`` cards. These route to the BACK of drawer 6.

    Detection is by card TYPE, read from the portion of ``type_line`` before
    the em dash (subtypes follow it). Word-level matching is deliberate so
    ``Planeswalker`` — whose type word is "planeswalker", not "plane" — is
    never mistaken for a Plane. Resolves entirely from the local ``type_line``
    column, so it is safe on the request path (no Scryfall fetch).
    """
    head = (card.type_line or "").lower().split("—")[0]
    types = head.split()
    return any(t in types for t in ("plane", "phenomenon", "scheme", "vanguard"))


def assign_drawer(row: InventoryRow) -> int:
    """Return the target drawer number (1-6) for an InventoryRow.

    Priority order (first match wins):
      1. oversized card (Plane/Phenomenon/Scheme/Vanguard) → drawer 6 (back)
      2. value >= VALUE_THRESHOLD ($5) → drawer 1
      3. is_proxy=True → drawer 6 (proxies section)
      4. token card → drawer 6 (tokens section)
      5. substitute card → drawer 6 (substitutes section)
      6. foreign language (language not en/None) → drawer 6 (foreign section)
      7. premium basic → drawer 6 (premium basics section)
      8. plain basic → drawer 6 (plain basics section)
      9. numeric set code or empty set → drawer 6 (numeric sets section)
      10. otherwise: letter-range routes to drawers 2-5

    Oversized cards win over the value rule on purpose: the reason they go to
    drawer 6 is physical size (they don't fit the normal slots), so even a $5+
    Plane can't live in drawer 1. The drawer-6 *section* (vs the drawer number)
    is determined by ``drawer_sort_key`` for the in-drawer sort ordering.
    """
    card = row.card
    finish = row.finish

    if is_oversized_card(card):
        return 6

    price = effective_price(card, finish) or 0.0
    if price >= VALUE_THRESHOLD:
        return 1

    if row.is_proxy:
        return 6
    if is_token_card(card):
        return 6
    if is_substitute_card(card):
        return 6
    language = (row.language or "en").lower()
    if language != "en":
        return 6
    if is_premium_basic(card, finish):
        return 6
    if is_basic_land_candidate(card, finish):
        return 6

    first_char = (card.set_code or "").strip().lower()[:1]
    if not first_char or first_char.isdigit():
        return 6
    if "a" <= first_char <= "d":
        return 2
    if "e" <= first_char <= "l":
        return 3
    if "m" <= first_char <= "r":
        return 4
    if "s" <= first_char <= "z":
        return 5
    return 6


def drawer_sort_key(row: InventoryRow) -> tuple:
    """In-drawer sort key. For drawer 6, a leading section number controls
    top-to-bottom physical layout: 0=numeric sets, 1=foreign, 2=premium
    basics, 3=plain basics, 4=tokens, 5=substitutes, 6=proxies, 7=oversized
    (Plane/Phenomenon/Scheme/Vanguard — physically last, at the back).
    """
    card = row.card
    drawer = assign_drawer(row)
    set_code = (card.set_code or "").strip().lower()
    collector = collector_sort_key(card.collector_number)
    name = (card.name or "").strip().lower()

    if drawer == 1:
        return (set_code, collector, name, row.id)

    if drawer == 6:
        # Section ordering matches the layout the drawer-sorter user
        # physically arranged: numeric → foreign → premium → plain → tokens
        # → substitutes → proxies. Same priority as assign_drawer but encoded
        # as a sort prefix so all section-0 rows sort before section-1 rows.
        first_char = set_code[:1]
        is_numeric_set = bool(first_char) and first_char.isdigit()
        language = (row.language or "en").lower()
        is_foreign = language != "en"
        is_premium = is_premium_basic(card, row.finish)
        is_basic = is_basic_land_candidate(card, row.finish)
        is_token = is_token_card(card)
        is_substitute = is_substitute_card(card)

        # Highest-priority classifications win the section assignment when
        # multiple apply (matches assign_drawer's first-match-wins ordering).
        # Oversized first: its section number is the largest named section, so
        # these sort to the very back of the drawer regardless of any other
        # trait they carry.
        if is_oversized_card(card):
            return (7, set_code, collector, name, row.id)
        if row.is_proxy:
            return (6, set_code, collector, name, row.id)
        if is_token:
            return (4, set_code, collector, name, row.id)
        if is_substitute:
            return (5, set_code, collector, name, row.id)
        if is_foreign:
            return (1, language, set_code, collector, name, row.id)
        if is_premium:
            return (2, basic_land_type_sort_key(card), set_code, collector, name, row.id)
        if is_basic:
            return (3, basic_land_type_sort_key(card), set_code, collector, name, row.id)
        if is_numeric_set:
            return (0, set_code, collector, name, row.id)
        # Fallback — shouldn't happen given assign_drawer's exhaustive rules,
        # but guard against future drift by sorting after every named section.
        return (8, set_code, collector, name, row.id)

    return (set_code, collector, name, row.id)


def get_or_create_card(
    session: Session,
    scryfall_id: str,
    card_data: dict | None = None,
) -> Card | None:
    existing = session.query(Card).filter(Card.scryfall_id == scryfall_id).first()
    if existing:
        payload = card_data
        if payload:
            existing.name = payload["name"]
            existing.set_code = payload["set_code"]
            existing.set_name = payload["set_name"]
            existing.collector_number = payload["collector_number"]
            existing.rarity = payload["rarity"]
            existing.image_url = payload["image_url"]
            existing.type_line = payload["type_line"]
            existing.oracle_text = payload["oracle_text"]
            existing.price_usd = payload["price_usd"]
            existing.price_usd_foil = payload["price_usd_foil"]
            existing.price_usd_etched = payload["price_usd_etched"]
            existing.colors = payload.get("colors")
            existing.color_identity = payload.get("color_identity")
            existing.mana_cost = payload.get("mana_cost")
            existing.cmc = payload.get("cmc")
            _apply_card_traits(existing, payload)
            existing.updated_at = utc_now()
            session.flush()
        elif (
            not existing.image_url
            or not existing.type_line
            or not existing.oracle_text
            or existing.color_identity is None
            or existing.set_type is None
        ):
            payload = fetch_card_by_scryfall_id(scryfall_id)
            if payload:
                existing.name = payload["name"]
                existing.set_code = payload["set_code"]
                existing.set_name = payload["set_name"]
                existing.collector_number = payload["collector_number"]
                existing.rarity = payload["rarity"]
                existing.image_url = payload["image_url"]
                existing.type_line = payload["type_line"]
                existing.oracle_text = payload["oracle_text"]
                existing.price_usd = payload["price_usd"]
                existing.price_usd_foil = payload["price_usd_foil"]
                existing.price_usd_etched = payload["price_usd_etched"]
                existing.colors = payload.get("colors")
                existing.color_identity = payload.get("color_identity")
                existing.mana_cost = payload.get("mana_cost")
                existing.cmc = payload.get("cmc")
                _apply_card_traits(existing, payload)
                existing.updated_at = utc_now()
                session.flush()
        return existing

    payload = card_data or fetch_card_by_scryfall_id(scryfall_id)
    if not payload:
        return None

    # v3.30.21 hotfix — strip scryfall_cards-only keys (produced_tokens)
    # from the payload before Card(). v3.30.11 added produced_tokens as
    # the 22nd seam key; the Card ORM has no produced_tokens column. Without
    # the strip, this Card() at switch-printing time 500s with TypeError.
    # See card_constructor_kwargs docstring in app.scryfall.
    card = Card(**card_constructor_kwargs(payload), updated_at=utc_now())
    session.add(card)
    session.flush()
    return card


def find_matching_inventory_row(
    session: Session,
    user_id: int,
    card_id: int,
    finish: str,
    drawer: str | None,
    slot: str | None,
    is_pending: bool,
) -> InventoryRow | None:
    return (
        session.query(InventoryRow)
        .filter(InventoryRow.user_id == user_id)
        .filter(InventoryRow.card_id == card_id)
        .filter(InventoryRow.finish == finish)
        .filter(InventoryRow.drawer == drawer)
        .filter(InventoryRow.slot == slot)
        .filter(InventoryRow.is_pending == is_pending)
        .first()
    )


def create_or_merge_inventory_row(
    session: Session,
    user_id: int,
    card_id: int,
    finish: str,
    quantity: int,
    drawer: str | None = None,
    slot: str | None = None,
    is_pending: bool = True,
    notes: str | None = None,
) -> InventoryRow:
    existing = find_matching_inventory_row(
        session=session,
        user_id=user_id,
        card_id=card_id,
        finish=finish,
        drawer=drawer,
        slot=slot,
        is_pending=is_pending,
    )

    if existing:
        existing.quantity += quantity
        existing.updated_at = utc_now()
        if notes:
            existing.notes = notes
        session.flush()
        return existing

    row = InventoryRow(
        user_id=user_id,
        card_id=card_id,
        finish=finish,
        quantity=quantity,
        drawer=drawer,
        slot=slot,
        is_pending=is_pending,
        notes=notes,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(row)
    session.flush()
    return row


def add_card_to_location(
    session: Session,
    *,
    user_id: int,
    location_id: int,
    scryfall_id: str,
    finish: str = "normal",
    quantity: int = 1,
    language: str = "en",
    is_proxy: bool = False,
    notes: str | None = None,
) -> InventoryRow | None:
    """Quick-add a single card directly into a StorageLocation (v3.32.x).

    The acquisition primitive behind the location quick-add modal. Unlike
    ``create_or_merge_inventory_row`` (which keys placement on drawer/slot,
    defaults to pending, and never sets ``storage_location_id``), this places
    the card AT the location as a non-pending row and merges into an existing
    matching placed row using the same key the bulk-place path uses:
    ``(user_id, card_id, finish, coalesce(language,'en'), is_proxy,
    storage_location_id, is_pending=False)`` — so language / proxy / finish
    differences create distinct rows rather than silently folding together.

    Ownership-checked: returns ``None`` if the location isn't owned by
    ``user_id``. Returns ``None`` if the card can't be resolved (unknown
    scryfall_id). ``get_or_create_card`` does at most ONE Scryfall fetch on
    cache miss — a single user-driven lookup, not a per-row loop, so the
    request-path network invariant holds. Commits on success (mirrors the
    bulk-place path; ``get_db_session`` does not auto-commit).
    """
    location = get_location(session, location_id=location_id, user_id=user_id)
    if location is None:
        return None

    quantity = max(1, min(int(quantity), 99))
    finish = normalize_finish(finish)
    language = (language or "en").strip().lower() or "en"
    is_proxy = bool(is_proxy)
    notes = (notes or "").strip() or None

    card = get_or_create_card(session, scryfall_id)
    if card is None:
        return None

    now = utc_now()
    existing = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.card_id == card.id,
            InventoryRow.finish == finish,
            func.coalesce(InventoryRow.language, "en") == language,
            InventoryRow.is_proxy == is_proxy,
            InventoryRow.storage_location_id == location.id,
            InventoryRow.is_pending.is_(False),
        )
        .first()
    )
    if existing is not None:
        existing.quantity += quantity
        existing.updated_at = now
        if notes:
            existing.notes = notes
        session.commit()
        return existing

    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        storage_location_id=location.id,
        finish=finish,
        quantity=quantity,
        language=language,
        is_proxy=is_proxy,
        is_pending=False,
        notes=notes,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
    return row


def _parse_numeric_op(value: str) -> tuple[str, float] | None:
    for op in (">=", "<=", ">", "<"):
        if value.startswith(op):
            try:
                return op, float(value[len(op) :])
            except ValueError:
                return None
    try:
        return "=", float(value)
    except ValueError:
        return None


def _tokenize_search(search: str) -> list[tuple]:
    """
    Tokenize a Scryfall-style search string into a flat list of tokens.

    Token types:
      ('OR',)
      ('AND',)
      ('LPAREN',)
      ('RPAREN',)
      ('TERM', key_or_None, value, negated)  — key is lowercased, value is lowercased
    """
    tokens: list[tuple] = []
    i = 0
    n = len(search)

    while i < n:
        if search[i].isspace():
            i += 1
            continue

        if search[i] == "(":
            tokens.append(("LPAREN",))
            i += 1
            continue

        if search[i] == ")":
            tokens.append(("RPAREN",))
            i += 1
            continue

        # Optional leading negation
        negated = False
        if search[i] == "-" and i + 1 < n and not search[i + 1].isspace() and search[i + 1] != ")":
            negated = True
            i += 1

        # Quoted bare name: "multi word"
        if i < n and search[i] == '"':
            j = search.find('"', i + 1)
            j = j if j != -1 else n
            value = search[i + 1 : j].lower()
            i = j + 1
            tokens.append(("TERM", None, value, negated))
            continue

        # Read until next whitespace or unquoted paren
        j = i
        while j < n and not search[j].isspace() and search[j] not in "()":
            if search[j] == '"':
                end = search.find('"', j + 1)
                j = (end + 1) if end != -1 else n
            else:
                j += 1

        raw = search[i:j]
        i = j

        if not raw:
            continue

        # OR / AND keywords (case-insensitive)
        if not negated and raw.upper() == "OR":
            tokens.append(("OR",))
        elif not negated and raw.upper() == "AND":
            tokens.append(("AND",))
        elif ":" in raw:
            colon = raw.index(":")
            key = raw[:colon].lower()
            val = raw[colon + 1 :]
            if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                val = val[1:-1]
            # not:X is syntactic sugar for negated is:X
            if key == "not":
                tokens.append(("TERM", "is", val.lower(), not negated))
            else:
                tokens.append(("TERM", key, val.lower(), negated))
        else:
            tokens.append(("TERM", None, raw.lower(), negated))

    return tokens


# Guild / shard / wedge / mono / colorless names → WUBRG letters. Used by the
# `c:` and `id:` search terms so a value like "izzet" resolves to "UR" before
# the per-letter scan (which otherwise extracts zero color letters from a name,
# silently producing the wrong filter — colorless-only for `id:`, a no-op for
# `c:`). 4-color nicknames are deliberately omitted (rarely typed, error-prone).
COLOR_NAME_ALIASES = {
    # mono
    "white": "W",
    "blue": "U",
    "black": "B",
    "red": "R",
    "green": "G",
    "colorless": "C",
    # guilds (2c)
    "azorius": "WU",
    "dimir": "UB",
    "rakdos": "BR",
    "gruul": "RG",
    "selesnya": "GW",
    "orzhov": "WB",
    "izzet": "UR",
    "golgari": "BG",
    "boros": "RW",
    "simic": "GU",
    # shards (allied 3c)
    "bant": "GWU",
    "esper": "WUB",
    "grixis": "UBR",
    "jund": "BRG",
    "naya": "RGW",
    # wedges (enemy 3c)
    "abzan": "WBG",
    "jeskai": "URW",
    "sultai": "BGU",
    "mardu": "RWB",
    "temur": "GUR",
}


def expand_color_alias(value: str) -> str:
    """Map a guild/shard/wedge/mono/colorless name to WUBRG letters.

    Strips a leading comparison operator (``id:`` ignores it anyway) so
    ``"<=izzet"`` resolves the same as ``"izzet"``. Non-alias values pass
    through unchanged, so bare letters (``"ur"``) and full sets (``"wubrg"``)
    still work.
    """
    v = value.strip().lstrip("<>=:").strip().lower()
    return COLOR_NAME_ALIASES.get(v, value)


def _term_to_clause(key: str | None, value: str):
    """Convert a single parsed term to a SQLAlchemy filter clause, or None."""
    if not value:
        return None

    if key is None:
        return Card.name.ilike(f"%{value}%")

    if key in ("t", "type"):
        return Card.type_line.ilike(f"%{value}%")
    if key in ("o", "oracle"):
        return Card.oracle_text.ilike(f"%{value}%")
    if key in ("s", "set"):
        return Card.set_code.ilike(f"%{value}%")
    if key in ("r", "rarity"):
        return Card.rarity.ilike(f"%{value}%")
    if key == "finish":
        return InventoryRow.finish == value
    if key == "drawer":
        return InventoryRow.drawer == value
    if key in ("lang", "language"):
        # Accept Scryfall codes ("ja"), long names ("japanese"), and country-
        # code aliases ("jp") — same alias surface as the paste-list `*XX*`
        # marker. Unknown input returns a clause that matches nothing rather
        # than silently coercing to English.
        code = coerce_language_code_strict(value)
        if code is None:
            if not (value or "").strip():
                return None
            return InventoryRow.language == "__no_match__"
        # Treat NULL as "en" so historic rows imported before the language
        # column existed answer `lang:en` correctly.
        if code == "en":
            return or_(InventoryRow.language == "en", InventoryRow.language.is_(None))
        return InventoryRow.language == code
    if key in ("c", "color", "colors"):
        value = expand_color_alias(value)
        color_clauses = []
        for letter in value.upper():
            if letter in "WUBRG":
                color_clauses.append(Card.colors.contains(letter))
            elif letter == "C":
                color_clauses.append((Card.colors == None) | (Card.colors == ""))  # noqa: E711
        if not color_clauses:
            return None
        return and_(*color_clauses) if len(color_clauses) > 1 else color_clauses[0]
    if key == "id":
        # Color identity "within" filter: card's identity must be a subset of the given colors.
        # Uses Card.color_identity (space-sep WUBRG, "" = colorless, NULL = not yet fetched).
        # NULL cards are excluded — we can't confirm they fit the identity.
        value = expand_color_alias(value)
        excluded = [lt for lt in "WUBRG" if lt not in value.upper()]
        if not excluded:
            return None  # id:wubrg matches everything
        clauses = [not_(Card.color_identity.contains(lt)) for lt in excluded]
        return and_(*clauses) if len(clauses) > 1 else clauses[0]
    if key in ("n", "name"):
        return Card.name.ilike(f"%{value}%")
    if key == "is":
        if value == "foil":
            return InventoryRow.finish == "foil"
        if value in ("nonfoil", "non-foil"):
            return InventoryRow.finish == "normal"
        if value == "etched":
            return InventoryRow.finish == "etched"
        if value == "commander":
            return InventoryRow.role == "commander"
        return None
    if key in ("qty", "q", "quantity"):
        parsed = _parse_numeric_op(value)
        if parsed is None:
            return None
        op, val = parsed
        qty = InventoryRow.quantity
        if op == "=":
            return qty == int(val)
        if op == ">":
            return qty > int(val)
        if op == "<":
            return qty < int(val)
        if op == ">=":
            return qty >= int(val)
        if op == "<=":
            return qty <= int(val)
    if key in ("price", "usd"):
        parsed = _parse_numeric_op(value)
        if parsed is None:
            return None
        op, val = parsed
        # v3.28.8 fix — finish-aware via _effective_price_expr (foil →
        # price_usd_foil with price_usd fallback; etched → price_usd_etched
        # with foil/normal fallback; else price_usd). Pre-v3.28.8 cast
        # Card.price_usd directly, which silently let foil rows with cheap
        # foil prices pass a `price:>=N` filter when their normal was ≥N
        # but their displayed price was the cheap foil.
        price_col = _effective_price_expr()
        if op == "=":
            return price_col == val
        if op == ">":
            return price_col > val
        if op == "<":
            return price_col < val
        if op == ">=":
            return price_col >= val
        if op == "<=":
            return price_col <= val
    if key == "legal":
        fmt = value.lower()
        return func.json_extract(Card.legalities, f"$.{fmt}") == "legal"
    if key == "banned":
        fmt = value.lower()
        return func.json_extract(Card.legalities, f"$.{fmt}") == "banned"
    if key in ("m", "mana"):
        return Card.mana_cost.ilike(f"%{value}%")
    if key == "cmc":
        parsed = _parse_numeric_op(value)
        if parsed is None:
            return None
        op, val = parsed
        if op == "=":
            return Card.cmc == val
        if op == ">":
            return Card.cmc > val
        if op == "<":
            return Card.cmc < val
        if op == ">=":
            return Card.cmc >= val
        if op == "<=":
            return Card.cmc <= val

    return None


def _parse_search_expr(tokens: list[tuple], pos: int) -> tuple:
    """Top-level: parse OR-separated AND-expressions."""
    clauses = []
    clause, pos = _parse_and_expr(tokens, pos)
    if clause is not None:
        clauses.append(clause)

    while pos < len(tokens) and tokens[pos][0] == "OR":
        pos += 1
        clause, pos = _parse_and_expr(tokens, pos)
        if clause is not None:
            clauses.append(clause)

    if not clauses:
        return None, pos
    if len(clauses) == 1:
        return clauses[0], pos
    return or_(*clauses), pos


def _parse_and_expr(tokens: list[tuple], pos: int) -> tuple:
    """Parse implicitly/explicitly AND-joined atoms."""
    clauses = []
    clause, pos = _parse_atom(tokens, pos)
    if clause is not None:
        clauses.append(clause)

    while pos < len(tokens) and tokens[pos][0] not in ("OR", "RPAREN"):
        if tokens[pos][0] == "AND":
            pos += 1
        clause, pos = _parse_atom(tokens, pos)
        if clause is not None:
            clauses.append(clause)

    if not clauses:
        return None, pos
    if len(clauses) == 1:
        return clauses[0], pos
    return and_(*clauses), pos


def _parse_atom(tokens: list[tuple], pos: int) -> tuple:
    """Parse a single term or a parenthesized sub-expression."""
    if pos >= len(tokens):
        return None, pos

    tok = tokens[pos]

    if tok[0] == "LPAREN":
        pos += 1
        clause, pos = _parse_search_expr(tokens, pos)
        if pos < len(tokens) and tokens[pos][0] == "RPAREN":
            pos += 1
        return clause, pos

    if tok[0] == "TERM":
        _, key, value, negated = tok
        clause = _term_to_clause(key, value)
        if clause is not None and negated:
            clause = not_(clause)
        return clause, pos + 1

    # OR/AND/RPAREN in unexpected position — skip
    return None, pos + 1


# v3.28.8 post-ship fix — finish-aware effective price expression. The
# pre-v3.28.8 `price:` search keyword cast Card.price_usd directly, which
# is the NORMAL price — a foil row with a cheap foil price but an
# expensive normal would pass `price:>=5` even though its display price
# was the cheap foil. Same shape as the v3.27.10 dashboard's
# _placed_value_expr — mirrors app.pricing.effective_price's
# finish-fallback order so SQL filtering matches what the user actually
# sees on the row. Used by the new v3.28.8 facet price range AND
# retroactively fixes the `price:` boolean-search keyword.
def _effective_price_expr():
    # v4-prep (pg-readiness "Type affinity" finding / cutover-checklist price
    # audit): the price_usd* columns are TEXT and get CAST
    # to Float here. SQLite coerces leniently (CAST('' AS REAL) -> 0.0); Postgres
    # is STRICT and ERRORs on CAST('' AS double precision) -> 'invalid input
    # syntax', which would 500 the price:/usd: search keyword and the facet price
    # range post-cutover. NULLIF(col, '') maps an empty string to NULL so the
    # cast is safe on both dialects (CAST(NULL) -> NULL). The 2026-06-03 prod
    # scan found ZERO empty and ZERO non-numeric values across all three columns
    # (Scryfall always sends a decimal string or nothing), so this is a defensive
    # guard, not a cleanup -- and it is behavior-identical on today's SQLite (a
    # real price is never '', so NULLIF is a no-op on every actual row).
    def nz(col):
        return func.nullif(col, "")

    return cast(
        case(
            (
                InventoryRow.finish == "foil",
                func.coalesce(nz(Card.price_usd_foil), nz(Card.price_usd)),
            ),
            (
                InventoryRow.finish == "etched",
                func.coalesce(
                    nz(Card.price_usd_etched), nz(Card.price_usd_foil), nz(Card.price_usd)
                ),
            ),
            else_=nz(Card.price_usd),
        ),
        SAFloat,
    )


def apply_collection_search_filters(query, search: str):
    if not search.strip():
        return query

    tokens = _tokenize_search(search)
    if not tokens:
        return query

    try:
        clause, _ = _parse_search_expr(tokens, 0)
    except Exception:
        return query

    if clause is not None:
        query = query.filter(clause)

    return query


# v3.28.8 — faceted-sidebar filters for the Collection redesign. Each facet
# uses OR semantics WITHIN itself (e.g., types="Creature,Instant" matches
# Creature OR Instant) and AND ACROSS facets (Creature-or-Instant AND
# Placed AND Foil). The boolean search parser (above) AND-composes on top of
# the facet predicates — search and facets are independent dimensions per
# the spec's Option A. All facets are backed by existing schema; no migration.
def apply_collection_facet_filters(
    session: Session,
    query,
    user_id: int,
    *,
    facet_colors: str = "",
    facet_types: str = "",
    facet_status: str = "",
    facet_finishes: str = "",
    facet_price_min: float | None = None,
    facet_price_max: float | None = None,
):
    """Apply faceted-sidebar filters to a Collection query.

    ``facet_colors`` — Letter string from WUBRG (e.g., "WU"). Commander-legal
    "within" semantics: a card matches iff its ``color_identity`` is a SUBSET
    of the requested letters — i.e. the card is castable in a Commander deck
    of those colors (à la Scryfall ``id<=``). Colorless cards (identity "")
    are a subset of any selection, so they auto-match; NULL identity (not yet
    fetched) can't be confirmed and is excluded. ``C`` selected ALONE filters
    to colorless only; ``C`` alongside colors is a redundant no-op.

    ``facet_types`` — CSV of type tokens (e.g., "Creature,Instant"). OR
    semantics: matches if ``type_line`` ILIKEs any of the listed tokens.

    ``facet_status`` — CSV of "placed", "pending", "in_deck", "watchlist".
    OR semantics. ``watchlist`` is matched via both v3.27.12 identity modes
    (card_id OR card_name).

    ``facet_finishes`` — CSV of "normal", "foil", "etched". OR semantics
    via IN clause.

    ``facet_price_min`` / ``facet_price_max`` — floats. Both ends optional.
    """
    from app.models import WatchlistItem

    if facet_colors:
        upper = facet_colors.upper()
        selected = [c for c in upper if c in "WUBRG"]
        if selected:
            # Commander-legal "within" filter (v3.32.x): a card matches iff its
            # color_identity is a SUBSET of the selected colors — castable in a
            # Commander deck of those colors (à la Scryfall id<=). Mirrors the
            # boolean-search `id:` filter (parse_query_term, key=="id"): exclude
            # any card whose identity contains a NON-selected color. Colorless
            # cards (identity="") are a subset of any selection → auto-match;
            # NULL identity (not yet fetched) can't be confirmed → excluded. The
            # "C" pip alongside colors is redundant (colorless already matches),
            # so it is intentionally a no-op here.
            for letter in [lt for lt in "WUBRG" if lt not in selected]:
                query = query.filter(not_(Card.color_identity.contains(letter)))
        elif "C" in upper:
            # C selected alone — colorless only (identity ""; NULL tolerated as
            # "no colors known"). Preserves the prior C-pip behavior.
            query = query.filter(or_(Card.color_identity == "", Card.color_identity.is_(None)))

    if facet_types:
        type_tokens = [t.strip() for t in facet_types.split(",") if t.strip()]
        if type_tokens:
            type_clauses = [Card.type_line.ilike(f"%{t}%") for t in type_tokens]
            query = query.filter(or_(*type_clauses))

    if facet_status:
        statuses = [s.strip() for s in facet_status.split(",") if s.strip()]
        status_clauses = []
        if "placed" in statuses:
            status_clauses.append(InventoryRow.is_pending == False)  # noqa: E712
        if "pending" in statuses:
            status_clauses.append(InventoryRow.is_pending == True)  # noqa: E712
        if "in_deck" in statuses:
            deck_loc_ids = select(StorageLocation.id).where(
                StorageLocation.user_id == user_id,
                StorageLocation.type == "deck",
            )
            status_clauses.append(InventoryRow.storage_location_id.in_(deck_loc_ids))
        if "watchlist" in statuses:
            watch_card_ids = select(WatchlistItem.card_id).where(
                WatchlistItem.user_id == user_id,
                WatchlistItem.card_id.isnot(None),
            )
            watch_names = select(WatchlistItem.card_name).where(
                WatchlistItem.user_id == user_id,
                WatchlistItem.card_name.isnot(None),
            )
            status_clauses.append(
                or_(
                    InventoryRow.card_id.in_(watch_card_ids),
                    Card.name.in_(watch_names),
                )
            )
        if status_clauses:
            query = query.filter(or_(*status_clauses))

    if facet_finishes:
        finishes = [f.strip() for f in facet_finishes.split(",") if f.strip()]
        if finishes:
            query = query.filter(InventoryRow.finish.in_(finishes))

    # v3.28.8 fix — finish-aware effective price (mirrors
    # app.pricing.effective_price's fallback order). The original v3.28.8
    # cut cast Card.price_usd directly, which produced a visible bug: foil
    # rows with cheap foil prices passed `price_min=N` filters even when
    # their displayed price was the cheap foil. Same expression now used
    # by the `price:` boolean-search keyword.
    if facet_price_min is not None or facet_price_max is not None:
        price_col = _effective_price_expr()
        if facet_price_min is not None:
            query = query.filter(price_col >= facet_price_min)
        if facet_price_max is not None:
            query = query.filter(price_col <= facet_price_max)

    return query


def get_collection_facet_counts(
    session: Session,
    user_id: int,
    search: str = "",
) -> dict:
    """Compute facet counts for the v3.28.8 Collection sidebar.

    Returns a dict with per-option counts for every facet group. Counts
    reflect the current SEARCH query (boolean parser) and the user filter,
    but ignore active FACET state — this is the simpler "global counts
    given the search" model. The "narrow counts" alternative (ignore
    self-facet, AND others) is a future refinement; v1 ships the simpler
    model to keep the aggregate query path tight (~<10ms budget per the
    v3.27.10 dashboard-tile reference).

    All counts come from a SINGLE conditional-sum query over the same
    InventoryRow ⋈ Card join the page itself uses. The 5 facets fold
    into one query so the round-trip cost is fixed regardless of facet
    surface. Total cost: 1 query, indexed columns throughout.
    """
    from sqlalchemy import case, func

    from app.models import Deck, WatchlistItem

    # Subqueries for "in_deck" + "watchlist" status counts.
    # Use explicit select() so SQLAlchemy doesn't emit the
    # "Coercing Subquery object into a select() for use in IN()" warning.
    deck_loc_ids = select(StorageLocation.id).where(
        StorageLocation.user_id == user_id, StorageLocation.type == "deck"
    )
    _ = Deck  # imported for symmetry with the in_deck JOIN above; not directly used in counts
    watch_card_ids = select(WatchlistItem.card_id).where(
        WatchlistItem.user_id == user_id, WatchlistItem.card_id.isnot(None)
    )
    watch_names = select(WatchlistItem.card_name).where(
        WatchlistItem.user_id == user_id, WatchlistItem.card_name.isnot(None)
    )

    # Aggregate query — one row, one query, many SUM(CASE WHEN) columns
    base = (
        session.query(
            # Colors
            func.sum(case((Card.color_identity.contains("W"), 1), else_=0)).label("c_w"),
            func.sum(case((Card.color_identity.contains("U"), 1), else_=0)).label("c_u"),
            func.sum(case((Card.color_identity.contains("B"), 1), else_=0)).label("c_b"),
            func.sum(case((Card.color_identity.contains("R"), 1), else_=0)).label("c_r"),
            func.sum(case((Card.color_identity.contains("G"), 1), else_=0)).label("c_g"),
            func.sum(
                case(
                    ((Card.color_identity == "") | (Card.color_identity.is_(None)), 1),
                    else_=0,
                )
            ).label("c_c"),
            # Types — fixed bucket set matching the design's facet list
            func.sum(case((Card.type_line.ilike("%Creature%"), 1), else_=0)).label("t_creature"),
            func.sum(case((Card.type_line.ilike("%Instant%"), 1), else_=0)).label("t_instant"),
            func.sum(case((Card.type_line.ilike("%Sorcery%"), 1), else_=0)).label("t_sorcery"),
            func.sum(case((Card.type_line.ilike("%Artifact%"), 1), else_=0)).label("t_artifact"),
            func.sum(case((Card.type_line.ilike("%Enchantment%"), 1), else_=0)).label(
                "t_enchantment"
            ),
            func.sum(case((Card.type_line.ilike("%Land%"), 1), else_=0)).label("t_land"),
            func.sum(case((Card.type_line.ilike("%Planeswalker%"), 1), else_=0)).label(
                "t_planeswalker"
            ),
            # Status
            func.sum(case((InventoryRow.is_pending == False, 1), else_=0)).label(  # noqa: E712
                "s_placed"
            ),
            func.sum(case((InventoryRow.is_pending == True, 1), else_=0)).label(  # noqa: E712
                "s_pending"
            ),
            func.sum(case((InventoryRow.storage_location_id.in_(deck_loc_ids), 1), else_=0)).label(
                "s_in_deck"
            ),
            func.sum(
                case(
                    (
                        or_(
                            InventoryRow.card_id.in_(watch_card_ids),
                            Card.name.in_(watch_names),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("s_watchlist"),
            # Finishes
            func.sum(case((InventoryRow.finish == "normal", 1), else_=0)).label("f_normal"),
            func.sum(case((InventoryRow.finish == "foil", 1), else_=0)).label("f_foil"),
            func.sum(case((InventoryRow.finish == "etched", 1), else_=0)).label("f_etched"),
            # Total
            func.count(InventoryRow.id).label("total"),
        )
        .select_from(InventoryRow)
        .join(Card, Card.id == InventoryRow.card_id)
        .filter(InventoryRow.user_id == user_id)
    )
    # Apply the search filter (boolean parser) — same shape as the row
    # query the page itself runs, so counts reflect the search context.
    base = apply_collection_search_filters(base, search)
    row = base.one_or_none()
    if row is None:
        return {
            "colors": {"W": 0, "U": 0, "B": 0, "R": 0, "G": 0, "C": 0},
            "types": {
                "Creature": 0,
                "Instant": 0,
                "Sorcery": 0,
                "Artifact": 0,
                "Enchantment": 0,
                "Land": 0,
                "Planeswalker": 0,
            },
            "status": {"placed": 0, "pending": 0, "in_deck": 0, "watchlist": 0},
            "finishes": {"normal": 0, "foil": 0, "etched": 0},
            "total": 0,
        }
    return {
        "colors": {
            "W": int(row.c_w or 0),
            "U": int(row.c_u or 0),
            "B": int(row.c_b or 0),
            "R": int(row.c_r or 0),
            "G": int(row.c_g or 0),
            "C": int(row.c_c or 0),
        },
        "types": {
            "Creature": int(row.t_creature or 0),
            "Instant": int(row.t_instant or 0),
            "Sorcery": int(row.t_sorcery or 0),
            "Artifact": int(row.t_artifact or 0),
            "Enchantment": int(row.t_enchantment or 0),
            "Land": int(row.t_land or 0),
            "Planeswalker": int(row.t_planeswalker or 0),
        },
        "status": {
            "placed": int(row.s_placed or 0),
            "pending": int(row.s_pending or 0),
            "in_deck": int(row.s_in_deck or 0),
            "watchlist": int(row.s_watchlist or 0),
        },
        "finishes": {
            "normal": int(row.f_normal or 0),
            "foil": int(row.f_foil or 0),
            "etched": int(row.f_etched or 0),
        },
        "total": int(row.total or 0),
    }


def build_collection_filter_query(
    session: Session,
    user_id: int,
    *,
    search: str = "",
    facet_colors: str = "",
    facet_types: str = "",
    facet_status: str = "",
    facet_finishes: str = "",
    facet_price_min: float | None = None,
    facet_price_max: float | None = None,
    finish: str = "",
    location_id: int = 0,
    drawer: str = "",
):
    """Joined + filtered Collection base query, BEFORE sort / count / paginate.

    The single source of the Collection result set's filter composition, in
    order: the boolean search clause (``apply_collection_search_filters``)
    AND the faceted-sidebar filters (``apply_collection_facet_filters``) AND
    the legacy single-value ``finish`` dropdown AND the drawer / location
    scope. ``list_inventory_rows`` builds on this (adding sort + pagination),
    and the filter-scoped bulk actions (``/collection/bulk-*``) reuse it
    verbatim so "add/move all matching" resolves to EXACTLY the set the user
    sees — no reimplementation drift (the recon's Q4 risk).
    """
    base_query = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .join(Card)
        .filter(InventoryRow.user_id == user_id)
    )

    base_query = apply_collection_search_filters(base_query, search)

    # v3.28.8 — facet filters applied on top of the search filter (AND).
    # Backward compatibility: the legacy single-value `finish` dropdown
    # filter below still works; facet_finishes (csv) layers on top via AND.
    base_query = apply_collection_facet_filters(
        session,
        base_query,
        user_id=user_id,
        facet_colors=facet_colors,
        facet_types=facet_types,
        facet_status=facet_status,
        facet_finishes=facet_finishes,
        facet_price_min=facet_price_min,
        facet_price_max=facet_price_max,
    )

    if finish.strip():
        base_query = base_query.filter(InventoryRow.finish == finish.strip().lower())

    if drawer.strip():
        base_query = base_query.join(InventoryRow.storage_location).filter(
            StorageLocation.user_id == user_id,
            StorageLocation.name == f"Drawer {drawer.strip()}",
            StorageLocation.type == "drawer",
        )
    elif location_id:
        base_query = base_query.filter(InventoryRow.storage_location_id == location_id)

    return base_query


def resolve_drawer_cull_candidates(
    session: Session,
    user_id: int,
    *,
    price_threshold: float = DRAWER_KEEP_PRICE_THRESHOLD,
    location_id: int = 0,
    search: str = "",
    facet_colors: str = "",
    facet_types: str = "",
    facet_status: str = "",
    facet_finishes: str = "",
    facet_price_min: float | None = None,
    facet_price_max: float | None = None,
    finish: str = "",
) -> list[InventoryRow]:
    """Retroactive-cull candidates: placed drawer rows with ``quantity > 1``
    whose surplus copies are NOT intrinsically protected (Call site A of the
    drawer-vs-bulk routing design, v3.38.0).

    Rides the v3.36.9 filter path — the candidate base is
    ``build_collection_filter_query`` (so the cull respects any active
    ``/collection`` filter and can never drift from the grid the user sees),
    intersected with the user's drawer locations + ``quantity > 1`` + placed,
    then ``should_keep_in_drawer`` drops the keepers (basic / in-deck / value).
    Per-row "keep one copy" is the caller's job (see ``move_surplus_to_location``);
    each returned row contributes ``quantity - 1`` bulk-bound copies.

    ``location_id`` narrows the scope to ONE drawer when it names a drawer-type
    location (so the user can cull a single drawer from the grid's drawer view);
    any other value — 0, a box/binder, an unknown id — is ignored and the cull
    spans all drawers (a drawer operation has nothing to do with a non-drawer
    scope, so it falls back rather than returning nothing).

    Pure read; ``deck_card_ids`` is resolved once (no per-row query). No
    Scryfall fetch — price is cached. Empty list when nothing qualifies.
    """
    drawer_loc_ids = [
        loc_id
        for (loc_id,) in session.query(StorageLocation.id).filter(
            StorageLocation.user_id == user_id,
            StorageLocation.type == "drawer",
        )
    ]
    if not drawer_loc_ids:
        return []

    # Single-drawer scope: only when location_id IS one of this user's drawers.
    scope_loc_ids = [location_id] if location_id in drawer_loc_ids else drawer_loc_ids

    base_query = build_collection_filter_query(
        session,
        user_id,
        search=search,
        facet_colors=facet_colors,
        facet_types=facet_types,
        facet_status=facet_status,
        facet_finishes=facet_finishes,
        facet_price_min=facet_price_min,
        facet_price_max=facet_price_max,
        finish=finish,
    )
    rows = (
        base_query.filter(
            InventoryRow.storage_location_id.in_(scope_loc_ids),
            InventoryRow.quantity > 1,
            InventoryRow.is_pending.is_(False),
        )
        .order_by(InventoryRow.id)
        .all()
    )
    if not rows:
        return []

    deck_card_ids = deck_member_card_ids(session, user_id)
    return [
        row
        for row in rows
        if not should_keep_in_drawer(
            session,
            row,
            user_id=user_id,
            price_threshold=price_threshold,
            deck_card_ids=deck_card_ids,
        )
    ]


def list_inventory_rows(
    session: Session,
    user_id: int,
    search: str = "",
    finish: str = "",
    drawer: str = "",
    location_id: int = 0,
    sort: str = "newest",
    direction: str = "desc",
    page: int = 1,
    per_page: int = 50,
    owned_counts: dict[str, int] | None = None,
    facet_colors: str = "",
    facet_types: str = "",
    facet_status: str = "",
    facet_finishes: str = "",
    facet_price_min: float | None = None,
    facet_price_max: float | None = None,
) -> tuple[list[InventoryRow], int]:
    """List inventory rows with optional search / filter / sort.

    v3.27.19 — gained an optional ``owned_counts`` parameter for the
    ``sort=="count"`` path. When provided, the count sort uses the
    pre-computed dict instead of re-running the GROUP BY query. The
    caller (``collection_page``) computes the dict once and passes it
    in so the same aggregation also feeds the template's group
    headers; this avoids running the same query twice.

    v3.28.8 — gained six ``facet_*`` parameters for the Folio Collection
    redesign's faceted sidebar. The boolean search parser (``search``) and
    the legacy single-value ``finish`` dropdown both continue to work
    unchanged — the new facets compose ON TOP via AND. Per the spec's
    Option A, search and facets are independent dimensions.
    """
    page = max(page, 1)
    per_page = max(1, min(per_page, 100))
    reverse = direction == "desc"

    base_query = build_collection_filter_query(
        session,
        user_id,
        search=search,
        facet_colors=facet_colors,
        facet_types=facet_types,
        facet_status=facet_status,
        facet_finishes=facet_finishes,
        facet_price_min=facet_price_min,
        facet_price_max=facet_price_max,
        finish=finish,
        location_id=location_id,
        drawer=drawer,
    )

    total_count = base_query.count()

    if sort == "name":
        query = base_query.order_by(
            Card.name.desc() if reverse else Card.name.asc(),
            Card.set_code.desc() if reverse else Card.set_code.asc(),
            Card.collector_number.desc() if reverse else Card.collector_number.asc(),
            InventoryRow.id.desc() if reverse else InventoryRow.id.asc(),
        )
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
    elif sort == "set":
        query = base_query.order_by(
            Card.set_code.desc() if reverse else Card.set_code.asc(),
            Card.collector_number.desc() if reverse else Card.collector_number.asc(),
            Card.name.desc() if reverse else Card.name.asc(),
            InventoryRow.id.desc() if reverse else InventoryRow.id.asc(),
        )
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
    elif sort == "type":
        query = base_query.order_by(
            Card.type_line.desc() if reverse else Card.type_line.asc(),
            Card.name.desc() if reverse else Card.name.asc(),
            InventoryRow.id.desc() if reverse else InventoryRow.id.asc(),
        )
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
    elif sort == "cmc":
        query = base_query.order_by(
            Card.cmc.desc() if reverse else Card.cmc.asc(),
            Card.name.desc() if reverse else Card.name.asc(),
            InventoryRow.id.desc() if reverse else InventoryRow.id.asc(),
        )
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
    elif sort == "rarity":
        # v3.36.11 — shared SORT control. Rarity by RANK (common<...<mythic;
        # unknown last) via the shared CASE, not alphabetical. SQL so the
        # paginated collection never fetches-all. Ascending name/id tiebreaker.
        rarity_order = sort_spec.rarity_case()
        query = base_query.order_by(
            rarity_order.desc() if reverse else rarity_order.asc(),
            Card.name.asc(),
            InventoryRow.id.asc(),
        )
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
    elif sort == "available":
        # v3.36.11 — quantity available = the owned InventoryRow.quantity here
        # (collection rows are not "offered"). SQL + stable tiebreaker.
        query = base_query.order_by(
            InventoryRow.quantity.desc() if reverse else InventoryRow.quantity.asc(),
            Card.name.asc(),
            InventoryRow.id.asc(),
        )
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
    elif sort == "color":
        rows = base_query.all()
        rows.sort(key=lambda r: sort_spec.color_sort_value(r.card), reverse=reverse)
        rows = rows[(page - 1) * per_page : (page - 1) * per_page + per_page]
    elif sort == "placement":
        rows = base_query.all()
        rows.sort(
            key=lambda r: (assign_drawer(r), drawer_sort_key(r)),
            reverse=reverse,
        )
        rows = rows[(page - 1) * per_page : (page - 1) * per_page + per_page]
    elif sort == "value":
        rows = base_query.all()
        rows.sort(key=lambda r: effective_price(r.card, r.finish) or 0.0, reverse=reverse)
        rows = rows[(page - 1) * per_page : (page - 1) * per_page + per_page]
    elif sort == "count":
        # v3.27.19 — count-sorted collection view (consumer of the shared
        # name-level owned-count aggregation). The caller passes the
        # pre-computed name→total dict via ``owned_counts`` so the same
        # single GROUP BY query also feeds the template's group headers
        # (no double query). If a caller forgets the dict we degrade to
        # an empty mapping — sort still terminates, just with everything
        # tying at count=0, which is harmless rather than crashing.
        owned = owned_counts or {}
        rows = base_query.all()
        # Two-pass stable sort: first the secondary key (name → printing
        # → location → id), then the primary key (total owned count).
        # Python's sort is stable, so applying the count sort last means
        # ties break in the secondary order. Three-level grouping is a
        # presentational outcome of this ordering.
        rows.sort(
            key=lambda r: (
                (r.card.name or "").lower(),
                (r.card.set_code or ""),
                (r.card.collector_number or ""),
                (r.storage_location.name if r.storage_location else "Unassigned"),
                r.id,
            )
        )
        rows.sort(
            key=lambda r: owned.get((r.card.name or "").lower(), 0),
            reverse=reverse,
        )
        rows = rows[(page - 1) * per_page : (page - 1) * per_page + per_page]
    else:
        query = base_query.order_by(InventoryRow.id.desc() if reverse else InventoryRow.id.asc())
        rows = query.offset((page - 1) * per_page).limit(per_page).all()

    return rows, total_count


def is_price_stale(price_updated_at: datetime | None) -> bool:
    if price_updated_at is None:
        return True
    return price_updated_at < utc_now() - timedelta(days=PRICE_STALE_DAYS)


def get_inventory_row_stats(
    session: Session,
    user_id: int,
    search: str = "",
    finish: str = "",
    drawer: str = "",
    location_id: int = 0,
) -> dict:
    query = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .join(Card)
        .filter(InventoryRow.user_id == user_id)
    )

    query = apply_collection_search_filters(query, search)

    if finish.strip():
        query = query.filter(InventoryRow.finish == finish.strip().lower())

    if drawer.strip():
        query = query.join(InventoryRow.storage_location).filter(
            StorageLocation.user_id == user_id,
            StorageLocation.name == f"Drawer {drawer.strip()}",
            StorageLocation.type == "drawer",
        )
    elif location_id:
        query = query.filter(InventoryRow.storage_location_id == location_id)

    rows = query.all()

    # v3.27.10 prereq 2: headline aggregates count PLACED cards only;
    # pending is surfaced as a separate sub-stat on every relevant surface
    # (dashboard tiles + the Collection page itself). The pre-v3.27.10
    # `total_value` / `total_cards` keys included pending — that left the
    # Collection page disagreeing with where placement actually happens.
    # New canonical: `total_value` / `total_cards` are placed-only;
    # `pending_value` / `pending_cards` carry the in-flight slice
    # separately. unique_cards stays inclusive — it answers "how many
    # distinct card names do you own", which doesn't care about placement
    # state.
    total_value = 0.0
    total_cards = 0
    pending_value = 0.0
    pending_cards = 0
    seen_names: set[str] = set()
    drawer_counts = {str(i): 0 for i in range(1, 7)}
    unassigned_count = 0

    for row in rows:
        price = effective_price(row.card, row.finish)
        line_value = (price or 0.0) * row.quantity
        if row.is_pending:
            pending_value += line_value
            pending_cards += row.quantity
        else:
            total_value += line_value
            total_cards += row.quantity
        if row.card and row.card.name:
            seen_names.add(row.card.name)

        if str(row.drawer) in drawer_counts:
            drawer_counts[str(row.drawer)] += row.quantity
        else:
            unassigned_count += row.quantity

    unique_cards = len(seen_names)

    return {
        "total_value": total_value,
        "total_cards": total_cards,
        "pending_value": pending_value,
        "pending_cards": pending_cards,
        "unique_cards": unique_cards,
        "drawer_counts": drawer_counts,
        "unassigned_count": unassigned_count,
    }


def update_inventory_location(
    session: Session,
    row_id: int,
    user_id: int,
    drawer: str | None,
    slot: str | None,
) -> InventoryRow | None:
    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == user_id)
        .first()
    )
    if not row:
        return None

    old_location = (
        "pending" if row.is_pending else f"drawer={row.drawer or '-'} slot={row.slot or '-'}"
    )

    row.drawer = (drawer or "").strip() or None
    row.slot = (slot or "").strip() or None
    row.is_pending = row.drawer is None or row.slot is None
    row.updated_at = datetime.now(UTC)

    if row.drawer:
        location = (
            session.query(StorageLocation)
            .filter(
                StorageLocation.user_id == user_id,
                StorageLocation.name == f"Drawer {row.drawer}",
                StorageLocation.type == "drawer",
            )
            .first()
        )
        row.storage_location_id = location.id if location else None
    else:
        row.storage_location_id = None

    log_transaction(
        session=session,
        user_id=user_id,
        event_type="location_updated",
        card_id=row.card_id,
        finish=row.finish,
        quantity_delta=0,
        source_location=old_location,
        destination_location=(
            "pending" if row.is_pending else f"drawer={row.drawer or '-'} slot={row.slot or '-'}"
        ),
        inventory_row_id=row.id,
        note="Inventory location updated",
    )
    session.commit()
    return row


def move_inventory_row_to_location(
    session: Session, row_id: int, user_id: int, location_id: int
) -> InventoryRow:
    """Move ``row_id`` to ``location_id``, auto-merging with any existing
    non-pending row at the destination matching ``(user_id, card_id, finish)``.

    Mirrors the v3.16.17 fix in ``place_imported_rows``: previously the
    manual card-move flow could create a second row when the destination
    already held the same ``(card, finish)``. Now it consolidates.

    Tag handling: when merging, the moved row's tags are unioned into the
    existing destination row's tags (de-duplicated, order preserved) before
    the moved row is deleted, so user-applied role tags are never silently
    lost.

    Returns the surviving row — the merged-into existing row when a merge
    happened, otherwise the moved row.
    """
    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == user_id)
        .first()
    )
    if not row:
        raise ValueError("Inventory row not found.")

    new_location = (
        session.query(StorageLocation)
        .filter(StorageLocation.id == location_id, StorageLocation.user_id == user_id)
        .one_or_none()
    )
    if new_location is None:
        raise ValueError("Storage location not found.")

    old_location = row.storage_location.name if row.storage_location else "unassigned"
    now = datetime.now(UTC)

    existing = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.card_id == row.card_id,
            InventoryRow.finish == row.finish,
            func.coalesce(InventoryRow.language, "en") == (row.language or "en"),
            InventoryRow.is_proxy == bool(row.is_proxy),
            InventoryRow.storage_location_id == new_location.id,
            InventoryRow.is_pending.is_(False),
            InventoryRow.id != row.id,
        )
        .first()
    )

    if existing is not None:
        merged_quantity = row.quantity
        existing.quantity += merged_quantity
        existing.updated_at = now

        moved_tags = _safe_load_tags(row.tags)
        if moved_tags:
            existing_tags = _safe_load_tags(existing.tags)
            for tag in moved_tags:
                if tag not in existing_tags:
                    existing_tags.append(tag)
            existing.tags = json.dumps(existing_tags) if existing_tags else None

        log_transaction(
            session=session,
            user_id=user_id,
            event_type="location_merge",
            card_id=row.card_id,
            finish=row.finish,
            quantity_delta=merged_quantity,
            source_location=old_location,
            destination_location=new_location.name,
            inventory_row_id=existing.id,
            note=f"Merged {merged_quantity} into existing row on move",
        )
        # The merged-away row is deleted — clean its dangling ShowcaseItem /
        # TradeItem refs first (FK-safe; this path previously orphaned them —
        # see collection-delete-investigation.md). References move to nothing,
        # not to the surviving row: a Showcase entry for a now-gone row is
        # meaningless, matching the delete paths' semantics.
        clean_inventory_row_references(session, [row.id])
        session.delete(row)
        session.commit()
        return existing

    row.storage_location_id = new_location.id
    row.is_pending = False
    row.updated_at = now

    log_transaction(
        session=session,
        user_id=user_id,
        event_type="location_updated",
        card_id=row.card_id,
        finish=row.finish,
        quantity_delta=0,
        source_location=old_location,
        destination_location=new_location.name,
        inventory_row_id=row.id,
        note="Card moved to new storage location",
    )
    session.commit()
    return row


def move_surplus_to_location(
    session: Session, row_id: int, user_id: int, location_id: int, *, keep: int = 1
) -> int:
    """Move SURPLUS copies of ``row_id`` to ``location_id``, leaving ``keep``
    copies behind in the source row. Returns the number of copies moved.

    The quantity-aware sibling of :func:`move_inventory_row_to_location` — that
    one relocates a whole row, this one splits a row's quantity. Used by the
    retroactive drawer cull (v3.38.0): a ``quantity > 1`` drawer row keeps one
    findable copy (the layer-2 keeper) and ships ``quantity - keep`` copies to
    Bulk. Merges into any existing non-pending destination row matching
    ``(card_id, finish, language, is_proxy)`` exactly as the whole-row move
    does; otherwise creates a fresh placed row at the destination carrying the
    same printing identity (drawer/slot/role/tags are NOT copied — surplus
    cardboard has no slot and no deck role).

    No-op (returns 0) when ``quantity <= keep``. ``resort_collection`` is NOT
    called here — the caller owns that decision (the cull never resorts).
    """
    if keep < 0:
        raise ValueError("keep must be >= 0.")

    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == user_id)
        .first()
    )
    if not row:
        raise ValueError("Inventory row not found.")

    surplus = row.quantity - keep
    if surplus <= 0:
        return 0

    new_location = (
        session.query(StorageLocation)
        .filter(StorageLocation.id == location_id, StorageLocation.user_id == user_id)
        .one_or_none()
    )
    if new_location is None:
        raise ValueError("Storage location not found.")

    old_location = row.storage_location.name if row.storage_location else "unassigned"
    now = utc_now()

    existing = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.card_id == row.card_id,
            InventoryRow.finish == row.finish,
            func.coalesce(InventoryRow.language, "en") == (row.language or "en"),
            InventoryRow.is_proxy == bool(row.is_proxy),
            InventoryRow.storage_location_id == new_location.id,
            InventoryRow.is_pending.is_(False),
            InventoryRow.id != row.id,
        )
        .first()
    )

    if existing is not None:
        existing.quantity += surplus
        existing.updated_at = now
        dest_row_id = existing.id
    else:
        moved_row = InventoryRow(
            user_id=user_id,
            card_id=row.card_id,
            finish=row.finish,
            language=row.language,
            is_proxy=bool(row.is_proxy),
            quantity=surplus,
            storage_location_id=new_location.id,
            is_pending=False,
        )
        session.add(moved_row)
        session.flush()
        dest_row_id = moved_row.id

    row.quantity -= surplus
    row.updated_at = now

    log_transaction(
        session=session,
        user_id=user_id,
        event_type="location_updated",
        card_id=row.card_id,
        finish=row.finish,
        quantity_delta=surplus,
        source_location=old_location,
        destination_location=new_location.name,
        inventory_row_id=dest_row_id,
        note=f"Moved {surplus} surplus copy(ies) to {new_location.name} (drawer cull)",
    )
    session.commit()
    return surplus


def _get_or_create_bulk_location(session: Session, user_id: int) -> StorageLocation:
    """The Bulk overflow destination for intake routing — a user's existing
    non-deck location named "Bulk" (case-insensitive), else a freshly created
    ``box`` in ``manual`` mode.

    ``manual`` is load-bearing: a ``managed``/``sink`` Bulk is a SORTABLE SOURCE,
    so ``resort_collection`` would scoop its contents straight back into the
    drawers on the next auto-sort, undoing every routing decision. We create it
    ``manual``; if the user already has a "Bulk" location we use it as-is (we
    do not silently rewrite their chosen mode — a managed one is on them)."""
    location = (
        session.query(StorageLocation)
        .filter(
            StorageLocation.user_id == user_id,
            func.lower(StorageLocation.name) == "bulk",
            StorageLocation.type != "deck",
        )
        .first()
    )
    if location is None:
        location = StorageLocation(
            user_id=user_id,
            name="Bulk",
            type="box",
            mode="manual",
            parent_id=None,
            sort_order=0,
        )
        session.add(location)
        session.flush()
    return location


def route_intake_to_bulk(
    session: Session,
    user_id: int,
    row_ids: Iterable[int],
    *,
    price_threshold: float = DRAWER_KEEP_PRICE_THRESHOLD,
) -> tuple[int, int]:
    """Divert the cheap, non-staple SURPLUS among freshly-imported rows to the
    Bulk location BEFORE the drawer sorter runs (Call site B of the routing
    design, v3.38.0). Returns ``(drawer_bound, bulk_bound)`` copy counts.

    Sits UPSTREAM of ``resort_collection`` (which is never modified): bulk-bound
    copies are moved out to a ``manual`` Bulk location (so the sorter leaves them
    alone), and the keepers stay pending for the sorter to place. Per imported
    row of quantity Q: intrinsically protected → all Q stay; otherwise keep one
    findable drawer copy only if none exists yet, the rest go to Bulk. Whole-row
    moves reuse ``move_inventory_row_to_location`` (keep 0), partial moves
    ``move_surplus_to_location`` (keep 1). The Bulk location is resolved lazily —
    no location is created unless something actually routes there."""
    row_ids = list(row_ids)
    if not row_ids:
        return (0, 0)

    deck_card_ids = deck_member_card_ids(session, user_id)
    bulk_location: StorageLocation | None = None
    drawer_bound = 0
    bulk_bound = 0

    for row_id in row_ids:
        row = (
            session.query(InventoryRow)
            .options(joinedload(InventoryRow.card))
            .filter(InventoryRow.id == row_id, InventoryRow.user_id == user_id)
            .first()
        )
        if row is None:
            continue
        quantity = row.quantity
        has_drawer_copy = _drawer_copy_exists(session, user_id, row.card_id, row.finish)
        keep, to_bulk = split_intake_quantity(
            row.card,
            row.finish,
            row.card_id,
            quantity,
            has_drawer_copy=has_drawer_copy,
            deck_card_ids=deck_card_ids,
            price_threshold=price_threshold,
        )
        if to_bulk <= 0:
            drawer_bound += quantity
            continue
        if bulk_location is None:
            bulk_location = _get_or_create_bulk_location(session, user_id)
        if keep == 0:
            move_inventory_row_to_location(session, row.id, user_id, bulk_location.id)
        else:
            move_surplus_to_location(session, row.id, user_id, bulk_location.id, keep=keep)
        bulk_bound += to_bulk
        drawer_bound += keep

    return (drawer_bound, bulk_bound)


def summarize_intake_routing(
    session: Session,
    user_id: int,
    matches_rows: list[dict],
    *,
    price_threshold: float = DRAWER_KEEP_PRICE_THRESHOLD,
) -> tuple[int, int]:
    """Preview-time ``(drawer_bound, bulk_bound)`` for the auto-sort path, over
    the collection reconcile ``matches_rows`` — so the reconcile-preview can show
    "N → drawers, M → bulk" before anything commits (never silent). Reuses
    ``split_intake_quantity`` (the same decision the commit router applies), so
    the preview cannot drift from the result. Only the copies that will actually
    be imported (``import_new`` / ``import_delta`` × ``recommended_new_qty``)
    count; already-owned skips don't enter the drawers."""
    importable = [
        (r["card_id"], r.get("finish") or "normal", int(r.get("recommended_new_qty") or 0))
        for r in matches_rows
        if r.get("card_id")
        and r.get("recommended_action") in ("import_new", "import_delta")
        and int(r.get("recommended_new_qty") or 0) > 0
    ]
    if not importable:
        return (0, 0)

    card_ids = {cid for cid, _finish, _qty in importable}
    cards = {c.id: c for c in session.query(Card).filter(Card.id.in_(card_ids))}
    deck_card_ids = deck_member_card_ids(session, user_id)

    drawer_bound = 0
    bulk_bound = 0
    for card_id, finish, qty in importable:
        keep, to_bulk = split_intake_quantity(
            cards.get(card_id),
            finish,
            card_id,
            qty,
            has_drawer_copy=_drawer_copy_exists(session, user_id, card_id, finish),
            deck_card_ids=deck_card_ids,
            price_threshold=price_threshold,
        )
        drawer_bound += keep
        bulk_bound += to_bulk
    return (drawer_bound, bulk_bound)


def _safe_load_tags(raw: str | None) -> list[str]:
    """Parse the ``InventoryRow.tags`` JSON text without raising. Returns
    [] for null/blank/malformed values."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, str)]


def place_imported_rows(
    session: Session, row_ids: list[int], user_id: int, location_id: int
) -> int:
    """Place freshly-imported rows at ``location_id``, auto-merging with
    any existing non-pending row at the destination matching
    ``(user_id, card_id, finish)``.

    Auto-merge closes the dup-row gap described in
    ``docs/collection_import_sync.md`` §8.1: previously a binder/box user
    who imported a card they already had at the destination ended up with
    two rows for the same printing+finish until manual consolidation.
    Drawer-sorter users got auto-consolidation via ``resort_collection``;
    everyone else now gets it here.

    Merge semantics: for each placed row, if an existing destination row
    matches ``(user_id, card_id, finish, storage_location_id, is_pending=False)``,
    increment its ``quantity`` by the placed row's quantity and ``session.delete``
    the placed row. The existing row's ``tags`` are preserved — the placed
    row carries no user-assigned tags at this point (imports don't auto-tag),
    so there's nothing to merge.

    Returns the count of input ``row_ids`` processed (every input row is
    handled, whether by merge or by direct placement).
    """
    location = (
        session.query(StorageLocation)
        .filter(StorageLocation.id == location_id, StorageLocation.user_id == user_id)
        .one_or_none()
    )
    if location is None:
        raise ValueError("Storage location not found.")

    rows = (
        session.query(InventoryRow)
        .filter(InventoryRow.id.in_(row_ids), InventoryRow.user_id == user_id)
        .all()
    )
    now = datetime.now(UTC)
    for row in rows:
        existing = (
            session.query(InventoryRow)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.card_id == row.card_id,
                InventoryRow.finish == row.finish,
                func.coalesce(InventoryRow.language, "en") == (row.language or "en"),
                InventoryRow.is_proxy == bool(row.is_proxy),
                InventoryRow.storage_location_id == location.id,
                InventoryRow.is_pending.is_(False),
                InventoryRow.id != row.id,
            )
            .first()
        )
        if existing is not None:
            existing.quantity += row.quantity
            existing.updated_at = now
            # Merged-away row is deleted — FK-safe cleanup of its references first.
            clean_inventory_row_references(session, [row.id])
            session.delete(row)
        else:
            row.storage_location_id = location.id
            row.is_pending = False
            row.updated_at = now

    session.commit()
    return len(rows)


# Tier priority for ordering owned-breakdown entries. Mirrors the deck
# reconciliation function's tier order but with "deck" inserted before
# "pending" since deck-located copies still count toward "owned" for sync
# purposes (design doc collection_import_sync.md §4.4).
_COLLECTION_TIER_PRIORITY: dict[str, int] = {
    "drawer": 0,
    "binder": 1,
    "box": 2,
    "other": 3,
    "deck": 4,
    "pending": 5,
}


def resolve_import_inventory_matches(
    session: Session,
    user_id: int,
    parsed_rows: list[dict],
) -> tuple[dict[str, int], set[tuple[int, str]], list[tuple]]:
    """Shared read-only scaffold for the two import-reconciliation lookups
    (``find_inventory_matches_for_collection_import`` here, and
    ``find_inventory_matches_for_deck_import`` in ``deck_service``).

    Performs the three steps both functions share, then hands the raw results
    back so each caller does its own (divergent) bucketing + recommendation:

      1. Resolve ``scryfall_id`` → ``card_id`` for all parsed rows in ONE query.
      2. Build the set of ``(card_id, finish)`` lookup keys (skipping rows with
         no scryfall_id / no catalog match; ``finish`` defaults to ``"normal"``,
         lower-cased + stripped).
      3. Run ONE ``tuple_(card_id, finish).in_(...)`` query for all matching
         InventoryRows, ``outerjoin``-ed to StorageLocation so pending rows
         (``storage_location_id IS NULL``) come through with ``loc=None``.

    Returns ``(card_by_sid, lookup_keys, rows)`` where ``rows`` is the list of
    ``(InventoryRow, StorageLocation | None)`` tuples. No DB writes, no N+1 —
    at most two queries regardless of row count.
    """
    scryfall_ids = sorted({r.get("scryfall_id") for r in parsed_rows if r.get("scryfall_id")})
    card_by_sid: dict[str, int] = {}
    if scryfall_ids:
        for card_row in (
            session.query(Card.id, Card.scryfall_id)
            .filter(Card.scryfall_id.in_(scryfall_ids))
            .all()
        ):
            card_by_sid[card_row.scryfall_id] = card_row.id

    lookup_keys: set[tuple[int, str]] = set()
    for r in parsed_rows:
        sid = r.get("scryfall_id")
        if not sid:
            continue
        card_id = card_by_sid.get(sid)
        if card_id is None:
            continue
        finish = (r.get("finish") or "normal").strip().lower()
        lookup_keys.add((card_id, finish))

    rows: list[tuple] = []
    if lookup_keys:
        rows = (
            session.query(InventoryRow, StorageLocation)
            .outerjoin(
                StorageLocation,
                InventoryRow.storage_location_id == StorageLocation.id,
            )
            .filter(
                InventoryRow.user_id == user_id,
                tuple_(InventoryRow.card_id, InventoryRow.finish).in_(list(lookup_keys)),
            )
            .all()
        )

    return card_by_sid, lookup_keys, rows


def find_inventory_matches_for_collection_import(
    session: Session,
    user_id: int,
    parsed_rows: list[dict],
) -> list[dict]:
    """Read-only sync-mode reconciliation lookup for non-deck imports.

    Sibling function to ``find_inventory_matches_for_deck_import`` in
    ``app/deck_service.py``. Where the deck function asks "what could I
    MOVE into this deck?", this function asks "how much do I already
    OWN of each card across all locations?" — the answer drives the
    skip / partial-import / new-import recommendation for full-collection
    re-imports (Helvault/Moxfield collection exports, etc.) per
    ``docs/collection_import_sync.md``.

    Pure read function — no DB writes. Callers (the eventual Session B
    commit handler) consume the recommendation by:
      - skipping rows where ``recommended_action == "skip_already_owned"``
      - importing ``recommended_new_qty`` copies for the remaining rows
        via the existing ``persist_import_rows`` + ``place_imported_rows``
        path

    Args:
        session:      SQLAlchemy session.
        user_id:      Owner of the inventory being reconciled. Per-user
                      scoped — never returns inventory from other users.
        parsed_rows:  List of dicts matching the shape produced by
                      ``parse_scanner_csv`` / ``parse_text_list`` in
                      ``app/import_service.py``. Each must have at least
                      ``line_number``, ``scryfall_id``, ``finish``,
                      ``quantity``.

    Returns:
        One dict per parsed row, preserving input order. Each output
        dict::

            {
                "line_number": int,
                "card_id": int | None,        # None if scryfall_id not in catalog
                "scryfall_id": str,
                "finish": str,
                "quantity_needed": int,
                "total_user_owned": int,      # sum across all locations + pending
                "owned_breakdown": [
                    {
                        "location_name": str,   # "Drawer 2" | "Binder A" | deck name | "Pending"
                        "location_type": str,   # drawer|binder|box|other|deck|pending
                        "quantity": int,
                    },
                    ...
                ],
                "recommended_action": str,
                    # "skip_already_owned" | "import_delta" | "import_new"
                "recommended_new_qty": int,
            }

        The "skip qty" is implicit: ``quantity_needed - recommended_new_qty``.

    Recommended action — pure function of ``total_user_owned`` vs
    ``quantity_needed``::

        total_user_owned >= quantity_needed
            -> "skip_already_owned", new_qty=0
        0 < total_user_owned < quantity_needed
            -> "import_delta", new_qty=quantity_needed - total_user_owned
        total_user_owned == 0
            -> "import_new", new_qty=quantity_needed

    Match selection rules:
      - Same ``(user_id, card_id, finish)``.
      - **Includes ALL locations** — decks, non-deck (drawer/binder/box/
        other), and pending rows. The whole point of the sync flow is
        "do I already own this card anywhere?" so deck-located copies
        and unplaced pending rows both contribute to the count. This is
        the key difference from the deck-reconciliation function, which
        excludes deck rows from its movable-matches list.
      - Pending rows (``is_pending=True``, no ``storage_location_id``)
        synthesize ``location_name="Pending"`` and ``location_type="pending"``
        for the breakdown.

    Owned-breakdown ordering (callers may iterate in tier order):
      1. ``drawer``
      2. ``binder``
      3. ``box``
      4. ``other``
      5. ``deck``
      6. ``pending``
      Within tier: ordered by ``inventory_row_id`` ASC for determinism.

    Performance: one query for Card-id resolution + one tuple-IN query
    for inventory matches (joined to StorageLocation via outerjoin so
    pending rows come through with loc=None). No N+1.

    Session A precursor notes (captured during implementation for future
    readers):

    Pending rows count as "owned."
        ``app/import_service.py::persist_import_rows`` (lines 379-394)
        merges new imports with existing PENDING rows from the same
        user. The merge query strictly filters
        ``drawer IS NULL AND slot IS NULL AND is_pending IS TRUE``.
        Pending rows are real ``InventoryRow`` records — quantity the
        user owns but hasn't filed yet. Including them here matches the
        sync semantics (the user does own these cards). If a later
        ``import_delta`` for the same row routes the delta through
        ``persist_import_rows`` again, the existing pending-merge logic
        will fold the delta qty into the same pending row rather than
        create a duplicate pending row — also correct, since pending
        rows merge by ``(card_id, finish)``.

    ``place_imported_rows`` doesn't auto-merge.
        ``app/inventory_service.py::place_imported_rows`` (lines
        814-837) sets ``storage_location_id`` + ``is_pending=False`` on
        the given row IDs without checking for existing matching rows
        at the destination. So ``recommended_new_qty`` translates to
        "new rows PLACED ALONGSIDE existing rows at the destination,"
        not "merged into existing rows." The drawer-sorter
        (``resort_collection``) consolidates these for drawer-sorter
        users on the next pass; binder/box destinations will see
        permanent scattered duplicates until a future v3.16.X polish
        ports the v3.16.14 deck-merge pattern to non-deck destinations
        (design doc §8.1, flagged as a polish target rather than
        deferred-indefinitely future work).
    """
    if not parsed_rows:
        return []

    # Shared scaffold: resolve sid→card_id, build (card_id, finish) keys, and
    # run the tuple-IN outerjoin fetch (pending rows come through with loc=None).
    card_by_sid, lookup_keys, rows = resolve_import_inventory_matches(session, user_id, parsed_rows)

    # Bucket each matching row into its (card_id, finish) breakdown.
    breakdown_by_key: dict[tuple[int, str], list[dict]] = {key: [] for key in lookup_keys}
    for row, loc in rows:
        if loc is None:
            location_name = "Pending"
            location_type = "pending"
        else:
            location_name = loc.name
            location_type = loc.type
        breakdown_by_key[(row.card_id, row.finish)].append(
            {
                "location_name": location_name,
                "location_type": location_type,
                "quantity": row.quantity,
                "_inventory_row_id": row.id,  # for sort, stripped before return
            }
        )

    # Sort each per-key breakdown by tier then row id, then drop the sort key.
    for entries in breakdown_by_key.values():
        entries.sort(
            key=lambda e: (
                _COLLECTION_TIER_PRIORITY.get(e["location_type"], 99),
                e["_inventory_row_id"],
            )
        )
        for e in entries:
            e.pop("_inventory_row_id", None)

    # Build per-parsed-row output in input order.
    output: list[dict] = []
    for r in parsed_rows:
        sid = r.get("scryfall_id") or ""
        card_id = card_by_sid.get(sid) if sid else None
        finish = (r.get("finish") or "normal").strip().lower()
        quantity_needed = max(1, int(r.get("quantity") or 1))
        line_number = r.get("line_number")

        if card_id is None:
            output.append(
                {
                    "line_number": line_number,
                    "card_id": None,
                    "scryfall_id": sid,
                    "finish": finish,
                    "quantity_needed": quantity_needed,
                    "total_user_owned": 0,
                    "owned_breakdown": [],
                    "recommended_action": "import_new",
                    "recommended_new_qty": quantity_needed,
                }
            )
            continue

        breakdown = breakdown_by_key.get((card_id, finish), [])
        total_user_owned = sum(e["quantity"] for e in breakdown)

        if total_user_owned >= quantity_needed:
            action = "skip_already_owned"
            new_qty = 0
        elif total_user_owned > 0:
            action = "import_delta"
            new_qty = quantity_needed - total_user_owned
        else:
            action = "import_new"
            new_qty = quantity_needed

        output.append(
            {
                "line_number": line_number,
                "card_id": card_id,
                "scryfall_id": sid,
                "finish": finish,
                "quantity_needed": quantity_needed,
                "total_user_owned": total_user_owned,
                "owned_breakdown": breakdown,
                "recommended_action": action,
                "recommended_new_qty": new_qty,
            }
        )

    return output


def list_pending_rows(session: Session, user_id: int) -> list[InventoryRow]:
    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .outerjoin(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
        .filter(
            InventoryRow.is_pending.is_(True),
            InventoryRow.user_id == user_id,
            or_(InventoryRow.storage_location_id.is_(None), StorageLocation.type != "deck"),
        )
        .all()
    )
    rows.sort(key=lambda r: (assign_drawer(r), drawer_sort_key(r)))
    return rows


def _get_or_create_drawer_location(session: Session, user_id: int, drawer: str) -> StorageLocation:
    location = (
        session.query(StorageLocation)
        .filter(
            StorageLocation.user_id == user_id,
            StorageLocation.name == f"Drawer {drawer}",
            StorageLocation.type == "drawer",
        )
        .one_or_none()
    )
    if location is None:
        location = StorageLocation(
            user_id=user_id,
            name=f"Drawer {drawer}",
            type="drawer",
            parent_id=None,
            sort_order=int(drawer) if drawer.isdigit() else 0,
        )
        session.add(location)
        session.flush()
    return location


def confirm_pending_row(
    session: Session, row_id: int, user_id: int, location_id: int | None = None
) -> InventoryRow | None:
    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == user_id)
        .first()
    )

    if not row:
        return None

    if not row.is_pending:
        return row

    if location_id is not None:
        location = (
            session.query(StorageLocation)
            .filter(StorageLocation.id == location_id, StorageLocation.user_id == user_id)
            .one_or_none()
        )
        if location is None:
            raise ValueError("Storage location not found.")
    else:
        if not row.drawer or not row.slot:
            raise ValueError("Pending row has no assigned drawer/slot yet.")
        location = _get_or_create_drawer_location(session, user_id, row.drawer)

    row.storage_location_id = location.id
    row.is_pending = False
    # Clear the previous-position breadcrumbs — the row is now physically
    # placed at its new home, so the FROM hints stop being useful.
    row.from_drawer = None
    row.from_slot = None
    row.updated_at = utc_now()

    if row.drawer:
        dest = f"drawer={row.drawer} slot={row.slot or '-'}"
    else:
        dest = location.name

    log_transaction(
        session=session,
        user_id=user_id,
        event_type="placement_confirmed",
        card_id=row.card_id,
        finish=row.finish,
        quantity_delta=0,
        source_location="pending",
        destination_location=dest,
        inventory_row_id=row.id,
        note="Pending row confirmed as placed",
    )
    session.commit()
    return row


def confirm_all_pending(session: Session, user_id: int) -> int:
    rows = (
        session.query(InventoryRow)
        .filter(InventoryRow.is_pending.is_(True), InventoryRow.user_id == user_id)
        .all()
    )
    count = 0
    now = utc_now()

    for row in rows:
        if not row.drawer or not row.slot:
            continue

        location = _get_or_create_drawer_location(session, user_id, row.drawer)

        row.storage_location_id = location.id
        row.is_pending = False
        row.updated_at = now

        log_transaction(
            session=session,
            user_id=user_id,
            event_type="placement_confirmed",
            card_id=row.card_id,
            finish=row.finish,
            quantity_delta=0,
            source_location="pending",
            destination_location=f"drawer={row.drawer or '-'} slot={row.slot or '-'}",
            inventory_row_id=row.id,
            note="Pending row confirmed as placed",
            flush=False,
        )
        count += 1

    session.commit()
    return count


def clean_inventory_row_references(session: Session, row_ids: list[int]) -> None:
    """Drop dangling references to inventory rows that are about to be deleted.

    The single FK-safe cleanup shared by EVERY path that ``session.delete()``s an
    InventoryRow. Two referencing tables point at ``inventory_rows.id``:

      - ``showcase_items.inventory_row_id`` — **NOT NULL / ON DELETE NO ACTION**.
        Under enforced FKs (Postgres) deleting the row would FK-error unless the
        ShowcaseItem is removed first; under SQLite (FK off today) it would
        silently orphan. Deleted here.
      - ``trade_items.inventory_row_id`` — nullable / NO ACTION. Pending trades
        referencing the row are abandoned and remaining TradeItem references are
        NULLed (the ``*_at_trade`` snapshot is the durable record — decision A4).

    Idempotent and a no-op on an empty list. **Call BEFORE deleting the rows.**
    Extracted (v3.39.x) from the bulk-delete + single-delete paths so the merge
    and import-undo paths use the *same* semantics — see
    ``collection-delete-investigation.md`` for why those two paths orphaned.
    """
    if not row_ids:
        return
    session.query(ShowcaseItem).filter(ShowcaseItem.inventory_row_id.in_(row_ids)).delete(
        synchronize_session=False
    )
    # issue #27 — a physical row sold/deleted drops any deck_card_share that
    # references it (the row is no longer a member of any sibling decklist).
    # ON DELETE CASCADE NOT NULL on PG; explicit here for SQLite (FK off).
    from app.models import DeckCardShare

    session.query(DeckCardShare).filter(DeckCardShare.inventory_row_id.in_(row_ids)).delete(
        synchronize_session=False
    )
    from app import trade_service

    trade_service.abandon_pending_trades_for_inventory_rows(session, row_ids)


def adjust_inventory_row_quantity(
    session: Session,
    row_id: int,
    user_id: int,
    quantity: int,
    event_type: str,
    note: str | None = None,
) -> InventoryRow | None:
    valid_event_types = {"remove", "sold", "traded", "row_deleted"}
    if event_type not in valid_event_types:
        raise ValueError(f"Unsupported event_type: {event_type}")

    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == user_id)
        .first()
    )

    if not row:
        raise ValueError("Inventory row not found.")
    if quantity <= 0:
        raise ValueError("Quantity must be at least 1.")
    if quantity > row.quantity:
        raise ValueError("Cannot remove more than the row quantity.")

    source_location = (
        "pending" if row.is_pending else f"drawer={row.drawer or '-'} slot={row.slot or '-'}"
    )

    log_transaction(
        session=session,
        user_id=user_id,
        event_type=event_type,
        card_id=row.card_id,
        finish=row.finish,
        quantity_delta=-quantity,
        source_location=source_location,
        destination_location=None,
        inventory_row_id=row.id,
        note=note,
        flush=False,
    )

    if quantity == row.quantity:
        # FK-safe cleanup of dangling ShowcaseItem / TradeItem refs before the
        # row goes (shared helper; see clean_inventory_row_references).
        clean_inventory_row_references(session, [row.id])
        session.delete(row)
        session.commit()
        return None

    row.quantity -= quantity
    row.updated_at = utc_now()

    session.commit()
    return row


def delete_inventory_row(session: Session, row_id: int, user_id: int) -> bool:
    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == user_id)
        .first()
    )

    if not row:
        return False

    adjust_inventory_row_quantity(
        session=session,
        row_id=row_id,
        user_id=user_id,
        quantity=row.quantity,
        event_type="row_deleted",
        note=f"Deleted inventory row {row_id}",
    )

    return True


def bulk_delete_inventory_rows(session: Session, row_ids: list[int], user_id: int) -> int:
    """Hard-delete multiple inventory rows in a single transaction.

    Per-row ownership validation: rows not owned by ``user_id`` (or not
    found) are silently skipped, mirroring the existing bulk-move
    pattern. Each deleted row produces one ``row_deleted`` TransactionLog
    entry, matching the single-row ``delete_inventory_row`` behavior;
    the difference is the entire operation commits exactly once, so a
    partial failure rolls back the whole batch.

    Returns the count of rows actually deleted.
    """
    if not row_ids:
        return 0

    rows = (
        session.query(InventoryRow)
        .filter(InventoryRow.id.in_(row_ids), InventoryRow.user_id == user_id)
        .all()
    )

    # FK-safe cleanup of referencing ShowcaseItems + pending trades, keyed on the
    # actually-resolvable (owned) row ids — a tampered batch with someone else's
    # ids gets neither the InventoryRow nor its references deleted (the user_id
    # filter above already dropped non-owned rows). Shared helper.
    if rows:
        owned_ids = [r.id for r in rows]
        clean_inventory_row_references(session, owned_ids)

    for row in rows:
        source_location = (
            "pending" if row.is_pending else f"drawer={row.drawer or '-'} slot={row.slot or '-'}"
        )
        log_transaction(
            session=session,
            user_id=user_id,
            event_type="row_deleted",
            card_id=row.card_id,
            finish=row.finish,
            quantity_delta=-row.quantity,
            source_location=source_location,
            destination_location=None,
            inventory_row_id=row.id,
            note=f"Bulk-deleted inventory row {row.id}",
            flush=False,
        )
        session.delete(row)

    session.commit()
    return len(rows)


def undo_last_import(session: Session, user_id: int) -> bool:
    last_import = (
        session.query(TransactionLog)
        .filter(
            TransactionLog.user_id == user_id,
            TransactionLog.event_type == "import",
        )
        .order_by(TransactionLog.id.desc())
        .first()
    )
    if not last_import or not last_import.inventory_row_id:
        return False

    row = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.id == last_import.inventory_row_id,
            InventoryRow.user_id == user_id,
        )
        .first()
    )
    if row:
        row.quantity -= abs(last_import.quantity_delta)
        row.updated_at = utc_now()
        if row.quantity <= 0:
            # FK-safe cleanup before the row goes (this path previously orphaned
            # ShowcaseItem / TradeItem refs — see the investigation doc).
            clean_inventory_row_references(session, [row.id])
            session.delete(row)

    session.flush()

    log_transaction(
        session=session,
        user_id=user_id,
        event_type="undo_import",
        card_id=last_import.card_id,
        finish=last_import.finish,
        quantity_delta=-abs(last_import.quantity_delta),
        batch_id=last_import.batch_id,
        inventory_row_id=last_import.inventory_row_id,
        note=f"Undid import log {last_import.id}",
    )
    session.commit()
    return True


def undo_last_batch(session: Session, batch_id: int, user_id: int) -> int:
    logs = (
        session.query(TransactionLog)
        .filter(
            TransactionLog.user_id == user_id,
            TransactionLog.batch_id == batch_id,
            TransactionLog.event_type == "import",
        )
        .order_by(TransactionLog.id.desc())
        .all()
    )

    undone = 0
    for log in logs:
        row = (
            session.query(InventoryRow)
            .filter(
                InventoryRow.id == log.inventory_row_id,
                InventoryRow.user_id == user_id,
            )
            .first()
        )
        if row:
            row.quantity -= abs(log.quantity_delta)
            row.updated_at = utc_now()
            if row.quantity <= 0:
                # FK-safe cleanup before the row goes (undo previously orphaned refs).
                clean_inventory_row_references(session, [row.id])
                session.delete(row)

        log_transaction(
            session=session,
            user_id=user_id,
            event_type="undo_batch_import",
            card_id=log.card_id,
            finish=log.finish,
            quantity_delta=-abs(log.quantity_delta),
            batch_id=log.batch_id,
            inventory_row_id=log.inventory_row_id,
            note=f"Undid import log {log.id} from batch {batch_id}",
            flush=False,
        )
        undone += 1

    session.commit()
    return undone


def get_previous_location_for_row(session: Session, row_id: int, user_id: int) -> str | None:
    log = (
        session.query(TransactionLog)
        .filter(
            TransactionLog.user_id == user_id,
            TransactionLog.inventory_row_id == row_id,
            TransactionLog.event_type == "resort",
            TransactionLog.source_location.isnot(None),
        )
        .order_by(TransactionLog.created_at.desc(), TransactionLog.id.desc())
        .first()
    )

    if not log or log.source_location == "pending":
        return None

    return log.source_location


def resort_collection(
    session: Session,
    user_id: int,
    row_ids: Iterable[int] | None = None,
) -> int:
    query = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .outerjoin(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
        .filter(
            InventoryRow.user_id == user_id,
            # v3.26.2: respect per-location sorter modes. Pending rows (no
            # storage_location_id) are always sortable. Rows in a non-deck
            # location are sortable only if that location's mode is in
            # SORTABLE_SOURCE_MODES (``managed`` or ``sink``). ``manual`` and
            # ``ignored`` locations keep their contents in place.
            or_(
                InventoryRow.storage_location_id.is_(None),
                and_(
                    StorageLocation.type != "deck",
                    StorageLocation.mode.in_(SORTABLE_SOURCE_MODES),
                ),
            ),
        )
    )
    if row_ids is not None:
        query = query.filter(InventoryRow.id.in_(list(row_ids)))
    rows = query.all()
    if not rows:
        return 0

    # No trait backfill here: resort_collection runs synchronously inside
    # request handlers, and a bulk Scryfall backfill while holding a
    # SQLite transaction blocked every other request and locked the pod
    # (v3.23.8 incident). Traits are populated entirely off the request
    # path by the background trait-backfill loop; card_traits() resolves
    # strictly from local columns here (type_line best-effort until a
    # card is backfilled), so this stays a pure in-memory sort.

    # Pre-load all drawer StorageLocations in one query instead of 6 separate ones.
    drawer_loc_ids: dict[int, int | None] = {i: None for i in range(1, 7)}
    for loc in session.query(StorageLocation).filter(
        StorageLocation.user_id == user_id,
        StorageLocation.type == "drawer",
    ):
        try:
            n = int(loc.name.replace("Drawer", "").strip())
            if 1 <= n <= 6:
                drawer_loc_ids[n] = loc.id
        except ValueError:
            pass

    # Compute target drawer once per row and sort.
    row_target_drawer: dict[int, int] = {row.id: assign_drawer(row) for row in rows}
    rows.sort(key=lambda r: (row_target_drawer[r.id], drawer_sort_key(r)))

    grouped: dict[int, list[InventoryRow]] = {i: [] for i in range(1, 7)}
    for row in rows:
        grouped[row_target_drawer[row.id]].append(row)

    now = utc_now()
    bulk_updates: list[dict] = []
    audit_logs: list[dict] = []

    for drawer_number, drawer_rows in grouped.items():
        loc_id = drawer_loc_ids[drawer_number]
        for index, row in enumerate(drawer_rows, start=1):
            target_drawer = str(drawer_number)
            target_slot = str(index)
            if row.drawer == target_drawer and row.slot == target_slot:
                continue

            old_drawer = row.drawer
            old_slot = row.slot
            old_is_pending = row.is_pending
            is_cross_drawer_move = not old_is_pending and old_drawer != target_drawer
            new_is_pending = bool(old_is_pending or is_cross_drawer_move)
            # Capture the old position when a placed row is pulled to pending
            # so the pending page can show the user where to physically pull
            # the card from. Imported rows (already pending) never had a
            # previous physical location, so they leave from_drawer NULL.
            new_from_drawer = old_drawer if is_cross_drawer_move else row.from_drawer
            new_from_slot = old_slot if is_cross_drawer_move else row.from_slot

            bulk_updates.append(
                {
                    "id": row.id,
                    "user_id": user_id,
                    "drawer": target_drawer,
                    "slot": target_slot,
                    "storage_location_id": loc_id,
                    "is_pending": new_is_pending,
                    "from_drawer": new_from_drawer,
                    "from_slot": new_from_slot,
                    "updated_at": now,
                }
            )

            # Only audit physical cross-drawer moves — slot renumbering within the
            # same drawer produces no actionable entry and would flood the log on
            # large imports.
            if not old_is_pending and old_drawer is not None and old_drawer != target_drawer:
                audit_logs.append(
                    {
                        "user_id": user_id,
                        "event_type": "resort",
                        "card_id": row.card_id,
                        "finish": row.finish,
                        "quantity_delta": 0,
                        "source_location": f"drawer={old_drawer} slot={row.slot or '-'}",
                        "destination_location": f"drawer={target_drawer} slot={target_slot}",
                        "inventory_row_id": row.id,
                        "note": "Auto-sorted collection row; moved to a new drawer and marked pending for physical relocation",
                        "batch_id": None,
                    }
                )

    if not bulk_updates:
        return 0

    session.execute(
        text(
            "UPDATE inventory_rows"
            " SET drawer=:drawer, slot=:slot, storage_location_id=:storage_location_id,"
            "     is_pending=:is_pending, from_drawer=:from_drawer, from_slot=:from_slot,"
            "     updated_at=:updated_at"
            " WHERE id=:id AND user_id=:user_id"
        ),
        bulk_updates,
    )
    if audit_logs:
        session.bulk_insert_mappings(TransactionLog, audit_logs)

    session.commit()
    return len(bulk_updates)


def get_owned_cards_by_set(session: Session, set_code: str, user_id: int) -> dict[str, int]:
    rows = (
        session.query(InventoryRow)
        .join(Card)
        .filter(
            InventoryRow.user_id == user_id,
            Card.set_code == set_code.lower(),
        )
        .all()
    )

    owned: dict[str, int] = {}
    for row in rows:
        key = row.card.collector_number
        owned[key] = owned.get(key, 0) + row.quantity

    return owned


def list_owned_sets(session: Session, user_id: int) -> list[dict]:
    rows = (
        session.query(
            Card.set_code,
            func.max(Card.set_name),
            func.count(func.distinct(Card.collector_number)),
            func.sum(InventoryRow.quantity),
        )
        .join(InventoryRow, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == user_id)
        .group_by(Card.set_code)
        .order_by(Card.set_code.asc())
        .all()
    )

    return [
        {
            "set_code": set_code,
            "set_name": set_name or set_code.upper(),
            "unique_owned": int(unique_owned or 0),
            "total_copies": int(total_copies or 0),
        }
        for set_code, set_name, unique_owned, total_copies in rows
    ]
