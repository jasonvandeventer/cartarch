"""#95 — per-game analytics replayed from the live event stream.

Drives the REAL live-game service (start → actions → end_game) so the analytics
consume exactly what the service persists — no hand-built payloads that could
drift from the writer.
"""

from __future__ import annotations

import itertools
import json

from app import live_game_service
from app.game_analytics_service import _PALETTE, build_game_analytics
from app.game_service import end_game
from app.models import Game, GameEvent, GameSeat, User

TABLE = "TABLETOK"
_seq = itertools.count(1)


def _user(db) -> User:
    u = User(username=f"u{next(_seq)}@ex.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


def _game3(db, owner_id):
    game = Game(user_id=owner_id, format="Commander", status="created", client_token=TABLE)
    db.add(game)
    db.flush()
    seats = []
    for i in range(1, 4):
        s = GameSeat(game_id=game.id, seat_number=i, player_name=f"P{i}", starting_life=40)
        db.add(s)
        seats.append(s)
    db.flush()
    return game, seats


def _act(db, game, owner, action):
    return live_game_service.apply_live_action(db, game.id, owner.id, action, TABLE)


def test_build_game_analytics_full_replay(db):
    owner = _user(db)
    game, (s1, s2, s3) = _game3(db, owner.id)
    live_game_service.start_live_game(db, game.id, owner.id)

    # Turn 1: chip s2, then 21 commander damage s3 -> s1 (lethal cmd on s1).
    _act(db, game, owner, {"type": "life", "seat_id": s2.id, "delta": -5})
    _act(
        db,
        game,
        owner,
        {"type": "cmd", "receiver_seat_id": s1.id, "attacker_seat_id": s3.id, "delta": 21},
    )
    # Advance a full round to turn 2 (state.turn is the round counter — it bumps
    # only when the rotation wraps past the first seat), then drop s3 to 0.
    for _ in range(6):
        live = _act(db, game, owner, {"type": "turn"})
        if json.loads(live.state)["turn"] >= 2:
            break
    _act(db, game, owner, {"type": "life", "seat_id": s3.id, "delta": -40})

    end_game(
        db,
        game.id,
        owner.id,
        placements={s2.id: 1, s1.id: 2, s3.id: 3},
        final_lives={s1.id: 19, s2.id: 35, s3.id: 0},
        turn_count=2,
        notes="",
    )

    a = build_game_analytics(db, game.id)
    assert a is not None

    # Life-over-time: one series per seat; finals reflect the coupled cmd + life hits.
    finals = {row["label"]: row["final"] for row in a["life_chart"]["series"]}
    assert finals == {"P1": 19, "P2": 35, "P3": 0}
    assert a["life_chart"]["max_turn"] == 2
    assert all(row["points"] for row in a["life_chart"]["series"])  # non-empty polylines

    # Elimination timeline: s1 out turn 1 (cmd, 2 left), s3 out turn 2 (life, 1 left).
    tl = a["timeline"]
    assert [(t["label"], t["turn"], t["cause"], t["remaining"]) for t in tl] == [
        ("P1", 1, "cmd", 2),
        ("P3", 2, "life", 1),
    ]

    # Commander-damage matrix: s3 -> s1 == 21, flagged lethal.
    cm = a["cmd_matrix"]
    assert cm["any"] is True
    cols = [c["sid"] for c in cm["columns"]]
    p1_row = next(r for r in cm["rows"] if r["label"] == "P1")
    cell = p1_row["cells"][cols.index(str(s3.id))]
    assert cell["value"] == 21 and cell["lethal"] is True
    # Diagonal is self (no self-damage).
    assert p1_row["cells"][cols.index(str(s1.id))]["self"] is True

    # Pace: two turns recorded, total present.
    assert a["pace"]["turn_count"] == 2
    assert a["pace"]["total"]  # formatted string


# ── pace-segment ownership (SaintWacko, 2026-07-26) ──────────────────────────
# Every case runs TWICE: once reading the `active_seat_id` the live service now
# stamps into the turn payload, and once with that key stripped — which is what
# every pre-existing recorded game looks like, so the replay fallback has to
# produce the identical sequence.


def _seat_game(db, owner_id, specs, *, first_seat_number=None):
    """specs: list of {name?, grid_position?}. Seat numbers are 1..N in order."""
    game = Game(
        user_id=owner_id,
        format="Commander",
        status="created",
        client_token=TABLE,
        first_seat_number=first_seat_number,
    )
    db.add(game)
    db.flush()
    seats = []
    for i, spec in enumerate(specs, start=1):
        s = GameSeat(
            game_id=game.id,
            seat_number=i,
            player_name=spec.get("name", f"P{i}"),
            starting_life=40,
            grid_position=spec.get("grid_position"),
        )
        db.add(s)
        seats.append(s)
    db.flush()
    live_game_service.start_live_game(db, game.id, owner_id)
    return game, seats


def _pace_owners(db, game):
    a = build_game_analytics(db, game.id)
    return [t["sid"] for t in a["pace"]["turns"]]


def _strip_stamps(db, game):
    """Make the game look pre-stamp (games 42/43/45/46) so the replay path runs."""
    for e in db.query(GameEvent).filter(GameEvent.game_id == game.id).all():
        if e.action_type == "turn":
            p = json.loads(e.payload)
            p.pop("active_seat_id", None)
            e.payload = json.dumps(p)
    db.flush()


def _both_ways(db, game, expected):
    """Owners match `expected` from the stamp AND from the replay fallback."""
    assert _pace_owners(db, game) == expected
    _strip_stamps(db, game)
    assert _pace_owners(db, game) == expected


def test_pace_bars_carry_seat_color_and_player_name(db):
    owner = _user(db)
    game, (s1, s2, s3) = _game3(db, owner.id)
    live_game_service.start_live_game(db, game.id, owner.id)
    for _ in range(3):
        _act(db, game, owner, {"type": "turn"})

    turns = build_game_analytics(db, game.id)["pace"]["turns"]
    colors = {
        r["sid"]: r["color"] for r in build_game_analytics(db, game.id)["life_chart"]["series"]
    }
    assert [t["sid"] for t in turns] == [str(s1.id), str(s2.id), str(s3.id)]
    assert [t["player"] for t in turns] == ["P1", "P2", "P3"]
    # The bar color IS the life-chart series color for that seat.
    assert [t["color"] for t in turns] == [colors[str(s.id)] for s in (s1, s2, s3)]
    # Segment index and round counter are distinct: 3 segments, still round 1.
    assert [t["turn"] for t in turns] == [1, 2, 3]
    assert [t["round"] for t in turns] == [1, 1, 1]


def test_eliminated_seat_is_skipped_and_later_segments_stay_aligned(db):
    owner = _user(db)
    game, (s1, s2, s3) = _game3(db, owner.id)
    live_game_service.start_live_game(db, game.id, owner.id)
    _act(db, game, owner, {"type": "eliminate", "seat_id": s2.id, "eliminated": True})
    for _ in range(3):
        _act(db, game, owner, {"type": "turn"})
    # s2 never gets a bar; the rotation keeps alternating s1/s3 after the skip.
    _both_ways(db, game, [str(s1.id), str(s3.id), str(s1.id)])


def test_auto_elimination_then_revive_returns_the_seat_to_the_rotation(db):
    owner = _user(db)
    game, (s1, s2, s3) = _game3(db, owner.id)
    live_game_service.start_live_game(db, game.id, owner.id)
    _act(db, game, owner, {"type": "life", "seat_id": s2.id, "delta": -40})  # auto-elim
    _act(db, game, owner, {"type": "turn"})  # s1 → skips s2 → s3
    _act(db, game, owner, {"type": "life", "seat_id": s2.id, "delta": 40})  # auto-revive
    for _ in range(3):
        _act(db, game, owner, {"type": "turn"})
    _both_ways(db, game, [str(s1.id), str(s3.id), str(s1.id), str(s2.id)])


def test_rotation_follows_clockwise_order_while_colors_follow_seat_order(db):
    owner = _user(db)
    # seat_number order 1,2,3,4 but clockwise slots put seat2 first, then 4, 1, 3.
    game, (s1, s2, s3, s4) = _seat_game(
        db,
        owner.id,
        [
            {"grid_position": "p3"},
            {"grid_position": "p1"},
            {"grid_position": "p4"},
            {"grid_position": "p2"},
        ],
    )
    for _ in range(4):
        _act(db, game, owner, {"type": "turn"})

    a = build_game_analytics(db, game.id)
    # Colors are indexed by SEAT NUMBER order, unchanged by the clockwise rotation.
    series = {r["sid"]: r["color"] for r in a["life_chart"]["series"]}
    assert [series[str(s.id)] for s in (s1, s2, s3, s4)] == _PALETTE[:4]
    # ...but the turn order is clockwise: s2 (p1) → s4 (p2) → s1 (p3) → s3 (p4).
    assert [t["color"] for t in a["pace"]["turns"]] == [series[str(s.id)] for s in (s2, s4, s1, s3)]
    _both_ways(db, game, [str(s2.id), str(s4.id), str(s1.id), str(s3.id)])


def test_five_seat_game_stays_in_palette_range(db):
    owner = _user(db)
    game, seats = _seat_game(db, owner.id, [{} for _ in range(5)])
    for _ in range(5):
        _act(db, game, owner, {"type": "turn"})
    turns = build_game_analytics(db, game.id)["pace"]["turns"]
    assert [t["color"] for t in turns] == _PALETTE[:5]
    assert all(t["color"] in _PALETTE for t in turns)


def test_null_first_seat_number_falls_back_to_first_clockwise_seat(db):
    owner = _user(db)
    # No first_seat_number; seat3 holds the earliest clockwise slot.
    game, (s1, s2, s3) = _seat_game(
        db,
        owner.id,
        [{"grid_position": "p5"}, {"grid_position": "p6"}, {"grid_position": "p2"}],
    )
    assert game.first_seat_number is None
    for _ in range(2):
        _act(db, game, owner, {"type": "turn"})
    _both_ways(db, game, [str(s3.id), str(s1.id)])


def test_no_events_returns_none(db):
    """A finalized game that never went live (localStorage tracker) has no events
    → analytics section is hidden."""
    owner = _user(db)
    game, _seats = _game3(db, owner.id)
    game.status = "finalized"
    db.flush()
    assert build_game_analytics(db, game.id) is None
