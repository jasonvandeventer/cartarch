"""Issue #130 — timezone-aware UTC + timestamptz.

Pins the flip: utc_now() is aware; UTCDateTime columns round-trip aware in both
Postgres (prod) and SQLite (this suite, where the type re-attaches UTC on read);
the Central display filter converts correctly across the DST boundary; analytics
duration math holds on round-tripped aware events; and the migration is
Postgres-only + leaves the one pre-existing timestamptz column alone.
"""

from __future__ import annotations

import importlib.util
import itertools
import pathlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.legacy_tables  # noqa: F401 — registers deck_bracket_* tables standalone
from app.db import Base
from app.dependencies import format_local_datetime
from app.models import Game, GameEvent, User, UTCDateTime
from app.timeutil import utc_now

_seq = itertools.count(1)


def _fresh():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _user(s, **kw):
    u = User(username=f"u{next(_seq)}", password_hash="x", **kw)
    s.add(u)
    s.commit()
    s.expire_all()
    return s.query(User).filter(User.id == u.id).one()


# --------------------------------------------------------------------------- #
# utc_now + round-trip
# --------------------------------------------------------------------------- #


def test_utc_now_is_aware_utc():
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_model_roundtrip_preserves_awareness():
    s = _fresh()()
    u = _user(s)  # created_at via default=utc_now
    assert u.created_at.tzinfo is not None
    assert u.created_at.utcoffset() == timedelta(0)


def test_explicit_aware_value_roundtrips_unchanged():
    s = _fresh()()
    u = _user(s, last_signed_in_at=datetime(2026, 1, 15, 18, 30, tzinfo=UTC))
    assert u.last_signed_in_at == datetime(2026, 1, 15, 18, 30, tzinfo=UTC)
    assert u.last_signed_in_at.tzinfo is not None


def test_naive_literal_is_bind_normalized_to_utc():
    # A stray naive value bound into a UTCDateTime column gets UTC attached, so it
    # reads back aware — no naive/aware split can survive the type boundary.
    s = _fresh()()
    u = _user(s, last_signed_in_at=datetime(2026, 1, 15, 18, 30))  # naive in
    assert u.last_signed_in_at == datetime(2026, 1, 15, 18, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Central display filter — across the DST boundary
# --------------------------------------------------------------------------- #


def test_display_filter_cdt_summer():
    # 2026-07-15 18:00 UTC = 13:00 CDT (UTC-5, daylight saving)
    dt = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
    assert format_local_datetime(dt, "%Y-%m-%d %H:%M") == "2026-07-15 13:00"


def test_display_filter_cst_winter():
    # 2026-01-15 18:00 UTC = 12:00 CST (UTC-6, standard time)
    dt = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
    assert format_local_datetime(dt, "%Y-%m-%d %H:%M") == "2026-01-15 12:00"


def test_display_filter_still_accepts_naive_input():
    # Legacy naive input is treated as UTC (the tzinfo-is-None guard is preserved).
    assert format_local_datetime(datetime(2026, 7, 15, 18, 0), "%H:%M") == "13:00"


# --------------------------------------------------------------------------- #
# Analytics replay — duration math on round-tripped aware game_events
# --------------------------------------------------------------------------- #


def test_analytics_duration_on_roundtripped_aware_events():
    # Mirrors game_analytics_service's pace math:
    # (turn_marks[i+1].created_at - turn_marks[i].created_at).total_seconds()
    s = _fresh()()
    g = Game()
    s.add(g)
    s.commit()
    marks = [
        datetime(2026, 7, 12, 16, 6, 0, tzinfo=UTC),
        datetime(2026, 7, 12, 16, 12, 30, tzinfo=UTC),
    ]
    for t in marks:
        s.add(
            GameEvent(
                game_id=g.id,
                action_type="turn",
                payload="{}",
                turn=1,
                actor_kind="table",
                created_at=t,
            )
        )
    s.commit()
    s.expire_all()
    evs = s.query(GameEvent).order_by(GameEvent.created_at.asc()).all()
    assert all(e.created_at.tzinfo is not None for e in evs)  # aware after round-trip
    assert (evs[1].created_at - evs[0].created_at).total_seconds() == 390  # no TypeError


# --------------------------------------------------------------------------- #
# Model ↔ migration agreement + migration guard
# --------------------------------------------------------------------------- #


def test_all_datetime_columns_are_timezone_aware():
    """Every datetime column is tz-aware (timestamptz) so a future autogenerate is
    an empty diff. Catches any new naive `DateTime` column added later."""
    offenders = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            t = col.type
            if not isinstance(t, (DateTime, UTCDateTime)):
                continue
            impl = getattr(t, "impl", t)  # UTCDateTime → its DateTime(timezone=True)
            if getattr(impl, "timezone", False) is not True:
                offenders.append(f"{table.name}.{col.name}")
    assert offenders == [], f"naive datetime columns remain: {offenders}"


def test_migration_is_postgres_only_and_knows_preexisting_tz(monkeypatch):
    mig_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "e1f2a3b4c5d6_issue_130_timestamptz.py"
    )
    spec = importlib.util.spec_from_file_location("mig_130", mig_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The one column that was already timestamptz (#27) is excluded from downgrade
    # and never selected by upgrade (data_type filter) — the idempotent skip.
    assert ("deck_card_shares", "created_at") in mod._PREEXISTING_TZ

    class _FakeDialect:
        name = "sqlite"

    class _FakeBind:
        dialect = _FakeDialect()

    class _FakeOp:
        def get_bind(self):
            return _FakeBind()

        def execute(self, *a, **k):  # would raise if the guard let us reach it
            raise AssertionError("migration must not emit DDL on a non-postgres bind")

    monkeypatch.setattr(mod, "op", _FakeOp())
    mod.upgrade()  # guard returns before any information_schema/ALTER
    mod.downgrade()
