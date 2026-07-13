"""Per-card daily price history (#98).

A once-a-day snapshot of every priced printing's resolved price, written by the
price ingest right after prices refresh, plus a helper to compute 1d/7d/30d
deltas for a printing+finish. Mirrors the #85 collection-value snapshot pattern
(:func:`app.dashboard_service.snapshot_collection_values`) at per-(card,finish)
grain; the series accrues forward (no backfill).
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import CardPrice, CardPriceHistory
from app.pricing import parse_price, resolve_price_value
from app.timeutil import utc_now


def snapshot_card_prices(session: Session, day: date | None = None) -> int:
    """Snapshot today's resolved price for every priced printing+finish.

    Idempotent per day (upsert on ``(scryfall_id, finish, snapshot_date)``), so a
    re-run overwrites rather than duplicates. Printings with no resolvable price
    are skipped (no null/zero history rows). Returns the count written."""
    day = day or utc_now().date()
    existing = {
        (r.scryfall_id, r.finish): r
        for r in session.query(CardPriceHistory).filter(CardPriceHistory.snapshot_date == day)
    }
    written = 0
    for row in session.query(CardPrice).all():
        raw = resolve_price_value(row)
        if raw is None:
            continue
        value = parse_price(raw)
        if value <= 0:
            continue
        hist = existing.get((row.scryfall_id, row.finish))
        if hist is None:
            session.add(
                CardPriceHistory(
                    scryfall_id=row.scryfall_id,
                    finish=row.finish,
                    snapshot_date=day,
                    price=value,
                )
            )
        else:
            hist.price = value
        written += 1
    session.commit()
    return written


def price_deltas(
    session: Session, scryfall_id: str, finish: str, windows: tuple[int, ...] = (1, 7, 30)
) -> dict[int, dict[str, float]]:
    """Percent + absolute change of a printing+finish over each window, latest
    snapshot vs the nearest row at-or-before ``latest - N days``. Windows with no
    old-enough history are omitted; ``{}`` when there's no history at all."""
    rows = (
        session.query(CardPriceHistory)
        .filter(
            CardPriceHistory.scryfall_id == scryfall_id,
            CardPriceHistory.finish == finish,
        )
        .order_by(CardPriceHistory.snapshot_date.desc())
        .all()
    )
    if not rows:
        return {}
    latest = rows[0]
    out: dict[int, dict[str, float]] = {}
    for w in windows:
        cutoff = latest.snapshot_date - timedelta(days=w)
        prior = next((r for r in rows if r.snapshot_date <= cutoff), None)
        if prior is None or not prior.price:
            continue
        out[w] = {
            "from": prior.price,
            "to": latest.price,
            "abs": round(latest.price - prior.price, 2),
            "pct": round((latest.price - prior.price) / prior.price * 100, 1),
        }
    return out
