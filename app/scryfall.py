"""Scryfall API integration.

This module owns HTTP retry/throttle behavior and normalization of Scryfall
responses into the Card model shape used by the rest of the app.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from functools import lru_cache
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from sqlalchemy.orm import Session
from urllib3.util.retry import Retry

from app.models import Card

SCRYFALL_CARD_URL = "https://api.scryfall.com/cards"
HEADERS = {"User-Agent": "ManaArchive/1.0", "Accept": "application/json"}
REQUEST_DELAY_SECONDS = 0.08

_session = requests.Session()
_retry = Retry(
    total=4,
    connect=4,
    read=4,
    backoff_factor=0.6,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    respect_retry_after_header=True,
)
_adapter = HTTPAdapter(max_retries=_retry)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    now = time.monotonic()
    elapsed = now - _last_request_at
    if elapsed < REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def _normalize_card_payload(raw: dict[str, Any]) -> dict[str, Any]:
    image_uris = raw.get("image_uris") or {}
    prices = raw.get("prices") or {}
    card_faces = raw.get("card_faces") or []
    if not image_uris and card_faces:
        first_face = card_faces[0] or {}
        image_uris = first_face.get("image_uris") or {}

    oracle_text = raw.get("oracle_text")
    if not oracle_text and card_faces:
        oracle_text = "\n\n".join(
            face.get("oracle_text", "") for face in card_faces if face.get("oracle_text")
        )

    type_line = raw.get("type_line")
    if not type_line and card_faces:
        type_line = " // ".join(
            face.get("type_line", "") for face in card_faces if face.get("type_line")
        )

    mana_cost = raw.get("mana_cost")
    if not mana_cost and card_faces:
        mana_cost = (
            " // ".join(face.get("mana_cost", "") for face in card_faces if face.get("mana_cost"))
            or None
        )

    raw_colors = raw.get("colors") or []
    colors_str = " ".join(raw_colors) if raw_colors else None

    raw_identity = raw.get("color_identity") or []
    color_identity_str = " ".join(raw_identity)  # "" = colorless, never None after a fetch

    return {
        "scryfall_id": raw.get("id"),
        "name": raw.get("name"),
        "set_code": raw.get("set"),
        "set_name": raw.get("set_name"),
        "collector_number": raw.get("collector_number"),
        "rarity": raw.get("rarity"),
        "image_url": image_uris.get("normal") or image_uris.get("large") or image_uris.get("small"),
        "type_line": type_line,
        "oracle_text": oracle_text,
        "price_usd": prices.get("usd"),
        "price_usd_foil": prices.get("usd_foil"),
        "price_usd_etched": prices.get("usd_etched"),
        "colors": colors_str,
        "color_identity": color_identity_str,
        "mana_cost": mana_cost,
        "cmc": raw.get("cmc"),
        "legalities": json.dumps(raw.get("legalities") or {}),
    }


def _get_json(url: str) -> dict[str, Any] | None:
    try:
        _throttle()
        response = _session.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        _throttle()
        response = _session.post(url, json=payload, headers=HEADERS, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


_COLLECTION_BATCH_SIZE = 75

# Cache keyed on (version, frozenset of scryfall_ids) → deduped token list.
# Bump _DECK_TOKEN_CACHE_VERSION when the returned dict shape changes.
_DECK_TOKEN_CACHE_VERSION = 2
_deck_token_cache: dict[tuple, list[dict[str, str]]] = {}


def fetch_deck_tokens(scryfall_ids: list[str]) -> list[dict[str, str]]:
    """Return deduplicated tokens produceable by the given cards, with images.

    Pass 1: batch-fetch deck cards to collect token stubs from all_parts.
    Pass 2: batch-fetch the token cards themselves to get image_uris and set info.
    Result is cached per unique set of scryfall_ids — deck page reloads are free.
    Returns sorted list of {name, type_line, image_url, set_code, collector_number, scryfall_id}.
    """
    cache_key = (_DECK_TOKEN_CACHE_VERSION, frozenset(sid for sid in scryfall_ids if sid))
    if cache_key in _deck_token_cache:
        return _deck_token_cache[cache_key]

    # Pass 1: collect token stubs from all_parts of the deck cards
    seen_ids: set[str] = set()
    token_stubs: list[dict[str, str]] = []
    ids = list(cache_key[1])

    for i in range(0, len(ids), _COLLECTION_BATCH_SIZE):
        batch = ids[i : i + _COLLECTION_BATCH_SIZE]
        payload = {"identifiers": [{"id": sid} for sid in batch]}
        data = _post_json(f"{SCRYFALL_CARD_URL}/collection", payload)
        if not data:
            continue
        for card in data.get("data", []):
            for part in card.get("all_parts") or []:
                if part.get("component") != "token":
                    continue
                token_id = part.get("id", "")
                if token_id and token_id not in seen_ids:
                    seen_ids.add(token_id)
                    token_stubs.append(
                        {
                            "id": token_id,
                            "name": part.get("name", ""),
                            "type_line": part.get("type_line", ""),
                        }
                    )

    # Pass 2: batch-fetch the token cards to get image_uris and set info
    token_meta: dict[str, dict[str, str]] = {}
    token_ids = [t["id"] for t in token_stubs]
    for i in range(0, len(token_ids), _COLLECTION_BATCH_SIZE):
        batch = token_ids[i : i + _COLLECTION_BATCH_SIZE]
        payload = {"identifiers": [{"id": tid} for tid in batch]}
        data = _post_json(f"{SCRYFALL_CARD_URL}/collection", payload)
        if not data:
            continue
        for card in data.get("data", []):
            card_id = card.get("id", "")
            image_uris = card.get("image_uris") or {}
            card_faces = card.get("card_faces") or []
            if not image_uris and card_faces:
                image_uris = (card_faces[0] or {}).get("image_uris") or {}
            url = (
                image_uris.get("normal") or image_uris.get("large") or image_uris.get("small") or ""
            )
            if card_id:
                token_meta[card_id] = {
                    "image_url": url,
                    "set_code": card.get("set", ""),
                    "collector_number": card.get("collector_number", ""),
                }

    result = sorted(
        [
            {
                "name": t["name"],
                "type_line": t["type_line"],
                "image_url": token_meta.get(t["id"], {}).get("image_url", ""),
                "set_code": token_meta.get(t["id"], {}).get("set_code", ""),
                "collector_number": token_meta.get(t["id"], {}).get("collector_number", ""),
                "scryfall_id": t["id"],
            }
            for t in token_stubs
        ],
        key=lambda t: t["name"],
    )
    _deck_token_cache[cache_key] = result
    return result


def bulk_refresh_prices(scryfall_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch fresh price data for many cards using the /cards/collection batch endpoint.

    Returns a dict keyed by scryfall_id with normalized card payloads.
    Makes ceil(N/75) requests instead of N individual requests.
    """
    results: dict[str, dict[str, Any]] = {}
    ids = [sid for sid in scryfall_ids if sid]

    for i in range(0, len(ids), _COLLECTION_BATCH_SIZE):
        batch = ids[i : i + _COLLECTION_BATCH_SIZE]
        payload = {"identifiers": [{"id": sid} for sid in batch]}
        data = _post_json(f"{SCRYFALL_CARD_URL}/collection", payload)
        if not data:
            continue
        for card in data.get("data", []):
            normalized = _normalize_card_payload(card)
            if normalized.get("scryfall_id"):
                results[normalized["scryfall_id"]] = normalized

    return results


def bulk_fetch_by_set_number(
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Batch-fetch cards by (set_code, collector_number) via /cards/collection.

    Returns a dict keyed by (set_code_lower, collector_number).
    Makes ceil(N/75) requests instead of N individual requests.
    """
    results: dict[tuple[str, str], dict[str, Any]] = {}
    seen: dict[tuple[str, str], None] = {}
    for s, c in pairs:
        key = ((s or "").strip().lower(), (c or "").strip())
        if key[0] and key[1]:
            seen[key] = None
    unique = list(seen.keys())

    for i in range(0, len(unique), _COLLECTION_BATCH_SIZE):
        batch = unique[i : i + _COLLECTION_BATCH_SIZE]
        payload = {"identifiers": [{"set": s, "collector_number": c} for s, c in batch]}
        data = _post_json(f"{SCRYFALL_CARD_URL}/collection", payload)
        if not data:
            continue
        for card in data.get("data", []):
            normalized = _normalize_card_payload(card)
            if normalized.get("scryfall_id"):
                key = (
                    (normalized.get("set_code") or "").lower(),
                    normalized.get("collector_number") or "",
                )
                results[key] = normalized

    return results


@lru_cache(maxsize=8192)
def _fetch_by_id_cached(scryfall_id: str) -> dict[str, Any] | None:
    scryfall_id = (scryfall_id or "").strip()
    if not scryfall_id:
        return None
    raw = _get_json(f"{SCRYFALL_CARD_URL}/{scryfall_id}")
    return _normalize_card_payload(raw) if raw else None


@lru_cache(maxsize=8192)
def _fetch_by_set_number_cached(set_code: str, collector_number: str) -> dict[str, Any] | None:
    set_code = (set_code or "").strip().lower()
    collector_number = (collector_number or "").strip()
    if not set_code or not collector_number:
        return None
    raw = _get_json(f"{SCRYFALL_CARD_URL}/{set_code}/{collector_number}")
    return _normalize_card_payload(raw) if raw else None


def fetch_card_by_scryfall_id(scryfall_id: str) -> dict[str, Any] | None:
    return _fetch_by_id_cached((scryfall_id or "").strip())


def fetch_card_by_set_and_number(set_code: str, collector_number: str) -> dict[str, Any] | None:
    collector_number = (collector_number or "").strip()
    if collector_number.endswith("*"):
        collector_number = collector_number[:-1].strip()
    return _fetch_by_set_number_cached((set_code or "").strip().lower(), collector_number)


@lru_cache(maxsize=4096)
def _fetch_by_name_cached(name: str, set_code: str) -> dict[str, Any] | None:
    if not name:
        return None
    params = f"exact={requests.utils.quote(name)}"
    if set_code:
        params += f"&set={set_code}"
    raw = _get_json(f"{SCRYFALL_CARD_URL}/named?{params}")
    if not raw:
        # Fall back to fuzzy match (handles minor typos and alternate punctuation)
        params = f"fuzzy={requests.utils.quote(name)}"
        if set_code:
            params += f"&set={set_code}"
        raw = _get_json(f"{SCRYFALL_CARD_URL}/named?{params}")
    return _normalize_card_payload(raw) if raw else None


def fetch_card_by_name(name: str, set_code: str = "") -> dict[str, Any] | None:
    return _fetch_by_name_cached(
        (name or "").strip(),
        (set_code or "").strip().lower(),
    )


def refresh_card_from_scryfall(session: Session, card_id: int) -> bool:
    """Refresh a single card from Scryfall. Caller is responsible for commit."""
    card = session.query(Card).filter(Card.id == card_id).first()
    if not card:
        return False

    # Bypass lru_cache so we get truly fresh data
    raw = _get_json(f"{SCRYFALL_CARD_URL}/{card.scryfall_id}")
    if not raw:
        return False
    fresh = _normalize_card_payload(raw)

    card.name = fresh["name"]
    card.set_code = fresh["set_code"]
    card.set_name = fresh["set_name"]
    card.collector_number = fresh["collector_number"]
    card.rarity = fresh["rarity"]
    card.image_url = fresh["image_url"]
    card.type_line = fresh["type_line"]
    card.oracle_text = fresh["oracle_text"]
    card.price_usd = fresh["price_usd"]
    card.price_usd_foil = fresh["price_usd_foil"]
    card.price_usd_etched = fresh["price_usd_etched"]
    card.colors = fresh["colors"]
    card.color_identity = fresh["color_identity"]
    card.mana_cost = fresh["mana_cost"]
    card.cmc = fresh["cmc"]
    card.legalities = fresh["legalities"]
    card.updated_at = datetime.utcnow()
    return True


@lru_cache(maxsize=4096)
def fetch_card_traits(scryfall_id: str) -> dict[str, bool] | None:
    scryfall_id = (scryfall_id or "").strip()
    if not scryfall_id:
        return None

    raw = _get_json(f"{SCRYFALL_CARD_URL}/{scryfall_id}")
    if not raw:
        return None

    type_line = (raw.get("type_line") or "").lower()
    card_faces = raw.get("card_faces") or []
    if not type_line and card_faces:
        type_line = " // ".join((face.get("type_line") or "") for face in card_faces).lower()

    return {
        "is_basic_land": "basic land" in type_line,
        "is_full_art": bool(raw.get("full_art")),
    }


def autocomplete_token_names(query: str, limit: int = 10) -> list[str]:
    """Return token name suggestions matching the user's typed prefix.

    Uses the search API (not the autocomplete catalog) because catalog has no
    token-only filter — `is:token` ensures we don't mix in real cards. Names
    are deduplicated since Scryfall returns one row per printing.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    url = (
        "https://api.scryfall.com/cards/search"
        f"?q=is%3Atoken+name%3A{requests.utils.quote(q)}&unique=cards"
    )
    data = _get_json(url)
    if not data:
        return []
    seen: list[str] = []
    seen_set: set[str] = set()
    for card in data.get("data", []):
        name = card.get("name")
        if name and name not in seen_set:
            seen_set.add(name)
            seen.append(name)
            if len(seen) >= limit:
                break
    return seen


def _format_token_response(card: dict[str, Any]) -> dict[str, Any]:
    """Convert a Scryfall card payload into the form-ready dict the
    /tokens/api/lookup endpoint returns. Handles double-faced tokens via
    card_faces and derives subtype from the type_line.
    """
    type_line = card.get("type_line") or ""
    subtype: str | None = None
    if "—" in type_line:
        subtype = type_line.split("—", 1)[1].strip().split(" ")[0] or None

    faces = card.get("card_faces") or []
    is_dfc = len(faces) == 2
    if is_dfc:
        front, back = faces[0], faces[1]
        image_url = (front.get("image_uris") or {}).get("normal") or (
            front.get("image_uris") or {}
        ).get("large")
        back_image_url = (back.get("image_uris") or {}).get("normal") or (
            back.get("image_uris") or {}
        ).get("large")
        return {
            "name": front.get("name") or card.get("name"),
            "type_line": front.get("type_line") or type_line,
            "subtype": subtype,
            "set_code": card.get("set"),
            "collector_number": card.get("collector_number"),
            "scryfall_id": card.get("id"),
            "image_url": image_url,
            "is_double_sided": True,
            "back_name": back.get("name"),
            "back_image_url": back_image_url,
        }

    image_url = (card.get("image_uris") or {}).get("normal") or (card.get("image_uris") or {}).get(
        "large"
    )
    # Substitute-card sets (SZNR etc.) have "Double-Faced" in the name because
    # they SUBSTITUTE for a DFC during play — but they themselves are
    # single-sided with a standard MTG back, so they can run in clear sleeves
    # without leaking what's substituted. Scryfall correctly stores them as
    # layout=normal with no card_faces; is_double_sided stays False. Real
    # double-faced tokens (Goblin // Treasure) are caught by the is_dfc branch
    # above via card_faces.
    return {
        "name": card.get("name"),
        "type_line": type_line,
        "subtype": subtype,
        "set_code": card.get("set"),
        "collector_number": card.get("collector_number"),
        "scryfall_id": card.get("id"),
        "image_url": image_url,
        "is_double_sided": False,
        "back_name": None,
        "back_image_url": None,
    }


def fetch_token_by_set_number(set_code: str, collector_number: str) -> dict[str, Any] | None:
    """Look up a single token by set + collector number (most precise path).

    Token sets on Scryfall are typically prefixed with `t` (e.g. tbig for the
    BIG token set). If the user enters "big" and the regular set's collector
    number doesn't return a token, this falls back to "tbig". Leading zeros
    on the collector number are stripped — Scryfall uses "6" not "0006".
    """
    code = (set_code or "").strip().lower()
    cn_raw = (collector_number or "").strip()
    if not code or not cn_raw:
        return None
    cn = cn_raw.lstrip("0") or "0"

    candidates = [code]
    if not code.startswith("t"):
        candidates.append("t" + code)

    for c in candidates:
        url = f"https://api.scryfall.com/cards/{c}/{requests.utils.quote(cn)}"
        data = _get_json(url)
        if data and data.get("set_type") == "token":
            return _format_token_response(data)
    return None


def search_tokens_by_name(name: str, limit: int = 12) -> list[dict[str, Any]]:
    """Return up to `limit` matching tokens with form-ready fields each.

    For disambiguating tokens with the same name across multiple sets — e.g.,
    'Treasure' has dozens of printings; 'Goblin' has many. The picker UI
    consumes this so the user can choose the right printing.

    DFC tokens are surfaced first within the limit because most users aren't
    looking up "yet another Goblin token" — if they want a double-faced
    Goblin/Blood, they want to see it ranked highly.
    """
    n = (name or "").strip()
    if len(n) < 2:
        return []
    # Pull a larger window so DFCs (often older printings buried under recent
    # singles in release order) survive the trim to `limit`.
    url = (
        "https://api.scryfall.com/cards/search"
        f"?q=is%3Atoken+name%3A{requests.utils.quote(n)}"
        "&unique=prints&order=released&dir=desc"
    )
    data = _get_json(url)
    cards = data.get("data", []) if data else []
    formatted = [_format_token_response(c) for c in cards]
    formatted.sort(key=lambda t: (0 if t["is_double_sided"] else 1))
    return formatted[:limit]


def fetch_token_by_name(name: str) -> dict[str, Any] | None:
    """Look up a single token by name and return form-ready fields.

    Tries an exact `!"name"` match first, falls back to fuzzy `name:` search.
    Returns None if no token matches.
    """
    n = (name or "").strip()
    if not n:
        return None
    url = (
        "https://api.scryfall.com/cards/search"
        f'?q=is%3Atoken+!"{requests.utils.quote(n)}"&unique=cards&order=released&dir=desc'
    )
    data = _get_json(url)
    cards = data.get("data", []) if data else []
    if not cards:
        url = (
            "https://api.scryfall.com/cards/search"
            f"?q=is%3Atoken+name%3A{requests.utils.quote(n)}&unique=cards&order=released&dir=desc"
        )
        data = _get_json(url)
        cards = data.get("data", []) if data else []
    if not cards:
        return None
    return _format_token_response(cards[0])


def fetch_game_changer_names() -> list[str]:
    """Return the current Scryfall `is:gamechanger` card-name list.

    Used to seed/refresh the game_changer_cards table for the bracket
    estimator. Returns an empty list on network failure so the caller can
    fall back to a hardcoded seed.
    """
    names: list[str] = []
    url = "https://api.scryfall.com/cards/search?q=is%3Agamechanger&unique=cards"
    while url:
        data = _get_json(url)
        if not data:
            break
        for card in data.get("data", []):
            name = card.get("name")
            if name:
                names.append(name)
        url = data.get("next_page") if data.get("has_more") else None
    return names


def fetch_set_cards(set_code: str) -> list[dict[str, Any]]:
    set_code = (set_code or "").strip().lower()
    if not set_code:
        return []

    results = []
    url = f"https://api.scryfall.com/cards/search?q=e:{set_code}&unique=prints&order=set"

    while url:
        data = _get_json(url)
        if not data:
            break

        for card in data.get("data", []):
            normalized = _normalize_card_payload(card)
            results.append(normalized)

        if data.get("has_more"):
            url = data.get("next_page")
        else:
            url = None

    return results


def search_cards_by_name(name: str, limit: int = 500) -> list[dict[str, Any]]:
    """Full printing list for a card-name search on the manual-import picker.

    Follows Scryfall's ``next_page`` pagination so popular reprints (Sol
    Ring ~80, basic lands ~hundreds) show every printing the user could
    pick. Each page is 175 cards; the helper iterates until ``has_more``
    is false or ``limit`` is hit. ``_throttle`` runs per page request so
    Scryfall's rate-limit is respected.

    ``limit`` defaults to 500 — high enough that even basic lands return
    every printing in practice, low enough that a degenerate one-letter
    query won't pull thousands of pages.
    """
    query = name.strip()
    if not query:
        return []

    url: str | None = (
        "https://api.scryfall.com/cards/search"
        f'?q=!"{query}" or {query}&unique=prints&order=released&dir=desc'
    )
    cards: list[dict[str, Any]] = []
    while url and len(cards) < limit:
        data = _get_json(url)
        if not data:
            break
        cards.extend(data.get("data", []))
        url = data.get("next_page") if data.get("has_more") else None
    cards = cards[:limit]

    return [
        {
            "id": card.get("id"),
            "name": card.get("name"),
            "set": card.get("set"),
            "set_name": card.get("set_name"),
            "collector_number": card.get("collector_number"),
            "rarity": card.get("rarity"),
            "image_uris": card.get("image_uris"),
            "card_faces": card.get("card_faces"),
            "prices": card.get("prices"),
        }
        for card in cards
    ]


def autocomplete_cards_for_add(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Slim card-autocomplete payload for the deck "Add card" UI.

    Used by ``/decks/api/card-autocomplete``: top printings matching the
    typed name, returned with just enough info to render a dropdown row
    (name + set/collector + thumbnail) and resolve the user's pick to a
    Scryfall ID. Ordered by release date desc (newest printing first)
    via ``unique=prints``; for a deck-builder use case the user usually
    wants the most recent reprint they can buy. Single-faced cards use
    ``image_uris.small``; DFCs fall back to ``card_faces[0].image_uris.small``.

    ``limit`` defaults to 50 — high enough to cover even popular reprints
    (Sol Ring has ~80 printings, but recent ones bubble to the top and 50
    is a reasonable scroll length). Short-tail cards return only their
    actual prints. Scryfall's API page is capped at 175 so we never need
    pagination at this limit.

    No DB writes. Pure Scryfall passthrough.
    """
    q = query.strip()
    if len(q) < 2:
        return []

    url = (
        "https://api.scryfall.com/cards/search"
        f"?q={requests.utils.quote(q)}&unique=prints&order=released&dir=desc"
    )
    data = _get_json(url)
    if not data:
        return []

    cards = data.get("data", [])
    out: list[dict[str, Any]] = []
    for card in cards[:limit]:
        image_small = None
        image_uris = card.get("image_uris") or {}
        if image_uris:
            image_small = image_uris.get("small")
        else:
            faces = card.get("card_faces") or []
            if faces and isinstance(faces[0], dict):
                front_uris = faces[0].get("image_uris") or {}
                image_small = front_uris.get("small")
        out.append(
            {
                "scryfall_id": card.get("id"),
                "name": card.get("name"),
                "set_code": card.get("set"),
                "set_name": card.get("set_name"),
                "collector_number": card.get("collector_number"),
                "image_uri_small": image_small,
            }
        )
    return out
