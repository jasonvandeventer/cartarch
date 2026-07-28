"""#161 — a run of taps is ONE x-position, so a big hit reads as a cliff not a staircase.

Players enter damage by tapping ±1 repeatedly, so a single hit for 12 drew as twelve
one-life steps. 824 of the 1075 life-affecting events on record are `life` taps and
almost all are ±1; collapsing absorbs 689 of 1075 x-positions (64.1%).

A run continues while all three hold and breaks when any one changes:

  1. same affected seat, 2. same round, 3. same direction.

**The direction guard is the one that matters.** Without it this reintroduces exactly
the bug #151 fixed — 40 → 10 → 35 with nobody acting in between is two consecutive
same-seat samples, and merging them renders 40 → 35 and erases the trough. That case
lives in `test_life_chart_resolution.py` and passes with its assertions UNCHANGED; the
version here approaches it from the collapse side.

Everything drives the real live-game service, so the chart consumes exactly what the
service persists.
"""

from __future__ import annotations

import itertools

from app import live_game_service
from app.game_analytics_service import build_game_analytics
from app.models import Game, GameSeat, User

TABLE = "TABLETOK"
_seq = itertools.count(1)


def _user(db) -> User:
    u = User(username=f"rc{next(_seq)}@ex.com", password_hash="x")
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


def _life(db, g, owner, seat, delta):
    return _act(db, g, owner, {"type": "life", "seat_id": seat.id, "delta": delta})


def _chart(db, g):
    return build_game_analytics(db, g.id)["life_chart"]


def _series(db, g, seat):
    return next(s for s in _chart(db, g)["series"] if s["sid"] == str(seat.id))


def _ys(points: str) -> list[float]:
    return [float(p.split(",")[1]) for p in points.split()]


def _samples(points: str) -> int:
    """Sample count for a rendered row. Step rendering emits `1 + 2*(N-1)` points for
    N samples, so this inverts that. The chart dict exposes no sample count, and
    deriving it from the geometry is the stronger check anyway — it proves what was
    actually drawn, not what the builder believed."""
    return (len(points.split()) + 1) // 2


def _n_samples(db, g) -> int:
    """Chart-wide sample count = the LONGEST row, i.e. a seat that ran to the right
    edge. A truncated (eliminated) row is shorter by construction."""
    return max(_samples(s["points"]) for s in _chart(db, g)["series"])


# ── The headline behaviour ──────────────────────────────────────────────────


def test_a_run_of_taps_is_one_x_position(db):
    """Twelve ±1 taps for one hit = one drop, not twelve steps."""
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    for _ in range(12):
        _life(db, g, owner, s1, -1)

    # baseline + ONE collapsed sample
    assert _n_samples(db, g) == 2
    # 1 + 2*(N-1) points for N samples
    assert len(_series(db, g, s1)["points"].split()) == 1 + 2 * 1


def test_the_collapsed_sample_carries_the_FULL_run_total(db):
    """Collapsing must not lose magnitude — the cliff is the whole hit."""
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    for _ in range(12):
        _life(db, g, owner, s1, -1)

    ys = _ys(_series(db, g, s1)["points"])
    # Two distinct y values: 40 and 28. Highest life sits at the smallest y.
    assert len(set(ys)) == 2
    assert _series(db, g, s1)["final"] == 28


def test_other_seats_carry_forward_across_a_collapsed_run(db):
    """Rows must stay the same length or the polylines desynchronise."""
    owner = _user(db)
    g, (s1, s2, s3) = _game(db, owner.id)
    for _ in range(5):
        _life(db, g, owner, s1, -2)

    lens = {len(_series(db, g, s)["points"].split()) for s in (s1, s2, s3)}
    assert len(lens) == 1, "seat rows drifted out of step"


# ── Each guard, shown to be load-bearing ────────────────────────────────────


def test_a_DIRECTION_change_breaks_the_run(db):
    """THE guard. Merging a drop and a recovery erases the trough (#151)."""
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    _life(db, g, owner, s1, -30)  # 40 -> 10
    _life(db, g, owner, s1, +25)  # 10 -> 35, nobody else acted in between

    ys = _ys(_series(db, g, s1)["points"])
    assert _n_samples(db, g) == 3, "the drop and the recovery merged"
    # The trough must be visible: some y is strictly lower on the chart (larger y
    # value) than both the start and the end.
    assert max(ys) > ys[0] and max(ys) > ys[-1], "the trough was erased"


def test_a_SEAT_change_breaks_the_run(db):
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    _life(db, g, owner, s1, -3)
    _life(db, g, owner, s2, -3)

    assert _n_samples(db, g) == 3


def test_a_ROUND_change_breaks_the_run(db):
    """Otherwise the round tick lands at the same x as the collapsed point."""
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    _life(db, g, owner, s1, -3)
    # Advance the rotation until the round counter increments.
    for _ in range(6):
        _act(db, g, owner, {"type": "turn"})
    _life(db, g, owner, s1, -3)

    assert _n_samples(db, g) == 3, "a run straddled a round boundary"
    # Ticks carry an x coordinate, not an index. A tick at the left edge would mean
    # the boundary collapsed onto the baseline column.
    assert all(t["x"] > 0 for t in _chart(db, g)["round_ticks"])


def test_a_zero_delta_event_never_joins_a_run(db):
    """sign 0 is not a direction; folding it in would hide a recorded no-op."""
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    _life(db, g, owner, s1, -3)
    _life(db, g, owner, s1, 0)
    _life(db, g, owner, s1, -3)

    assert _n_samples(db, g) == 4


# ── The index-based consumers stay consistent ───────────────────────────────


def test_an_elimination_cut_lands_on_the_collapsed_index(db):
    """`cut_at` is set from the live idx, so collapsing must keep it aligned."""
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    for _ in range(4):
        _life(db, g, owner, s1, -10)  # one collapsed sample, s1 -> 0
    _act(db, g, owner, {"type": "eliminate", "seat_id": s1.id, "eliminated": True})
    for _ in range(3):
        _life(db, g, owner, s2, -5)  # more samples AFTER s1 is out

    s1_pts = len(_series(db, g, s1)["points"].split())
    s2_pts = len(_series(db, g, s2)["points"].split())
    assert _n_samples(db, g) == 3  # baseline + s1 run + s2 run
    assert s1_pts < s2_pts, "the eliminated seat's line did not stop early"


def test_an_elimination_breaks_the_open_run(db):
    """A revived seat must not fold back into a column before its own cut."""
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    _life(db, g, owner, s1, -5)
    _act(db, g, owner, {"type": "eliminate", "seat_id": s1.id, "eliminated": True})
    _act(db, g, owner, {"type": "eliminate", "seat_id": s1.id, "eliminated": False})
    _life(db, g, owner, s1, -5)  # same seat, same round, same direction

    # Without the elimination break these two would collapse into one column.
    assert _n_samples(db, g) == 3


def test_a_mixed_sequence_collapses_only_the_runs(db):
    """End to end: 3 taps + 3 taps on another seat + 2 back on the first."""
    owner = _user(db)
    g, (s1, s2, _s3) = _game(db, owner.id)
    for _ in range(3):
        _life(db, g, owner, s1, -1)
    for _ in range(3):
        _life(db, g, owner, s2, -1)
    for _ in range(2):
        _life(db, g, owner, s1, -1)

    # 8 events -> baseline + 3 runs
    assert _n_samples(db, g) == 4
    assert _series(db, g, s1)["final"] == 35
    assert _series(db, g, s2)["final"] == 37


def test_the_write_path_is_untouched(db):
    """#161 is a RENDERING change: the stored stream keeps full per-tap resolution."""
    from app.models import GameEvent

    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    for _ in range(12):
        _life(db, g, owner, s1, -1)

    stored = (
        db.query(GameEvent)
        .filter(GameEvent.game_id == g.id, GameEvent.action_type == "life")
        .count()
    )
    assert stored == 12, "collapsing must not thin the recorded event stream"
    assert _n_samples(db, g) == 2
