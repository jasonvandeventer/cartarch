"""MTGJSON daily price ingest (MTGJSON ingest issue).

Replaces Scryfall as the price source. Pulls MTGJSON's current-day paper prices,
joins them to Cartarch printings via Scryfall id → MTGJSON uuid, and upserts a
per-printing-per-finish row into ``card_prices``. The resolved display value
(``app.pricing.resolve_price_value`` — manual override, then
tcgplayer/cardkingdom/cardsphere retail) is denormalized back onto
``Card.price_usd*`` so every existing read surface keeps working unchanged.

Invokable as ``python -m app.jobs.price_ingest``. The daily schedule is a thin
CronJob in the platform repo calling this entrypoint (out of scope here).

Network is confined to :func:`stream_identifiers` / :func:`stream_prices`, both
streamed with ijson so the multi-hundred-MB files are never ``json.load()``ed.
Tests monkeypatch those two functions with in-memory fixtures, so the rest of
the pipeline runs with no live network.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import ijson
import requests

from app.db import SessionLocal
from app.models import Card, CardPrice
from app.pricing import PRICE_PROVIDER_ORDER, resolve_price_value
from app.timeutil import utc_now

IDENTIFIERS_URL = "https://mtgjson.com/api/v5/AllIdentifiers.json"
PRICES_URL = "https://mtgjson.com/api/v5/AllPricesToday.json"

# MTGJSON paper finishes; these are the ones we denormalize onto the three
# Card.price_usd* columns. card_prices itself can hold any finish string.
_FINISH_TO_CARD_COLUMN = {
    "normal": "price_usd",
    "foil": "price_usd_foil",
    "etched": "price_usd_etched",
}

_SQLITE_PARAM_CHUNK = 900  # stay under SQLite's 999-bound parameter limit


def _stream_data(url: str) -> Iterator[tuple[str, dict]]:
    """Stream ``data.{uuid} = {...}`` pairs from a large MTGJSON v5 file.

    ijson kvitems over the ``data`` key yields one (uuid, object) at a time, so
    the file is never fully materialized — same posture as the Scryfall bulk
    loop. ``decode_content`` transparently inflates gzip.
    """
    resp = requests.get(url, stream=True, timeout=(30, 600))
    resp.raise_for_status()
    resp.raw.decode_content = True
    try:
        yield from ijson.kvitems(resp.raw, "data", use_float=True)
    finally:
        resp.close()


def stream_identifiers() -> Iterator[tuple[str, str]]:
    """Yield ``(uuid, scryfall_id)`` from MTGJSON ``AllIdentifiers.json``."""
    for uuid, entry in _stream_data(IDENTIFIERS_URL):
        scryfall_id = (entry.get("identifiers") or {}).get("scryfallId")
        if scryfall_id:
            yield uuid, scryfall_id


def stream_prices() -> Iterator[tuple[str, dict]]:
    """Yield ``(uuid, paper_dict)`` from MTGJSON ``AllPricesToday.json``.

    ``paper_dict`` is ``{provider: {listType: {finish: {date: value}}}}``.
    """
    for uuid, entry in _stream_data(PRICES_URL):
        paper = entry.get("paper")
        if paper:
            yield uuid, paper


def _latest(retail_by_date: dict | None) -> str | None:
    """Most-recent date's value from a ``{date: value}`` map (ISO dates sort
    lexically). ``None`` when empty or the value is null."""
    if not retail_by_date:
        return None
    value = retail_by_date[max(retail_by_date)]
    return None if value is None else str(value)


def extract_prices(paper: dict) -> dict[str, dict[str, str]]:
    """``paper`` → ``{finish: {provider: value}}`` over the kept USD providers'
    ``retail`` lists. cardmarket (EUR) and every ``buylist`` are ignored."""
    out: dict[str, dict[str, str]] = {}
    for provider in PRICE_PROVIDER_ORDER:
        retail = ((paper.get(provider) or {}).get("retail")) or {}
        for finish, by_date in retail.items():
            value = _latest(by_date)
            if value is not None:
                out.setdefault(finish, {})[provider] = value
    return out


def _chunks(items: list, size: int = _SQLITE_PARAM_CHUNK) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _denormalize_card(card: Card, price_rows: list[CardPrice]) -> None:
    """Write the resolved per-finish display price onto Card.price_usd*."""
    by_finish = {r.finish: r for r in price_rows}
    for finish, column in _FINISH_TO_CARD_COLUMN.items():
        setattr(card, column, resolve_price_value(by_finish.get(finish)))


def run_ingest(session) -> dict[str, int]:
    """Pull MTGJSON prices, upsert ``card_prices``, denormalize onto cards.

    Returns a small stats dict. Preserves manual overrides and last-known values
    (a transient provider miss keeps the prior value and does NOT advance
    ``price_updated_at``). Never falls back to Scryfall.
    """
    our_scryfall_ids = {sid for (sid,) in session.query(Card.scryfall_id).all() if sid}

    # scryfall_id → uuid, restricted to printings we actually hold.
    uuid_to_sid: dict[str, str] = {}
    for uuid, scryfall_id in stream_identifiers():
        if scryfall_id in our_scryfall_ids:
            uuid_to_sid[uuid] = scryfall_id

    unresolved = our_scryfall_ids - set(uuid_to_sid.values())
    if unresolved:
        print(
            f"[price-ingest] {len(unresolved)} printing(s) did not map to an MTGJSON uuid",
            flush=True,
        )

    # uuid → prices, only for the uuids we mapped.
    prices_by_sid: dict[str, dict[str, dict[str, str]]] = {}
    for uuid, paper in stream_prices():
        sid = uuid_to_sid.get(uuid)
        if sid is not None:
            prices_by_sid[sid] = extract_prices(paper)

    if not prices_by_sid:
        print("[price-ingest] no prices resolved; nothing to upsert", flush=True)
        return {"printings": 0, "rows": 0, "unresolved": len(unresolved)}

    sids = list(prices_by_sid)
    card_by_sid: dict[str, Card] = {}
    existing_by_sid: dict[str, dict[str, CardPrice]] = {}
    for chunk in _chunks(sids):
        for card in session.query(Card).filter(Card.scryfall_id.in_(chunk)):
            card_by_sid[card.scryfall_id] = card
        for row in session.query(CardPrice).filter(CardPrice.scryfall_id.in_(chunk)):
            existing_by_sid.setdefault(row.scryfall_id, {})[row.finish] = row

    now = utc_now()
    upserted_rows = 0
    for sid, by_finish in prices_by_sid.items():
        card = card_by_sid.get(sid)
        if card is None:
            continue
        rows = existing_by_sid.setdefault(sid, {})
        for finish, providers in by_finish.items():
            row = rows.get(finish)
            if row is None:
                row = CardPrice(scryfall_id=sid, finish=finish)
                session.add(row)
                rows[finish] = row
            changed = False
            for provider in PRICE_PROVIDER_ORDER:
                value = providers.get(provider)
                if value:  # null this run → keep last-known; never wipe to None
                    setattr(row, f"{provider}_retail", value)
                    changed = True
            if changed:  # fresh value confirmed today → stamp; a miss does not
                row.price_updated_at = now
            upserted_rows += 1
        _denormalize_card(card, list(rows.values()))

    session.commit()
    stats = {
        "printings": len(prices_by_sid),
        "rows": upserted_rows,
        "unresolved": len(unresolved),
    }
    print(f"[price-ingest] {stats}", flush=True)
    return stats


def set_price_override(session, scryfall_id: str, finish: str, value: Any) -> None:
    """Set (or clear, when blank) the manual override for a printing + finish and
    re-denormalize the resolved price onto the Card. The override always wins and
    survives re-ingest (the ingest never touches ``manual_override``)."""
    finish = (finish or "normal").strip().lower()
    cleaned = (str(value).strip() if value is not None else "") or None

    row = (
        session.query(CardPrice)
        .filter(CardPrice.scryfall_id == scryfall_id, CardPrice.finish == finish)
        .first()
    )
    if row is None:
        if cleaned is None:
            return
        row = CardPrice(scryfall_id=scryfall_id, finish=finish)
        session.add(row)
    row.manual_override = cleaned

    card = session.query(Card).filter(Card.scryfall_id == scryfall_id).first()
    if card is not None:
        price_rows = session.query(CardPrice).filter(CardPrice.scryfall_id == scryfall_id).all()
        _denormalize_card(card, price_rows)
    session.commit()


def main() -> None:
    session = SessionLocal()
    try:
        run_ingest(session)
    finally:
        session.close()


if __name__ == "__main__":
    main()
