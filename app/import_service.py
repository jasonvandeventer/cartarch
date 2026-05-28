"""CSV/manual import parsing and persistence logic.

Important placement rule:
Imports do not assign drawer/slot positions directly. Imported rows are created as
pending with ``drawer=None`` and ``slot=None`` so placement can be calculated by
``resort_collection`` against the full collection. This avoids slot collisions
with existing rows already assigned in the drawers.
"""

from __future__ import annotations

import csv
import io
import re
import time
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit_service import create_import_batch, log_transaction
from app.deck_service import create_deck
from app.location_service import VALID_LOCATION_TYPES, create_location
from app.models import Card, Deck, InventoryRow, StorageLocation
from app.scryfall import (
    BulkFetchResult,
    bulk_fetch_by_set_number,
    bulk_refresh_prices,
    fetch_card_by_set_and_number,
)

# Matches the trailing (SET) or [SET] and optional collector number on a list line.
# SET must be 2–6 alphanumeric chars to distinguish from long parenthetical phrases.
_SET_SUFFIX_RE = re.compile(
    r"\s+[\(\[]([A-Za-z0-9]{2,6})[\)\]]"  # (SET) or [SET]
    r"(?:\s+(\S+))?"  # optional collector number
    r"\s*$"
)

HEADER_ALIASES = {
    # Internal / scanner-app format
    "scryfallid": "scryfall_id",
    "scryfall_id": "scryfall_id",
    "setcode": "set_code",
    "set_code": "set_code",
    "set": "set_code",
    "collectornumber": "collector_number",
    "collector_number": "collector_number",
    "collector#": "collector_number",
    "finish": "finish",
    "quantity": "quantity",
    "qty": "quantity",
    "count": "quantity",
    "location": "location",
    "name": "name",
    "type": "type",
    "language": "language",
    "lang": "language",
    # v3.30.16 — round-trip fidelity. /collection/export now emits these
    # five additional columns; the importer recognizes them so a round-trip
    # preserves language, commander attribution, tags, proxy flag, and the
    # type of auto-created locations.
    "locationtype": "location_type",
    "location_type": "location_type",
    "role": "role",
    "tags": "tags",
    "isproxy": "is_proxy",
    "is_proxy": "is_proxy",
    # Helvault: finish is in a column called "extras"
    "extras": "finish",
    # Moxfield: set code is in "Edition", foil status is in "Foil"
    "edition": "set_code",
    "foil": "finish",
}

# v3.30.16 — service-layer enum mirroring the model contract.
# `InventoryRow.role` is String(32) nullable; only "commander" is in
# production use today (v3.5 architecture — Currently only value used).
# Empty / NULL is the unset state. Any non-empty non-commander value
# routes the row to invalid_rows with an explicit reason at parse time.
VALID_ROLE_VALUES = frozenset({"commander"})


# Scryfall canonical 2-3 char language codes. Anything outside this set is
# coerced to "en" rather than persisted as garbage. Helvault/Moxfield exports
# may write "ja" or "Japanese" — `normalize_language` handles both.
_SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {
        "en",
        "es",
        "fr",
        "de",
        "it",
        "pt",
        "ja",
        "ko",
        "ru",
        "zhs",
        "zht",
        "he",
        "la",
        "grc",
        "ar",
        "ph",
        "sa",
        "px",
        "qya",
    }
)

_LANGUAGE_NAME_TO_CODE: dict[str, str] = {
    # Long-name forms (Helvault / Moxfield CSVs occasionally use these)
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "portuguese": "pt",
    "japanese": "ja",
    "korean": "ko",
    "russian": "ru",
    "chinese": "zhs",
    "simplified chinese": "zhs",
    "traditional chinese": "zht",
    "chinese (simplified)": "zhs",
    "chinese (traditional)": "zht",
    "hebrew": "he",
    "latin": "la",
    "ancient greek": "grc",
    "arabic": "ar",
    "phyrexian": "ph",
    "sanskrit": "sa",
    # Country-code aliases — users typing the paste-list `*XX*` marker often
    # reach for the country code rather than the Scryfall language code,
    # so jp/cn/tw/kr need to map to ja/zhs/zht/ko respectively.
    "jp": "ja",
    "cn": "zhs",
    "zh": "zhs",
    "tw": "zht",
    "kr": "ko",
}


def normalize_language(value: str | None) -> str:
    """Coerce a language string to a Scryfall code.

    Accepts the code form ("ja"), the long name ("Japanese", case-insensitive),
    or empty/unknown → "en". Anything outside the recognized set falls back
    to "en" so import never persists garbage values.
    """
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return "en"
    if cleaned in _SUPPORTED_LANGUAGES:
        return cleaned
    mapped = _LANGUAGE_NAME_TO_CODE.get(cleaned)
    if mapped:
        return mapped
    return "en"


def coerce_language_code_strict(value: str | None) -> str | None:
    """Coerce a language string to a Scryfall code, returning None on unknown.

    Same alias logic as ``normalize_language`` but does NOT fall back to "en"
    for unrecognized input — search callers want a strict result so they can
    treat unknown input as "match nothing" rather than silently returning
    English rows. Empty/None input also returns None.
    """
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return None
    if cleaned in _SUPPORTED_LANGUAGES:
        return cleaned
    return _LANGUAGE_NAME_TO_CODE.get(cleaned)


def detect_csv_format(headers: list[str]) -> str:
    """Return a human-readable format name based on raw CSV header names."""
    lower = {(h or "").strip().lower() for h in headers}
    if "extras" in lower:
        return "Helvault"
    if "edition" in lower:
        return "Moxfield"
    return "Scanner App"


def normalize_finish(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    if cleaned in {"foil", "traditional foil"}:
        return "foil"
    if cleaned in {"etched", "foil etched", "etched foil"}:
        return "etched"
    return "normal"


def parse_role(value: str | None) -> tuple[str | None, bool]:
    """v3.30.16 — parse a CSV Role column value.

    Returns ``(normalized_or_none, is_valid)``:
      * empty / whitespace → ``(None, True)`` — row carries no role.
      * "commander" (case-insensitive) → ``("commander", True)``.
      * any other non-empty value → ``(None, False)`` — caller routes the
        row to invalid_rows with an explicit reason.

    Mirrors the model contract: ``InventoryRow.role`` is String(32) nullable;
    only ``"commander"`` is in production use today. Service-layer-enum
    pattern (matches ``VALID_LOCATION_TYPES`` / ``CANONICAL_GAME_FORMATS``).
    No silent coercion (spec Decision 9).
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return (None, True)
    lowered = cleaned.lower()
    if lowered in VALID_ROLE_VALUES:
        return (lowered, True)
    return (None, False)


def parse_proxy_bool(value: str | None) -> tuple[bool, bool]:
    """v3.30.16 — parse a CSV Is Proxy column value.

    Returns ``(parsed_bool, is_valid)``:
      * empty / whitespace → ``(False, True)`` — column absent or blank
        means "not a proxy" (matches ``InventoryRow.is_proxy`` default).
      * "true" / "True" / "TRUE" → ``(True, True)``.
      * "false" / "False" / "FALSE" → ``(False, True)``.
      * any other value → ``(False, False)`` — caller routes the row to
        invalid_rows with an explicit reason. No silent coercion
        (spec Decision 11).
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return (False, True)
    lowered = cleaned.lower()
    if lowered == "true":
        return (True, True)
    if lowered == "false":
        return (False, True)
    return (False, False)


def normalize_header(value: str | None) -> str:
    cleaned = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    cleaned = cleaned.replace("__", "_")
    return HEADER_ALIASES.get(cleaned.replace("_", ""), HEADER_ALIASES.get(cleaned, cleaned))


def build_finish_warnings(card_data: dict | None, finish: str) -> list[str]:
    warnings: list[str] = []
    normalized_finish = (finish or "normal").strip().lower()

    if not card_data:
        return warnings

    normal_price = card_data.get("price_usd")
    foil_price = card_data.get("price_usd_foil")
    etched_price = card_data.get("price_usd_etched")

    if normalized_finish == "foil":
        if not foil_price and normal_price:
            warnings.append(
                "Selected finish is Foil, but foil pricing is missing while normal pricing exists. Check the scanned finish."
            )
    elif normalized_finish == "etched":
        if not etched_price and (foil_price or normal_price):
            warnings.append(
                "Selected finish is Etched, but etched pricing is missing. Check the scanned finish."
            )
    else:
        if not normal_price and (foil_price or etched_price):
            warnings.append(
                "Selected finish is Normal, but normal pricing is missing while foil/etched pricing exists. Check the scanned finish."
            )

    return warnings


def parse_scanner_csv(file_bytes: bytes) -> dict[str, Any]:
    # [import-preview] diagnostic instrumentation (no logic changes) — added to
    # localize the 1,176-row Helvault CSV /import/preview 524 timeout.
    _t_start = time.perf_counter()

    text = file_bytes.decode("utf-8-sig", errors="replace")
    stream = io.StringIO(text)
    reader = csv.DictReader(stream)

    format_name = detect_csv_format(reader.fieldnames or [])
    valid_rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []

    # --- Pass 1: parse rows without touching Scryfall ---
    _t_p1 = time.perf_counter()
    pre_rows: list[dict[str, Any]] = []
    for line_number, raw_row in enumerate(reader, start=2):
        row = {normalize_header(k): (v or "").strip() for k, v in raw_row.items()}
        scryfall_id = row.get("scryfall_id", "")
        set_code = row.get("set_code", "").lower()
        collector_number = row.get("collector_number", "")
        finish = normalize_finish(row.get("finish", ""))
        location = row.get("location", "")
        # v3.30.16 — Location Type rides alongside Location. Empty for old
        # 6-column CSVs (backward-compatible) or for rows in clean/ambiguous
        # resolutions (the existing location's type wins per Decision 12;
        # the CSV value is only consulted at auto-create time).
        location_type = row.get("location_type", "").lower()
        quantity_raw = row.get("quantity", "1")
        name = row.get("name", "")
        card_type = row.get("type", "")
        language = normalize_language(row.get("language", ""))
        # v3.30.16 — role / tags / is_proxy. Empty for old 6-column CSVs.
        # Validation is in Pass 3 (after the row's Scryfall identity has
        # been resolved) so failures route to invalid_rows alongside other
        # row-level rejection reasons.
        role_raw = row.get("role", "")
        tags_raw = row.get("tags", "")
        is_proxy_raw = row.get("is_proxy", "")
        pre_rows.append(
            {
                "line_number": line_number,
                "scryfall_id": scryfall_id,
                "set_code": set_code,
                "collector_number": collector_number,
                "finish": finish,
                "quantity": quantity_raw if quantity_raw else "1",  # validated below
                "location": location,
                "location_type": location_type,
                "name": name,
                "type": card_type,
                "language": language,
                "role_raw": role_raw,
                "tags_raw": tags_raw,
                "is_proxy_raw": is_proxy_raw,
            }
        )

    # Coerce quantity post-append so the int-conversion failure path here
    # doesn't drift the dict shape downstream.
    for r in pre_rows:
        try:
            r["quantity"] = max(1, int(r["quantity"] or "1"))
        except ValueError:
            r["quantity"] = 1

    print(
        f"[import-preview] pass1 parse: {len(pre_rows)} rows "
        f"in {time.perf_counter() - _t_p1:.2f}s format={format_name}",
        flush=True,
    )

    # --- Pass 2: batch-fetch card data ---
    # Separate rows by identifier type; batch each group.
    id_rows = [r for r in pre_rows if r["scryfall_id"]]
    set_rows = [
        r for r in pre_rows if not r["scryfall_id"] and r["set_code"] and r["collector_number"]
    ]

    # Batch-fetch by scryfall_id
    id_map: dict[str, dict[str, Any]] = {}
    id_result = BulkFetchResult()
    if id_rows:
        unique_ids = list({r["scryfall_id"] for r in id_rows})
        _t_bulk = time.perf_counter()
        id_result = bulk_refresh_prices(unique_ids)
        id_map = id_result.cards
        print(
            f"[import-preview] pass2 bulk_refresh_prices: {len(unique_ids)} unique ids "
            f"-> {len(id_map)} resolved in {time.perf_counter() - _t_bulk:.2f}s "
            f"({len(unique_ids) - len(id_map)} unresolved -> per-row fallback in pass3)",
            flush=True,
        )

    # Batch-fetch by set+collector
    set_map: dict[tuple[str, str], dict[str, Any]] = {}
    set_result = BulkFetchResult()
    if set_rows:
        pairs = [(r["set_code"], r["collector_number"]) for r in set_rows]
        _t_setbulk = time.perf_counter()
        set_result = bulk_fetch_by_set_number(pairs)
        set_map = set_result.cards
        print(
            f"[import-preview] pass2 bulk_fetch_by_set_number: {len(pairs)} pairs "
            f"-> {len(set_map)} resolved in {time.perf_counter() - _t_setbulk:.2f}s",
            flush=True,
        )

    # --- Pass 3: build output rows (batch-only resolution; NO network) ---
    # Invariant: nothing in this loop may call Scryfall. Pass 2's batched
    # lookup is the single resolution point. A row that Pass 2 did not
    # resolve becomes an invalid_row the user sees — it must never trigger
    # a per-row live fetch (the v3.23.9 request-path-immunity principle;
    # the per-row fallback here was the 5,758-row /import/preview 524).
    _t_p3 = time.perf_counter()
    _id_failed = set(id_result.failed)
    _set_failed = set(set_result.failed)
    _unresolved_count = 0
    for r in pre_rows:
        # v3.30.16 — validate role + is_proxy at parse time. Failures route
        # to invalid_rows alongside Scryfall-resolution failures. The
        # `role` and `is_proxy` keys in the cleaned dict carry the
        # NORMALIZED values for downstream consumers; the raw inputs stay
        # in the dict only on the invalid-row branch for the reason string.
        role_normalized, role_valid = parse_role(r.get("role_raw"))
        is_proxy_value, is_proxy_valid = parse_proxy_bool(r.get("is_proxy_raw"))
        cleaned = {
            "line_number": r["line_number"],
            "scryfall_id": r["scryfall_id"],
            "set_code": r["set_code"],
            "collector_number": r["collector_number"],
            "finish": r["finish"],
            "quantity": r["quantity"],
            "location": r["location"],
            "location_type": r.get("location_type", ""),
            "name": r["name"],
            "type": r["type"],
            "language": r["language"],
            "role": role_normalized or "",
            "tags": r.get("tags_raw", ""),
            "is_proxy": is_proxy_value,
            "warnings": [],
        }

        card_data: dict[str, Any] | None = None
        if r["scryfall_id"]:
            card_data = id_map.get(r["scryfall_id"])
        elif r["set_code"] and r["collector_number"]:
            card_data = set_map.get((r["set_code"], r["collector_number"]))
            if card_data and not cleaned["scryfall_id"]:
                cleaned["scryfall_id"] = card_data.get("scryfall_id", "")

        has_identifier = bool(r["scryfall_id"]) or bool(r["set_code"] and r["collector_number"])

        # v3.30.16 — explicit per-field validation rejection. The row
        # surfaces in the existing § II Invalid Rows panel with a clear
        # reason so the user can fix the CSV and re-import. No silent
        # coercion (spec Decisions 9, 11).
        if not role_valid:
            cleaned["reason"] = (
                f"Invalid Role value: {r.get('role_raw')!r} " f"(allowed: empty or 'commander')."
            )
            invalid_rows.append(cleaned)
            continue
        if not is_proxy_valid:
            cleaned["reason"] = (
                f"Invalid Is Proxy value: {r.get('is_proxy_raw')!r} "
                f"(allowed: empty, 'true', or 'false')."
            )
            invalid_rows.append(cleaned)
            continue

        if card_data:
            cleaned["warnings"] = build_finish_warnings(card_data, r["finish"])
            cleaned["name"] = card_data.get("name") or cleaned["name"]
            cleaned["set_code"] = card_data.get("set_code") or cleaned["set_code"]
            cleaned["collector_number"] = (
                card_data.get("collector_number") or cleaned["collector_number"]
            )
            valid_rows.append(cleaned)
        elif not has_identifier:
            cleaned["reason"] = "Missing Scryfall ID and set/collector fallback fields."
            invalid_rows.append(cleaned)
        else:
            # Had an identifier but Pass 2's batch did not resolve it.
            # Distinguish transient (batch errored after retries — re-import
            # may fix) from permanent (Scryfall genuinely has no such card).
            _unresolved_count += 1
            if (
                r["scryfall_id"] in _id_failed
                or (
                    r["set_code"],
                    r["collector_number"],
                )
                in _set_failed
            ):
                cleaned["reason"] = (
                    "Scryfall lookup temporarily failed for this batch — " "re-import to retry."
                )
            else:
                cleaned["reason"] = "Card not found in Scryfall data."
            invalid_rows.append(cleaned)

    print(
        f"[import-preview] pass3 build: {len(pre_rows)} rows "
        f"in {time.perf_counter() - _t_p3:.2f}s "
        f"unresolved_to_invalid={_unresolved_count} "
        f"(zero in-loop network — batch-only resolution)",
        flush=True,
    )
    print(
        f"[import-preview] parse_scanner_csv TOTAL {time.perf_counter() - _t_start:.2f}s "
        f"valid={len(valid_rows)} invalid={len(invalid_rows)}",
        flush=True,
    )

    return {"valid_rows": valid_rows, "invalid_rows": invalid_rows, "format_name": format_name}


def persist_import_rows(
    session: Session,
    rows: list[dict[str, Any]],
    user_id: int,
    filename: str = "manual import",
    line_to_location_id: dict[int, int] | None = None,
) -> dict[str, Any]:
    """Persist imported rows into the current user's inventory.

    User ownership is required at the service boundary. Authentication can change
    later, but this function must never infer or default the owning user.

    v3.30.15 — per-row location resolution
    ------------------------------------------------------------------
    ``line_to_location_id`` maps a parsed row's ``line_number`` to the
    ``StorageLocation.id`` the user resolved its Location column to (via the
    preview-step picker / auto-create / clean single-match). Rows present in
    the map are created **directly as placed** (``storage_location_id`` set,
    ``is_pending=False``); they skip the pending-merge lookup entirely. The
    commit handler is expected to run ``place_imported_rows`` against the
    returned ``placed_row_ids_by_location`` so placed-row merge with any
    existing placed copy at the same location happens through the established
    code path — keeping the merge invariant in one place.

    Rows whose ``line_number`` is NOT in the map (or if ``line_to_location_id``
    is ``None``/empty) fall through to the unchanged pending-merge behavior.

    Trap closed in this release: pre-v3.30.15 the per-row Location string was
    written to ``InventoryRow.notes`` (a free-text annotation field) and the
    location semantic was silently lost. v3.30.15 stops writing notes on
    import for both paths (resolved-placed AND pending fallback). Existing
    rows with location-strings-in-notes from past imports are NOT touched.
    """
    if user_id <= 0:
        raise ValueError("user_id must be a positive integer when importing rows")

    line_to_location_id = line_to_location_id or {}

    imported_count = 0
    total_quantity = 0
    failed_rows: list[dict[str, Any]] = []
    imported_row_ids: list[int] = []
    batch = create_import_batch(
        session=session,
        user_id=user_id,
        filename=filename,
        row_count=len(rows),
    )
    now = datetime.utcnow()

    # Batch-resolve any rows that are missing a scryfall_id (safety net — preview should
    # have populated these, but handle gracefully if the commit path is called directly)
    no_id_rows = [
        r
        for r in rows
        if not (r.get("scryfall_id") or "").strip()
        and r.get("set_code")
        and r.get("collector_number")
    ]
    fallback_set_map: dict[tuple[str, str], dict[str, Any]] = {}
    if no_id_rows:
        fallback_set_map = bulk_fetch_by_set_number(
            [(r["set_code"], r["collector_number"]) for r in no_id_rows]
        ).cards

    candidate_rows: list[dict[str, Any]] = []
    for row in rows:
        scryfall_id = (row.get("scryfall_id") or "").strip()
        if scryfall_id:
            row["_resolved_scryfall_id"] = scryfall_id
            candidate_rows.append(row)
            continue

        key = (
            (row.get("set_code") or "").strip().lower(),
            (row.get("collector_number") or "").strip(),
        )
        card_data = fallback_set_map.get(key) or fetch_card_by_set_and_number(
            row.get("set_code", ""), row.get("collector_number", "")
        )
        if not card_data:
            failed_rows.append(
                {
                    "line_number": row.get("line_number"),
                    "reason": "Scryfall lookup failed by set/collector fallback.",
                }
            )
            continue

        row["_resolved_scryfall_id"] = card_data["scryfall_id"]
        row["_prefetched_card_data"] = card_data
        candidate_rows.append(row)

    if not candidate_rows:
        session.commit()
        return {
            "imported_count": 0,
            "total_quantity": 0,
            "failed_rows": failed_rows,
            "batch_id": batch.id,
            "imported_row_ids": [],
        }

    unique_ids = sorted(
        {row["_resolved_scryfall_id"] for row in candidate_rows if row.get("_resolved_scryfall_id")}
    )

    existing_cards = session.query(Card).filter(Card.scryfall_id.in_(unique_ids)).all()
    card_map: dict[str, Card] = {card.scryfall_id: card for card in existing_cards}

    # Batch-fetch any cards not yet in the local DB
    new_cards: list[Card] = []
    missing_ids = [sid for sid in unique_ids if sid not in card_map]
    if missing_ids:
        # Build from prefetched data first, then batch-fetch the rest
        prefetch_map: dict[str, dict[str, Any]] = {
            row["_resolved_scryfall_id"]: row["_prefetched_card_data"]
            for row in candidate_rows
            if row.get("_prefetched_card_data") and row.get("_resolved_scryfall_id") in missing_ids
        }
        need_fetch = [sid for sid in missing_ids if sid not in prefetch_map]
        if need_fetch:
            prefetch_map.update(bulk_refresh_prices(need_fetch).cards)

        for sid in missing_ids:
            payload = prefetch_map.get(sid)
            if not payload:
                for row in candidate_rows:
                    if row.get("_resolved_scryfall_id") == sid:
                        row["_failed"] = True
                        failed_rows.append(
                            {
                                "line_number": row.get("line_number"),
                                "reason": "Card lookup failed by Scryfall ID.",
                            }
                        )
                continue
            card = Card(**payload, updated_at=now)
            session.add(card)
            new_cards.append(card)

    if new_cards:
        session.flush()
        for card in new_cards:
            card_map[card.scryfall_id] = card

    candidate_rows = [row for row in candidate_rows if not row.get("_failed")]

    for card in existing_cards:
        prefetched = next(
            (
                r.get("_prefetched_card_data")
                for r in candidate_rows
                if r.get("_resolved_scryfall_id") == card.scryfall_id
                and r.get("_prefetched_card_data")
            ),
            None,
        )
        if prefetched:
            card.name = prefetched["name"]
            card.set_code = prefetched["set_code"]
            card.set_name = prefetched["set_name"]
            card.collector_number = prefetched["collector_number"]
            card.rarity = prefetched["rarity"]
            card.image_url = prefetched["image_url"]
            card.type_line = prefetched["type_line"]
            card.oracle_text = prefetched["oracle_text"]
            card.price_usd = prefetched["price_usd"]
            card.price_usd_foil = prefetched["price_usd_foil"]
            card.price_usd_etched = prefetched["price_usd_etched"]
            card.colors = prefetched.get("colors")
            card.mana_cost = prefetched.get("mana_cost")
            card.cmc = prefetched.get("cmc")
            card.updated_at = now

    card_ids = sorted(
        {
            card_map[row["_resolved_scryfall_id"]].id
            for row in candidate_rows
            if row.get("_resolved_scryfall_id") in card_map
        }
    )

    existing_pending_rows: list[InventoryRow] = []
    if card_ids:
        existing_pending_rows = (
            session.query(InventoryRow)
            .filter(InventoryRow.card_id.in_(card_ids))
            .filter(InventoryRow.user_id == user_id)
            .filter(InventoryRow.drawer.is_(None))
            .filter(InventoryRow.slot.is_(None))
            .filter(InventoryRow.is_pending.is_(True))
            .all()
        )

    inventory_map: dict[
        tuple[int, int, str, str, bool, str | None, str | None, bool], InventoryRow
    ] = {
        (
            row.user_id,
            row.card_id,
            row.finish,
            row.language or "en",
            bool(row.is_proxy),
            row.drawer,
            row.slot,
            row.is_pending,
        ): row
        for row in existing_pending_rows
    }

    created_rows: list[InventoryRow] = []
    audit_payloads: list[dict[str, Any]] = []

    for row in candidate_rows:
        sid = row["_resolved_scryfall_id"]
        card = card_map.get(sid)
        if not card:
            failed_rows.append(
                {
                    "line_number": row.get("line_number"),
                    "reason": "Card creation failed after resolution.",
                }
            )
            continue

        qty = max(1, int(row.get("quantity") or 1))
        finish = (row.get("finish") or "normal").strip().lower()
        language = normalize_language(row.get("language"))
        # v3.30.16 — per-row Role / Tags / Is Proxy. Validated at parse
        # time (parse_role / parse_proxy_bool); invalid values route to
        # invalid_rows before reaching this function. The cleaned dict
        # carries the normalized values; an empty string in `role` means
        # "no role" (NULL on the model).
        row_role = (row.get("role") or "").strip() or None
        row_tags = row.get("tags") or None  # preserved verbatim
        row_is_proxy = bool(row.get("is_proxy"))

        # v3.30.15 — per-row location resolution. If the line is in the
        # resolution map, the row lands DIRECTLY placed (no pending-merge
        # lookup, no notes-dump). The commit handler will run
        # place_imported_rows per resolved location afterward to merge with
        # any existing placed copy.
        resolved_loc_id = line_to_location_id.get(row.get("line_number"))

        if resolved_loc_id:
            target_row = InventoryRow(
                user_id=user_id,
                card_id=card.id,
                finish=finish,
                quantity=qty,
                drawer=None,
                slot=None,
                is_pending=False,
                storage_location_id=resolved_loc_id,
                language=language,
                role=row_role,
                tags=row_tags,
                is_proxy=row_is_proxy,
                notes=None,
                created_at=now,
                updated_at=now,
            )
            session.add(target_row)
            created_rows.append(target_row)
        else:
            # v3.30.16: notes is NULL on import (was previously the dump-site
            # for the per-row Location string; the trap that caused
            # round-trip data loss in v3.30.14 and earlier).
            #
            # v3.30.15 Decision 5 — pending-merge key unchanged. The key
            # below intentionally still hardcodes ``is_proxy=False`` because
            # any non-False is_proxy from this CSV would have routed
            # through the resolved-location branch instead (round-trip
            # exports carry resolvable Location values). A pending row
            # being created here is from a blank-Location CSV (typical
            # new-acquisition scanner workflow) where the row is treated
            # as a fresh non-proxy import.
            #
            # NOTE on merge: when an existing pending row is found in the
            # merge map, the imported row's role / tags / is_proxy are
            # SILENTLY IGNORED — the merge bumps quantity only. v3.30.15
            # Decision 5 keeps this contract (the pending-merge key is
            # load-bearing logic for every import path, not just round-trip;
            # a refinement needs the "Round-trip merge semantics" follow-up
            # to settle). For unresolved-Location CSVs the round-trip data
            # loss this would cause is theoretical anyway — round-trip
            # CSVs all carry resolvable Location values and go through the
            # resolved-location branch above.
            key = (user_id, card.id, finish, language, False, None, None, True)
            target_row = inventory_map.get(key)

            if target_row:
                target_row.quantity += qty
                target_row.updated_at = now
            else:
                target_row = InventoryRow(
                    user_id=user_id,
                    card_id=card.id,
                    finish=finish,
                    quantity=qty,
                    drawer=None,
                    slot=None,
                    is_pending=True,
                    language=language,
                    role=row_role,
                    tags=row_tags,
                    is_proxy=row_is_proxy,
                    notes=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(target_row)
                created_rows.append(target_row)
                inventory_map[key] = target_row

        imported_count += 1
        total_quantity += qty
        audit_payloads.append(
            {
                "card_id": card.id,
                "finish": finish,
                "quantity_delta": qty,
                "batch_id": batch.id,
                "inventory_row": target_row,
                "note": f"Imported from row {row.get('line_number')}",
            }
        )

    if created_rows:
        session.flush()

    # v3.30.15 — split imported_row_ids into placed vs pending sub-lists +
    # group placed ones by resolved location so the commit handler can run
    # place_imported_rows per-location to apply the existing placed-row
    # merge logic. imported_row_ids retains its prior semantic (every row
    # the import touched, in audit order) so existing consumers keep working.
    placed_row_ids: list[int] = []
    pending_row_ids: list[int] = []
    placed_row_ids_by_location: dict[int, list[int]] = {}

    for payload in audit_payloads:
        inv = payload["inventory_row"]
        rid = inv.id
        imported_row_ids.append(rid)
        if inv.is_pending:
            pending_row_ids.append(rid)
            destination_label = "pending"
        else:
            placed_row_ids.append(rid)
            loc_id = inv.storage_location_id
            if loc_id:
                placed_row_ids_by_location.setdefault(loc_id, []).append(rid)
            destination_label = f"location:{loc_id}" if loc_id else "placed"
        log_transaction(
            session=session,
            user_id=user_id,
            event_type="import",
            card_id=payload["card_id"],
            finish=payload["finish"],
            quantity_delta=payload["quantity_delta"],
            source_location=None,
            destination_location=destination_label,
            batch_id=payload["batch_id"],
            inventory_row_id=rid,
            note=payload["note"],
            flush=False,
        )

    session.commit()
    return {
        "imported_count": imported_count,
        "total_quantity": total_quantity,
        "failed_rows": failed_rows,
        "batch_id": batch.id,
        "imported_row_ids": imported_row_ids,
        "placed_row_ids": placed_row_ids,
        "pending_row_ids": pending_row_ids,
        "placed_row_ids_by_location": placed_row_ids_by_location,
    }


# ---------------------------------------------------------------------------
# v3.30.15 — per-row Location resolution helpers
#
# The pre-v3.30.15 importer parsed the Location column, surfaced it in the
# preview UI, then dumped it into ``InventoryRow.notes`` at write time — the
# Location semantic was silently lost. v3.30.15 resolves Location names
# against the user's StorageLocations and lands rows directly placed:
#
#   - clean (one existing match by case-insensitive name) → use that id.
#   - ambiguous (2+ existing matches) → preview surfaces a per-distinct-name
#     picker so the user chooses which match each name resolves to.
#   - missing (0 existing matches) → preview surfaces an explicit auto-create
#     confirm; on confirm the locations are created with ``type="other"``
#     before rows are placed.
#   - blank or user-cancelled → row falls through to the existing
#     target_location_id / drawer-sorter / pending behavior (notes still NULL
#     — no more location-string dump).
# ---------------------------------------------------------------------------


def _distinct_locations_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the unique case-insensitive Location values in preserved
    first-seen order, each paired with the first-seen Location Type hint
    for that name. Blank/whitespace Location values are excluded.

    v3.30.16 — return shape extended from ``list[str]`` to
    ``list[{"name": str, "csv_type": str}]``. ``csv_type`` is the
    (case-folded) Location Type the CSV supplied for the FIRST row carrying
    that Location value; empty string if absent. Used by ``resolve_location_names``
    to suggest the right type when a missing name triggers auto-create.
    First-seen wins; subsequent rows with a different Location Type for the
    same name are ignored (the user can edit the resolved type in the
    preview's auto-create dropdown).
    """
    seen: dict[str, dict[str, str]] = {}
    for r in rows:
        raw = (r.get("location") or "").strip()
        if not raw:
            continue
        key = raw.lower()
        if key not in seen:
            seen[key] = {
                "name": raw,
                "csv_type": (r.get("location_type") or "").strip().lower(),
            }
    return list(seen.values())


def resolve_location_names(
    session: Session, user_id: int, distinct_entries: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Resolve a list of distinct CSV Location entries against the user's
    StorageLocations (case-insensitive name match) and return per-distinct
    resolution metadata for the preview step.

    v3.30.16 — input shape extended to accept the ``[{name, csv_type}]``
    dicts emitted by ``_distinct_locations_from_rows``. Each returned dict
    carries:
      * ``name`` — first-seen display form of the location name.
      * ``key`` — lowercased name (form-field value).
      * ``matches`` — list of matching StorageLocation rows (0, 1, or many).
      * ``status`` — ``"clean"`` / ``"ambiguous"`` / ``"missing"``.
      * ``csv_type_hint`` — the CSV-supplied type (lowercased), empty if
        absent. Surfaced in the auto-create UI; ignored for clean +
        ambiguous resolutions (existing data wins per Decision 12).
      * ``csv_type_valid`` — True iff ``csv_type_hint`` is empty OR matches
        a value in ``VALID_LOCATION_TYPES`` (root excluded — never a
        legitimate user choice). False surfaces in the auto-create dropdown
        defaulting to ``other``.
      * ``csv_type_for_create`` — the default-fill value for the auto-create
        dropdown: the validated CSV value when present, else ``"other"``.

    Single-pass query against the user's locations — no N+1.
    """
    if not distinct_entries:
        return []
    locations = session.query(StorageLocation).filter(StorageLocation.user_id == user_id).all()
    by_name: dict[str, list[StorageLocation]] = {}
    for loc in locations:
        by_name.setdefault((loc.name or "").strip().lower(), []).append(loc)

    # v3.30.17 — batched lookup of decks paired with deck-type
    # StorageLocations in the user's set. Surfaces in the preview as the
    # Part C informational note when a clean-match resolution lands a
    # batch into an existing deck. Single query, no N+1.
    deck_loc_ids = [loc.id for locs in by_name.values() for loc in locs if loc.type == "deck"]
    deck_by_loc: dict[int, Deck] = {}
    if deck_loc_ids:
        decks = (
            session.query(Deck)
            .filter(Deck.user_id == user_id, Deck.storage_location_id.in_(deck_loc_ids))
            .all()
        )
        deck_by_loc = {d.storage_location_id: d for d in decks if d.storage_location_id is not None}

    # User-selectable types — VALID_LOCATION_TYPES minus the structural
    # 'root' kind (which can only appear on the user's auto-seeded root).
    selectable_types = VALID_LOCATION_TYPES - {"root"}

    out: list[dict[str, Any]] = []
    for entry in distinct_entries:
        raw_name = entry["name"]
        raw_type = (entry.get("csv_type") or "").strip().lower()
        key = raw_name.lower()
        matches = by_name.get(key, [])
        if len(matches) == 0:
            status = "missing"
        elif len(matches) == 1:
            status = "clean"
        else:
            status = "ambiguous"

        # csv_type validation only matters for missing → auto-create.
        # For clean / ambiguous, the existing data wins (Decision 12).
        csv_type_valid = (raw_type == "") or (raw_type in selectable_types)
        csv_type_for_create = raw_type if (raw_type in selectable_types) else "other"
        # v3.30.17.1 — distinguish "CSV explicitly named a valid type"
        # from "CSV was blank or unknown". The preview's auto-create
        # dropdown uses this to scope the "deck" option: an explicit
        # non-deck CSV choice (csv_type_explicit=True, csv_type_for_create
        # != "deck") gets binder/box/drawer/other only (the v3.30.17
        # rule that the user shouldn't re-classify an explicit CSV
        # choice); a blank/unknown CSV value (csv_type_explicit=False)
        # gets binder/box/drawer/deck/other so the user CAN designate
        # a location as a deck at import time. Third-party CSVs
        # (Moxfield, Deckbox, hand-crafted) all land here because they
        # don't carry a Location Type column — without this field the
        # user has no in-import way to designate a deck.
        csv_type_explicit = (raw_type != "") and csv_type_valid

        # v3.30.17 — if the clean-match resolution lands in a paired
        # deck-type location, surface the deck name for the Part C
        # informational note in the preview UI.
        existing_deck_name: str | None = None
        if status == "clean":
            loc = matches[0]
            if loc.type == "deck":
                paired = deck_by_loc.get(loc.id)
                if paired is not None:
                    existing_deck_name = paired.name

        out.append(
            {
                "name": raw_name,
                "key": key,
                "matches": matches,
                "status": status,
                "csv_type_hint": raw_type,
                "csv_type_valid": csv_type_valid,
                "csv_type_for_create": csv_type_for_create,
                "csv_type_explicit": csv_type_explicit,
                "existing_deck_name": existing_deck_name,
            }
        )
    return out


def auto_create_locations(
    session: Session,
    user_id: int,
    names: list[str],
    name_to_type: dict[str, str] | None = None,
) -> dict[str, int]:
    """Create one StorageLocation per name and return a map of
    lowercased-name → new StorageLocation.id.

    v3.30.16 — optional ``name_to_type`` argument supplies the type per
    name (keyed by lowercased name). Names absent from the map fall back
    to ``type="other"`` (the v3.30.15 default). Types are validated against
    ``VALID_LOCATION_TYPES`` minus ``root``; invalid types raise ValueError
    (caught by the route handler and surfaced via preview re-render —
    same shape as v3.30.15's auto-create-not-confirmed branch).

    v3.30.17 — type="deck" now routes through ``deck_service.create_deck``
    so the paired Deck row is created atomically alongside the
    StorageLocation. v3.30.15/.16 had reached ``create_location`` directly
    with type="deck", producing orphan deck-locations (no paired Deck row)
    that were invisible to /decks, /goldfish, deck-detail, and the
    deck-destination dropdowns. After v3.30.17 the v3.3 invariant holds in
    every code path: a type="deck" StorageLocation always has a paired
    Deck row. Existing-Deck-by-name (uq_decks_user_name) is honored:
    if a Deck with the imported name already exists for this user, its
    paired ``storage_location_id`` is reused (no duplicate create).

    Non-deck behaviour unchanged: still routes through ``create_location``
    so the same validation + one-name-per-user rule the manual /locations
    flow uses is honored. If a StorageLocation already exists by the time
    this is called (race), the existing row is used and no new row is
    created.
    """
    out: dict[str, int] = {}
    if not names:
        return out
    name_to_type = name_to_type or {}
    selectable_types = VALID_LOCATION_TYPES - {"root"}
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        existing = (
            session.query(StorageLocation)
            .filter(
                StorageLocation.user_id == user_id,
                func.lower(StorageLocation.name) == name.lower(),
            )
            .first()
        )
        if existing is not None:
            out[name.lower()] = existing.id
            continue
        # Resolve the type from the per-name map, defaulting to "other".
        requested_type = (name_to_type.get(name.lower()) or "other").strip().lower()
        if requested_type not in selectable_types:
            raise ValueError(
                f"Invalid Location Type for auto-create: {requested_type!r} "
                f"(allowed: {sorted(selectable_types)})"
            )
        if requested_type == "deck":
            # v3.30.17 — route deck creation through the canonical atomic
            # path so the paired Deck row lands with the StorageLocation.
            # First, honour uq_decks_user_name: if a Deck with this name
            # already exists for the user, reuse its storage_location_id
            # rather than letting create_deck fail on the unique constraint.
            existing_deck = (
                session.query(Deck)
                .filter(
                    Deck.user_id == user_id,
                    func.lower(Deck.name) == name.lower(),
                )
                .first()
            )
            if existing_deck is not None and existing_deck.storage_location_id is not None:
                out[name.lower()] = existing_deck.storage_location_id
                continue
            if existing_deck is not None:
                # Deck exists but is orphaned (no paired location, e.g.
                # historical drift before v3.10.6 kept names in sync). We
                # cannot reuse and we cannot create_deck (would violate
                # uq_decks_user_name). Skip the resolution: row falls
                # through to target_location_id like an unresolved name.
                # User can clean up the orphaned Deck separately.
                continue
            # v3.30.17.2 — try create_deck with IntegrityError fallback.
            # The per-user check above handles the canonical
            # uq_decks_user_name (user_id, name) constraint case. But
            # installs that pre-date v3.1.0 carry a LEGACY auto-index
            # `sqlite_autoindex_decks_1` left over from when Deck.name
            # was declared `unique=True, index=True` (single-tenant
            # assumption). v3.1.0 switched the model to the compound
            # (user_id, name) UniqueConstraint but did not drop the
            # legacy auto-index — SQLite cannot drop an inline-UNIQUE
            # auto-index without a full table rebuild, deferred to v4.
            # On those installs, calling create_deck for a name that
            # ANY other user already owns triggers the legacy constraint
            # ("UNIQUE constraint failed: decks.name") and 500s. The
            # try/except catches that case (and any other unique-
            # constraint shape we don't know about); rollback restores
            # the session for subsequent rows in the import; the
            # resolution is skipped so the row falls through to
            # target_location_id. Self-heals when v4's table rebuild
            # drops the legacy constraint.
            try:
                new_deck = create_deck(session, user_id=user_id, name=name)
            except IntegrityError:
                session.rollback()
                continue
            # create_deck always pairs a StorageLocation — storage_location_id
            # is non-None by construction.
            if new_deck.storage_location_id is not None:
                out[name.lower()] = new_deck.storage_location_id
            continue
        loc = create_location(
            session=session,
            user_id=user_id,
            name=name,
            type=requested_type,
        )
        out[name.lower()] = loc.id
    return out


def compute_duplicate_counts_for_resolved(
    session: Session,
    user_id: int,
    rows: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
) -> dict[str, int]:
    """For each "clean" (single-match) resolution, count how many CSV rows
    resolve to a card+finish+language already placed at that location. Used
    by the preview step to surface a duplicate warning (per Decision 5: the
    rows will merge into existing placed copies via place_imported_rows —
    not strictly "duplicates" today but the user should know the import will
    add to existing quantities).

    Only computed for ``status == "clean"`` resolutions — ambiguous/missing
    don't have a known destination at preview time.
    """
    counts: dict[str, int] = {}

    clean_by_key: dict[str, int] = {
        r["key"]: r["matches"][0].id for r in resolutions if r["status"] == "clean" and r["matches"]
    }
    if not clean_by_key:
        return counts

    rows_by_key: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = (r.get("location") or "").strip().lower()
        if key in clean_by_key:
            rows_by_key.setdefault(key, []).append(r)
    if not rows_by_key:
        return counts

    scryfall_ids = sorted(
        {
            (r.get("scryfall_id") or "").strip()
            for rows_list in rows_by_key.values()
            for r in rows_list
            if (r.get("scryfall_id") or "").strip()
        }
    )
    if not scryfall_ids:
        return counts

    card_id_by_sid: dict[str, int] = dict(
        session.query(Card.scryfall_id, Card.id).filter(Card.scryfall_id.in_(scryfall_ids)).all()
    )

    location_ids = list({loc_id for loc_id in clean_by_key.values()})
    existing_placed = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(False),
            InventoryRow.storage_location_id.in_(location_ids),
        )
        .all()
    )
    existing_set: set[tuple[int, int, str, str]] = {
        (
            row.storage_location_id or 0,
            row.card_id,
            (row.finish or "normal"),
            (row.language or "en"),
        )
        for row in existing_placed
    }

    for key, rows_at_key in rows_by_key.items():
        loc_id = clean_by_key[key]
        hits = 0
        for r in rows_at_key:
            sid = (r.get("scryfall_id") or "").strip()
            cid = card_id_by_sid.get(sid)
            if not cid:
                continue
            finish = (r.get("finish") or "normal").strip().lower()
            language = normalize_language(r.get("language"))
            if (loc_id, cid, finish, language) in existing_set:
                hits += 1
        if hits > 0:
            counts[key] = hits

    return counts


# ---------------------------------------------------------------------------
# Text list import (Moxfield deck export, MTGA, MTGO)
# ---------------------------------------------------------------------------

_SECTION_HEADERS = frozenset(
    {"deck", "sideboard", "commander", "companion", "maybeboard", "considering", "tokens"}
)

# Short-form: bare set + collector with optional qty / foil marker.
# SET = 2-6 chars starting with a letter; COLLECTOR = digits with optional letter suffix
# (e.g. "145", "23a"); QTY = 1-3 digits.
_SHORT_SET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,5}$")
_SHORT_COLL_RE = re.compile(r"^[0-9]+[A-Za-z]?$")
_SHORT_QTY_RE = re.compile(r"^[0-9]{1,3}$")


def _parse_short_list_line(line: str) -> dict[str, Any] | None:
    """Parse a bare 'SET COLLECTOR [qty]' or 'qty SET COLLECTOR' line.

    Returns None if the line doesn't fit the short form so the caller can fall
    through to the standard Moxfield-style parser.
    """
    rest = line.strip()
    if not rest:
        return None

    # Detect *F* foil marker anywhere on the line and strip it.
    finish = "normal"
    if re.search(r"(?i)\*F\*", rest):
        finish = "foil"
        rest = re.sub(r"(?i)\s*\*F\*\s*", " ", rest).strip()

    # Detect *XX* / *XXX* language marker (e.g. *JP* → ja, *DE* → de).
    # 2-3 letter codes so it can't collide with the 1-letter *F* foil marker.
    language = "en"
    lang_match = re.search(r"\*([A-Za-z]{2,3})\*", rest)
    if lang_match:
        language = normalize_language(lang_match.group(1))
        rest = (rest[: lang_match.start()] + rest[lang_match.end() :]).strip()
        rest = re.sub(r"\s+", " ", rest)

    parts = rest.split()
    if len(parts) not in (2, 3):
        return None

    set_code = ""
    collector_number = ""
    quantity = 1

    if len(parts) == 2:
        a, b = parts
        if _SHORT_SET_RE.match(a) and _SHORT_COLL_RE.match(b):
            set_code, collector_number = a.lower(), b
        else:
            return None
    else:
        if (
            _SHORT_QTY_RE.match(parts[0])
            and _SHORT_SET_RE.match(parts[1])
            and _SHORT_COLL_RE.match(parts[2])
        ):
            quantity = int(parts[0])
            set_code, collector_number = parts[1].lower(), parts[2]
        elif (
            _SHORT_SET_RE.match(parts[0])
            and _SHORT_COLL_RE.match(parts[1])
            and _SHORT_QTY_RE.match(parts[2])
        ):
            set_code, collector_number = parts[0].lower(), parts[1]
            quantity = int(parts[2])
        else:
            return None

    if quantity < 1:
        return None

    return {
        "name": "",
        "set_code": set_code,
        "collector_number": collector_number,
        "quantity": quantity,
        "finish": finish,
        "language": language,
    }


def _parse_list_line(line: str) -> dict[str, Any] | None:
    """Parse one line of a pasted card list. Returns None for non-card lines."""
    line = line.strip()
    if not line:
        return None

    short = _parse_short_list_line(line)
    if short is not None:
        return short

    if not line[0].isdigit():
        return None

    # Extract leading quantity (supports "4 " and "4x ")
    m = re.match(r"^(\d+)x?\s+", line)
    if not m:
        return None

    quantity = int(m.group(1))
    rest = line[m.end() :]

    # Detect *XX* / *XXX* language marker first. 2-3 letter codes so it
    # can't collide with the 1-letter *F* foil marker. Strip from `rest`
    # before the (SET) extraction so the trailing-suffix regex sees a
    # clean tail.
    language = "en"
    lang_match = re.search(r"\*([A-Za-z]{2,3})\*", rest)
    if lang_match:
        language = normalize_language(lang_match.group(1))
        rest = (rest[: lang_match.start()] + rest[lang_match.end() :]).strip()
        rest = re.sub(r"\s+", " ", rest)

    # Detect MTGA foil marker (*F*) anywhere on the line and strip it.
    # Search-anywhere (not just endswith) so it works when combined with
    # a language marker like `*F* *DE*` in any order.
    finish = "normal"
    if re.search(r"(?i)\*F\*", rest):
        finish = "foil"
        rest = re.sub(r"(?i)\s*\*F\*\s*", " ", rest).strip()
        rest = re.sub(r"\s+", " ", rest)

    # Extract trailing (SET) and optional collector number
    set_code = ""
    collector_number = ""
    set_match = _SET_SUFFIX_RE.search(rest)
    if set_match:
        set_code = set_match.group(1).lower()
        collector_number = set_match.group(2) or ""
        rest = rest[: set_match.start()].strip()

    name = rest
    if not name:
        return None

    return {
        "name": name,
        "set_code": set_code,
        "collector_number": collector_number,
        "quantity": quantity,
        "finish": finish,
        "language": language,
    }


def _text_list_unresolved_reason(
    parsed: dict[str, Any], label: str, set_failed: set[tuple[str, str]]
) -> str:
    """Reason string for a paste-list line Pass 2's batch did not resolve.

    Distinguishes the three post-v3.23.x cases so the user knows what to do:
    transient batch failure (retry), bare name (no batch path — needs a
    set+collector or the name-search import UI), or genuinely-unknown card.
    """
    if parsed["set_code"] and parsed["collector_number"]:
        if (parsed["set_code"], parsed["collector_number"]) in set_failed:
            return f"Scryfall lookup temporarily failed — re-import to retry: {label}"
        return f"Card not found on Scryfall: {label}"
    # No set+collector: a bare name line. There is no batch name endpoint,
    # and per-line live name lookup was removed (request-path immunity).
    return (
        f"Bare card names can't be batch-resolved — add a set + collector "
        f'(e.g. "MH3 145") or use Import by name: {label}'
    )


def parse_text_list(text: str) -> dict[str, Any]:
    """Parse a pasted card list in Moxfield / MTGA / MTGO format.

    Resolves each line via Scryfall. Uses set+collector when available,
    falls back to exact name (then fuzzy) when only a name is given.
    """
    valid_rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []

    # --- Pass 1: parse all lines without touching Scryfall ---
    pre_lines: list[tuple[int, dict[str, Any]]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower() in _SECTION_HEADERS:
            continue
        parsed = _parse_list_line(stripped)
        if not parsed:
            continue
        pre_lines.append((line_number, parsed))

    # --- Pass 2: batch-fetch all set+collector pairs at once ---
    set_map: dict[tuple[str, str], dict[str, Any]] = {}
    set_result = BulkFetchResult()
    batchable = [
        (p["set_code"], p["collector_number"])
        for _, p in pre_lines
        if p["set_code"] and p["collector_number"]
    ]
    if batchable:
        set_result = bulk_fetch_by_set_number(batchable)
        set_map = set_result.cards

    # --- Pass 3: resolve each line from Pass 2's batch ONLY (no network) ---
    # Same invariant as parse_scanner_csv: nothing in this loop calls
    # Scryfall. Bare card-name lines (no set/collector) cannot be
    # batch-resolved — Scryfall has no batch name endpoint — so they now
    # surface as invalid rows instead of triggering a per-line live name
    # lookup (the v3.23.9 request-path-immunity principle).
    _set_failed = set(set_result.failed)
    for line_number, parsed in pre_lines:
        card_data: dict[str, Any] | None = None
        if parsed["set_code"] and parsed["collector_number"]:
            card_data = set_map.get((parsed["set_code"], parsed["collector_number"]))

        if card_data:
            valid_rows.append(
                {
                    "line_number": line_number,
                    "scryfall_id": card_data["scryfall_id"],
                    "set_code": card_data["set_code"],
                    "collector_number": card_data["collector_number"],
                    "name": card_data["name"],
                    "finish": parsed["finish"],
                    "quantity": parsed["quantity"],
                    "location": "",
                    "language": parsed.get("language", "en"),
                    "warnings": build_finish_warnings(card_data, parsed["finish"]),
                }
            )
        else:
            if parsed["name"]:
                label = parsed["name"]
                if parsed["set_code"]:
                    label += f" ({parsed['set_code'].upper()})"
            elif parsed["set_code"]:
                label = f"({parsed['set_code'].upper()}) {parsed['collector_number']}".strip()
            else:
                label = "(unknown)"
            invalid_rows.append(
                {
                    "line_number": line_number,
                    "name": parsed["name"],
                    "set_code": parsed["set_code"],
                    "collector_number": parsed["collector_number"],
                    "finish": parsed["finish"],
                    "quantity": parsed["quantity"],
                    "reason": _text_list_unresolved_reason(parsed, label, _set_failed),
                }
            )

    return {
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "format_name": "Text List",
    }
