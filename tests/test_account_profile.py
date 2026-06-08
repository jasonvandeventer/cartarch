"""Email-case hardening for profile edit + login (v3.33.1).

Pytest module (matches tests/test_share_service).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_account_profile.py

The /account "Update Profile" form already existed; this pins the v3.33.1
case-normalization fix:
  - update-profile lowercases the email on write (canonical username), still
    rejecting bad/duplicate emails and setting/clearing display_name
  - login is case-insensitive: authenticate_user finds a lowercase-stored user
    from a mixed-case / whitespaced input, while wrong password / inactive
    accounts still fail; end-to-end POST /login with mixed-case email succeeds
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


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_authenticate_case_insensitive():
    failed = 0
    s = _fresh_session()
    s.add(User(username="user@x.com", password_hash=auth.hash_password("pw123456")))
    s.commit()

    # Mixed case + surrounding whitespace still authenticates.
    if auth.authenticate_user(s, "  User@X.COM ", "pw123456") is not None:
        print("  [OK] authenticate_user is case/whitespace-insensitive on username")
    else:
        print("  [FAIL] mixed-case login did not match the lowercase-stored user")
        failed += 1

    # Wrong password still fails (case-folding only touches the username).
    if auth.authenticate_user(s, "USER@X.COM", "wrongpw") is None:
        print("  [OK] wrong password still rejected")
    else:
        print("  [FAIL] wrong password accepted")
        failed += 1

    # Inactive account still rejected.
    u = s.query(User).filter(User.username == "user@x.com").first()
    u.is_active = False
    s.commit()
    if auth.authenticate_user(s, "User@X.com", "pw123456") is None:
        print("  [OK] inactive account still rejected")
    else:
        print("  [FAIL] inactive account authenticated")
        failed += 1
    assert failed == 0


def _client(seed):
    """TestClient over an isolated in-memory DB; `seed(session)` populates it.
    Returns (client, main, overrides_to_pop, sessionmaker)."""
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    s = sm()
    seed(s)
    s.commit()
    s.close()

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    return (
        TestClient(main.app, follow_redirects=False),
        main,
        (
            get_db_session,
            get_current_user,
            require_csrf_token,
        ),
        sm,
    )


def test_update_profile_lowercases():
    from app.dependencies import get_current_user, require_csrf_token

    failed = 0

    def seed(s):
        s.add(User(username="owner@x.com", password_hash=auth.hash_password("pw123456")))
        s.add(User(username="taken@x.com", password_hash=auth.hash_password("pw123456")))

    client, main, overrides, sm = _client(seed)
    s0 = sm()
    owner = s0.query(User).filter(User.username == "owner@x.com").first()
    owner_id = owner.id
    s0.close()
    main.app.dependency_overrides[get_current_user] = lambda: sm().get(User, owner_id)
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    try:
        # Mixed-case edit is stored lowercase.
        r = client.post(
            "/account/update-profile",
            data={"email": "MixedCase@X.com", "display_name": "  Owner  ", "csrf_token": "x"},
        )
        s = sm()
        u = s.get(User, owner_id)
        if r.status_code == 303 and u.username == "mixedcase@x.com" and u.display_name == "Owner":
            print("  [OK] update-profile lowercases email + trims display_name")
        else:
            print(f"  [FAIL] update-profile: status={r.status_code} username={u.username!r}")
            failed += 1
        s.close()

        # Duplicate email rejected (compares against the other user, lowercased).
        r = client.post(
            "/account/update-profile",
            data={"email": "Taken@X.com", "display_name": "", "csrf_token": "x"},
        )
        s = sm()
        u = s.get(User, owner_id)
        if (
            "error=email_taken" in str(r.headers.get("location", ""))
            and u.username != "taken@x.com"
        ):
            print("  [OK] duplicate email rejected (case-folded)")
        else:
            print(f"  [FAIL] email_taken not enforced: loc={r.headers.get('location')!r}")
            failed += 1
        s.close()

        # Malformed email rejected; display_name clears to None on empty.
        r = client.post(
            "/account/update-profile",
            data={"email": "not-an-email", "display_name": "", "csrf_token": "x"},
        )
        if "error=bad_email" in str(r.headers.get("location", "")):
            print("  [OK] malformed email rejected")
        else:
            print(f"  [FAIL] bad_email not enforced: loc={r.headers.get('location')!r}")
            failed += 1
    finally:
        for dep in overrides:
            main.app.dependency_overrides.pop(dep, None)
    assert failed == 0


def test_login_route_case_insensitive():
    failed = 0

    def seed(s):
        s.add(User(username="a@x.com", password_hash=auth.hash_password("pw123456")))

    client, main, overrides, sm = _client(seed)
    try:
        # GET /login to obtain a CSRF token + session cookie.
        page = client.get("/login")
        m = _TOKEN_RE.search(page.text)
        token = m.group(1) if m else ""
        r = client.post(
            "/login",
            data={"username": "A@X.com", "password": "pw123456", "csrf_token": token},
        )
        # Success = 303 redirect to "/"; failure = 200 re-render with error.
        if r.status_code == 303 and r.headers.get("location") == "/":
            print("  [OK] POST /login succeeds with mixed-case email")
        else:
            print(
                f"  [FAIL] mixed-case login: status={r.status_code} loc={r.headers.get('location')!r}"
            )
            failed += 1
    finally:
        for dep in overrides:
            main.app.dependency_overrides.pop(dep, None)
    assert failed == 0
