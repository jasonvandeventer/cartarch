"""Issue #63 — stateless pre-auth CSRF for the four public forms.

The guard (`require_preauth_csrf`) validates an Origin/Referer cross-site gate +
a server-signed, 1h-fresh token, WITHOUT depending on the session cookie
surviving the GET->POST round trip (the FxiOS/Opera-Touch failure: they drop our
SameSite=Lax `session` cookie on the POST). Exercises the real route ->
SessionMiddleware path with a real cookie jar, like tests/test_auth_csrf.py.

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_preauth_csrf.py
"""

from __future__ import annotations

import re
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth
from app.db import Base
from app.models import User

_TOKEN_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def _csrf_token(html: str) -> str | None:
    m = _TOKEN_RE.search(html)
    return m.group(1) if m else None


def _client_and_user():
    """TestClient over an isolated in-memory DB with one user; real cookie jar."""
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


def _login(client, token, **kw):
    return client.post(
        "/login",
        data={"username": "alice@example.com", "password": "pw123456", "csrf_token": token},
        follow_redirects=False,
        **kw,
    )


def test_cookieless_valid_token_succeeds():
    """AC1 (the core fix): a valid signed token with NO session cookie logs in.

    This is the FxiOS/OPT case — the browser never sends our cookie back, yet the
    stateless token validates on its signature alone, so the POST succeeds (303)
    instead of dead-ending on a 403/re-render."""
    from fastapi.testclient import TestClient

    client, main, get_db_session = _client_and_user()
    try:
        # Mint a real token from a GET, then submit from a SEPARATE empty cookie jar.
        token = _csrf_token(TestClient(main.app).get("/login").text)
        assert token, "GET /login must embed a stateless csrf_token"
        nocookie = TestClient(main.app)  # empty jar — no session cookie at all
        r = _login(nocookie, token)
        assert r.status_code == 303, f"cookie-less valid token -> {r.status_code} (want 303)"
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)


def test_tampered_token_403():
    """AC2: a tampered (bad-signature) token -> 403."""
    from fastapi.testclient import TestClient

    client, main, get_db_session = _client_and_user()
    try:
        token = _csrf_token(TestClient(main.app).get("/login").text)
        tampered = token[:-3] + ("aaa" if token[-3:] != "aaa" else "bbb")
        r = _login(TestClient(main.app), tampered)
        assert r.status_code == 403, f"tampered token -> {r.status_code} (want 403)"
        # A totally missing token is likewise rejected.
        r2 = _login(TestClient(main.app), "")
        assert r2.status_code == 403, f"empty token -> {r2.status_code} (want 403)"
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)


def test_expired_token_friendly_refresh_not_403():
    """AC3: an expired (but validly signed) token -> friendly re-render, NOT 403,
    NOT the cookie 'session expired' copy."""
    from fastapi.testclient import TestClient

    from app import dependencies

    client, main, get_db_session = _client_and_user()
    orig = dependencies.PREAUTH_CSRF_MAX_AGE
    try:
        token = _csrf_token(TestClient(main.app).get("/login").text)
        time.sleep(1)
        dependencies.PREAUTH_CSRF_MAX_AGE = 0  # the 1s-old token now reads as expired
        r = _login(TestClient(main.app), token)
        assert r.status_code == 200, f"expired token -> {r.status_code} (want 200 re-render)"
        assert "expired" in r.text.lower()
        assert "session expired" not in r.text.lower(), "must not use the cookie copy"
    finally:
        dependencies.PREAUTH_CSRF_MAX_AGE = orig
        main.app.dependency_overrides.pop(get_db_session, None)


def test_cross_origin_rejected():
    """AC4: a valid token with a cross-origin Origin header -> 403 (login CSRF)."""
    from fastapi.testclient import TestClient

    client, main, get_db_session = _client_and_user()
    try:
        token = _csrf_token(TestClient(main.app).get("/login").text)
        r = _login(TestClient(main.app), token, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, f"cross-origin Origin -> {r.status_code} (want 403)"
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)


def test_headerless_degraded_path_accepted():
    """AC5: a valid token with NEITHER Origin nor Referer -> accepted (degraded
    path). The same privacy browsers that drop the cookie still send Origin on a
    same-origin POST, so in practice they hit gate (a); this pins the fallback."""
    from fastapi.testclient import TestClient

    client, main, get_db_session = _client_and_user()
    try:
        token = _csrf_token(TestClient(main.app).get("/login").text)
        # TestClient sends no Origin/Referer by default -> headerless branch.
        r = _login(TestClient(main.app), token)
        assert r.status_code == 303, f"headerless valid token -> {r.status_code} (want 303)"
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)


def test_same_origin_header_accepted():
    """A same-origin Origin header is accepted (gate (a) happy path)."""
    from fastapi.testclient import TestClient

    client, main, get_db_session = _client_and_user()
    try:
        token = _csrf_token(TestClient(main.app).get("/login").text)
        # TestClient's base_url host is 'testserver'; in DEV_MODE http is allowed.
        r = _login(TestClient(main.app), token, headers={"Origin": "http://testserver"})
        assert r.status_code == 303, f"same-origin Origin -> {r.status_code} (want 303)"
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)


def _fake_request(headers: dict, host: str = "cartarch.com"):
    """Minimal Starlette Request with a controllable Host + Origin/Referer, for
    unit-testing _preauth_cross_site_ok without a live server."""
    from starlette.requests import Request

    raw = [(b"host", host.encode())]
    for k, v in headers.items():
        raw.append((k.lower().encode(), v.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "query_string": b"",
            "scheme": "http",
            "server": (host, 443),
            "headers": raw,
        }
    )


def test_rerender_token_is_a_valid_signed_token():
    """REGRESSION GUARD (the critical bug the forked review caught): an error
    re-render must embed a fresh SIGNED token, not the session hex token. Before
    the fix, a single mistyped password re-rendered with get_csrf_token's hex,
    which the stateless POST guard then rejected as BadData -> 403, locking EVERY
    user (not just privacy browsers) out of the retry. Pins login + register +
    reset re-render paths."""
    from fastapi.testclient import TestClient

    client, main, get_db_session = _client_and_user()
    try:
        # login: wrong password -> re-render -> resubmit correct with that token.
        c = TestClient(main.app)
        good = _csrf_token(c.get("/login").text)
        rr = c.post(
            "/login",
            data={"username": "alice@example.com", "password": "WRONG", "csrf_token": good},
            follow_redirects=False,
        )
        assert rr.status_code == 200
        rtok = _csrf_token(rr.text)
        assert rtok and "." in rtok, f"login re-render token not signed: {rtok!r}"
        ok = _login(c, rtok)
        assert ok.status_code == 303, f"resubmit after login typo -> {ok.status_code} (want 303)"

        # register: weak password -> re-render -> resubmit strong with that token.
        rc = TestClient(main.app)
        rtok0 = _csrf_token(rc.get("/register").text)
        rr2 = rc.post(
            "/register",
            data={"username": "bob@example.com", "password": "weak", "csrf_token": rtok0},
            follow_redirects=False,
        )
        assert rr2.status_code == 200  # weak-password re-render
        rtok2 = _csrf_token(rr2.text)
        assert rtok2 and "." in rtok2, f"register re-render token not signed: {rtok2!r}"
        ok2 = rc.post(
            "/register",
            data={"username": "bob@example.com", "password": "Str0ng!pass99", "csrf_token": rtok2},
            follow_redirects=False,
        )
        assert ok2.status_code == 303, f"resubmit after weak pw -> {ok2.status_code} (want 303)"
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)


def test_all_four_forms_reach_past_guard_cookieless():
    """AC1 across ALL four forms (not just /login): a cookie-less valid signed
    token is never CSRF-403'd. Each form's GET must embed a stateless token and
    each POST must call the stateless guard."""
    from fastapi.testclient import TestClient

    from app import auth as auth_mod
    from app import main
    from app.db import Base
    from app.dependencies import get_db_session
    from app.models import User
    from app.password_reset_service import create_reset_token

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    s = sm()
    u = User(username="alice@example.com", password_hash=auth_mod.hash_password("pw123456"))
    s.add(u)
    s.commit()
    s.close()

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    try:
        # login -> 303
        c = TestClient(main.app)
        r = c.post(
            "/login",
            data={
                "username": "alice@example.com",
                "password": "pw123456",
                "csrf_token": _csrf_token(c.get("/login").text),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, f"login cookie-less -> {r.status_code}"

        # register (fresh email) -> 303
        c = TestClient(main.app)
        r = c.post(
            "/register",
            data={
                "username": "carol@example.com",
                "password": "Str0ng!pass99",
                "csrf_token": _csrf_token(c.get("/register").text),
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, f"register cookie-less -> {r.status_code}"

        # forgot -> 200 neutral (NOT 403)
        c = TestClient(main.app)
        r = c.post(
            "/forgot-password",
            data={
                "email": "alice@example.com",
                "csrf_token": _csrf_token(c.get("/forgot-password").text),
            },
            follow_redirects=False,
        )
        assert r.status_code == 200, f"forgot cookie-less -> {r.status_code}"

        # reset (valid reset token + matching strong passwords) -> 200 done (NOT 403).
        # Mint the token HERE: forgot-password above invalidated any earlier one
        # (one active reset token per user).
        sr = sm()
        reset_raw = create_reset_token(
            sr, sr.query(User).filter_by(username="alice@example.com").one()
        )
        sr.commit()
        sr.close()
        c = TestClient(main.app)
        page = c.get(f"/reset-password?token={reset_raw}")
        r = c.post(
            "/reset-password",
            data={
                "token": reset_raw,
                "password": "Str0ng!pass99",
                "password_confirm": "Str0ng!pass99",
                "csrf_token": _csrf_token(page.text),
            },
            follow_redirects=False,
        )
        assert r.status_code == 200, f"reset cookie-less -> {r.status_code}"
        assert "403" not in r.text
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)


def test_referer_only_branch():
    """AC's gate (a) third case: Origin absent but Referer present. Same-host
    Referer -> accept; foreign Referer -> reject. (The AC explicitly asks each of
    the three header cases be pinned.)"""
    from fastapi.testclient import TestClient

    client, main, get_db_session = _client_and_user()
    try:
        token = _csrf_token(TestClient(main.app).get("/login").text)
        # Same-host Referer, no Origin -> accepted (303). DEV_MODE=true allows http.
        r_ok = _login(TestClient(main.app), token, headers={"Referer": "http://testserver/login"})
        assert r_ok.status_code == 303, f"same-host Referer -> {r_ok.status_code} (want 303)"
        # Foreign Referer, no Origin -> rejected (403).
        token2 = _csrf_token(TestClient(main.app).get("/login").text)
        r_bad = _login(TestClient(main.app), token2, headers={"Referer": "http://evil.example/x"})
        assert r_bad.status_code == 403, f"foreign Referer -> {r_bad.status_code} (want 403)"
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)


def test_cross_site_gate_prod_scheme_and_case(monkeypatch):
    """Unit-pin the prod posture (the whole suite otherwise runs DEV_MODE=true):
    with DEV_MODE off, an http:// same-host Origin is rejected (https-only), https
    is accepted, host compare is case-insensitive, foreign host rejected, and the
    Referer fallback enforces the same scheme rule as Origin."""
    from app.dependencies import _preauth_cross_site_ok

    monkeypatch.setenv("DEV_MODE", "false")
    ok = _preauth_cross_site_ok
    assert ok(_fake_request({"origin": "https://cartarch.com"})) is True
    assert ok(_fake_request({"origin": "http://cartarch.com"})) is False  # https-only in prod
    assert ok(_fake_request({"origin": "https://CARTARCH.com"})) is True  # case-insensitive host
    assert ok(_fake_request({"origin": "https://evil.com"})) is False  # foreign host
    assert ok(_fake_request({"origin": "null"})) is False  # suppressed Origin -> empty netloc
    # Referer fallback now enforces scheme too (was host-only): http Referer in prod -> reject.
    assert ok(_fake_request({"referer": "http://cartarch.com/login"})) is False
    assert ok(_fake_request({"referer": "https://cartarch.com/login"})) is True
    assert ok(_fake_request({})) is True  # both absent -> degraded accept


def test_authenticated_csrf_unchanged():
    """AC6 (regression): the session-bound guard for authenticated, state-changing
    endpoints (require_csrf_token / CsrfRequired) is untouched — it still 403s a
    request whose session carries no token."""
    import pytest
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.dependencies import CsrfRequired, require_csrf_token

    # CsrfRequired is still wired to require_csrf_token.
    assert CsrfRequired.dependency is require_csrf_token

    # A request with an empty session is rejected (no stateless softening here).
    scope = {"type": "http", "headers": [], "session": {}}
    req = Request(scope)
    with pytest.raises(HTTPException) as ei:
        require_csrf_token(req, csrf_token="anything")
    assert ei.value.status_code == 403
