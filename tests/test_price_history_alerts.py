"""#98 per-card daily price history + #99 watchlist price alerts."""

from __future__ import annotations

import itertools
from datetime import date, timedelta

from app.jobs import price_alerts, price_ingest
from app.models import Card, CardPrice, CardPriceHistory, WatchlistItem
from app.price_history_service import price_deltas, snapshot_card_prices

_seq = itertools.count(1)


def _card(db, sid, name, price_usd=None):
    c = Card(
        scryfall_id=sid,
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        price_usd=price_usd,
    )
    db.add(c)
    db.flush()
    return c


def _cardprice(db, sid, finish, tcg):
    db.add(CardPrice(scryfall_id=sid, finish=finish, tcgplayer_retail=tcg))
    db.flush()


# ── #98 snapshot writer ──────────────────────────────────────────────────────


def test_snapshot_upserts_and_skips_unpriced(db):
    _cardprice(db, "sid-a", "normal", "1.50")
    _cardprice(db, "sid-b", "normal", None)  # no resolvable price → skipped
    _cardprice(db, "sid-c", "foil", "0")  # zero → skipped
    assert snapshot_card_prices(db, day=date(2026, 7, 10)) == 1
    rows = db.query(CardPriceHistory).all()
    assert len(rows) == 1 and rows[0].scryfall_id == "sid-a" and rows[0].price == 1.5

    # Same-day re-run overwrites in place — idempotent, no duplicate row.
    db.query(CardPrice).filter_by(scryfall_id="sid-a").one().tcgplayer_retail = "2.00"
    db.flush()
    assert snapshot_card_prices(db, day=date(2026, 7, 10)) == 1
    rows = db.query(CardPriceHistory).all()
    assert len(rows) == 1 and rows[0].price == 2.0


# ── #98 deltas ───────────────────────────────────────────────────────────────


def test_price_deltas_windows(db):
    base = date(2026, 7, 10)
    for d, p in [
        (base - timedelta(days=30), 10.0),
        (base - timedelta(days=7), 8.0),
        (base - timedelta(days=1), 9.0),
        (base, 12.0),
    ]:
        db.add(CardPriceHistory(scryfall_id="sid-x", finish="normal", snapshot_date=d, price=p))
    db.flush()
    out = price_deltas(db, "sid-x", "normal")
    assert out[1]["pct"] == round((12 - 9) / 9 * 100, 1)
    assert out[7]["pct"] == round((12 - 8) / 8 * 100, 1)
    assert out[30]["pct"] == round((12 - 10) / 10 * 100, 1)
    assert out[30]["abs"] == 2.0
    assert price_deltas(db, "missing", "normal") == {}


def test_price_sparkline(db):
    from app.price_history_service import price_sparkline

    base = date(2026, 7, 10)
    # One point → no trend → None.
    db.add(CardPriceHistory(scryfall_id="sid-s", finish="normal", snapshot_date=base, price=5.0))
    db.flush()
    assert price_sparkline(db, "sid-s", "normal") is None

    for d, p in [(base + timedelta(days=1), 6.0), (base + timedelta(days=2), 9.0)]:
        db.add(CardPriceHistory(scryfall_id="sid-s", finish="normal", snapshot_date=d, price=p))
    db.flush()
    sp = price_sparkline(db, "sid-s", "normal")
    assert sp is not None
    assert sp["days"] == 3
    assert len(sp["points"].split(" ")) == 3  # one coord per day, oldest→newest
    assert sp["min"] == 5.0 and sp["max"] == 9.0
    assert sp["delta"] == 4.0  # 5.0 → 9.0
    assert sp["delta_pct"] == 80.0
    assert price_sparkline(db, "missing", "normal") is None


# ── #99 alerts ───────────────────────────────────────────────────────────────


def _watch(db, user, card, target):
    w = WatchlistItem(user_id=user.id, card_id=card.id, target_price=target)
    db.add(w)
    db.flush()
    return w


def test_alert_fires_once_then_rearms_above_target(db, user, monkeypatch):
    user.price_alerts_enabled = True
    c = _card(db, "sid-w", "Sol Ring", price_usd="5.00")  # 5 <= 10 → target met
    w = _watch(db, user, c, target=10.0)
    db.commit()
    sent = []
    monkeypatch.setattr(
        price_alerts, "send_email", lambda to, subject, text: sent.append((to, text)) or True
    )

    assert price_alerts.run_alerts(db) == 1  # fires
    assert len(sent) == 1 and sent[0][0] == user.username and "Sol Ring" in sent[0][1]
    db.refresh(w)
    assert w.last_alerted_at is not None and w.last_alerted_price == 5.0

    assert price_alerts.run_alerts(db) == 0  # still met → no re-fire
    assert len(sent) == 1

    c.price_usd = "15.00"  # above target → re-arm
    db.flush()
    assert price_alerts.run_alerts(db) == 0
    db.refresh(w)
    assert w.last_alerted_at is None

    c.price_usd = "5.00"  # back below → fires again
    db.flush()
    assert price_alerts.run_alerts(db) == 1
    assert len(sent) == 2


def test_alert_respects_opt_out(db, user, monkeypatch):
    user.price_alerts_enabled = False  # opted out
    c = _card(db, "sid-o", "Mana Crypt", price_usd="1.00")
    _watch(db, user, c, target=100.0)  # would be met
    db.commit()
    sent = []
    monkeypatch.setattr(
        price_alerts, "send_email", lambda to, subject, text: sent.append(to) or True
    )
    assert price_alerts.run_alerts(db) == 0
    assert sent == []


def test_failed_send_does_not_consume_the_crossing(db, user, monkeypatch):
    user.price_alerts_enabled = True
    c = _card(db, "sid-f", "Rhystic Study", price_usd="20.00")
    w = _watch(db, user, c, target=30.0)  # met
    db.commit()
    monkeypatch.setattr(
        price_alerts, "send_email", lambda to, subject, text: False
    )  # provider down
    assert price_alerts.run_alerts(db) == 0
    db.refresh(w)
    assert w.last_alerted_at is None  # not stamped → retried next run


# ── ingest main() piggybacks both new hooks ──────────────────────────────────


def test_price_ingest_main_calls_history_and_alerts(monkeypatch):
    calls = {"ingest": 0, "snapshot": 0, "history": 0, "alerts": 0}
    monkeypatch.setattr(price_ingest, "run_ingest", lambda session: calls.__setitem__("ingest", 1))
    import app.dashboard_service as ds

    monkeypatch.setattr(
        ds, "snapshot_collection_values", lambda session: calls.__setitem__("snapshot", 1) or 0
    )
    import app.price_history_service as phs

    monkeypatch.setattr(
        phs, "snapshot_card_prices", lambda session: calls.__setitem__("history", 1) or 0
    )
    monkeypatch.setattr(
        price_alerts, "run_alerts", lambda session: calls.__setitem__("alerts", 1) or 0
    )
    price_ingest.main()
    assert calls == {"ingest": 1, "snapshot": 1, "history": 1, "alerts": 1}
