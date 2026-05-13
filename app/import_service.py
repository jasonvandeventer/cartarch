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
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.audit_service import create_import_batch, log_transaction
from app.models import Card, InventoryRow
from app.scryfall import (
    bulk_fetch_by_set_number,
    bulk_refresh_prices,
    fetch_card_by_name,
    fetch_card_by_scryfall_id,
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
    # Helvault: finish is in a column called "extras"
    "extras": "finish",
    # Moxfield: set code is in "Edition", foil status is in "Foil"
    "edition": "set_code",
    "foil": "finish",
}


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
    text = file_bytes.decode("utf-8-sig", errors="replace")
    stream = io.StringIO(text)
    reader = csv.DictReader(stream)

    format_name = detect_csv_format(reader.fieldnames or [])
    valid_rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []

    # --- Pass 1: parse rows without touching Scryfall ---
    pre_rows: list[dict[str, Any]] = []
    for line_number, raw_row in enumerate(reader, start=2):
        row = {normalize_header(k): (v or "").strip() for k, v in raw_row.items()}
        scryfall_id = row.get("scryfall_id", "")
        set_code = row.get("set_code", "").lower()
        collector_number = row.get("collector_number", "")
        finish = normalize_finish(row.get("finish", ""))
        location = row.get("location", "")
        quantity_raw = row.get("quantity", "1")
        name = row.get("name", "")
        card_type = row.get("type", "")
        language = normalize_language(row.get("language", ""))
        try:
            quantity = max(1, int(quantity_raw or "1"))
        except ValueError:
            quantity = 1
        pre_rows.append(
            {
                "line_number": line_number,
                "scryfall_id": scryfall_id,
                "set_code": set_code,
                "collector_number": collector_number,
                "finish": finish,
                "quantity": quantity,
                "location": location,
                "name": name,
                "type": card_type,
                "language": language,
            }
        )

    # --- Pass 2: batch-fetch card data ---
    # Separate rows by identifier type; batch each group.
    id_rows = [r for r in pre_rows if r["scryfall_id"]]
    set_rows = [
        r for r in pre_rows if not r["scryfall_id"] and r["set_code"] and r["collector_number"]
    ]

    # Batch-fetch by scryfall_id
    id_map: dict[str, dict[str, Any]] = {}
    if id_rows:
        unique_ids = list({r["scryfall_id"] for r in id_rows})
        id_map = bulk_refresh_prices(unique_ids)

    # Batch-fetch by set+collector
    set_map: dict[tuple[str, str], dict[str, Any]] = {}
    if set_rows:
        pairs = [(r["set_code"], r["collector_number"]) for r in set_rows]
        set_map = bulk_fetch_by_set_number(pairs)

    # --- Pass 3: build output rows ---
    for r in pre_rows:
        cleaned = {
            "line_number": r["line_number"],
            "scryfall_id": r["scryfall_id"],
            "set_code": r["set_code"],
            "collector_number": r["collector_number"],
            "finish": r["finish"],
            "quantity": r["quantity"],
            "location": r["location"],
            "name": r["name"],
            "type": r["type"],
            "language": r["language"],
            "warnings": [],
        }

        card_data: dict[str, Any] | None = None
        if r["scryfall_id"]:
            card_data = id_map.get(r["scryfall_id"])
            # Fall back to individual fetch for any the batch missed
            if card_data is None:
                card_data = fetch_card_by_scryfall_id(r["scryfall_id"])
        elif r["set_code"] and r["collector_number"]:
            card_data = set_map.get((r["set_code"], r["collector_number"]))
            if card_data is None:
                card_data = fetch_card_by_set_and_number(r["set_code"], r["collector_number"])
            if card_data and not cleaned["scryfall_id"]:
                cleaned["scryfall_id"] = card_data.get("scryfall_id", "")

        if r["scryfall_id"] or (r["set_code"] and r["collector_number"]):
            cleaned["warnings"] = build_finish_warnings(card_data, r["finish"])
            if card_data:
                cleaned["name"] = card_data.get("name") or cleaned["name"]
                cleaned["set_code"] = card_data.get("set_code") or cleaned["set_code"]
                cleaned["collector_number"] = (
                    card_data.get("collector_number") or cleaned["collector_number"]
                )
            valid_rows.append(cleaned)
        else:
            cleaned["reason"] = "Missing Scryfall ID and set/collector fallback fields."
            invalid_rows.append(cleaned)

    return {"valid_rows": valid_rows, "invalid_rows": invalid_rows, "format_name": format_name}


def persist_import_rows(
    session: Session,
    rows: list[dict[str, Any]],
    user_id: int,
    filename: str = "manual import",
) -> dict[str, Any]:
    """Persist imported rows into the current user's pending inventory.

    User ownership is required at the service boundary. Authentication can change
    later, but this function must never infer or default the owning user.
    """
    if user_id <= 0:
        raise ValueError("user_id must be a positive integer when importing rows")

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
        )

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
            prefetch_map.update(bulk_refresh_prices(need_fetch))

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

    inventory_map: dict[tuple[int, int, str, str, str | None, str | None, bool], InventoryRow] = {
        (
            row.user_id,
            row.card_id,
            row.finish,
            row.language or "en",
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
        location_note = (row.get("location") or "").strip() or None

        key = (user_id, card.id, finish, language, None, None, True)
        target_row = inventory_map.get(key)

        if target_row:
            target_row.quantity += qty
            target_row.updated_at = now
            if location_note:
                target_row.notes = location_note
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
                notes=location_note,
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

    for payload in audit_payloads:
        imported_row_ids.append(payload["inventory_row"].id)
        log_transaction(
            session=session,
            user_id=user_id,
            event_type="import",
            card_id=payload["card_id"],
            finish=payload["finish"],
            quantity_delta=payload["quantity_delta"],
            source_location=None,
            destination_location="pending",
            batch_id=payload["batch_id"],
            inventory_row_id=payload["inventory_row"].id,
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
    }


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

    # Detect MTGA foil marker (*F*)
    finish = "normal"
    if rest.upper().endswith("*F*"):
        finish = "foil"
        rest = rest[:-3].strip()

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
    }


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
    batchable = [
        (p["set_code"], p["collector_number"])
        for _, p in pre_lines
        if p["set_code"] and p["collector_number"]
    ]
    if batchable:
        set_map = bulk_fetch_by_set_number(batchable)

    # --- Pass 3: resolve each line, falling back to name search for misses ---
    for line_number, parsed in pre_lines:
        card_data: dict[str, Any] | None = None
        try:
            if parsed["set_code"] and parsed["collector_number"]:
                card_data = set_map.get((parsed["set_code"], parsed["collector_number"]))
                if card_data is None:
                    card_data = fetch_card_by_set_and_number(
                        parsed["set_code"], parsed["collector_number"]
                    )
            if not card_data and parsed["name"]:
                card_data = fetch_card_by_name(parsed["name"], set_code=parsed["set_code"])
        except Exception:
            card_data = None

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
                    "language": "en",
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
                    "reason": f"Card not found on Scryfall: {label}",
                }
            )

    return {
        "valid_rows": valid_rows,
        "invalid_rows": invalid_rows,
        "format_name": "Text List",
    }
