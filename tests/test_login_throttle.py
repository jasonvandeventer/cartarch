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
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, login_throttle
from app.db import Base
from app.login_throttle import (
    LOGIN_RATE_LIMIT_MAX,
    LOGIN_RATE_LIMIT_WINDOW,
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


# --- memory-safety: read path never leaks, store is bounded -----------------


def test_check_does_not_insert_keys():
    """is_login_throttled (the read path) must never create a tracking entry —
    otherwise an attacker probing with randomized usernames/IPs grows the store
    unbounded. Recorded against the previous defaultdict implementation."""
    for i in range(1000):
        assert is_login_throttled(f"probe{i}@example.com", f"203.0.113.{i % 256}") is False
    assert len(login_throttle._fail_log) == 0


def test_expired_keys_are_pruned_to_empty():
    """A key whose entire window has expired is removed, not left as an empty
    list — so churn through distinct keys doesn't accumulate dead entries."""
    record_failed_login("transient@example.com", "10.0.0.5")
    assert len(login_throttle._fail_log) == 2  # one user: key, one ip: key
    # Force every recorded timestamp to be older than the window.
    stale = datetime.now(UTC) - LOGIN_RATE_LIMIT_WINDOW - timedelta(seconds=1)
    for log in login_throttle._fail_log.values():
        log[:] = [stale]
    # A check prunes the now-expired windows and drops the empty keys.
    assert is_login_throttled("transient@example.com", "10.0.0.5") is False
    assert len(login_throttle._fail_log) == 0


def test_store_is_bounded_lru(monkeypatch):
    """The store enforces a hard key cap by evicting the least-recently-touched
    entry — bounding memory under a spoofed-IP / random-username flood."""
    monkeypatch.setattr(login_throttle, "LOGIN_RATE_LIMIT_MAX_KEYS", 10)
    # Record far more distinct IPs than the cap (distinct usernames too, so the
    # username keys also churn) — the store must never exceed the cap.
    for i in range(500):
        record_failed_login(f"u{i}@example.com", f"198.51.100.{i % 256}")
        assert len(login_throttle._fail_log) <= 10


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


# --- client_ip_for: spoof-resistant IP extraction ---------------------------


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    """Minimal stand-in for starlette.Request — only what client_ip_for reads."""

    def __init__(self, headers=None, peer="127.0.0.1"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = _FakeClient(peer) if peer is not None else None


def test_client_ip_prefers_cf_connecting_ip():
    """CF-Connecting-IP is Cloudflare-overwritten and unspoofable — it wins even
    when the client forges an X-Forwarded-For."""
    from app.dependencies import client_ip_for

    req = _FakeRequest(
        headers={"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
    )
    assert client_ip_for(req) == "203.0.113.7"


def test_client_ip_takes_rightmost_xff():
    """Without CF-Connecting-IP, the rightmost XFF hop (appended by the trusted
    proxy) is used — NOT the leftmost, which a client can forge to bypass the
    per-IP limit. Regression test for the spoofing defect."""
    from app.dependencies import client_ip_for

    # Attacker prepends a fake IP; the real proxy appends the true one.
    req = _FakeRequest(headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.9"})
    assert client_ip_for(req) == "198.51.100.9"


def test_client_ip_falls_back_to_peer():
    from app.dependencies import client_ip_for

    assert client_ip_for(_FakeRequest(peer="10.0.0.42")) == "10.0.0.42"
