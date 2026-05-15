from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Float as SAFloat
from sqlalchemy import and_, cast, func, not_, or_, text, tuple_
from sqlalchemy.orm import Session, joinedload

from app.audit_service import log_transaction
from app.import_service import coerce_language_code_strict
from app.models import Card, InventoryRow, StorageLocation, TransactionLog
from app.pricing import effective_price
from app.scryfall import fetch_card_by_scryfall_id

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


def assign_drawer(row: InventoryRow) -> int:
    """Return the target drawer number (1-6) for an InventoryRow.

    Priority order (first match wins):
      1. value >= VALUE_THRESHOLD ($5) → drawer 1
      2. is_proxy=True → drawer 6 (proxies section)
      3. token card → drawer 6 (tokens section)
      4. substitute card → drawer 6 (substitutes section)
      5. foreign language (language not en/None) → drawer 6 (foreign section)
      6. premium basic → drawer 6 (premium basics section)
      7. plain basic → drawer 6 (plain basics section)
      8. numeric set code or empty set → drawer 6 (numeric sets section)
      9. otherwise: letter-range routes to drawers 2-5

    The drawer-6 *section* (vs the drawer number) is determined by
    ``drawer_sort_key`` for the in-drawer sort ordering.
    """
    card = row.card
    finish = row.finish

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
    basics, 3=plain basics, 4=tokens, 5=substitutes, 6=proxies.
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
        return (7, set_code, collector, name, row.id)

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
            existing.updated_at = datetime.utcnow()
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
                existing.updated_at = datetime.utcnow()
                session.flush()
        return existing

    payload = card_data or fetch_card_by_scryfall_id(scryfall_id)
    if not payload:
        return None

    card = Card(**payload, updated_at=datetime.utcnow())
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
        existing.updated_at = datetime.utcnow()
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
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(row)
    session.flush()
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
        price_col = cast(Card.price_usd, SAFloat)
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
) -> tuple[list[InventoryRow], int]:
    page = max(page, 1)
    per_page = max(1, min(per_page, 100))
    reverse = direction == "desc"

    base_query = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .join(Card)
        .filter(InventoryRow.user_id == user_id)
    )

    base_query = apply_collection_search_filters(base_query, search)

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

    total_count = base_query.count()

    _COLOR_ORDER = {"W": 0, "U": 1, "B": 2, "R": 3, "G": 4}

    def _color_sort_key(row: InventoryRow) -> tuple:
        colors = (row.card.colors or "").split()
        if not colors:
            return (6, "")
        if len(colors) > 1:
            return (5, " ".join(colors))
        return (_COLOR_ORDER.get(colors[0], 7), colors[0])

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
    elif sort == "color":
        rows = base_query.all()
        rows.sort(key=_color_sort_key, reverse=reverse)
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
    else:
        query = base_query.order_by(InventoryRow.id.desc() if reverse else InventoryRow.id.asc())
        rows = query.offset((page - 1) * per_page).limit(per_page).all()

    return rows, total_count


def is_price_stale(price_updated_at: datetime | None) -> bool:
    if price_updated_at is None:
        return True
    return price_updated_at < datetime.utcnow() - timedelta(days=PRICE_STALE_DAYS)


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

    total_value = 0.0
    total_cards = 0
    seen_names: set[str] = set()
    drawer_counts = {str(i): 0 for i in range(1, 7)}
    unassigned_count = 0

    for row in rows:
        price = effective_price(row.card, row.finish)
        if price is not None:
            total_value += price * row.quantity
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

    # Resolve scryfall_ids → card_ids in one query.
    scryfall_ids = sorted({r.get("scryfall_id") for r in parsed_rows if r.get("scryfall_id")})
    card_by_sid: dict[str, int] = {}
    if scryfall_ids:
        for card_row in (
            session.query(Card.id, Card.scryfall_id)
            .filter(Card.scryfall_id.in_(scryfall_ids))
            .all()
        ):
            card_by_sid[card_row.scryfall_id] = card_row.id

    # Build the set of (card_id, finish) tuples to look up.
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

    # One tuple-IN query for all matching inventory rows. Outerjoin so
    # pending rows (storage_location_id IS NULL) come through with loc=None.
    breakdown_by_key: dict[tuple[int, str], list[dict]] = {key: [] for key in lookup_keys}
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
    row.updated_at = datetime.utcnow()

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
    now = datetime.utcnow()

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
        session.delete(row)
        session.commit()
        return None

    row.quantity -= quantity
    row.updated_at = datetime.utcnow()

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
        row.updated_at = datetime.utcnow()
        if row.quantity <= 0:
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
            row.updated_at = datetime.utcnow()
            if row.quantity <= 0:
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
            or_(InventoryRow.storage_location_id.is_(None), StorageLocation.type != "deck"),
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

    now = datetime.utcnow()
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
