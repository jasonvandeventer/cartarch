"""Issue #66 — the password-reset email link is built from the canonical
PUBLIC_BASE_URL, never from the per-request scheme/host.

Behind cloudflared uvicorn's per-request ``request.url.scheme`` is the internal
``http``; a request-derived reset link therefore shipped a non-TLS auth URL.
The fix:

  * ``public_base_url()``        — canonical scheme+host from PUBLIC_BASE_URL
                                    (dev fallback http://localhost:8000 ONLY under
                                    DEV_MODE=true), trailing slash normalized off.
  * ``validate_public_base_url`` — fail-loud startup check, mirrors the
                                    SESSION_SECRET_KEY / RESEND_API_KEY checks.
  * the /forgot-password handler builds the link from that origin via
                                    urlunsplit + urlencode (no double slash,
                                    token always encoded); the PATH comes from
                                    url_path_for("reset_password_page") so a
                                    route rename can't silently 404 the link.

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_reset_link_canonical.py
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, main
from app.db import Base
from app.dependencies import get_db_session
from app.main import public_base_url, validate_public_base_url
from app.models import User

_TOKEN_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


# --------------------------------------------------------------------------
# validate_public_base_url — the startup fail-loud contract (AC #3 / #4)
# Called directly: this IS the function the lifespan block invokes at boot, so
# driving it avoids standing up the daemon threads just to assert a raise.
# --------------------------------------------------------------------------


def test_reject_unset():
    """Case 1: unset/empty → raise in prod."""
    with pytest.raises(RuntimeError, match="must be set"):
        validate_public_base_url(None, is_prod=True)
    with pytest.raises(RuntimeError, match="must be set"):
        validate_public_base_url("", is_prod=True)


def test_reject_non_https():
    """Case 2: scheme != https → raise (http:// and any other scheme)."""
    with pytest.raises(RuntimeError, match="must use https"):
        validate_public_base_url("http://cartarch.com", is_prod=True)
    with pytest.raises(RuntimeError, match="must use https"):
        validate_public_base_url("ftp://cartarch.com", is_prod=True)


def test_reject_missing_host():
    """Case 3: no netloc → raise ('https:/x', 'https://')."""
    with pytest.raises(RuntimeError, match="missing a host"):
        validate_public_base_url("https:/x", is_prod=True)
    with pytest.raises(RuntimeError, match="missing a host"):
        validate_public_base_url("https://", is_prod=True)


def test_reject_path_query_fragment():
    """Case 4: anything beyond an optional trailing slash → raise."""
    for bad in (
        "https://cartarch.com/app",
        "https://cartarch.com/reset-password",
        "https://cartarch.com?x=1",
        "https://cartarch.com#frag",
        "https://cartarch.com/a/",
    ):
        with pytest.raises(RuntimeError, match="bare origin"):
            validate_public_base_url(bad, is_prod=True)


def test_accept_bare_https_origin():
    """Accept case: host, host:port, and a single trailing slash — all boot."""
    for good in (
        "https://cartarch.com",
        "https://cartarch.com/",
        "https://cartarch.com:8443",
        "https://cartarch.com:8443/",
    ):
        validate_public_base_url(good, is_prod=True)  # must NOT raise


def test_dev_skips_validation():
    """Dev (is_prod False) never raises — the localhost fallback is allowed."""
    validate_public_base_url(None, is_prod=False)
    validate_public_base_url("http://localhost:8000", is_prod=False)


# --------------------------------------------------------------------------
# public_base_url — dev fallback gating (AC #6)
# --------------------------------------------------------------------------


def test_dev_fallback_only_under_dev_mode(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("DEV_MODE", "true")
    assert public_base_url() == "http://localhost:8000"

    # Unset + not dev → fail loud rather than emit a relative/broken link.
    monkeypatch.setenv("DEV_MODE", "false")
    with pytest.raises(RuntimeError):
        public_base_url()


def test_trailing_slash_normalized(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://cartarch.com/")
    assert public_base_url() == "https://cartarch.com"


def test_public_base_url_validates_on_read_in_prod(monkeypatch):
    """Per-request guard, not just the boot gate: in prod a set-but-invalid
    PUBLIC_BASE_URL must RAISE on read — never return a relative or http://
    string that would ship a non-TLS reset link mid-request.
    """
    monkeypatch.setenv("DEV_MODE", "false")

    # set-but-non-https → raise (NOT return "http://cartarch.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://cartarch.com")
    with pytest.raises(RuntimeError, match="must use https"):
        public_base_url()

    # missing host → raise
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://")
    with pytest.raises(RuntimeError, match="missing a host"):
        public_base_url()

    # unset → raise
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="must be set"):
        public_base_url()


# --------------------------------------------------------------------------
# End-to-end: link is canonical https EVEN over internal http (AC #1 / #7)
# --------------------------------------------------------------------------


def _client_and_user():
    """TestClient over an isolated in-memory DB with one active user."""
    from fastapi.testclient import TestClient

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    s = sm()
    s.add(
        User(
            username="alice@example.com",
            password_hash=auth.hash_password("pw123456"),
            is_active=True,
        )
    )
    s.commit()
    s.close()

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    return TestClient(main.app)


def _capture_reset_url(monkeypatch, *, base_url_env, token="a b/+=&x"):
    """Drive POST /forgot-password and return the reset_url passed to the
    email queue. TestClient serves over http://testserver (internal http) —
    so a request-derived link would be http://, proving the canonical override.
    """
    monkeypatch.setenv("PUBLIC_BASE_URL", base_url_env)
    monkeypatch.setenv("DEV_MODE", "true")  # cookieless pre-auth gate passes

    # Pin the raw token so we can assert exact urlencoding of reserved chars.
    monkeypatch.setattr(main, "create_reset_token", lambda session, user: token)

    captured = {}
    monkeypatch.setattr(main, "queue_reset_email", lambda **kw: captured.update(kw))

    client = _client_and_user()
    try:
        page = client.get("/forgot-password")
        m = _TOKEN_RE.search(page.text)
        csrf = m.group(1) if m else ""
        r = client.post(
            "/forgot-password",
            data={"email": "alice@example.com", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert r.status_code == 200, f"forgot-password -> {r.status_code}"
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
    return captured.get("reset_url")


def test_reset_link_is_canonical_https_over_internal_http(monkeypatch):
    reset_url = _capture_reset_url(monkeypatch, base_url_env="https://cartarch.com")
    assert reset_url is not None, "no reset email queued"

    parts = urlsplit(reset_url)
    assert parts.scheme == "https", f"scheme not https: {reset_url!r}"
    assert parts.netloc == "cartarch.com", f"host not canonical: {reset_url!r}"

    # Path is DERIVED from the route name, not a hardcoded literal on both
    # sides — so renaming the route keeps the link correct, while a regression
    # back to a hardcoded path (decoupled from the route) fails this assert.
    expected_path = str(main.app.url_path_for("reset_password_page"))
    assert parts.path == expected_path, f"path not route-derived: {reset_url!r}"

    # token urlencoded (reserved chars escaped) and round-trips exactly.
    assert parts.query == "token=a+b%2F%2B%3D%26x"
    assert parse_qs(parts.query)["token"] == ["a b/+=&x"]
    # Exact wire form (path from the route): proves encoding, no hand-concat.
    assert reset_url == f"https://cartarch.com{expected_path}?token=a+b%2F%2B%3D%26x"

    # No double slash anywhere after the scheme separator.
    assert "//" not in reset_url[len("https://") :]


def test_reset_link_no_double_slash_with_trailing_slash_base(monkeypatch):
    """A trailing slash in PUBLIC_BASE_URL must not double-slash the link."""
    reset_url = _capture_reset_url(
        monkeypatch, base_url_env="https://cartarch.com/", token="tok123"
    )
    assert reset_url == "https://cartarch.com/reset-password?token=tok123"
