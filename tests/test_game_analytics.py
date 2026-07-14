"""#95 — per-game analytics replayed from the live event stream.

Drives the REAL live-game service (start → actions → end_game) so the analytics
consume exactly what the service persists — no hand-built payloads that could
drift from the writer.
"""

from __future__ import annotations

import itertools
import json

from app import live_game_service
from app.game_analytics_service import build_game_analytics
from app.game_service import end_game
from app.models import Game, GameSeat, User

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


def test_no_events_returns_none(db):
    """A finalized game that never went live (localStorage tracker) has no events
    → analytics section is hidden."""
    owner = _user(db)
    game, _seats = _game3(db, owner.id)
    game.status = "finalized"
    db.flush()
    assert build_game_analytics(db, game.id) is None
