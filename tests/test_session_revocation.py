"""Real signed cookies must stop authenticating after every password-set path."""

import base64
import json
import os
import re

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy.orm import sessionmaker

from app import main
from app.auth import hash_password, set_password
from app.dependencies import get_db_session
from app.models import User
from app.password_reset_service import create_reset_token


def csrf(page):
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
    assert match, page.status_code
    return match[1]


@pytest.fixture
def browsers(db_engine, db, user, monkeypatch):
    user.password_hash = hash_password("old-password-123")
    user.is_admin = True
    db.commit()
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr("app.dependencies.SessionLocal", factory)
    monkeypatch.setattr("app.routes.live_games.SessionLocal", factory)

    def database():
        with factory() as session:
            yield session

    main.app.dependency_overrides[get_db_session] = database
    clients = [TestClient(main.app) for _ in range(2)]
    try:
        for client in clients:
            result = client.post(
                "/login",
                data={
                    "username": user.username,
                    "password": "old-password-123",
                    "csrf_token": csrf(client.get("/login")),
                },
                follow_redirects=False,
            )
            assert result.status_code == 303
            assert client.get("/account").status_code == 200
        yield clients
    finally:
        for client in clients:
            client.close()
        main.app.dependency_overrides.pop(get_db_session, None)


@pytest.mark.parametrize("path", ["account", "reset", "admin", "script"])
def test_old_cookie_is_revoked_and_fresh_login_works(browsers, db, user, path, monkeypatch):
    first, second = browsers
    cookie = second.cookies.get("session")
    if path == "account":
        response = first.post(
            "/account/change-password",
            data={
                "current_password": "old-password-123",
                "new_password": "new-password-456",
                "confirm_password": "new-password-456",
                "csrf_token": csrf(first.get("/account")),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert first.get("/account").status_code == 200  # Keep the changing browser signed in.
    elif path == "reset":
        token = create_reset_token(db, user)
        db.commit()
        response = first.post(
            "/reset-password",
            data={
                "token": token,
                "password": "new-password-456",
                "password_confirm": "new-password-456",
                "csrf_token": csrf(first.get(f"/reset-password?token={token}")),
            },
        )
        assert response.status_code == 200
    elif path == "admin":
        response = first.post(
            f"/admin/users/{user.id}/reset-password",
            data={
                "new_password": "new-password-456",
                "csrf_token": csrf(first.get("/account")),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
    else:
        from scripts import set_user_password

        monkeypatch.setattr(set_user_password, "SessionLocal", sessionmaker(bind=db.bind))
        monkeypatch.setattr("sys.argv", ["set_user_password.py", user.username, "new-password-456"])
        set_user_password.main()
    # Replay the original cookie, even after a response clears it.
    second.cookies.clear()
    second.cookies.set("session", cookie)
    assert second.get("/account", follow_redirects=False).status_code == 401
    second.cookies.clear()
    second.cookies.set("session", cookie)
    assert second.get("/games/1/live/stream").status_code == 401
    second.cookies.clear()
    second.cookies.set("session", cookie)
    assert 'href="/account"' not in second.get("/").text  # Optional auth is revoked too.
    result = second.post(
        "/login",
        data={
            "username": user.username,
            "password": "new-password-456",
            "csrf_token": csrf(second.get("/login")),
        },
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert second.get("/account").status_code == 200


def test_legacy_cookie_survives_migration_but_not_password_change(browsers, db, user):
    client = browsers[0]
    cookie = (
        TimestampSigner(os.environ["SESSION_SECRET_KEY"])
        .sign(base64.b64encode(json.dumps({"user_id": user.id}).encode()))
        .decode()
    )
    client.cookies.clear()
    client.cookies.set("session", cookie)
    assert client.get("/account").status_code == 200
    set_password(user, "changed-password")
    db.commit()
    assert client.get("/account", follow_redirects=False).status_code == 401
    assert db.get(User, user.id).session_version == 1


@pytest.mark.parametrize("publish", [False, True])
def test_open_stream_stops_after_password_change(db_engine, db, user, monkeypatch, publish):
    import asyncio
    from types import SimpleNamespace

    from app import live_game_events
    from app.routes import live_games

    monkeypatch.setattr(live_games, "SessionLocal", sessionmaker(bind=db_engine))
    monkeypatch.setattr(live_games, "get_live_state", lambda *_: object())
    monkeypatch.setattr(live_games, "state_payload", lambda _: {"version": 1, "state": {}})
    monkeypatch.setattr(live_games, "_SSE_HEARTBEAT_SECONDS", 0.01)

    async def connected():
        return False

    request = SimpleNamespace(session={"user_id": user.id}, is_disconnected=connected)

    async def run():
        response = await live_games.live_stream(request, 12345)
        stream = response.body_iterator
        assert '"version": 1' in await anext(stream)
        set_password(user, "changed-password")
        db.commit()
        if publish:
            live_game_events.publish(12345, '{"version":2,"state":{}}')
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert live_game_events.subscriber_count(12345) == 0

    asyncio.run(run())
