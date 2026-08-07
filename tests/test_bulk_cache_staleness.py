"""The catalog's age is VISIBLE, because a broken ingest looks like a healthy one.

Scryfall changed the bulk export format on 2026-07-28; the loop guarded on the
missing key, logged one calm line and returned 0, every 24h for ten days. Every
page still rendered. It was found by accident, chasing an unrelated empty column
(v4.13.14). The fix for the format is not the fix for the blindness.
"""

from datetime import timedelta

import app.legacy_tables  # noqa
import app.scryfall as sc
from app.legacy_tables import scryfall_bulk_meta
from app.timeutil import utc_now


def _set_meta(db, value):
    db.execute(scryfall_bulk_meta.delete())
    if value is not None:
        db.execute(scryfall_bulk_meta.insert().values(key=sc._BULK_META_KEY, value=value))
    db.commit()


def _status(db, monkeypatch, value):
    _set_meta(db, value)
    monkeypatch.setattr(sc, "engine", db.get_bind())
    return sc.bulk_cache_status()


def test_a_fresh_cache_is_not_stale(db, monkeypatch):
    fresh = (utc_now() - timedelta(hours=6)).isoformat()
    status = _status(db, monkeypatch, fresh)
    assert status["is_stale"] is False
    assert status["age_days"] < 1


def test_one_missed_cycle_is_tolerated(db, monkeypatch):
    """Scryfall rebuilds ~daily and the loop polls every 24h, so ~1 day behind is
    normal operation. Alerting on it would train the reader to ignore it."""
    status = _status(db, monkeypatch, (utc_now() - timedelta(days=1, hours=12)).isoformat())
    assert status["is_stale"] is False


def test_the_real_outage_reads_as_stale(db, monkeypatch):
    """The actual stamp prod was frozen at, against the day it was found."""
    status = _status(db, monkeypatch, "2026-07-28T09:09:18.622+00:00")
    assert status["is_stale"] is True
    assert status["age_days"] > 2
    assert "refresh is not running" in status["detail"]


def test_an_unpopulated_cache_is_stale_not_unknown(db, monkeypatch):
    """No data is not reassurance. A missing row must not read as healthy."""
    status = _status(db, monkeypatch, None)
    assert status["is_stale"] is True
    assert status["age_days"] is None
    assert status["detail"] == "never populated"


def test_an_unparseable_stamp_is_stale(db, monkeypatch):
    status = _status(db, monkeypatch, "not-a-timestamp")
    assert status["is_stale"] is True
    assert status["age_days"] is None


def test_a_naive_stamp_does_not_explode(db, monkeypatch):
    """Older rows may carry no timezone; comparing naive to aware raises."""
    naive = (utc_now() - timedelta(days=5)).replace(tzinfo=None).isoformat()
    status = _status(db, monkeypatch, naive)
    assert status["is_stale"] is True


def _as_admin(db, user):
    """`require_admin` reads is_admin off the same object the `client` fixture
    pins as the current user, so promoting it is all that is needed."""
    user.is_admin = True
    db.commit()


def test_the_admin_page_shows_the_warning(client, db, user, monkeypatch):
    """A service-level reading nobody renders is the same blindness in a new
    place — the #152 failure mode. Drive the PAGE."""
    _as_admin(db, user)
    monkeypatch.setattr(sc, "engine", db.get_bind())
    _set_meta(db, "2026-07-28T09:09:18.622+00:00")
    html = client.get("/admin").text
    assert "Card data age" in html
    assert "Card catalog is stale" in html


def test_the_admin_page_is_quiet_when_fresh(client, db, user, monkeypatch):
    """A panel that shouts on every visit is one people stop reading (#176)."""
    _as_admin(db, user)
    monkeypatch.setattr(sc, "engine", db.get_bind())
    _set_meta(db, (utc_now() - timedelta(hours=3)).isoformat())
    html = client.get("/admin").text
    assert "Card catalog is stale" not in html
    assert "Card catalog refreshed" in html
