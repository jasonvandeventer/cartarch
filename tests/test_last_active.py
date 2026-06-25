"""last_active_at — per-user last-authenticated-request stamp.

Pins the throttled, isolated, best-effort write performed by
``_stamp_last_active`` (called from ``get_current_user`` /
``get_optional_current_user``), and that it is distinct from
``last_signed_in_at``.

The write runs in its OWN ``SessionLocal()`` (isolated transaction), so these
tests monkeypatch ``app.dependencies.SessionLocal`` to bind to the per-test
engine, and monkeypatch ``app.dependencies.utc_now`` to a controllable clock so
the 5-minute throttle window can be crossed deterministically (no real sleeping
— a value-only assertion would not prove the throttle).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app import dependencies
from app.dependencies import LAST_ACTIVE_THROTTLE, _stamp_last_active
from app.models import User


@pytest.fixture
def isolated_sessionmaker(db_engine, monkeypatch):
    """Point the module-global ``SessionLocal`` (used by the isolated write) at
    the per-test engine, and return a factory for seeding/reading the same DB."""
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(dependencies, "SessionLocal", factory)
    return factory


@pytest.fixture
def clock(monkeypatch):
    """A controllable ``utc_now`` for app.dependencies. Mutate ``clock.now``."""

    class Clock:
        now = datetime(2026, 6, 25, 12, 0, 0)

    c = Clock()
    monkeypatch.setattr(dependencies, "utc_now", lambda: c.now)
    return c


def _seed_user(factory) -> int:
    s = factory()
    try:
        u = User(username="active@example.com", password_hash="x")
        s.add(u)
        s.commit()
        return u.id
    finally:
        s.close()


def _load(factory, user_id) -> User:
    """Fresh load — mirrors how each request re-resolves the user, so the
    throttle reads the PERSISTED value, not a stale in-memory one."""
    s = factory()
    try:
        return s.query(User).filter(User.id == user_id).one()
    finally:
        s.close()


def test_first_request_stamps(isolated_sessionmaker, clock):
    uid = _seed_user(isolated_sessionmaker)
    assert _load(isolated_sessionmaker, uid).last_active_at is None

    _stamp_last_active(_load(isolated_sessionmaker, uid))

    assert _load(isolated_sessionmaker, uid).last_active_at == clock.now


def test_throttled_within_window_does_not_write(isolated_sessionmaker, clock):
    uid = _seed_user(isolated_sessionmaker)
    t0 = clock.now
    _stamp_last_active(_load(isolated_sessionmaker, uid))
    assert _load(isolated_sessionmaker, uid).last_active_at == t0

    # A second request still inside the window must NOT advance the stamp.
    clock.now = t0 + (LAST_ACTIVE_THROTTLE - timedelta(seconds=1))
    _stamp_last_active(_load(isolated_sessionmaker, uid))
    assert _load(isolated_sessionmaker, uid).last_active_at == t0


def test_writes_again_after_window(isolated_sessionmaker, clock):
    uid = _seed_user(isolated_sessionmaker)
    t0 = clock.now
    _stamp_last_active(_load(isolated_sessionmaker, uid))
    assert _load(isolated_sessionmaker, uid).last_active_at == t0

    # Past the window — the stamp advances.
    t1 = t0 + LAST_ACTIVE_THROTTLE
    clock.now = t1
    _stamp_last_active(_load(isolated_sessionmaker, uid))
    assert _load(isolated_sessionmaker, uid).last_active_at == t1


def test_write_failure_is_swallowed(isolated_sessionmaker, clock, monkeypatch):
    """A forced error in the stamp-write path is caught and logged, not
    propagated — the authenticated request must still succeed."""
    uid = _seed_user(isolated_sessionmaker)

    def _boom():
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(dependencies, "SessionLocal", _boom)

    # Must not raise (best-effort) and must leave the persisted value untouched
    # — the isolated write never reached commit.
    _stamp_last_active(_load(isolated_sessionmaker, uid))
    assert _load(isolated_sessionmaker, uid).last_active_at is None


def test_last_signed_in_at_untouched(isolated_sessionmaker, clock):
    """last_active_at is independent of last_signed_in_at — the stamp must not
    read, write, or backfill the sign-in column."""
    s = isolated_sessionmaker()
    try:
        u = User(
            username="both@example.com",
            password_hash="x",
            last_signed_in_at=datetime(2020, 1, 1, 0, 0, 0),
        )
        s.add(u)
        s.commit()
        uid = u.id
    finally:
        s.close()

    _stamp_last_active(_load(isolated_sessionmaker, uid))

    reloaded = _load(isolated_sessionmaker, uid)
    assert reloaded.last_active_at == clock.now
    assert reloaded.last_signed_in_at == datetime(2020, 1, 1, 0, 0, 0)
