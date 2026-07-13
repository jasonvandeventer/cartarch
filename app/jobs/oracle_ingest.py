"""Scryfall oracle_cards bulk ingest → oracle_catalog (Momir Sim #109).

Downloads Scryfall's ``oracle_cards`` bulk file (one entry per oracle_id) and
upserts a row per card NAME into ``oracle_catalog``, the Momir creature source.
Replaces the collection-bounded ``cards`` table for Momir: ``cards`` only holds
owned printings, starving the pool and lacking keywords.

Invokable as ``python -m app.jobs.oracle_ingest`` — this IS the standing manual
invocation (catalog refresh is occasional, NOT scheduled; no CronJob, unlike the
daily price ingest).

Network is confined to :func:`stream_oracle_cards`, streamed with ijson so the
~180 MB file is never ``json.load()``ed. Tests pass an in-memory iterable to
:func:`run_ingest` so the pipeline runs with no live network.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

import ijson
import requests

from app.db import SessionLocal
from app.models import OracleCatalog
from app.timeutil import utc_now

BULK_INDEX_URL = "https://api.scryfall.com/bulk-data"

# Scryfall REQUIRES a descriptive, non-default User-Agent — the stdlib/requests
# default is rejected with HTTP 400 (subcode generic_user_agent). Without this the
# whole ingest fails on the very first request. (Their guidelines also ask for an
# explicit Accept.)
_HEADERS = {"User-Agent": "Cartarch/1.0 (+https://cartarch.com)", "Accept": "application/json"}

# Layouts that are not real castable creatures even when the type line says
# "Creature" (game tokens, emblems, the double-faced-token printings, art cards).
_EXCLUDED_LAYOUTS = {"token", "emblem", "art_series", "double_faced_token"}


def _stream_json_array(url: str) -> Iterator[dict]:
    """Yield each object from a top-level JSON array without materializing it.

    Scryfall bulk files are a bare ``[ {...}, {...} ]`` array, so ``ijson.items``
    over the ``item`` path streams one card at a time. ``decode_content``
    transparently inflates gzip."""
    resp = requests.get(url, headers=_HEADERS, stream=True, timeout=(30, 600))
    resp.raise_for_status()
    resp.raw.decode_content = True
    try:
        yield from ijson.items(resp.raw, "item")
    finally:
        resp.close()


def stream_oracle_cards() -> Iterator[dict]:
    """Resolve the current oracle_cards download URI from the bulk index, then
    stream every card object from it."""
    resp = requests.get(BULK_INDEX_URL, headers=_HEADERS, timeout=(30, 60))
    resp.raise_for_status()
    index = resp.json()
    uri = next(e["download_uri"] for e in index["data"] if e["type"] == "oracle_cards")
    yield from _stream_json_array(uri)


def _is_momir_legal(card: dict, type_line: str) -> bool:
    """Momir pool filter (adjustable later). A creature by front-face type, not a
    token/emblem/art layout, Vintage-legal (excludes acorn/un-set and
    digital-only Alchemy), and not a memorabilia set (oversized/promos)."""
    if "Creature" not in type_line:
        return False
    if card.get("layout") in _EXCLUDED_LAYOUTS:
        return False
    if (card.get("legalities") or {}).get("vintage") == "not_legal":
        return False
    if card.get("set_type") == "memorabilia":
        return False
    return True


def extract(card: dict) -> dict | None:
    """Map one Scryfall oracle entry to oracle_catalog column values, or ``None``
    to skip it (no oracle_id, or not a creature).

    Multi-face layouts (transform, modal_dfc, flip, adventure) take the FRONT
    face's name/text/P-T/mana_cost/colors; cmc, color_identity, keywords, layout
    and the representative scryfall_id (``id``) stay root-level."""
    oracle_id = card.get("oracle_id")
    if not oracle_id:
        return None
    faces = card.get("card_faces")
    front = faces[0] if faces else card
    type_line = front.get("type_line") or card.get("type_line") or ""
    if "Creature" not in type_line:
        return None
    return {
        "oracle_id": oracle_id,
        "name": front.get("name") or card.get("name"),
        "mana_cost": front.get("mana_cost") or card.get("mana_cost"),
        "cmc": card.get("cmc"),
        "type_line": type_line,
        "oracle_text": front.get("oracle_text") or card.get("oracle_text"),
        "keywords": json.dumps(card.get("keywords") or []),
        "power": front.get("power"),
        "toughness": front.get("toughness"),
        "colors": json.dumps(front.get("colors") or card.get("colors") or []),
        "color_identity": json.dumps(card.get("color_identity") or []),
        "layout": card.get("layout"),
        "scryfall_id": card.get("id"),
        "is_momir_legal": _is_momir_legal(card, type_line),
    }


def run_ingest(session, cards: Iterable[dict] | None = None) -> dict[str, int]:
    """Upsert oracle_catalog from ``cards`` (defaults to the live Scryfall bulk
    stream). Non-creatures and entries without an oracle_id are skipped. Returns
    ``{inserted, updated, skipped}``.

    The whole table (~18k rows) fits in memory, so we load existing rows once and
    upsert against that map — no per-row SELECT.
    """
    cards = cards if cards is not None else stream_oracle_cards()
    existing: dict[str, OracleCatalog] = {
        row.oracle_id: row for row in session.query(OracleCatalog).all()
    }
    inserted = updated = skipped = 0
    now = utc_now()
    for card in cards:
        values = extract(card)
        if values is None:
            skipped += 1
            continue
        oracle_id = values.pop("oracle_id")
        row = existing.get(oracle_id)
        if row is None:
            row = OracleCatalog(oracle_id=oracle_id)
            session.add(row)
            existing[oracle_id] = row
            inserted += 1
        else:
            updated += 1
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = now
    session.commit()
    stats = {"inserted": inserted, "updated": updated, "skipped": skipped}
    print(f"[oracle-ingest] {stats}", flush=True)
    return stats


def main() -> None:
    session = SessionLocal()
    try:
        run_ingest(session)
    finally:
        session.close()


if __name__ == "__main__":
    main()
