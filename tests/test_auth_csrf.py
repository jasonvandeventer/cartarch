"""Regression tests for the v3.31.0 "Invalid CSRF token" sign-in failure.

Background: v3.31.0 deployed and users hit ``POST /login -> 403 Invalid CSRF
token``. The request/response auth code was byte-identical to the known-good
v3.30.x line; the failure is a *cookie-continuity* one — a logged-out browser
reaches POST /login carrying no usable session cookie (a stale/expired cookie
the server now drops as an empty session, a cookie scoped to the pre-cutover
host, or a login page served from an edge cache without its per-user
Set-Cookie). Strict double-submit then has no session token to match and
hard-fails, and GET /login alone can't repair a cookie the browser won't
replace — so the user is stuck on a permanent 403.

The fix (``require_csrf_or_reissue``) makes the public pre-auth forms self-heal:
when the session carries no token at all, re-render with a freshly issued
token + cookie instead of 403, so the immediate resubmit succeeds. A genuine
token mismatch against a live session still hard-fails with 403.

Exercises the full route -> SessionMiddleware -> CSRF path with a real cookie
jar, so it would have caught the regression that service-only tests missed.

Pytest module (matches tests/test_share_service):

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_auth_csrf.py
"""

from __future__ import annotations

import re

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
    """A TestClient over an isolated in-memory DB seeded with one user.

    StaticPool so every connection sees the same in-memory DB; a
    ``get_db_session`` override points the login route at it. SessionMiddleware
    / cookies are exercised for real (not overridden) — that's the whole point.
    """
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


def test_login_csrf_recovery() -> int:
    from fastapi.testclient import TestClient

    client, main, get_db_session = _client_and_user()
    failed = 0
    try:
        # 1) Happy path: GET then POST on the same client still succeeds (303).
        page = client.get("/login")
        r = _login(client, _csrf_token(page.text))
        if r.status_code != 303:
            print(f"  [FAIL] happy-path login -> {r.status_code} (expected 303)")
            failed += 1
        else:
            print("  [OK] happy-path login -> 303")

        # 2) THE REGRESSION: a valid-looking form token but NO session cookie
        #    (CDN-cached login page / dropped Set-Cookie / stale-host cookie).
        #    Must self-heal: 200 re-render that issues a fresh session cookie,
        #    NOT a hard 403 dead-end.
        src = TestClient(main.app)
        form_token = _csrf_token(src.get("/login").text)
        nocookie = TestClient(main.app)  # empty cookie jar
        r2 = _login(nocookie, form_token)
        reissued = "session" in r2.headers.get("set-cookie", "").lower()
        if r2.status_code != 200 or not reissued:
            print(
                f"  [FAIL] no-cookie login -> {r2.status_code} "
                f"reissue={reissued} (expected 200 + Set-Cookie)"
            )
            failed += 1
        else:
            print("  [OK] no-cookie login self-heals -> 200 + fresh cookie")

        # 2b) The immediate retry now carries the reissued cookie -> succeeds.
        r2b = _login(nocookie, _csrf_token(r2.text))
        if r2b.status_code != 303:
            print(f"  [FAIL] retry after reissue -> {r2b.status_code} (expected 303)")
            failed += 1
        else:
            print("  [OK] retry after reissue -> 303")

        # 3) A genuine mismatch (live session, wrong token) must STILL 403 —
        #    the softening only applies to the empty-session first-contact case.
        live = TestClient(main.app)
        live.get("/login")  # establishes a session cookie + token
        r3 = _login(live, "deadbeef" * 8)
        if r3.status_code != 403:
            print(f"  [FAIL] live-session wrong token -> {r3.status_code} (expected 403)")
            failed += 1
        else:
            print("  [OK] live-session wrong token -> 403 (CSRF still enforced)")
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)

    # Hard assert so pytest fails (not just warns) on a regression.
    assert failed == 0, f"{failed} CSRF-recovery check(s) failed"
    assert failed == 0
