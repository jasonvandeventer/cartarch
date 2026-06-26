"""Scryfall API integration.

This module owns HTTP retry/throttle behavior and normalization of Scryfall
responses into the Card model shape used by the rest of the app.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import ijson
import requests
from requests.adapters import HTTPAdapter
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session
from urllib3.util.retry import Retry

from app.db import engine, shutdown_event
from app.models import Card
from app.timeutil import utc_now

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
    # POST is included because the only POSTs we make are to
    # /cards/collection — an idempotent read-only batch *lookup* (it
    # mutates nothing server-side), so retrying it on 429/5xx is safe and
    # correct. Without this, a single transient 429/503 on a 75-id batch
    # POST raised immediately and silently dropped all 75 ids to the
    # per-row fallback path (the 5,758-row Helvault /import/preview 524
    # timeout: 56 of ~75 batches lost, 4,301 sequential GET fallbacks).
    allowed_methods=frozenset(["GET", "POST"]),
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

    # v3.36.1 — loyalty / defense. Faithful raw Scryfall strings, stored
    # verbatim (parsing/int-coercion is the goldfish Step-4 job, not here).
    # Same top-level-then-first-face fallback the other face-split attrs
    # use: planeswalker loyalty rides the PW face on transform/MDFC PWs;
    # Battle defense rides the front face on Siege/DFC battles. None on
    # cards that carry neither.
    loyalty = raw.get("loyalty")
    if loyalty is None and card_faces:
        loyalty = next((face.get("loyalty") for face in card_faces if face.get("loyalty")), None)

    defense = raw.get("defense")
    if defense is None and card_faces:
        defense = next((face.get("defense") for face in card_faces if face.get("defense")), None)

    raw_colors = raw.get("colors") or []
    colors_str = " ".join(raw_colors) if raw_colors else None

    raw_identity = raw.get("color_identity") or []
    color_identity_str = " ".join(raw_identity)  # "" = colorless, never None after a fetch

    # v3.30.11 — produced_tokens. Capture the subset of Scryfall's all_parts
    # array whose component is exactly "token" (mirrors the discrimination
    # in fetch_deck_tokens line 182). Per-entry shape: {name, type_line,
    # scryfall_id} — `id` from Scryfall is stored as `scryfall_id` so the
    # consumer flip can look up token cards in the scryfall_cards bulk cache
    # directly. component is the filter criterion, not stored (only-tokens-
    # stored, implicit by presence). object/uri not useful downstream.
    # Empty list → "[]" (NOT NULL); a NULL column means "this row predates
    # the v3.30.11 daemon backfill" and is distinguishable from "we processed
    # this card and it has no tokens". v3.30.11 is the data half of a
    # two-release sequence; v3.30.19 is the consumer-flip release that
    # retires fetch_deck_tokens's request-path Scryfall calls in favour
    # of local reads against this column. (The consumer flip was renumbered
    # six times from its original v3.30.12 slot — preempted by counter-pill
    # render fix, click-to-adjust UX, drag-attach, the round-trip importer
    # fix, the export schema expansion, the deck auto-create orphan fix,
    # and the v3.30.17 corrective patch — before shipping as v3.30.19.)
    produced_tokens_list: list[dict[str, str]] = []
    seen_token_ids: set[str] = set()
    for part in raw.get("all_parts") or []:
        if (part or {}).get("component") != "token":
            continue
        tok_id = part.get("id") or ""
        if not tok_id or tok_id in seen_token_ids:
            continue
        seen_token_ids.add(tok_id)
        produced_tokens_list.append(
            {
                "name": part.get("name") or "",
                "type_line": part.get("type_line") or "",
                "scryfall_id": tok_id,
            }
        )

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
        "full_art": bool(raw.get("full_art")),
        "frame_effects": json.dumps(raw.get("frame_effects") or []),
        "set_type": (raw.get("set_type") or "").lower(),
        "layout": (raw.get("layout") or "").lower(),
        # v3.30.11 — 22nd key, appended at the end to preserve byte-
        # identical ordering of the existing 21 keys. _CACHE_COLUMNS
        # below + _cached_row_to_payload track this addition.
        # NOT a Card ORM column — scryfall_cards-only field. Card-
        # constructor call sites MUST strip this key before
        # ``Card(**payload)`` via ``card_constructor_kwargs`` below.
        "produced_tokens": json.dumps(produced_tokens_list),
        # v3.36.1 — 23rd + 24th keys, appended LAST to preserve byte-
        # identical ordering of the existing 22 keys. _CACHE_COLUMNS +
        # _cached_row_to_payload track these. UNLIKE produced_tokens
        # these ARE Card ORM columns, so they are NOT stripped by
        # card_constructor_kwargs.
        "loyalty": loyalty,
        "defense": defense,
    }


# v3.30.21 hotfix — keys present in the normalized scryfall payload but
# NOT modeled as Card columns. Card-constructor call sites must strip
# these before splatting into ``Card(**payload)`` or SQLAlchemy raises
# TypeError ("invalid keyword argument for Card"). Centralized here as
# the single source of truth so any future scryfall_cards-only column
# additions need exactly one update site (this set) — the daemon writes
# them via _BULK_UPSERT_SQL (which reads _CACHE_COLUMNS, the FULL set),
# fetch_deck_tokens reads them via _cache_get_by_ids → _cached_row_to_payload
# (which also returns the FULL set), and only the Card(**payload) splat
# path needs the sanitized subset.
_CARD_PAYLOAD_EXCLUDED_KEYS: frozenset[str] = frozenset({"produced_tokens"})


def card_constructor_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a Card-constructor-safe subset of a scryfall payload dict.

    Strips keys that exist in the normalized scryfall payload (v3.30.11
    extended the seam to 22 keys, adding ``produced_tokens``) but are NOT
    modeled as ``Card`` ORM columns. Without this strip, ``Card(**payload)``
    raises TypeError on every cache-miss code path that builds Cards
    from a fresh Scryfall fetch:

    - ``import_service.py`` ``Card(**payload, updated_at=now)`` — the
      ``POST /decks/{id}/add-card`` flow and import-rows persistence.
    - ``inventory_service.py`` ``Card(**payload, updated_at=...)`` — the
      ``POST /decks/{id}/rows/{row_id}/switch-printing`` flow.

    The daemon's ``_bulk_data_loop`` and the v3.30.19 ``fetch_deck_tokens``
    / v3.30.21 ``get_deck_produced_tokens_for_goldfish`` consumers all
    read ``produced_tokens`` from the FULL payload — they MUST NOT use
    this helper. Only Card-constructor splat sites should call it.
    """
    return {k: v for k, v in payload.items() if k not in _CARD_PAYLOAD_EXCLUDED_KEYS}


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
    except requests.RequestException as exc:
        # Case (a): HTTP/network error AFTER the retry adapter exhausted its
        # attempts. With POST now retried, reaching here means Scryfall was
        # persistently unreachable for this batch — make it observable
        # instead of silently dropping the whole chunk.
        n = len(payload.get("identifiers", []))
        print(
            f"[scryfall-bulk] POST failed after retries ({n} identifiers): {exc}",
            flush=True,
        )
        return None


_COLLECTION_BATCH_SIZE = 75

# Cache keyed on (version, frozenset of scryfall_ids) → deduped token list.
# Bump _DECK_TOKEN_CACHE_VERSION when the returned dict shape changes.
_DECK_TOKEN_CACHE_VERSION = 2
_deck_token_cache: dict[tuple, list[dict[str, str]]] = {}


def extract_token_stubs(payloads: dict) -> list[dict[str, str]]:
    """Parse the ``produced_tokens`` JSON of a set of card payloads into
    deduped token stubs ``{id, name, type_line}``.

    ``payloads`` is the dict returned by ``_cache_get_by_ids`` (id → payload);
    only ``.values()`` are read. Per-payload, ``produced_tokens`` is the v3.30.11
    daemon-written JSON list of ``{name, type_line, scryfall_id}`` dicts. NULL
    (not-yet-backfilled) and ``"[]"`` (confirmed no tokens) both contribute
    nothing; malformed JSON / non-list / non-dict entries are skipped. Stubs are
    deduplicated by token id across all payloads.

    ``name``/``type_line`` are ``.strip()``-ed — whitespace padding in the
    all_parts payload was never intentional. This is the single source for both
    ``fetch_deck_tokens`` (here) and ``get_deck_produced_tokens_for_goldfish``
    (deck_service), which previously held drifted copies of this block.
    """
    seen_token_ids: set[str] = set()
    token_stubs: list[dict[str, str]] = []
    for payload in payloads.values():
        raw = payload.get("produced_tokens")
        if not raw or raw == "[]":
            continue
        try:
            parts = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            tid = part.get("scryfall_id") or part.get("id") or ""
            if not tid or tid in seen_token_ids:
                continue
            seen_token_ids.add(tid)
            token_stubs.append(
                {
                    "id": tid,
                    "name": (part.get("name") or "").strip(),
                    "type_line": (part.get("type_line") or "").strip(),
                }
            )
    return token_stubs


def fetch_deck_tokens(scryfall_ids: list[str]) -> list[dict[str, str]]:
    """Return deduplicated tokens produceable by the given cards, with images.

    v3.30.19 — local read path. Reads ``scryfall_cards.produced_tokens``
    (the v3.30.11 22nd column) for each deck card, then resolves the
    token cards locally to attach image_url / set_code / collector_number.
    **Zero Scryfall network calls.** Closes the request-path Scryfall
    violation v3.27.9 tolerated and v3.30.11 built the data foundation
    to retire — the third such retirement after v3.27.9 (combos) and
    v3.27.13 (set completion).

    Pass 1 (local): ``_cache_get_by_ids(deck_card_ids)`` → for each card
    with non-NULL non-``"[]"`` ``produced_tokens``, parse the JSON list
    of {name, type_line, scryfall_id} dicts the v3.30.11 daemon wrote.
    Pass 2 (local): ``_cache_get_by_ids(token_ids)`` → resolve each
    token's image_url, set_code, collector_number, plus canonical name
    + type_line from the token card's own row (preferred over the
    all_parts stub, which can lag set releases).

    NULL ``produced_tokens`` → silently skip (the v3.30.11 NULL-vs-"[]"
    contract: NULL means "not yet backfilled"; "[]" means "confirmed
    no tokens"; either way the card contributes no tokens here). Prod
    has zero NULL after the v3.30.11 daemon's first full pass; fresh
    installs before the daemon runs will have NULLs and degrade
    gracefully (the deck's token list will be incomplete for those
    cards — accepted; the request-path invariant is the hard constraint).

    Result is deduplicated by token name (per the v3.30.19 spec — same
    token produced by multiple deck cards appears once; if multiple
    printings exist, prefer the entry with a non-empty image_url) and
    cached per unique frozenset of input ids. Return shape is byte-
    identical to the legacy Scryfall path: sorted list of
    {name, type_line, image_url, set_code, collector_number, scryfall_id}.
    Downstream consumers (compute_deck_tokens, deck-detail Tokens panel,
    v3.30.10 goldfish enrichment via the panels-cache) are unchanged.
    """
    cache_key = (_DECK_TOKEN_CACHE_VERSION, frozenset(sid for sid in scryfall_ids if sid))
    if cache_key in _deck_token_cache:
        return _deck_token_cache[cache_key]
    ids = list(cache_key[1])
    if not ids:
        _deck_token_cache[cache_key] = []
        return []

    # Pass 1 (local) — read produced_tokens for the deck cards.
    # _cache_get_by_ids batches internally; we just hand it the full list.
    # NULL produced_tokens (not-yet-backfilled) and "[]" (confirmed no
    # tokens) both end up contributing nothing — graceful degradation
    # for the not-backfilled case, common-case skip for the empty case.
    # Stub parse/dedup is the shared extract_token_stubs helper.
    deck_card_payloads = _cache_get_by_ids(ids)
    token_stubs = extract_token_stubs(deck_card_payloads)

    # Pass 2 (local) — resolve token card details (image_url, set_code,
    # collector_number, canonical name/type_line). Replaces the second
    # Scryfall POST. A token id that's NOT in scryfall_cards (rare —
    # would mean the bulk export hasn't propagated this token yet)
    # falls through to the stub's name/type_line with empty image_url
    # — render-time fallback renders the text card face per the
    # v3.30.10 graceful-degradation pattern.
    token_meta: dict[str, dict[str, str]] = {}
    if token_stubs:
        token_payloads = _cache_get_by_ids([t["id"] for t in token_stubs])
        for tid, payload in token_payloads.items():
            token_meta[tid] = {
                "image_url": payload.get("image_url") or "",
                "set_code": payload.get("set_code") or "",
                "collector_number": payload.get("collector_number") or "",
                "name": payload.get("name") or "",
                "type_line": payload.get("type_line") or "",
            }

    # Build per-token dicts, deduplicating BY NAME (per v3.30.19 spec
    # step 6 — same token produced by multiple deck cards appears once;
    # if multiple printings exist, prefer the entry with a non-empty
    # image_url). Canonical name/type_line from the token card's own row
    # wins over the all_parts stub when available.
    by_name: dict[str, dict[str, str]] = {}
    for t in token_stubs:
        meta = token_meta.get(t["id"], {})
        name = meta.get("name") or t["name"]
        if not name:
            continue
        type_line = meta.get("type_line") or t["type_line"]
        entry = {
            "name": name,
            "type_line": type_line,
            "image_url": meta.get("image_url", ""),
            "set_code": meta.get("set_code", ""),
            "collector_number": meta.get("collector_number", ""),
            "scryfall_id": t["id"],
        }
        existing = by_name.get(name)
        if existing is None:
            by_name[name] = entry
        elif not existing.get("image_url") and entry.get("image_url"):
            # Prefer the entry with a non-empty image_url
            by_name[name] = entry

    result = sorted(by_name.values(), key=lambda t: t["name"])
    _deck_token_cache[cache_key] = result
    return result


@dataclass(frozen=True)
class BulkFetchResult:
    """Result of a batched /cards/collection lookup.

    ``cards`` is the resolved payload map (the previous bare return value —
    callers that only need lookups read ``.cards``). ``not_found`` and
    ``failed`` make the resolution gap structurally visible:

    - ``not_found``: identifiers Scryfall explicitly returned in its
      ``not_found`` array — genuinely unknown to Scryfall. Permanent;
      retrying will not resolve them.
    - ``failed``: identifiers whose batch POST errored after the retry
      adapter exhausted its attempts (Scryfall unreachable/persistently
      5xx for that chunk). Transient; a later import may resolve them.

    The identifier element type matches the function: ``str`` scryfall_id
    for :func:`bulk_refresh_prices`, ``(set_lower, collector)`` tuple for
    :func:`bulk_fetch_by_set_number`. Keeping the two distinct lets the
    caller tell "this card does not exist" apart from "Scryfall was down"
    when reporting to the user.
    """

    cards: dict = field(default_factory=dict)
    not_found: list = field(default_factory=list)
    failed: list = field(default_factory=list)


# Local Scryfall bulk-data cache (v3.25.0).
#
# These helpers turn the two batch resolvers into local-first lookups: a
# SQLite SELECT against `scryfall_cards` resolves what it can, and ONLY the
# miss subset falls through to the existing /cards/collection POST. No new
# network call is introduced on the request path — the only added
# request-path code is the SELECT below. An empty/missing cache returns {}
# from every helper, so every identifier misses and the functions behave
# exactly as they do on `main` today (the safe first-deploy default). Any
# cache error is logged and degraded to {} for the same reason — a cache
# problem can never make resolution worse than the pre-cache behavior.

# Column list in the EXACT order _normalize_card_payload emits its keys.
# _cached_row_to_payload rebuilds the dict in this order so a cache-path
# value is indistinguishable from an API-path value. v3.30.11 added
# produced_tokens as the 22nd column; v3.36.1 appended loyalty + defense
# as the 23rd + 24th — all at the end, preserving byte-identical ordering
# of the existing keys.
_CACHE_COLUMNS = (
    "scryfall_id, name, set_code, set_name, collector_number, rarity, "
    "image_url, type_line, oracle_text, price_usd, price_usd_foil, "
    "price_usd_etched, colors, color_identity, mana_cost, cmc, legalities, "
    "full_art, frame_effects, set_type, layout, produced_tokens, "
    "loyalty, defense"
)


def _cached_row_to_payload(m) -> dict[str, Any]:
    """Reconstruct a normalized payload from a `scryfall_cards` row.

    Byte-identical to _normalize_card_payload's return value:
    - same key order (built explicitly below);
    - TEXT columns pass through verbatim, so NULL→None and ""→"" are
      preserved (e.g. a colorless card has colors=None but
      color_identity="" exactly as the normalizer produced them);
    - `legalities` / `frame_effects` are returned as the stored JSON text,
      not parsed — identical to the API path, which also returns
      json.dumps(...) strings for the caller to parse if needed;
    - `cmc` round-trips as REAL (float|None);
    - `full_art` is cast INTEGER 0/1 → Python bool to match
      bool(raw.get("full_art")) on the API path (bool(None) is False,
      consistent with a missing field).
    """
    return {
        "scryfall_id": m["scryfall_id"],
        "name": m["name"],
        "set_code": m["set_code"],
        "set_name": m["set_name"],
        "collector_number": m["collector_number"],
        "rarity": m["rarity"],
        "image_url": m["image_url"],
        "type_line": m["type_line"],
        "oracle_text": m["oracle_text"],
        "price_usd": m["price_usd"],
        "price_usd_foil": m["price_usd_foil"],
        "price_usd_etched": m["price_usd_etched"],
        "colors": m["colors"],
        "color_identity": m["color_identity"],
        "mana_cost": m["mana_cost"],
        "cmc": m["cmc"],
        "legalities": m["legalities"],
        "full_art": bool(m["full_art"]),
        "frame_effects": m["frame_effects"],
        "set_type": m["set_type"],
        "layout": m["layout"],
        # v3.30.11 — 22nd field. Stored as JSON text on the v3.25.0
        # daemon-write path; passed through verbatim here (consumers
        # parse on demand). NULL means "this row predates the v3.30.11
        # backfill" — distinguishable from "[]" which means "this card
        # has no tokens". Either value passes through cleanly.
        "produced_tokens": m["produced_tokens"],
        # v3.36.1 — 23rd + 24th fields. Faithful raw strings (planeswalker
        # loyalty / Battle defense), passed through verbatim; NULL when the
        # card carries neither. Appended LAST, lockstep with _CACHE_COLUMNS
        # and _normalize_card_payload.
        "loyalty": m["loyalty"],
        "defense": m["defense"],
    }


def _cache_get_by_ids(ids: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve scryfall_ids from the local cache. Missing/empty → {}."""
    if not ids:
        return {}
    try:
        stmt = text(
            f"SELECT {_CACHE_COLUMNS} FROM scryfall_cards WHERE scryfall_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        with engine.connect() as conn:
            rows = conn.execute(stmt, {"ids": ids}).mappings().all()
        return {m["scryfall_id"]: _cached_row_to_payload(m) for m in rows if m["scryfall_id"]}
    except Exception as exc:  # noqa: BLE001 — degrade to network (today's behavior)
        print(f"[scryfall-cache] id lookup failed, falling through to API: {exc}", flush=True)
        return {}


def _cache_get_by_set_number(
    keys: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Resolve (set_lower, collector) pairs from the local cache.

    `keys` are already (lowercased set, stripped collector) tuples — the same
    key shape bulk_fetch_by_set_number builds. Scryfall `set` codes are
    lowercase by API contract and stored verbatim, so an equality match on
    set_code is correct and uses the (set_code, collector_number) index; the
    candidate rows are filtered back down to the exact requested pairs.
    Missing/empty → {}.
    """
    if not keys:
        return {}
    try:
        sets = sorted({s for s, _ in keys})
        cols = sorted({c for _, c in keys})
        stmt = text(
            f"SELECT {_CACHE_COLUMNS} FROM scryfall_cards "
            "WHERE set_code IN :sets AND collector_number IN :cols"
        ).bindparams(bindparam("sets", expanding=True), bindparam("cols", expanding=True))
        with engine.connect() as conn:
            rows = conn.execute(stmt, {"sets": sets, "cols": cols}).mappings().all()
        want = set(keys)
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for m in rows:
            key = ((m["set_code"] or "").lower(), m["collector_number"] or "")
            if key in want and key not in out:
                out[key] = _cached_row_to_payload(m)
        return out
    except Exception as exc:  # noqa: BLE001 — degrade to network (today's behavior)
        print(
            f"[scryfall-cache] set/number lookup failed, falling through to API: {exc}",
            flush=True,
        )
        return {}


def bulk_refresh_prices(scryfall_ids: list[str]) -> BulkFetchResult:
    """Fetch fresh card payloads for many ids via the /cards/collection batch endpoint.

    Returns a :class:`BulkFetchResult`; ``.cards`` is keyed by scryfall_id.
    Local-first: the cache resolves what it can; only the miss subset makes
    the (unchanged) batched POST. Makes ceil(M/75) requests where M is the
    number of cache misses. (Name is historical — it fetches payloads; it
    does not write the DB.)
    """
    results: dict[str, dict[str, Any]] = {}
    not_found: list[str] = []
    failed: list[str] = []
    ids = [sid for sid in scryfall_ids if sid]

    results.update(_cache_get_by_ids(ids))
    miss_ids = [sid for sid in ids if sid not in results]

    for i in range(0, len(miss_ids), _COLLECTION_BATCH_SIZE):
        bn = i // _COLLECTION_BATCH_SIZE
        batch = miss_ids[i : i + _COLLECTION_BATCH_SIZE]
        payload = {"identifiers": [{"id": sid} for sid in batch]}
        data = _post_json(f"{SCRYFALL_CARD_URL}/collection", payload)
        if not data:
            # (a) follow-on: _post_json already logged the cause; this whole
            # batch is unresolved-but-retryable for this call.
            failed.extend(batch)
            print(
                f"[scryfall-bulk] bulk_refresh_prices batch {bn} dropped "
                f"({len(batch)} ids unresolved this call)",
                flush=True,
            )
            continue
        found = data.get("data", [])
        batch_not_found = [
            nf.get("id", "") for nf in data.get("not_found", []) if isinstance(nf, dict)
        ]
        not_found.extend(sid for sid in batch_not_found if sid)
        if not found:
            # (b) successful response, zero cards matched.
            print(
                f"[scryfall-bulk] bulk_refresh_prices batch {bn} empty data "
                f"({len(batch)} ids, {len(batch_not_found)} not_found)",
                flush=True,
            )
        elif batch_not_found:
            # (c) partial: some identifiers genuinely unknown to Scryfall.
            print(
                f"[scryfall-bulk] bulk_refresh_prices batch {bn}: "
                f"{len(found)} resolved, {len(batch_not_found)} not_found",
                flush=True,
            )
        for card in found:
            normalized = _normalize_card_payload(card)
            if normalized.get("scryfall_id"):
                results[normalized["scryfall_id"]] = normalized

    return BulkFetchResult(cards=results, not_found=not_found, failed=failed)


def fetch_payloads_uncached(scryfall_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch normalized card payloads straight from Scryfall's /cards/collection
    batch endpoint, BYPASSING the local ``scryfall_cards`` cache.

    The cache-first ``bulk_refresh_prices`` is correct for the request path and
    most backfills, but it returns the cached row even when a field has not yet
    been re-streamed into the cache — e.g. a freshly added seam column (v3.36.1
    ``loyalty`` / ``defense``) whose daily bulk backfill has not run, so the
    cache still holds NULL. An off-request daemon that needs authoritative
    values for such a column reads the live source directly via this helper.

    **Request-path invariant**: this makes Scryfall calls, so it MUST NOT be
    reachable from an HTTP request handler — daemon/one-shot use only. Batched
    (``ceil(N/75)`` POSTs), bounded by the caller. Returns
    ``{scryfall_id: normalized_payload}`` for whatever Scryfall resolved;
    unresolved ids are simply absent.
    """
    results: dict[str, dict[str, Any]] = {}
    ids = [sid for sid in scryfall_ids if sid]
    for i in range(0, len(ids), _COLLECTION_BATCH_SIZE):
        batch = ids[i : i + _COLLECTION_BATCH_SIZE]
        data = _post_json(
            f"{SCRYFALL_CARD_URL}/collection",
            {"identifiers": [{"id": sid} for sid in batch]},
        )
        for card in (data or {}).get("data", []):
            normalized = _normalize_card_payload(card)
            if normalized.get("scryfall_id"):
                results[normalized["scryfall_id"]] = normalized
    return results


def bulk_fetch_by_set_number(
    pairs: list[tuple[str, str]],
) -> BulkFetchResult:
    """Batch-fetch cards by (set_code, collector_number) via /cards/collection.

    Returns a :class:`BulkFetchResult`; ``.cards`` is keyed by
    (set_code_lower, collector_number), and ``.not_found`` / ``.failed``
    hold the same tuple key shape.
    Makes ceil(N/75) requests instead of N individual requests.
    """
    results: dict[tuple[str, str], dict[str, Any]] = {}
    not_found: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    seen: dict[tuple[str, str], None] = {}
    for s, c in pairs:
        key = ((s or "").strip().lower(), (c or "").strip())
        if key[0] and key[1]:
            seen[key] = None
    unique = list(seen.keys())

    results.update(_cache_get_by_set_number(unique))
    miss = [k for k in unique if k not in results]

    for i in range(0, len(miss), _COLLECTION_BATCH_SIZE):
        bn = i // _COLLECTION_BATCH_SIZE
        batch = miss[i : i + _COLLECTION_BATCH_SIZE]
        payload = {"identifiers": [{"set": s, "collector_number": c} for s, c in batch]}
        data = _post_json(f"{SCRYFALL_CARD_URL}/collection", payload)
        if not data:
            # (a) follow-on: _post_json already logged the cause;
            # unresolved-but-retryable for this call.
            failed.extend(batch)
            print(
                f"[scryfall-bulk] bulk_fetch_by_set_number batch {bn} dropped "
                f"({len(batch)} pairs unresolved this call)",
                flush=True,
            )
            continue
        found = data.get("data", [])
        batch_not_found = [
            ((nf.get("set") or "").lower(), nf.get("collector_number") or "")
            for nf in data.get("not_found", [])
            if isinstance(nf, dict)
        ]
        not_found.extend(k for k in batch_not_found if k[0] and k[1])
        if not found:
            # (b) successful response, zero cards matched.
            print(
                f"[scryfall-bulk] bulk_fetch_by_set_number batch {bn} empty data "
                f"({len(batch)} pairs, {len(batch_not_found)} not_found)",
                flush=True,
            )
        elif batch_not_found:
            # (c) partial: some pairs genuinely unknown to Scryfall.
            print(
                f"[scryfall-bulk] bulk_fetch_by_set_number batch {bn}: "
                f"{len(found)} resolved, {len(batch_not_found)} not_found",
                flush=True,
            )
        for card in found:
            normalized = _normalize_card_payload(card)
            if normalized.get("scryfall_id"):
                key = (
                    (normalized.get("set_code") or "").lower(),
                    normalized.get("collector_number") or "",
                )
                results[key] = normalized

    return BulkFetchResult(cards=results, not_found=not_found, failed=failed)


def bulk_fetch_by_name(
    names: list[tuple[str, str]],
) -> BulkFetchResult:
    """Batch-resolve card *names* (optionally hinted with a set) via the
    /cards/collection ``{"name": ...}`` identifier.

    This is the batched, request-path-safe analogue of the single-card
    "Import by name" flow: it makes ceil(N/75) POSTs — never one-per-name —
    so a paste list of bare names resolves without the per-row live-lookup
    pattern that caused the v3.23.x import outages. Scryfall's name
    identifier does exact (case-insensitive) matching and returns its
    preferred printing for the name (or the named printing when a ``set``
    hint is supplied), so a typo'd name lands in ``.not_found`` rather than
    silently resolving to a wrong card.

    ``names`` is a list of ``(name, set_code)`` tuples; ``set_code`` may be
    "" for a truly bare name. The key shape (for ``.cards`` / ``.not_found``
    / ``.failed``) is ``(name_lower, set_lower)`` mirroring the request, so
    the caller looks up by exactly what it asked for.

    Unlike the id / set+number resolvers this path is **API-only — it does
    NOT consult the local ``scryfall_cards`` cache.** The cache has no
    ``released_at`` column, so it cannot replicate Scryfall's "preferred
    printing" selection for a bare name deterministically; letting cache
    state decide which printing a name resolves to would make the result
    depend on what the daemon happened to have mirrored. Bare-name lines are
    the minority and are fully batched, so always querying Scryfall is both
    correct and deterministic.
    """
    results: dict[tuple[str, str], dict[str, Any]] = {}
    not_found: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    seen: dict[tuple[str, str], None] = {}
    for n, s in names:
        key = ((n or "").strip().lower(), (s or "").strip().lower())
        if key[0]:
            seen[key] = None
    unique = list(seen.keys())

    for i in range(0, len(unique), _COLLECTION_BATCH_SIZE):
        bn = i // _COLLECTION_BATCH_SIZE
        batch = unique[i : i + _COLLECTION_BATCH_SIZE]
        identifiers: list[dict[str, str]] = []
        for name_l, set_l in batch:
            ident = {"name": name_l}
            if set_l:
                ident["set"] = set_l
            identifiers.append(ident)
        data = _post_json(f"{SCRYFALL_CARD_URL}/collection", {"identifiers": identifiers})
        if not data:
            # (a) follow-on: _post_json already logged the cause; the whole
            # chunk is transiently unresolved for this call.
            failed.extend(batch)
            print(
                f"[scryfall-bulk] bulk_fetch_by_name batch {bn} dropped "
                f"({len(batch)} names unresolved this call)",
                flush=True,
            )
            continue
        found = [_normalize_card_payload(c) for c in data.get("data", []) if isinstance(c, dict)]
        # Scryfall does NOT echo which identifier produced each returned
        # card, so match each requested (name, set) back to a card
        # explicitly. A double-faced card resolves on either its full
        # "A // B" name or its front-face name; the optional set hint must
        # also match when supplied. O(batch^2) over <=75 items.
        resolved_this_batch = 0
        for key in batch:
            name_l, set_l = key
            match: dict[str, Any] | None = None
            for card in found:
                if not card.get("scryfall_id"):
                    continue
                cname = (card.get("name") or "").lower()
                cfront = cname.split(" // ")[0]
                if name_l not in (cname, cfront):
                    continue
                if set_l and (card.get("set_code") or "").lower() != set_l:
                    continue
                match = card
                break
            if match is not None:
                results[key] = match
                resolved_this_batch += 1
            else:
                # Batch POST succeeded but this name had no match — genuinely
                # unknown to Scryfall (or the set hint excluded it). Permanent.
                not_found.append(key)
        if resolved_this_batch < len(batch):
            print(
                f"[scryfall-bulk] bulk_fetch_by_name batch {bn}: "
                f"{resolved_this_batch} resolved, "
                f"{len(batch) - resolved_this_batch} not_found",
                flush=True,
            )

    return BulkFetchResult(cards=results, not_found=not_found, failed=failed)


# ---------------------------------------------------------------------------
# Local cache population daemon (v3.25.0).
#
# Off the request path entirely: a background thread that mirrors Scryfall's
# `default-cards` bulk export into the local scryfall_cards table so the
# request-path resolvers above become local-first SQLite reads. Modeled on
# main._trait_backfill_loop: initial sleep, while True, per-batch commit,
# never dies silently. No VACUUM anywhere (the 5 Gi PVC cannot absorb a 2x
# transient rewrite).
# ---------------------------------------------------------------------------

SCRYFALL_BULK_URL = "https://api.scryfall.com/bulk-data"
_BULK_META_KEY = "default_cards_updated_at"
# 1000 normalized dicts × ~2 KB ≈ ~2 MB transient — well under the pod
# memory limit, and a transaction-size sweet spot for SQLite write
# throughput that does not hold the write lock long enough to matter (the
# v3.23.9 lock-hold lesson).
_BULK_UPSERT_BATCH = 1000
_BULK_INITIAL_SLEEP_SECONDS = 60  # let the app come up before the first poll
_BULK_POLL_INTERVAL_SECONDS = 24 * 60 * 60  # Scryfall rebuilds ~daily

# Built once from _CACHE_COLUMNS so the upsert can never drift from the
# normalizer/seam contract. scryfall_id is the conflict target, never updated.
_BULK_COLS = [c.strip() for c in _CACHE_COLUMNS.split(",")]
_BULK_UPSERT_SQL = text(
    f"INSERT INTO scryfall_cards ({_CACHE_COLUMNS}) "
    f"VALUES ({', '.join(':' + c for c in _BULK_COLS)}) "
    "ON CONFLICT(scryfall_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _BULK_COLS if c != "scryfall_id")
)
_BULK_META_UPSERT_SQL = text(
    "INSERT INTO scryfall_bulk_meta (key, value) VALUES (:key, :value) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
)


def _bulk_meta_get(key: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM scryfall_bulk_meta WHERE key = :k"), {"k": key}
        ).fetchone()
    return row[0] if row else None


def _flush_bulk_batch(batch: list[dict[str, Any]]) -> None:
    """Upsert one batch and commit it. ``engine.begin()`` commits on context
    exit, so a crash mid-population leaves every prior batch durably written
    and the next pass simply no-ops over them (partial-write recovery).
    """
    if not batch:
        return
    with engine.begin() as conn:
        conn.execute(_BULK_UPSERT_SQL, batch)


def refresh_bulk_cache() -> int:
    """One freshness-guarded population pass over the default-cards export.

    Returns the number of cards upserted: 0 when the cache is already current
    (freshness guard skipped the download) or the bulk listing could not be
    read. Raises on download/parse failure so the caller logs it and the meta
    row stays at the OLD updated_at — the next pass then re-attempts the same
    export from scratch (upsert makes the re-run idempotent).
    """
    # download_uri changes every rebuild — it MUST come from the live
    # /bulk-data listing, never a constructed/hardcoded URL.
    listing = _get_json(SCRYFALL_BULK_URL)
    if not listing:
        print("[bulk-data] GET /bulk-data failed; will retry next cycle", flush=True)
        return 0
    entry = next(
        (d for d in listing.get("data", []) if d.get("type") == "default_cards"),
        None,
    )
    if not entry:
        print("[bulk-data] no default_cards entry in /bulk-data response", flush=True)
        return 0
    updated_at = entry.get("updated_at")
    download_uri = entry.get("download_uri")
    if not updated_at or not download_uri:
        print("[bulk-data] default_cards entry missing updated_at/download_uri", flush=True)
        return 0

    if _bulk_meta_get(_BULK_META_KEY) == updated_at:
        print(
            f"[bulk-data] cache current (updated_at={updated_at}); skipping download",
            flush=True,
        )
        return 0

    print(f"[bulk-data] new export {updated_at}; streaming {download_uri}", flush=True)
    _throttle()
    resp = _session.get(download_uri, headers=HEADERS, stream=True, timeout=(30, 600))
    resp.raise_for_status()
    resp.raw.decode_content = True  # transparently inflate gzip/deflate

    total = 0
    batch: list[dict[str, Any]] = []
    try:
        # Stream one card object at a time — the export is a single ~2 GB
        # JSON array; it is NEVER json.load()ed. use_float=True yields float
        # (not Decimal) so cmc round-trips byte-identically with the API path.
        for card in ijson.items(resp.raw, "item", use_float=True):
            normalized = _normalize_card_payload(card)
            if not normalized.get("scryfall_id"):
                continue  # PK cannot be NULL — skip malformed/idless entries
            batch.append(normalized)
            if len(batch) >= _BULK_UPSERT_BATCH:
                _flush_bulk_batch(batch)
                total += len(batch)
                batch = []
                if shutdown_event.is_set():
                    # Abort mid-stream on shutdown. Flushed batches are durable;
                    # the meta row is NOT advanced below, so the next start
                    # re-downloads the same export from scratch (idempotent).
                    print(
                        f"[bulk-data] shutdown during refresh; stopping after {total} cards",
                        flush=True,
                    )
                    return total
        _flush_bulk_batch(batch)
        total += len(batch)
    finally:
        resp.close()

    # Meta is written ONLY after a fully successful population. If anything
    # above raised we never reach here: the meta row keeps its old value and
    # the next pass re-downloads the same export from scratch.
    with engine.begin() as conn:
        conn.execute(_BULK_META_UPSERT_SQL, {"key": _BULK_META_KEY, "value": updated_at})
    print(f"[bulk-data] populated {total} cards; meta -> {updated_at}", flush=True)
    return total


def _bulk_data_loop() -> None:
    if shutdown_event.wait(_BULK_INITIAL_SLEEP_SECONDS):  # bail if stopping
        return
    while not shutdown_event.is_set():
        try:
            refresh_bulk_cache()
        except Exception as exc:  # noqa: BLE001 — daemon must never die silently
            print(
                f"[bulk-data] refresh error (will retry next cycle): {exc}",
                flush=True,
            )
        shutdown_event.wait(_BULK_POLL_INTERVAL_SECONDS)


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
    # Price now comes from the MTGJSON ingest (app.jobs.price_ingest), NOT
    # Scryfall — the price columns are deliberately left untouched here so a
    # metadata refresh can't clobber the MTGJSON-resolved value.
    card.colors = fresh["colors"]
    card.color_identity = fresh["color_identity"]
    card.mana_cost = fresh["mana_cost"]
    card.cmc = fresh["cmc"]
    card.legalities = fresh["legalities"]
    card.full_art = fresh["full_art"]
    card.frame_effects = fresh["frame_effects"]
    card.set_type = fresh["set_type"]
    card.layout = fresh["layout"]
    card.updated_at = utc_now()
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

    frame_effects = raw.get("frame_effects") or []
    frame_effects_lc = {(eff or "").lower() for eff in frame_effects}

    set_type = (raw.get("set_type") or "").lower()
    layout = (raw.get("layout") or "").lower()
    is_token_substitute = set_type == "token" and layout == "normal" and "token" not in type_line

    return {
        "is_basic_land": "basic land" in type_line,
        "is_full_art": bool(raw.get("full_art")),
        "is_snow": "snow" in type_line,
        "has_showcase_frame": "showcase" in frame_effects_lc,
        "has_extended_art_frame": "extendedart" in frame_effects_lc,
        "is_token": "token" in type_line,
        "is_token_substitute": is_token_substitute,
        "is_token_set": set_type == "token",
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
    formatted.sort(key=lambda t: 0 if t["is_double_sided"] else 1)
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
    """Paginate Scryfall's ``/cards/search?q=e:{set_code}`` endpoint.

    Live-network primitive. Returns the normalized 21-key payload shape
    per ``_normalize_card_payload``. **NOT FOR REQUEST-PATH USE** —
    pagination is rate-limited (~3 GETs per typical set, throttled),
    so a single call takes seconds and a request handler that loops
    over multiple sets will easily blow the Cloudflare 100s ceiling.

    See ``fetch_set_cards_from_cache`` for the v3.27.13 request-path
    replacement reading the v3.25.0 ``scryfall_cards`` bulk cache. This
    primitive is preserved for legitimate non-request-path use:
    background daemons that need fresh Scryfall data (e.g. validating
    the bulk cache against the live API, or fetching a set the bulk
    export hasn't propagated yet).
    """
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


def _collector_natural_sort_key(card: dict[str, Any]) -> tuple:
    """Return a natural-sort key for a card's ``collector_number``.

    Scryfall's ``order=set`` ordering is roughly: parse the leading
    integer prefix; rows with a numeric prefix sort numerically before
    rows that start with non-digit characters; the alphabetic suffix
    (e.g. ``"1a"``, ``"1b"``) breaks ties within a numeric prefix; rows
    starting with a letter (e.g. ``"T1"``, ``"★1"``) come last sorted
    by the literal string. The cache table has no natural ordering, so
    we sort in Python after the SELECT.

    Returns ``(prefix_sentinel, prefix_int, suffix)`` so Python's default
    tuple comparison gives the right order without any sort-direction
    flags. ``prefix_sentinel`` is 0 when there's a numeric prefix (sort
    these first) and 1 when there isn't (sort these after).
    """
    raw = (card.get("collector_number") or "").strip()
    i = 0
    while i < len(raw) and raw[i].isdigit():
        i += 1
    if i == 0:
        return (1, 0, raw)
    return (0, int(raw[:i]), raw[i:])


def fetch_set_cards_from_cache(set_code: str) -> list[dict[str, Any]]:
    """Read a set's cards from the v3.25.0 ``scryfall_cards`` bulk cache.

    Replaces the request-path ``fetch_set_cards`` Scryfall pagination —
    the request-path network invariant violation flagged in the v3.27.13
    diagnostic (18-27s per ``/sets/{set_code}`` visit, ALL set detail
    page loads). Local SQLite query; no network call on the request
    path. The bulk cache is kept current by the ``_bulk_data_loop``
    daemon (v3.25.0) and mirrors all ~114k Scryfall cards locally.

    Returns the same byte-identical 21-key normalized payload shape that
    ``fetch_set_cards`` returns (via the shared ``_cached_row_to_payload``
    helper), so consumers don't care which path produced their list.

    Sort: natural collector-number order matching Scryfall's
    ``order=set`` (numeric prefixes first numerically; alphabetic
    suffixes break ties; non-numeric prefixes sort last). See
    ``_collector_natural_sort_key`` for the parse.

    Cache miss (set not in ``scryfall_cards`` — typical for very new
    sets the bulk export hasn't propagated yet, or for malformed set
    codes) returns ``[]``. No fallback to a live Scryfall fetch — the
    request-path-network-invariant contract requires that misses fail
    visibly to the user rather than degrade into per-row live fetches.
    Same shape as the v3.23.x import-path lesson.

    Any cache read error is logged and degrades to ``[]`` — a database
    problem can never make this path slower than network fallback would
    have been. (Same defensive pattern as ``_cache_get_by_ids`` /
    ``_cache_get_by_set_number``.)
    """
    set_code = (set_code or "").strip().lower()
    if not set_code:
        return []
    try:
        stmt = text(f"SELECT {_CACHE_COLUMNS} FROM scryfall_cards WHERE set_code = :set_code")
        with engine.connect() as conn:
            rows = conn.execute(stmt, {"set_code": set_code}).mappings().all()
    except Exception as exc:  # noqa: BLE001 — degrade to empty (request-path safe)
        print(
            f"[scryfall-cache] set lookup failed for {set_code}, returning []: {exc}",
            flush=True,
        )
        return []
    payloads = [_cached_row_to_payload(m) for m in rows]
    payloads.sort(key=_collector_natural_sort_key)
    return payloads


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


@lru_cache(maxsize=2048)
def fetch_card_printings(name: str) -> list[dict[str, Any]]:
    """Every printing of an exact card name, with per-printing finish data.

    Powers the deck "Switch printing" modal: the user picks a different
    printing of the same card name to swap into a deck row in place.
    Scryfall's `unique=prints` mode plus an exact-name match (`!"Name"`)
    returns one entry per (set, collector, frame variation). Ordered by
    release date desc so the newest reprint is at the top.

    Each entry carries `finishes` — the actual finish list Scryfall reports
    for that printing (e.g. `["nonfoil", "foil"]` or `["foil", "etched"]`).
    The modal uses this to gate the foil/etched toggle buttons so users
    can't pick a finish that doesn't exist for the chosen printing.

    Cached aggressively (LRU 2048) since printing lists rarely change.
    Returns [] when the name resolves to no Scryfall cards.

    No DB writes; pure Scryfall passthrough. Pagination would only matter
    for the most-reprinted cards (Sol Ring ~80 prints, Forest ~1000) —
    follow the `next_page` cursor when present so we return everything.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return []

    # Exact-name match: !"Name" produces all printings of that one card.
    # Quoting the name guards against names with special tokens (e.g.
    # "Lim-Dûl's Vault", "Borborygmos").
    quoted = cleaned.replace('"', '\\"')
    url = (
        "https://api.scryfall.com/cards/search"
        f'?q=!"{requests.utils.quote(quoted)}"&unique=prints'
        "&order=released&dir=desc"
    )
    out: list[dict[str, Any]] = []
    while url:
        data = _get_json(url)
        if not data:
            break
        for card in data.get("data", []):
            image_small = None
            image_normal = None
            image_uris = card.get("image_uris") or {}
            if image_uris:
                image_small = image_uris.get("small")
                image_normal = image_uris.get("normal") or image_uris.get("large")
            else:
                faces = card.get("card_faces") or []
                if faces and isinstance(faces[0], dict):
                    front_uris = faces[0].get("image_uris") or {}
                    image_small = front_uris.get("small")
                    image_normal = front_uris.get("normal") or front_uris.get("large")
            out.append(
                {
                    "scryfall_id": card.get("id"),
                    "name": card.get("name"),
                    "set_code": (card.get("set") or "").lower(),
                    "set_name": card.get("set_name"),
                    "collector_number": card.get("collector_number"),
                    "released_at": card.get("released_at"),
                    "finishes": card.get("finishes") or ["nonfoil"],
                    "frame_effects": card.get("frame_effects") or [],
                    "promo_types": card.get("promo_types") or [],
                    "image_uri_small": image_small,
                    "image_uri_normal": image_normal,
                }
            )
        url = data.get("next_page") if data.get("has_more") else None
    return out


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
