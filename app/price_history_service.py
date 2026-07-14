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


# Card-detail price sparkline geometry (inline SVG, no chart lib — mirrors the
# #85 dashboard value chart). y is inverted so a higher price sits higher.
_SPARK_W, _SPARK_H, _SPARK_PAD = 240, 44, 3


def price_sparkline(session: Session, scryfall_id: str, finish: str, days: int = 90) -> dict | None:
    """Inline-SVG sparkline for a printing+finish's price over the last ``days``.
    Returns ``None`` with fewer than 2 points (one dot isn't a trend — the card
    page keeps its "history is still accruing" note until a second day lands)."""
    rows = (
        session.query(CardPriceHistory)
        .filter(
            CardPriceHistory.scryfall_id == scryfall_id,
            CardPriceHistory.finish == finish,
        )
        .order_by(CardPriceHistory.snapshot_date.desc())
        .limit(days)
        .all()
    )
    rows = list(reversed(rows))  # oldest → newest for the left-to-right line
    if len(rows) < 2:
        return None
    values = [float(r.price) for r in rows]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0  # flat series → a centered horizontal line
    inner_w = _SPARK_W - 2 * _SPARK_PAD
    inner_h = _SPARK_H - 2 * _SPARK_PAD
    last_i = len(values) - 1
    coords = [
        f"{_SPARK_PAD + inner_w * (i / last_i):.1f},"
        f"{_SPARK_PAD + inner_h * (1 - (v - lo) / span):.1f}"
        for i, v in enumerate(values)
    ]
    first, last = values[0], values[-1]
    delta = round(last - first, 2)
    return {
        "points": " ".join(coords),
        "width": _SPARK_W,
        "height": _SPARK_H,
        "min": round(lo, 2),
        "max": round(hi, 2),
        "days": len(values),
        "delta": delta,
        "delta_pct": round(delta / first * 100, 1) if first else 0.0,
    }
