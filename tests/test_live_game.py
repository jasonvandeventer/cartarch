"""Companion mode — live game state (schema + service + mutation API + SSE + auth).

The authorization matrix (table token vs seat-scoped) is the critical suite and
is tested at the service level, where user identity can be varied directly."""

from __future__ import annotations

import asyncio
import base64
import itertools
import json

import pytest

from app import live_game_events, live_game_service
from app.game_service import end_game
from app.models import Game, GameLiveState, GameSeat, Playgroup, PlaygroupMember, User

TABLE = "TABLETOK"
_seq = itertools.count(1)


def _user(db, name=None) -> User:
    u = User(username=(name or f"u{next(_seq)}@ex.com"), password_hash="x")
    db.add(u)
    db.flush()
    return u


def _make_game(
    db,
    owner_id,
    *,
    seats,
    status="created",
    first_seat_number=None,
    client_token=TABLE,
    playgroup_id=None,
):
    """seats: list of {user_id?, starting_life?}. Returns (game, [seat, ...])."""
    game = Game(
        user_id=owner_id,
        format="Commander",
        status=status,
        client_token=client_token,
        first_seat_number=first_seat_number,
        playgroup_id=playgroup_id,
    )
    db.add(game)
    db.flush()
    seat_objs = []
    for i, spec in enumerate(seats, start=1):
        s = GameSeat(
            game_id=game.id,
            seat_number=i,
            player_name=f"P{i}",
            user_id=spec.get("user_id"),
            starting_life=spec.get("starting_life", 40),
            grid_position=spec.get("grid_position"),
        )
        db.add(s)
        seat_objs.append(s)
    db.flush()
    return game, seat_objs


def _state(live: GameLiveState) -> dict:
    return json.loads(live.state)


def _act(db, game_id, user_id, action, token=None):
    return live_game_service.apply_live_action(db, game_id, user_id, action, token)


# ── 5a: lifecycle ────────────────────────────────────────────────────────────


def test_start_initializes_state_from_seats(db):
    owner = _user(db)
    a, b = _user(db), _user(db)
    game, seats = _make_game(
        db,
        owner.id,
        first_seat_number=2,
        seats=[{"user_id": a.id, "starting_life": 40}, {"user_id": b.id, "starting_life": 30}],
    )
    live = live_game_service.start_live_game(db, game.id, owner.id)
    st = _state(live)
    assert st["lives"] == {str(seats[0].id): 40, str(seats[1].id): 30}
    assert st["turn"] == 1
    assert st["currentTurnId"] == seats[1].id  # first_seat_number=2
    assert st["eliminated"] == {} and st["cmd"] == {} and st["extraCounters"] == {}
    assert db.get(Game, game.id).status == "in_progress"
    assert live.version == 1


def test_start_is_idempotent(db):
    owner = _user(db)
    game, _ = _make_game(db, owner.id, seats=[{}, {}])
    live1 = live_game_service.start_live_game(db, game.id, owner.id)
    live2 = live_game_service.start_live_game(db, game.id, owner.id)
    assert live2.id == live1.id and live2.version == 1


def test_start_rejects_finalized(db):
    owner = _user(db)
    game, _ = _make_game(db, owner.id, seats=[{}, {}], status="finalized")
    with pytest.raises(ValueError):
        live_game_service.start_live_game(db, game.id, owner.id)


def test_start_rejects_non_owner(db):
    owner, other = _user(db), _user(db)
    game, _ = _make_game(db, owner.id, seats=[{}, {}])
    with pytest.raises(PermissionError):
        live_game_service.start_live_game(db, game.id, other.id)


# ── 5b: authorization matrix (the critical suite) ────────────────────────────


@pytest.fixture
def matrix(db):
    """Owner (NOT seated), seat1→player_a, seat2→player_b, seat3 unattributed,
    linked to a playgroup that `viewer` belongs to but is not seated in."""
    owner = _user(db, "owner@ex.com")
    a = _user(db, "a@ex.com")
    b = _user(db, "b@ex.com")
    viewer = _user(db, "viewer@ex.com")
    pg = Playgroup(name="PG", created_by=owner.id)
    db.add(pg)
    db.flush()
    db.add_all(
        [
            PlaygroupMember(playgroup_id=pg.id, user_id=owner.id),
            PlaygroupMember(playgroup_id=pg.id, user_id=viewer.id),
        ]
    )
    game, seats = _make_game(
        db,
        owner.id,
        first_seat_number=1,
        playgroup_id=pg.id,
        seats=[{"user_id": a.id}, {"user_id": b.id}, {}],
    )
    live_game_service.start_live_game(db, game.id, owner.id)
    return {
        "db": db,
        "game": game,
        "seats": seats,
        "owner": owner,
        "a": a,
        "b": b,
        "viewer": viewer,
    }


def test_table_token_controls_any_seat(matrix):
    m = matrix
    s1, s2, s3 = m["seats"]
    # life / counter / eliminate / cmd / turn on any seat, all via the table token.
    _act(
        m["db"], m["game"].id, m["owner"].id, {"type": "life", "seat_id": s2.id, "delta": -3}, TABLE
    )
    _act(
        m["db"],
        m["game"].id,
        m["a"].id,
        {"type": "counter", "seat_id": s3.id, "counter": "poison", "delta": 2},
        TABLE,
    )
    _act(
        m["db"],
        m["game"].id,
        m["a"].id,
        {"type": "cmd", "receiver_seat_id": s1.id, "attacker_seat_id": s2.id, "delta": 4},
        TABLE,
    )
    _act(
        m["db"],
        m["game"].id,
        m["a"].id,
        {"type": "eliminate", "seat_id": s3.id, "eliminated": True},
        TABLE,
    )
    live = _act(m["db"], m["game"].id, m["a"].id, {"type": "turn"}, TABLE)
    st = _state(live)
    assert st["lives"][str(s2.id)] == 37
    assert st["cmd"][str(s1.id)][str(s2.id)] == 4


def test_wrong_or_absent_token_falls_through_to_user_checks(matrix):
    m = matrix
    s1 = m["seats"][0]
    # Wrong token → treated as no token → seat-scoped. player_a owns seat1.
    _act(m["db"], m["game"].id, m["a"].id, {"type": "life", "seat_id": s1.id, "delta": 1}, "WRONG")
    # ...but player_b (owns seat2) may not touch seat1 even with a bad token.
    with pytest.raises(PermissionError):
        _act(
            m["db"],
            m["game"].id,
            m["b"].id,
            {"type": "life", "seat_id": s1.id, "delta": 1},
            "WRONG",
        )


def test_seat_player_own_seat_allowed_other_seat_403(matrix):
    m = matrix
    s1, s2, _ = m["seats"]
    _act(m["db"], m["game"].id, m["a"].id, {"type": "life", "seat_id": s1.id, "delta": -5})  # own
    with pytest.raises(PermissionError):
        _act(m["db"], m["game"].id, m["a"].id, {"type": "life", "seat_id": s2.id, "delta": -5})


def test_creator_without_token_cannot_touch_unattributed_seat(matrix):
    # KEY tablet-vs-phone test: the game creator on their phone (no table token)
    # is seat-scoped like everyone else; they are attributed to NO seat here.
    m = matrix
    s1 = m["seats"][0]
    with pytest.raises(PermissionError):
        _act(m["db"], m["game"].id, m["owner"].id, {"type": "life", "seat_id": s1.id, "delta": 1})


def test_creator_attributed_to_seat_controls_own(db):
    owner = _user(db)
    game, seats = _make_game(db, owner.id, seats=[{"user_id": owner.id}, {}])
    live_game_service.start_live_game(db, game.id, owner.id)
    live = _act(db, game.id, owner.id, {"type": "life", "seat_id": seats[0].id, "delta": -2})
    assert _state(live)["lives"][str(seats[0].id)] == 38


def test_cmd_scoped_to_receiving_seat(matrix):
    m = matrix
    s1, s2, _ = m["seats"]
    # player_a owns seat1 → may edit damage RECEIVED by seat1.
    _act(
        m["db"],
        m["game"].id,
        m["a"].id,
        {"type": "cmd", "receiver_seat_id": s1.id, "attacker_seat_id": s2.id, "delta": 3},
    )
    # ...but not damage received by seat2 (player_b's seat).
    with pytest.raises(PermissionError):
        _act(
            m["db"],
            m["game"].id,
            m["a"].id,
            {"type": "cmd", "receiver_seat_id": s2.id, "attacker_seat_id": s1.id, "delta": 3},
        )


def test_any_seated_player_advances_turn(matrix):
    m = matrix
    # player_b is seated → may advance turn even though it's not "their" seat action.
    live = _act(m["db"], m["game"].id, m["b"].id, {"type": "turn"})
    assert _state(live)["currentTurnId"] == m["seats"][1].id  # 1 → 2


def test_playgroup_viewer_cannot_mutate_but_can_read(matrix):
    m = matrix
    s1 = m["seats"][0]
    with pytest.raises(PermissionError):
        _act(m["db"], m["game"].id, m["viewer"].id, {"type": "life", "seat_id": s1.id, "delta": 1})
    with pytest.raises(PermissionError):
        _act(m["db"], m["game"].id, m["viewer"].id, {"type": "turn"})
    # ...but reading the live state is viewer-scoped and allowed.
    live = live_game_service.get_live_state(m["db"], m["game"].id, m["viewer"].id)
    assert _state(live)["turn"] == 1


# ── 5c: action semantics ─────────────────────────────────────────────────────


@pytest.fixture
def live4(db):
    """A started 4-seat game (owner + table token control). first seat = seat1."""
    owner = _user(db)
    game, seats = _make_game(db, owner.id, first_seat_number=1, seats=[{}, {}, {}, {}])
    live_game_service.start_live_game(db, game.id, owner.id)
    return {"db": db, "gid": game.id, "uid": owner.id, "seats": seats}


def test_life_delta_accumulates(live4):
    L = live4
    s = L["seats"][0]
    _act(L["db"], L["gid"], L["uid"], {"type": "life", "seat_id": s.id, "delta": -7}, TABLE)
    live = _act(L["db"], L["gid"], L["uid"], {"type": "life", "seat_id": s.id, "delta": 3}, TABLE)
    assert _state(live)["lives"][str(s.id)] == 40 - 7 + 3


def test_counter_upsert_creates_then_increments(live4):
    L = live4
    s = L["seats"][0]
    _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "counter", "seat_id": s.id, "counter": "poison", "delta": 3},
        TABLE,
    )
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "counter", "seat_id": s.id, "counter": "poison", "delta": 2},
        TABLE,
    )
    arr = _state(live)["extraCounters"][str(s.id)]
    assert arr == [{"type": "poison", "value": 5}]


def test_cmd_floors_at_zero_and_restores_life_symmetrically(live4):
    L = live4
    r, a = L["seats"][0], L["seats"][1]  # r starts at 40 life
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "cmd", "receiver_seat_id": r.id, "attacker_seat_id": a.id, "delta": 2},
        TABLE,
    )
    st = _state(live)
    assert st["cmd"][str(r.id)][str(a.id)] == 2
    assert st["lives"][str(r.id)] == 38  # cmd +2 → life −2 (coupled)

    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "cmd", "receiver_seat_id": r.id, "attacker_seat_id": a.id, "delta": -9},
        TABLE,
    )
    st = _state(live)
    assert st["cmd"][str(r.id)][str(a.id)] == 0  # cmd floored at 0
    # Decrement restores only the actual 2 that was there (post-floor), not 9.
    assert st["lives"][str(r.id)] == 40


def test_cmd_couples_life_matching_local_tracker(live4):
    L = live4
    r, a = L["seats"][0], L["seats"][1]
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "cmd", "receiver_seat_id": r.id, "attacker_seat_id": a.id, "delta": 3},
        TABLE,
    )
    st = _state(live)
    assert st["cmd"][str(r.id)][str(a.id)] == 3
    assert st["lives"][str(r.id)] == 37  # cmd +3 → receiver life −3


def test_turn_skips_eliminated_and_wraps(live4):
    L = live4
    s1, s2, s3, s4 = L["seats"]
    # Eliminate seat2 → advancing from seat1 lands on seat3.
    _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "eliminate", "seat_id": s2.id, "eliminated": True},
        TABLE,
    )
    live = _act(L["db"], L["gid"], L["uid"], {"type": "turn"}, TABLE)
    assert _state(live)["currentTurnId"] == s3.id
    assert _state(live)["turn"] == 1  # no wrap yet


def test_turn_increments_on_wrap_past_first(live4):
    L = live4
    s1, s2, s3, s4 = L["seats"]
    for _ in range(3):  # 1→2→3→4
        live = _act(L["db"], L["gid"], L["uid"], {"type": "turn"}, TABLE)
    assert _state(live)["currentTurnId"] == s4.id and _state(live)["turn"] == 1
    live = _act(L["db"], L["gid"], L["uid"], {"type": "turn"}, TABLE)  # 4 → 1 (wrap)
    assert _state(live)["currentTurnId"] == s1.id and _state(live)["turn"] == 2


# ── turn rotation follows PHYSICAL clockwise order (grid_position), not
#    seat_number. Regression: a 4-seat game rotated by badge as 1,2,4,3 because
#    the server used seat_number while the UI/badges use grid_position. ─────────

# The real-world game-38 layout: the 4-player default seating is p1,p2,p6,p5, so
# seat_number 3 sits in clockwise slot 4 and seat_number 4 in slot 3. Clockwise
# (turn) order is therefore seat_numbers 1,2,4,3.
_G38_POS = [
    {"grid_position": "p1"},
    {"grid_position": "p2"},
    {"grid_position": "p6"},
    {"grid_position": "p5"},
]


def _started(db, *, first_seat_number=1, seats=_G38_POS):
    owner = _user(db)
    game, seat_objs = _make_game(db, owner.id, first_seat_number=first_seat_number, seats=seats)
    live_game_service.start_live_game(db, game.id, owner.id)
    return db, game.id, owner.id, seat_objs


def test_turn_rotation_follows_clockwise_not_seat_number(db):
    d, gid, uid, (s1, s2, s3, s4) = _started(db)
    # Seat ids ascend with seat_number (1<2<3<4); the CLOCKWISE order is 1,2,4,3,
    # so the visited id sequence is NOT ascending — pins grid_position ordering.
    assert _state(live_game_service.get_live_state(d, gid, uid))["currentTurnId"] == s1.id
    clockwise = [s2.id, s4.id, s3.id, s1.id]  # after s1: 2,4,3, wrap to 1
    seq, turns = [], []
    for _ in range(8):  # two full laps
        live = _act(d, gid, uid, {"type": "turn"}, TABLE)
        st = _state(live)
        seq.append(st["currentTurnId"])
        turns.append(st["turn"])
    assert seq == clockwise * 2
    assert turns == [1, 1, 1, 2, 2, 2, 2, 3]  # turn++ on each wrap to seat1
    assert seq != [s2.id, s3.id, s4.id, s1.id] * 2  # would be the seat_number bug


def test_turn_first_seat_number_starts_clockwise_rotation(db):
    # first_seat_number=3 → start at seat3, then clockwise 3→1→2→4→3 (wrap).
    d, gid, uid, (s1, s2, s3, s4) = _started(db, first_seat_number=3)
    assert _state(live_game_service.get_live_state(d, gid, uid))["currentTurnId"] == s3.id
    expected = [(s1.id, 1), (s2.id, 1), (s4.id, 1), (s3.id, 2)]
    for want_id, want_turn in expected:
        st = _state(_act(d, gid, uid, {"type": "turn"}, TABLE))
        assert (st["currentTurnId"], st["turn"]) == (want_id, want_turn)


def test_turn_skips_eliminated_in_clockwise_order(db):
    # Clockwise order 1,2,4,3; eliminate seat2 → advancing from seat1 skips to
    # seat4 (the next clockwise), not seat3.
    d, gid, uid, (s1, s2, s3, s4) = _started(db)
    _act(d, gid, uid, {"type": "eliminate", "seat_id": s2.id, "eliminated": True}, TABLE)
    live = _act(d, gid, uid, {"type": "turn"}, TABLE)
    assert _state(live)["currentTurnId"] == s4.id


def test_eliminate_records_turn_and_revive_clears(live4):
    L = live4
    s = L["seats"][0]
    _act(L["db"], L["gid"], L["uid"], {"type": "turn"}, TABLE)  # bump so turn context != 1 later
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "eliminate", "seat_id": s.id, "eliminated": True},
        TABLE,
    )
    st = _state(live)
    assert st["eliminated"][str(s.id)] is True
    assert st["eliminatedAtTurn"][str(s.id)] == st["turn"]
    live = _act(
        L["db"],
        L["gid"],
        L["uid"],
        {"type": "eliminate", "seat_id": s.id, "eliminated": False},
        TABLE,
    )
    st = _state(live)
    assert st["eliminated"][str(s.id)] is False
    assert str(s.id) not in st["eliminatedAtTurn"]


def test_invalid_seat_and_unknown_type_raise(live4):
    L = live4
    with pytest.raises(ValueError):
        _act(L["db"], L["gid"], L["uid"], {"type": "life", "seat_id": 999999, "delta": 1}, TABLE)
    with pytest.raises(ValueError):
        _act(L["db"], L["gid"], L["uid"], {"type": "teleport"}, TABLE)


def test_version_increments_every_action(live4):
    L = live4
    s = L["seats"][0]
    v1 = _act(
        L["db"], L["gid"], L["uid"], {"type": "life", "seat_id": s.id, "delta": 1}, TABLE
    ).version
    v2 = _act(
        L["db"], L["gid"], L["uid"], {"type": "life", "seat_id": s.id, "delta": 1}, TABLE
    ).version
    assert (v1, v2) == (2, 3)  # start=1, then +1 each action


# ── 5d: finalize integration ─────────────────────────────────────────────────


def test_finalize_deletes_live_state_and_persists_final_life(db):
    owner = _user(db)
    game, seats = _make_game(db, owner.id, seats=[{}, {}])
    live_game_service.start_live_game(db, game.id, owner.id)
    assert db.query(GameLiveState).filter_by(game_id=game.id).count() == 1

    ok = end_game(
        db,
        game.id,
        owner.id,
        placements={seats[0].id: 1, seats[1].id: 2},
        final_lives={seats[0].id: 12, seats[1].id: 0},
        turn_count=7,
        notes="",
    )
    assert ok
    assert db.query(GameLiveState).filter_by(game_id=game.id).count() == 0
    assert db.get(Game, game.id).status == "finalized"
    assert db.get(GameSeat, seats[0].id).final_life == 12


# ── 5e: SSE pub/sub ──────────────────────────────────────────────────────────


def test_pubsub_delivers_published_state():
    async def scenario():
        async with live_game_events.subscribe(4242) as q:
            live_game_events.publish(4242, '{"version":5,"state":{}}')
            return await asyncio.wait_for(q.get(), timeout=1)

    assert json.loads(asyncio.run(scenario()))["version"] == 5


def test_apply_action_publishes_new_state(db):
    owner = _user(db)
    game, seats = _make_game(db, owner.id, seats=[{}, {}])
    live_game_service.start_live_game(db, game.id, owner.id)

    async def scenario():
        async with live_game_events.subscribe(game.id) as q:
            _act(
                db, game.id, owner.id, {"type": "life", "seat_id": seats[0].id, "delta": -4}, TABLE
            )
            return await asyncio.wait_for(q.get(), timeout=1)

    payload = json.loads(asyncio.run(scenario()))
    assert payload["state"]["lives"][str(seats[0].id)] == 36
    assert payload["version"] == 2


# ── Route wiring (start + action through the app) ────────────────────────────


def _session_cookie(client, user_id, csrf="tok"):
    """Sign a Starlette session cookie carrying user_id + csrf_token (the JSON
    action route reads request.session for CSRF). Uses the SAME secret the app's
    SessionMiddleware uses so the signature verifies."""
    import os

    from itsdangerous import TimestampSigner

    signer = TimestampSigner(os.getenv("SESSION_SECRET_KEY", "test-only-secret"))
    data = base64.b64encode(json.dumps({"user_id": user_id, "csrf_token": csrf}).encode())
    client.cookies.set("session", signer.sign(data).decode())


def test_route_start_then_action(db, client, user):
    game, seats = _make_game(db, user.id, seats=[{"user_id": user.id}, {}])
    db.commit()

    r = client.post(f"/games/{game.id}/live/start", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Game, game.id).status == "in_progress"

    _session_cookie(client, user.id, csrf="tok")
    r = client.post(
        f"/games/{game.id}/live/action",
        json={
            "type": "life",
            "seat_id": seats[0].id,
            "delta": -6,
            "table_token": TABLE,
            "csrf_token": "tok",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2
    assert body["state"]["lives"][str(seats[0].id)] == 34


def test_route_action_bad_action_400_and_csrf_403(db, client, user):
    game, seats = _make_game(db, user.id, seats=[{"user_id": user.id}, {}])
    live_game_service.start_live_game(db, game.id, user.id)
    db.commit()

    # Missing/invalid CSRF (no session cookie) → 403 before any mutation.
    r = client.post(
        f"/games/{game.id}/live/action", json={"type": "life", "seat_id": seats[0].id, "delta": 1}
    )
    assert r.status_code == 403

    _session_cookie(client, user.id, csrf="tok")
    r = client.post(
        f"/games/{game.id}/live/action",
        json={"type": "nonsense", "csrf_token": "tok", "table_token": TABLE},
    )
    assert r.status_code == 400
