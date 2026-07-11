"""Companion mode Session 2 — token scoping, companion route, live-mode rendering.

Renders through the real app (the repo's established template-test pattern).
The `client` fixture is pinned to `user`, so owner vs. non-owner scenarios are
built by choosing who owns the game and who is seated."""

from __future__ import annotations

import itertools

from app import live_game_service
from app.models import Game, GameSeat, Playgroup, PlaygroupMember, User

TABLE = "TABLETOK"
_seq = itertools.count(1)


def _user(db, name=None) -> User:
    u = User(username=(name or f"cu{next(_seq)}@ex.com"), password_hash="x")
    db.add(u)
    db.flush()
    return u


def _game(db, owner_id, *, seats, status="created", playgroup_id=None):
    game = Game(
        user_id=owner_id,
        format="Commander",
        status=status,
        client_token=TABLE,
        first_seat_number=1,
        playgroup_id=playgroup_id,
    )
    db.add(game)
    db.flush()
    objs = []
    for i, spec in enumerate(seats, start=1):
        s = GameSeat(
            game_id=game.id,
            seat_number=i,
            player_name=f"P{i}",
            user_id=spec.get("user_id"),
            starting_life=spec.get("starting_life", 40),
        )
        db.add(s)
        objs.append(s)
    db.flush()
    return game, objs


# ── C2: table-token scoping ──────────────────────────────────────────────────


def test_owner_game_detail_has_table_token(db, client, user):
    game, _ = _game(db, user.id, seats=[{}, {}])
    db.commit()
    html = client.get(f"/games/{game.id}").text
    assert TABLE in html  # owner tablet gets the table token
    assert "const clientToken" in html


def test_non_owner_game_detail_omits_table_token(db, client, user):
    owner = _user(db)
    game, seats = _game(db, owner.id, seats=[{"user_id": user.id}, {}])  # user is a seat player
    db.commit()
    html = client.get(f"/games/{game.id}").text
    assert TABLE not in html  # the token NEVER reaches a non-owner
    assert "const clientToken = null" in html


# ── C1: companion route ──────────────────────────────────────────────────────


def test_companion_seated_user_gets_seat_controls(db, client, user):
    # Live-mode seat controls appear once the game is in_progress (a `created`
    # game shows the pre-live deck picker instead).
    game, seats = _game(db, user.id, seats=[{"user_id": user.id}, {}])
    live_game_service.start_live_game(db, game.id, user.id)
    db.commit()
    html = client.get(f"/games/{game.id}/companion").text
    assert "(you)" in html
    assert "End turn" in html and "adjLife" in html
    assert f"const MY_SEAT_ID = {seats[0].id}" in html
    assert TABLE not in html  # never the table token on a phone


def test_companion_unseated_playgroup_member_is_spectator(db, client, user):
    owner = _user(db)
    pg = Playgroup(name="PG", created_by=owner.id)
    db.add(pg)
    db.flush()
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=user.id))
    game, _ = _game(db, owner.id, seats=[{"user_id": owner.id}, {}], playgroup_id=pg.id)
    db.commit()
    html = client.get(f"/games/{game.id}/companion").text
    assert "Spectating" in html
    assert "const MY_SEAT_ID = null" in html


def test_companion_outsider_404(db, client, user):
    owner = _user(db)
    game, _ = _game(
        db, owner.id, seats=[{"user_id": owner.id}, {}]
    )  # user not seated, no playgroup
    db.commit()
    assert client.get(f"/games/{game.id}/companion").status_code == 404


# ── C3 / C4: live-mode vs. localStorage rendering ────────────────────────────


def test_created_game_renders_localstorage_tracker_not_live(db, client, user):
    game, _ = _game(db, user.id, seats=[{}, {}], status="created")
    db.commit()
    html = client.get(f"/games/{game.id}").text
    # LIVE=false → the (always-present) live boot block stays inert; the
    # localStorage tracker runs exactly as today.
    assert "const LIVE = false" in html
    assert "Go Live (multi-device)" in html  # opt-in offered to the owner
    assert "localStorage.getItem(storageKey)" in html  # tracker untouched
    assert 'id="undo-btn"' in html and 'id="pause-btn"' in html  # controls present when not live


def test_in_progress_game_renders_live_mode(db, client, user):
    game, _ = _game(db, user.id, seats=[{}, {}])
    live_game_service.start_live_game(db, game.id, user.id)  # → in_progress + live state
    db.commit()
    html = client.get(f"/games/{game.id}").text
    assert "const LIVE = true" in html
    assert "new EventSource(`/games/" in html
    assert "/companion" in html  # share link present
    # Undo/Pause hidden in live mode.
    assert 'id="undo-btn"' not in html and 'id="pause-btn"' not in html
    # Live turn timer wired: the SSE-driven reset hook + the elapsed element.
    assert "syncLiveTurnEvents(state)" in html  # reset hook attached to SSE frame
    assert 'id="mast-elapsed"' in html
    # Optimistic turn advance rotates in clockwise (grid_position) order, matching
    # the server — not seat_number (the rotation bug).
    assert "const bySeat = clockwiseSeats.map(s => s.id);" in html


def test_companion_page_renders(db, client, user):
    game, seats = _game(db, user.id, seats=[{"user_id": user.id}, {}])
    live_game_service.start_live_game(db, game.id, user.id)
    db.commit()
    r = client.get(f"/games/{game.id}/companion")
    assert r.status_code == 200
    assert "new EventSource" in r.text and "Commander damage received" in r.text


def _session_cookie(client, user_id, csrf="tok"):
    import os

    from itsdangerous import TimestampSigner

    signer = TimestampSigner(os.getenv("SESSION_SECRET_KEY", "test-only-secret"))
    import base64
    import json

    data = base64.b64encode(json.dumps({"user_id": user_id, "csrf_token": csrf}).encode())
    client.cookies.set("session", signer.sign(data).decode())


def test_companion_seat_scoped_action_no_token(db, client, user):
    # The phone path: a seated player mutates their OWN seat with NO table token.
    game, seats = _game(db, user.id, seats=[{"user_id": user.id}, {}])
    live_game_service.start_live_game(db, game.id, user.id)
    db.commit()

    _session_cookie(client, user.id, csrf="tok")
    r = client.post(
        f"/games/{game.id}/live/action",
        json={"type": "life", "seat_id": seats[0].id, "delta": -6, "csrf_token": "tok"},
    )
    assert r.status_code == 200
    assert r.json()["state"]["lives"][str(seats[0].id)] == 34

    # ...but not another seat (403), even seated — no token means seat-scoped.
    r = client.post(
        f"/games/{game.id}/live/action",
        json={"type": "life", "seat_id": seats[1].id, "delta": -6, "csrf_token": "tok"},
    )
    assert r.status_code == 403
