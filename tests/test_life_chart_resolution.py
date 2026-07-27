"""#151 — per-event life chart: resolution, step rendering, truncation at elimination.

One test per acceptance criterion. All of them drive the REAL live-game service so the
chart consumes exactly what the service persists.

Geometry note: the x-axis is one sample per life-affecting event (`life` / `cmd`), with
sample 0 the `live_started` baseline. Step rendering means a transition emits TWO points
(across at the old value, then down to the new), so a row of N samples renders as
`1 + 2*(N-1)` points.
"""

from __future__ import annotations

import itertools

from app import live_game_service
from app.game_analytics_service import build_game_analytics
from app.game_service import end_game
from app.models import Game, GameSeat, User

TABLE = "TABLETOK"
_seq = itertools.count(1)


def _user(db) -> User:
    u = User(username=f"lc{next(_seq)}@ex.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


def _game(db, owner_id, seats=3):
    g = Game(user_id=owner_id, format="Commander", status="created", client_token=TABLE)
    db.add(g)
    db.flush()
    objs = [
        GameSeat(game_id=g.id, seat_number=i, player_name=f"P{i}", starting_life=40)
        for i in range(1, seats + 1)
    ]
    db.add_all(objs)
    db.flush()
    live_game_service.start_live_game(db, g.id, owner_id)
    return g, objs


def _act(db, g, owner, action):
    return live_game_service.apply_live_action(db, g.id, owner.id, action, TABLE)


def _series(db, g, seat):
    a = build_game_analytics(db, g.id)
    return next(s for s in a["life_chart"]["series"] if s["sid"] == str(seat.id))


def _ys(points: str) -> list[float]:
    return [float(p.split(",")[1]) for p in points.split()]


def _xs(points: str) -> list[float]:
    return [float(p.split(",")[0]) for p in points.split()]


def test_a_drop_and_recovery_inside_one_rotation_both_render(db):
    """THE reported bug: a seat swung high→low→high within a single round and the chart
    showed a flat line at the ending value, because samples were keyed on `e.turn`."""
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": -30})  # 40 → 10
    _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": 25})  # 10 → 35
    # No turn events at all: everything above happened inside round 1.
    assert build_game_analytics(db, g.id)["life_chart"]["max_turn"] == 1

    ys = _ys(_series(db, g, s1)["points"])
    # Three distinct life levels must be visible: 40, then the 10 trough, then 35.
    assert len(set(ys)) == 3, ys
    trough = max(ys)  # SVG y grows downward, so the lowest life is the largest y
    assert ys.index(trough) not in (0, len(ys) - 1), "the dip must be interior, not an endpoint"


def test_life_changes_render_as_steps_not_diagonals(db):
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    for _ in range(3):
        _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": -4})

    pts = _series(db, g, s1)["points"].split()
    xs, ys = _xs(_series(db, g, s1)["points"]), _ys(_series(db, g, s1)["points"])
    assert len(pts) == 1 + 2 * 3  # baseline + two points per transition
    # Every segment is axis-aligned: either x holds (vertical) or y holds (horizontal).
    for i in range(1, len(pts)):
        assert xs[i] == xs[i - 1] or ys[i] == ys[i - 1], (i, pts[i - 1], pts[i])


def test_an_eliminated_seats_line_stops_and_does_not_reach_the_right_edge(db):
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": -1})
    _act(db, g, owner, {"type": "eliminate", "seat_id": s1.id, "eliminated": True})
    for _ in range(5):  # the game continues without them
        _act(db, g, owner, {"type": "life", "seat_id": s2.id, "delta": -2})

    dead, alive = _series(db, g, s1), _series(db, g, s2)
    assert dead["ended_at_elimination"] is True
    assert alive["ended_at_elimination"] is False
    assert max(_xs(dead["points"])) < max(_xs(alive["points"]))
    assert round(max(_xs(alive["points"]))) == 330  # the survivor reaches _W - _PAD


def test_elimination_then_revive_continues_past_the_first_elimination(db):
    """Manual revive. An auto-elimination that later un-triggers must behave the same —
    covered by the auto case below."""
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    _act(db, g, owner, {"type": "eliminate", "seat_id": s1.id, "eliminated": True})
    _act(db, g, owner, {"type": "life", "seat_id": s2.id, "delta": -1})
    _act(db, g, owner, {"type": "eliminate", "seat_id": s1.id, "eliminated": False})
    for _ in range(3):
        _act(db, g, owner, {"type": "life", "seat_id": s2.id, "delta": -1})

    revived = _series(db, g, s1)
    assert revived["ended_at_elimination"] is False
    assert round(max(_xs(revived["points"]))) == 330  # runs to the right edge again


def test_auto_elimination_then_auto_revive_also_continues(db):
    """Life to 0 auto-eliminates; healing back up auto-revives (_auto_eliminate). The
    truncation must follow the LAST elimination event, not the first."""
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": -40})  # auto-elim
    assert _series(db, g, s1)["ended_at_elimination"] is True
    _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": 40})  # auto-revive
    for _ in range(3):
        _act(db, g, owner, {"type": "life", "seat_id": s2.id, "delta": -1})

    assert _series(db, g, s1)["ended_at_elimination"] is False
    assert round(max(_xs(_series(db, g, s1)["points"]))) == 330


def test_a_seat_marked_out_only_at_finalize_runs_to_the_right_edge(db):
    """`end_game` writes placement / final_life / the finalized bookend and NO eliminate
    events, so there is no position to truncate at. Do not invent one."""
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    for _ in range(4):
        _act(db, g, owner, {"type": "life", "seat_id": s2.id, "delta": -3})
    end_game(
        db,
        g.id,
        owner.id,
        placements={s1.id: 1, s2.id: 2},
        final_lives={s1.id: 40, s2.id: 0},
        turn_count=1,
        notes="",
    )
    out = _series(db, g, s2)
    assert out["ended_at_elimination"] is False
    assert round(max(_xs(out["points"]))) == 330


def test_a_seat_eliminated_before_any_life_change_still_renders(db):
    """Truncating at sample 0 leaves one point, and a one-point polyline draws nothing."""
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    _act(db, g, owner, {"type": "eliminate", "seat_id": s1.id, "eliminated": True})
    for _ in range(3):
        _act(db, g, owner, {"type": "life", "seat_id": s2.id, "delta": -2})

    pts = _series(db, g, s1)["points"].split()
    assert len(pts) == 2, pts  # a visible stub, not a single invisible point
    xs = _xs(_series(db, g, s1)["points"])
    assert xs[1] > xs[0]


def test_a_game_with_no_eliminations_is_untruncated(db):
    owner = _user(db)
    g, seats = _game(db, owner.id)
    for s in seats:
        _act(db, g, owner, {"type": "life", "seat_id": s.id, "delta": -6})
    a = build_game_analytics(db, g.id)
    assert all(not s["ended_at_elimination"] for s in a["life_chart"]["series"])
    assert all(round(max(_xs(s["points"]))) == 330 for s in a["life_chart"]["series"])


def test_round_boundaries_become_ticks_and_the_axis_is_no_longer_per_turn(db):
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": -1})
    for _ in range(6):  # rotate a full round so `turn` increments
        live = _act(db, g, owner, {"type": "turn"})
        import json as _json

        if _json.loads(live.state)["turn"] >= 2:
            break
    _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": -1})

    lc = build_game_analytics(db, g.id)["life_chart"]
    assert lc["max_turn"] == 2
    assert [t["round"] for t in lc["round_ticks"]] == [2]
    assert 10 <= lc["round_ticks"][0]["x"] <= 330


def test_cmd_damage_uses_the_persisted_actual_delta(db):
    """The floor rule must never be re-derived here: a -9 against a cmd value of 2
    restores 2, not 9."""
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    _act(
        db,
        g,
        owner,
        {"type": "cmd", "receiver_seat_id": s1.id, "attacker_seat_id": s2.id, "delta": 2},
    )
    _act(
        db,
        g,
        owner,
        {"type": "cmd", "receiver_seat_id": s1.id, "attacker_seat_id": s2.id, "delta": -9},
    )
    assert _series(db, g, s1)["final"] == 40  # 40 - 2 + 2


def test_pre_v43_games_still_hide_the_section(db):
    owner = _user(db)
    g = Game(user_id=owner.id, format="Commander", status="finalized", client_token=TABLE)
    db.add(g)
    db.flush()
    db.add(GameSeat(game_id=g.id, seat_number=1, player_name="P1", starting_life=40))
    db.flush()
    assert build_game_analytics(db, g.id) is None


def test_a_game_with_no_life_events_does_not_divide_by_zero(db):
    """n_samples == 1, so the x-span guard has to hold. Reachable in practice: a game
    started, a few turns passed, nobody took damage."""
    owner = _user(db)
    g, _seats = _game(db, owner.id)
    _act(db, g, owner, {"type": "turn"})
    _act(db, g, owner, {"type": "turn"})

    lc = build_game_analytics(db, g.id)["life_chart"]
    assert len(lc["series"]) == 3
    for s in lc["series"]:
        assert s["final"] == 40
        assert len(s["points"].split()) == 2  # the stub, drawn rather than lost


# ── #154 — the chart/standings caveat ────────────────────────────────────────


def test_the_caveat_appears_only_when_a_line_is_actually_truncated(db):
    """#154, option 1 (label, don't reconcile). The note explains a real divergence; a
    game with no eliminations has nothing to caveat and must not carry one."""
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    for _ in range(3):
        _act(db, g, owner, {"type": "life", "seat_id": s2.id, "delta": -2})
    assert build_game_analytics(db, g.id)["life_chart"]["any_truncated"] is False

    _act(db, g, owner, {"type": "eliminate", "seat_id": s1.id, "eliminated": True})
    assert build_game_analytics(db, g.id)["life_chart"]["any_truncated"] is True

    # ...and it goes away again on revive, along with the truncation it describes.
    _act(db, g, owner, {"type": "eliminate", "seat_id": s1.id, "eliminated": False})
    assert build_game_analytics(db, g.id)["life_chart"]["any_truncated"] is False


def test_a_commander_damage_death_is_the_case_the_caveat_covers(db):
    """21 commander damage kills on POSITIVE life, so chart terminal (19) and recorded
    final_life (0) differ by construction — not a bug in either. #154."""
    owner = _user(db)
    g, (victim, killer, _s3) = _game(db, owner.id)
    _act(
        db,
        g,
        owner,
        {"type": "cmd", "receiver_seat_id": victim.id, "attacker_seat_id": killer.id, "delta": 21},
    )
    end_game(
        db,
        g.id,
        owner.id,
        placements={killer.id: 1, victim.id: 2},
        final_lives={victim.id: 0, killer.id: 40},
        turn_count=1,
        notes="",
    )
    lc = build_game_analytics(db, g.id)["life_chart"]
    row = next(s for s in lc["series"] if s["sid"] == str(victim.id))
    assert row["final"] == 19 and row["ended_at_elimination"] is True
    assert db.get(GameSeat, victim.id).final_life == 0  # standings disagrees, correctly
    assert lc["any_truncated"] is True  # so the caveat is shown
