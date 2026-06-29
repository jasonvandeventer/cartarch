"""End-to-end /login CSRF contract.

Issue #63 moved the four PUBLIC pre-auth forms (login / register / forgot /
reset) onto a STATELESS, server-signed CSRF token (`require_preauth_csrf`),
so a client that never returns our `session` cookie on the POST (Firefox for
iOS, Opera Touch) can still sign in. That supersedes the v3.31.0
`require_csrf_or_reissue` cookie-continuity self-heal — and the #62/#65
`no_session` diagnostic logging that fired from it — which this module used to
pin. The full stateless-guard behaviour (cross-site gate, expiry, tamper,
degraded path) is covered by tests/test_preauth_csrf.py; this module keeps the
real route -> SessionMiddleware -> cookie-jar /login contract that would catch a
regression a service-only test misses.

The superseded `require_csrf_or_reissue` / `csrf_state` / `_log_no_session` /
`_classify_*` helpers remain defined in app/dependencies.py (now unused by the
forms) and can be pruned separately.

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


def test_login_csrf_recovery():
    """Issue #63: /login validates a stateless signed token, so the no-cookie
    client now SUCCEEDS where the old session-bound guard dead-ended it.

    Supersedes the v3.31.0 self-heal this test used to pin (no-cookie -> 200
    reissue). Stateless-guard edge cases live in tests/test_preauth_csrf.py;
    this keeps the end-to-end cookie-jar /login contract.
    """
    from fastapi.testclient import TestClient

    client, main, get_db_session = _client_and_user()
    failed = 0
    try:
        # 1) Happy path: GET then POST on the same client succeeds (303).
        r = _login(client, _csrf_token(client.get("/login").text))
        if r.status_code != 303:
            print(f"  [FAIL] happy-path login -> {r.status_code} (expected 303)")
            failed += 1
        else:
            print("  [OK] happy-path login -> 303")

        # 2) THE #63 FIX: a valid signed token with NO session cookie now SUCCEEDS
        #    (303) — the FxiOS/OPT case the old guard dead-ended on a 200 reissue.
        form_token = _csrf_token(TestClient(main.app).get("/login").text)
        nocookie = TestClient(main.app)  # empty cookie jar — no session cookie
        r2 = _login(nocookie, form_token)
        if r2.status_code != 303:
            print(f"  [FAIL] no-cookie login -> {r2.status_code} (expected 303)")
            failed += 1
        else:
            print("  [OK] no-cookie login succeeds (stateless token) -> 303")

        # 3) A tampered / invalid-signature token still hard-fails with 403.
        r3 = _login(TestClient(main.app), "deadbeef" * 8)
        if r3.status_code != 403:
            print(f"  [FAIL] invalid token -> {r3.status_code} (expected 403)")
            failed += 1
        else:
            print("  [OK] invalid token -> 403 (CSRF still enforced)")
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)

    # Hard assert so pytest fails (not just warns) on a regression.
    assert failed == 0, f"{failed} login CSRF contract check(s) failed"
