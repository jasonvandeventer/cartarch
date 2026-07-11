"""Game event history (Phase 1) + win condition at finalize (Phase 2)."""

from __future__ import annotations

import itertools
import json

from app import game_service, live_game_service
from app.models import Game, GameEvent, GameSeat, User

TABLE = "TOK"
_seq = itertools.count(1)


def _user(db, name=None) -> User:
    u = User(username=(name or f"ge{next(_seq)}@ex.com"), password_hash="x")
    db.add(u)
    db.flush()
    return u


def _started_game(db, owner, seat_users):
    """A game flipped live via start_live_game (which writes a live_started event)."""
    game = Game(
        user_id=owner.id,
        format="Commander",
        status="created",
        client_token=TABLE,
        first_seat_number=1,
    )
    db.add(game)
    db.flush()
    seats = []
    for i, uid in enumerate(seat_users, start=1):
        s = GameSeat(
            game_id=game.id, seat_number=i, player_name=f"P{i}", user_id=uid, starting_life=40
        )
        db.add(s)
        seats.append(s)
    db.flush()
    live_game_service.start_live_game(db, game.id, owner.id)
    return game, seats


def _events(db, game_id):
    return db.query(GameEvent).filter_by(game_id=game_id).order_by(GameEvent.id).all()


def _act(db, game_id, user_id, action, token=TABLE):
    return live_game_service.apply_live_action(db, game_id, user_id, action, token)


# ── per-action-type events ───────────────────────────────────────────────────


def test_each_action_type_writes_one_event_with_seat_and_turn(db, user):
    game, seats = _started_game(db, user, [user.id, None])
    s1, s2 = seats
    before = len(_events(db, game.id))

    cases = [
        ({"type": "life", "seat_id": s1.id, "delta": -3}, s1.id),
        ({"type": "counter", "seat_id": s1.id, "counter": "poison", "delta": 2}, s1.id),
        ({"type": "cmd", "receiver_seat_id": s1.id, "attacker_seat_id": s2.id, "delta": 4}, s1.id),
        ({"type": "eliminate", "seat_id": s2.id, "eliminated": True}, s2.id),
        ({"type": "turn"}, None),
    ]
    for i, (action, expected_seat) in enumerate(cases, start=1):
        live = _act(db, game.id, user.id, action)
        evs = _events(db, game.id)
        assert len(evs) == before + i  # exactly one new event
        ev = evs[-1]
        assert ev.action_type == action["type"]
        assert ev.seat_id == expected_seat
        assert ev.turn == json.loads(live.state)["turn"]  # NEW turn (post-advance)
        assert ev.actor_kind == "table"


def test_cmd_event_records_raw_and_actual_delta_with_floor(db, user):
    game, seats = _started_game(db, user, [user.id, None])
    s1, s2 = seats
    _act(
        db,
        game.id,
        user.id,
        {"type": "cmd", "receiver_seat_id": s1.id, "attacker_seat_id": s2.id, "delta": 2},
    )
    # cmd is now 2; a -9 floors to 0 → actual_delta is -2, not -9.
    _act(
        db,
        game.id,
        user.id,
        {"type": "cmd", "receiver_seat_id": s1.id, "attacker_seat_id": s2.id, "delta": -9},
    )
    ev = _events(db, game.id)[-1]
    payload = json.loads(ev.payload)
    assert payload["raw_delta"] == -9
    assert payload["actual_delta"] == -2  # post-floor value the service computed


def test_payload_strips_table_and_csrf_tokens(db, user):
    game, seats = _started_game(db, user, [user.id, None])
    s1 = seats[0]
    _act(
        db,
        game.id,
        user.id,
        {"type": "life", "seat_id": s1.id, "delta": 1, "table_token": TABLE, "csrf_token": "x"},
    )
    payload = json.loads(_events(db, game.id)[-1].payload)
    assert "table_token" not in payload and "csrf_token" not in payload
    assert payload["delta"] == 1


def test_actor_kind_table_vs_seat(db, user):
    seated = _user(db)
    game, seats = _started_game(db, user, [seated.id, None])
    s1 = seats[0]
    # Table token → "table".
    _act(db, game.id, user.id, {"type": "life", "seat_id": s1.id, "delta": 1}, token=TABLE)
    assert _events(db, game.id)[-1].actor_kind == "table"
    # Phone seat player (no token) → "seat".
    _act(db, game.id, seated.id, {"type": "life", "seat_id": s1.id, "delta": 1}, token=None)
    assert _events(db, game.id)[-1].actor_kind == "seat"


# ── bookends ─────────────────────────────────────────────────────────────────


def test_live_started_event_and_no_duplicate_on_reentry(db, user):
    game, _ = _started_game(db, user, [user.id, None])
    started = [e for e in _events(db, game.id) if e.action_type == "live_started"]
    assert len(started) == 1
    ev = started[0]
    assert ev.seat_id is None and ev.turn == 1 and ev.actor_kind == "table"
    assert json.loads(ev.payload)["lives"]  # the initial state blob

    # Idempotent re-entry must NOT append a second live_started.
    live_game_service.start_live_game(db, game.id, user.id)
    assert len([e for e in _events(db, game.id) if e.action_type == "live_started"]) == 1


def test_finalize_writes_finalized_event_deletes_live_state_keeps_events(db, user):
    game, seats = _started_game(db, user, [user.id, None])
    _act(db, game.id, user.id, {"type": "life", "seat_id": seats[0].id, "delta": -5})
    n_before = len(_events(db, game.id))

    ok = game_service.end_game(
        db,
        game.id,
        user.id,
        placements={seats[0].id: 1},
        final_lives={seats[0].id: 35},
        turn_count=3,
        notes="",
    )
    assert ok
    evs = _events(db, game.id)
    assert len(evs) == n_before + 1  # events survive finalize + one added
    fin = evs[-1]
    assert fin.action_type == "finalized" and fin.seat_id is None
    assert json.loads(fin.payload)["lives"][str(seats[0].id)] == 35  # the final blob
    from app.models import GameLiveState

    assert db.query(GameLiveState).filter_by(game_id=game.id).count() == 0  # live row gone


def test_non_live_finalize_writes_no_events(db, user):
    game = Game(user_id=user.id, format="Commander", status="created", client_token=TABLE)
    db.add(game)
    db.flush()
    seat = GameSeat(
        game_id=game.id, seat_number=1, player_name="P1", user_id=user.id, starting_life=40
    )
    db.add(seat)
    db.flush()
    ok = game_service.end_game(
        db,
        game.id,
        user.id,
        placements={seat.id: 1},
        final_lives={seat.id: 40},
        turn_count=1,
        notes="",
    )
    assert ok
    assert _events(db, game.id) == []
    assert db.get(Game, game.id).status == "finalized"


def test_game_delete_cascades_events(db, user):
    game, seats = _started_game(db, user, [user.id, None])
    _act(db, game.id, user.id, {"type": "life", "seat_id": seats[0].id, "delta": 1})
    assert len(_events(db, game.id)) > 0
    gid = game.id
    game_service.delete_game(db, gid, user.id)
    assert db.query(GameEvent).filter_by(game_id=gid).count() == 0


# ── win condition ────────────────────────────────────────────────────────────


def test_win_condition_persists_and_normalizes(db, user):
    game, seats = _started_game(db, user, [user.id, None])
    game_service.end_game(
        db,
        game.id,
        user.id,
        placements={},
        final_lives={},
        turn_count=1,
        notes="",
        win_condition="commander",
    )
    assert db.get(Game, game.id).win_condition == "commander"

    # Absent → NULL.
    g2, s2 = _started_game(db, user, [user.id, None])
    game_service.end_game(db, g2.id, user.id, placements={}, final_lives={}, turn_count=1, notes="")
    assert db.get(Game, g2.id).win_condition is None

    # Unknown value → NULL (non-blocking normalize).
    g3, s3 = _started_game(db, user, [user.id, None])
    game_service.end_game(
        db,
        g3.id,
        user.id,
        placements={},
        final_lives={},
        turn_count=1,
        notes="",
        win_condition="bogus",
    )
    assert db.get(Game, g3.id).win_condition is None


def test_win_condition_displayed_on_finalized_view(db, client, user):
    game = Game(
        user_id=user.id, format="Commander", status="finalized", win_condition="combo", turn_count=5
    )
    db.add(game)
    db.flush()
    db.add(
        GameSeat(
            game_id=game.id,
            seat_number=1,
            player_name="P1",
            user_id=user.id,
            starting_life=40,
            placement=1,
        )
    )
    db.commit()
    html = client.get(f"/games/{game.id}").text
    assert "won by Combo" in html


def test_win_condition_from_end_game_form(db, client, user):
    game, seats = _started_game(db, user, [user.id, None])
    db.commit()
    r = client.post(
        f"/games/{game.id}/end",
        data={"win_condition": "concession", "turn_count": "4"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Game, game.id).win_condition == "concession"
