"""Login brute-force throttling (S1).

Defends the per-IP + per-username sliding-window throttle on POST /login:

  - The 6th failed attempt within the window is throttled (429).
  - A successful login resets the username counter.
  - Different IPs are tracked independently.

Service-level cases exercise app.login_throttle directly (deterministic, no
CSRF dance); the integration case drives the real /login route through the
TestClient so it would catch a wiring regression.

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_login_throttle.py
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, login_throttle
from app.db import Base
from app.login_throttle import (
    LOGIN_RATE_LIMIT_MAX,
    is_login_throttled,
    record_failed_login,
    reset_login_attempts,
)
from app.models import User

_TOKEN_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


@pytest.fixture(autouse=True)
def _clear_throttle_state():
    """Module-level counters are process-global — reset around each test so
    cases don't bleed into each other."""
    login_throttle._fail_log.clear()
    yield
    login_throttle._fail_log.clear()


def test_sixth_failure_is_throttled():
    u, ip = "victim@example.com", "10.0.0.1"
    # First MAX failures are allowed through (not throttled BEFORE they happen).
    for _ in range(LOGIN_RATE_LIMIT_MAX):
        assert is_login_throttled(u, ip) is False
        record_failed_login(u, ip)
    # The next attempt (the 6th, with MAX=5) is now throttled.
    assert is_login_throttled(u, ip) is True


def test_success_resets_username_counter():
    u, ip = "victim@example.com", "10.0.0.1"
    for _ in range(LOGIN_RATE_LIMIT_MAX):
        record_failed_login(u, ip)
    assert is_login_throttled(u, ip) is True
    reset_login_attempts(u)
    # Username counter cleared → no longer throttled by the username key.
    assert is_login_throttled(u, "10.0.0.99") is False


def test_different_ips_tracked_independently():
    u = "victim@example.com"
    # Exhaust IP-A's quota with that IP only (spread across distinct usernames
    # so the username key never trips — isolating the IP key).
    for i in range(LOGIN_RATE_LIMIT_MAX):
        record_failed_login(f"u{i}@example.com", "10.0.0.1")
    # IP-A is throttled...
    assert is_login_throttled(u, "10.0.0.1") is True
    # ...but a fresh IP for the same username is not.
    assert is_login_throttled(u, "10.0.0.2") is False


def test_case_insensitive_username_shares_counter():
    for _ in range(LOGIN_RATE_LIMIT_MAX):
        record_failed_login("Victim@Example.com", "10.0.0.1")
    # Casing variant resolves to the same key (matches login canonicalization),
    # checked from a DIFFERENT IP so only the username key can trip.
    assert is_login_throttled("victim@example.com", "10.0.0.7") is True


# --- integration: real route returns 429 ------------------------------------


def _client_and_user():
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_db_session

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    s = sm()
    s.add(User(username="alice@example.com", password_hash=auth.hash_password("pw123456")))
    s.commit()
    s.close()

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    return TestClient(main.app), main, get_db_session


def test_route_throttles_after_repeated_failures():
    client, main, get_db_session = _client_and_user()
    try:
        # Establish a live session + CSRF token so the wrong-password POSTs
        # actually reach authenticate_user (not the CSRF self-heal path).
        page = client.get("/login")
        token = _TOKEN_RE.search(page.text).group(1)

        def attempt():
            return client.post(
                "/login",
                data={"username": "alice@example.com", "password": "WRONG", "csrf_token": token},
                follow_redirects=False,
            )

        # First MAX wrong-password attempts → 200 re-render with the error.
        for _ in range(LOGIN_RATE_LIMIT_MAX):
            r = attempt()
            assert r.status_code == 200, f"expected 200 during quota, got {r.status_code}"

        # The next attempt is throttled → 429.
        r = attempt()
        assert r.status_code == 429
        assert "Too many failed login attempts" in r.text

        # A correct password is now also throttled (the gate is before auth) —
        # confirms the 429 truly precedes authentication.
        r = client.post(
            "/login",
            data={"username": "alice@example.com", "password": "pw123456", "csrf_token": token},
            follow_redirects=False,
        )
        assert r.status_code == 429
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
