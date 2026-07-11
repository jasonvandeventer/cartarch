"""Daily collection-value snapshot tests (issue #85).

Covers the price-only daily valuation series: the per-user upsert
(``snapshot_collection_values``), its idempotency and reconciliation with the
dashboard's Collection Value tile, and the price-ingest entrypoint wiring.
"""

from __future__ import annotations

from datetime import date

import app.jobs.price_ingest as price_ingest
from app.dashboard_service import get_dashboard_data, snapshot_collection_values
from app.models import Card, DailyCollectionValue, InventoryRow, User

_N = [0]


def _placed(db, user, price, qty=1, finish="normal", pending=False, foil_price=None):
    _N[0] += 1
    card = Card(
        scryfall_id=f"dcv-{_N[0]}",
        name=f"Card {_N[0]}",
        set_code="tst",
        collector_number=str(_N[0]),
        type_line="Artifact",
        price_usd=str(price),
        price_usd_foil=(str(foil_price) if foil_price is not None else None),
    )
    db.add(card)
    db.flush()
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=card.id,
            quantity=qty,
            finish=finish,
            is_pending=pending,
            is_proxy=False,
        )
    )
    db.flush()


def _rows(db, user):
    return db.query(DailyCollectionValue).filter(DailyCollectionValue.user_id == user.id).all()


def test_snapshot_writes_one_row_with_placed_value(db, user):
    _placed(db, user, "10.00", qty=2)  # 20.00 normal
    _placed(db, user, "5.00", qty=1, finish="foil", foil_price="8.00")  # 8.00 foil
    _placed(db, user, "100.00", qty=1, pending=True)  # excluded (pending)
    db.commit()

    n = snapshot_collection_values(db, day=date(2026, 7, 10))
    assert n == 1
    rows = _rows(db, user)
    assert len(rows) == 1
    assert rows[0].snapshot_date == date(2026, 7, 10)
    assert rows[0].total_value == 28.0  # 20 + 8, pending 100 excluded


def test_snapshot_defaults_to_today(db, user):
    _placed(db, user, "3.00")
    db.commit()
    snapshot_collection_values(db)
    (row,) = _rows(db, user)
    assert row.snapshot_date == __import__("app.timeutil", fromlist=["utc_now"]).utc_now().date()


def test_snapshot_is_idempotent_and_updates_value(db, user):
    _placed(db, user, "10.00", qty=1)
    db.commit()
    day = date(2026, 7, 10)

    snapshot_collection_values(db, day=day)
    # holdings grow, same day re-run must UPDATE the single row, not add one
    _placed(db, user, "15.00", qty=1)
    db.commit()
    snapshot_collection_values(db, day=day)

    rows = _rows(db, user)
    assert len(rows) == 1
    assert rows[0].total_value == 25.0


def test_snapshot_per_user_isolation(db, user):
    other = User(username="other85@example.com", password_hash="x")
    db.add(other)
    db.flush()
    _placed(db, user, "10.00", qty=1)
    _placed(db, other, "40.00", qty=1)
    db.commit()

    n = snapshot_collection_values(db, day=date(2026, 7, 10))
    assert n == 2
    assert _rows(db, user)[0].total_value == 10.0
    assert _rows(db, other)[0].total_value == 40.0


def test_snapshot_reconciles_with_dashboard_tile(db, user):
    _placed(db, user, "12.50", qty=3)
    _placed(db, user, "4.00", qty=1, finish="foil", foil_price="9.00")
    _placed(db, user, "99.00", qty=2, pending=True)
    db.commit()

    snapshot_collection_values(db, day=date(2026, 7, 10))
    (row,) = _rows(db, user)
    tile_value = get_dashboard_data(db, user.id)["holdings"]["placed_value"]
    assert row.total_value == tile_value


def test_price_ingest_main_calls_snapshot(monkeypatch):
    """The daily entrypoint runs the snapshot after run_ingest, and a snapshot
    failure is swallowed so it can't fail the price job."""
    calls = {"ingest": 0, "snapshot": 0}
    monkeypatch.setattr(price_ingest, "run_ingest", lambda session: calls.__setitem__("ingest", 1))
    # main() lazy-imports snapshot_collection_values from app.dashboard_service.
    import app.dashboard_service as ds

    monkeypatch.setattr(
        ds, "snapshot_collection_values", lambda session: calls.__setitem__("snapshot", 1) or 0
    )

    price_ingest.main()
    assert calls == {"ingest": 1, "snapshot": 1}


# --- Phase 2: value-over-time chart ---------------------------------------------

from app.dashboard_service import _value_history_chart  # noqa: E402


def test_chart_none_with_fewer_than_two_points():
    assert _value_history_chart([]) is None
    assert _value_history_chart([{"date": "2026-07-10", "value": 5.0}]) is None


def test_chart_geometry_and_delta():
    series = [
        {"date": "2026-07-08", "value": 100.0},
        {"date": "2026-07-09", "value": 150.0},
        {"date": "2026-07-10", "value": 120.0},
    ]
    c = _value_history_chart(series)
    assert c is not None
    assert len(c["points"].split()) == 3  # one coord per point
    assert c["days"] == 3
    assert c["min"] == 100.0 and c["max"] == 150.0
    assert c["delta"] == 20.0  # 120 - 100
    assert round(c["delta_pct"], 1) == 20.0
    # y is inverted: the max-value point sits at the top padding, the min at the
    # bottom; first & last x span the full inner width.
    xs = [float(p.split(",")[0]) for p in c["points"].split()]
    assert xs[0] == 4.0 and round(xs[-1], 1) == 316.0


def test_chart_flat_series_does_not_divide_by_zero():
    c = _value_history_chart(
        [{"date": "2026-07-09", "value": 7.0}, {"date": "2026-07-10", "value": 7.0}]
    )
    assert c is not None and c["delta"] == 0.0 and c["delta_pct"] == 0.0


def test_dashboard_holdings_exposes_chart_once_history_accrues(db, user):
    _placed(db, user, "10.00", qty=1)
    db.commit()
    snapshot_collection_values(db, day=date(2026, 7, 9))
    snapshot_collection_values(db, day=date(2026, 7, 10))
    db.commit()

    holdings = get_dashboard_data(db, user.id)["holdings"]
    assert holdings["history_deferred"] is False
    assert holdings["chart"] is not None
    assert [p["date"] for p in holdings["value_series"]] == ["2026-07-09", "2026-07-10"]


def test_dashboard_holdings_deferred_until_two_points(db, user):
    _placed(db, user, "10.00", qty=1)
    db.commit()
    snapshot_collection_values(db, day=date(2026, 7, 10))  # only one day
    db.commit()

    holdings = get_dashboard_data(db, user.id)["holdings"]
    assert holdings["history_deferred"] is True
    assert holdings["chart"] is None


def _dashboard_html(client, user):
    """GET the authenticated dashboard. ``/`` branches on
    ``get_optional_current_user`` (which the client fixture doesn't override, so
    it would otherwise render the anonymous landing page)."""
    from app import main
    from app.dependencies import get_optional_current_user

    main.app.dependency_overrides[get_optional_current_user] = lambda: user
    try:
        return client.get("/")
    finally:
        main.app.dependency_overrides.pop(get_optional_current_user, None)


def test_dashboard_page_renders_sparkline(db, user, client):
    """End-to-end: the home page draws the SVG sparkline once history exists."""
    _placed(db, user, "10.00", qty=1)
    db.commit()
    snapshot_collection_values(db, day=date(2026, 7, 9))
    snapshot_collection_values(db, day=date(2026, 7, 10))
    db.commit()

    resp = _dashboard_html(client, user)
    assert resp.status_code == 200
    assert "dashboard-holdings-chart" in resp.text
    assert "dashboard-holdings-spark" in resp.text  # the sparkline SVG


def test_dashboard_page_renders_accrual_note_without_history(db, user, client):
    # Placed inventory (so the populated dashboard + Holdings panel render) but
    # no snapshots yet → the panel shows the accrual note, not a chart.
    _placed(db, user, "10.00", qty=1)
    db.commit()

    resp = _dashboard_html(client, user)
    assert resp.status_code == 200
    assert "daily price snapshots accrue" in resp.text
    assert "dashboard-holdings-chart" not in resp.text  # no sparkline yet
