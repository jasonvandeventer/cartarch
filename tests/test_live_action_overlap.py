"""#155 — request-overlap instrumentation on the live-action path.

Diagnostic only: these tests pin that the record is emitted with the right fields,
that a real lost update is FLAGGED, and — the part that matters most — that none of
it changes what the app does.

The lost-update setup is the same stale-read pair as `test_life_consistency.py`:
two sessions both read the live row before either commits, so the second write is
computed from the pre-first-commit snapshot. See the note there on why a strong
reference to the loaded object is load-bearing (the identity map is weak).
"""

from __future__ import annotations

import itertools
import json
import logging

from sqlalchemy.orm import sessionmaker

from app import live_game_service
from app.live_game_service import apply_live_action
from app.models import Game, GameLiveState, GameSeat, User

TABLE = "TABLETOK"
_seq = itertools.count(1)


def _setup(db_engine, seats=3):
    Sess = sessionmaker(bind=db_engine)
    s = Sess()
    u = User(username=f"o{next(_seq)}@ex.com", password_hash="x")
    s.add(u)
    s.flush()
    g = Game(user_id=u.id, format="Commander", status="created", client_token=TABLE)
    s.add(g)
    s.flush()
    objs = [
        GameSeat(game_id=g.id, seat_number=i, player_name=f"P{i}", starting_life=40)
        for i in range(1, seats + 1)
    ]
    s.add_all(objs)
    s.flush()
    live_game_service.start_live_game(s, g.id, u.id)
    ids = (g.id, u.id, [o.id for o in objs])
    s.commit()
    s.close()
    return Sess, ids


def _reset_state():
    live_game_service._last_written_version.clear()
    live_game_service._live_in_flight.clear()


def _records(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("live_action ")]


def _warnings(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("LOST UPDATE ")]


def test_every_applied_action_emits_a_record_with_the_required_fields(db_engine, caplog):
    _reset_state()
    Sess, (gid, uid, seats) = _setup(db_engine)
    s = Sess()
    with caplog.at_level(logging.INFO, logger="app.live_game_service"):
        apply_live_action(s, gid, uid, {"type": "life", "seat_id": seats[0], "delta": -3}, TABLE)
    s.close()

    recs = _records(caplog)
    assert len(recs) == 1
    msg = recs[0].getMessage()
    # AC: game id, actor, action type, start, duration, and BOTH versions.
    for field in (
        f"game={gid}",
        f"actor={uid}",
        "type=life",
        "v_read=1",
        "v_written=2",
        "start=",
        "dur_ms=",
        "concurrent=0",
        "lost_update=False",
    ):
        assert field in msg, (field, msg)
    assert not _warnings(caplog)


def test_a_real_lost_update_is_flagged(db_engine, caplog):
    """Two requests read version N and both write N+1; the second discarded the
    first's mutation. That is the #153 hypothesis, and it must be visible in the
    log without correlating two lines by hand."""
    _reset_state()
    Sess, (gid, uid, seats) = _setup(db_engine)
    a, b = Sess(), Sess()
    # Strong refs: the identity map is a WeakValueDictionary, so an unreferenced
    # instance is collected and the next query silently re-reads fresh rows.
    held = [s.query(GameLiveState).filter(GameLiveState.game_id == gid).first() for s in (a, b)]
    assert [h.version for h in held] == [1, 1]

    with caplog.at_level(logging.INFO, logger="app.live_game_service"):
        apply_live_action(a, gid, uid, {"type": "life", "seat_id": seats[0], "delta": -5}, TABLE)
        apply_live_action(b, gid, uid, {"type": "life", "seat_id": seats[0], "delta": -3}, TABLE)

    recs = _records(caplog)
    assert len(recs) == 2
    assert "lost_update=False" in recs[0].getMessage()
    assert "lost_update=True" in recs[1].getMessage()

    warns = _warnings(caplog)
    assert len(warns) == 1
    w = warns[0].getMessage()
    assert "v_read=1" in w and "v_written=2" in w and "already_written=2" in w

    # And the loss is real: only the second write's mutation survived.
    chk = Sess()
    assert json.loads(chk.get(Game, gid).live_state.state)["lives"][str(seats[0])] == 37
    chk.close()
    a.close()
    b.close()


def test_sequential_actions_are_never_flagged(db_engine, caplog):
    """The detector must not cry wolf on ordinary play — one client, many taps."""
    _reset_state()
    Sess, (gid, uid, seats) = _setup(db_engine)
    s = Sess()
    with caplog.at_level(logging.INFO, logger="app.live_game_service"):
        for i in range(12):
            apply_live_action(
                s, gid, uid, {"type": "life", "seat_id": seats[i % 3], "delta": -1}, TABLE
            )
    s.close()
    assert len(_records(caplog)) == 12
    assert not _warnings(caplog)
    assert all("lost_update=False" in r.getMessage() for r in _records(caplog))


def test_a_rejected_action_does_not_leak_an_in_flight_count(db_engine, caplog):
    """The in-flight counter is what 'concurrent=' reads. If a raise leaked a count,
    every later action would over-report overlap forever."""
    _reset_state()
    Sess, (gid, uid, _seats) = _setup(db_engine)
    s = Sess()
    for bad in ({"type": "nonsense"}, {"type": "life", "seat_id": 999999, "delta": -1}):
        try:
            apply_live_action(s, gid, uid, bad, TABLE)
        except (ValueError, LookupError, PermissionError):
            pass
        s.rollback()
    assert live_game_service._live_in_flight == {}

    with caplog.at_level(logging.INFO, logger="app.live_game_service"):
        apply_live_action(s, gid, uid, {"type": "turn"}, TABLE)
    assert "concurrent=0" in _records(caplog)[0].getMessage()
    s.close()


def test_instrumentation_failure_cannot_break_an_action(db_engine, monkeypatch):
    """Telemetry is not allowed to fail a live game. Break it and the action still
    applies and returns normally."""
    _reset_state()
    Sess, (gid, uid, seats) = _setup(db_engine)

    def boom(*_a, **_k):
        raise RuntimeError("instrumentation exploded")

    monkeypatch.setattr(live_game_service, "_last_written_version", boom)
    s = Sess()
    live = apply_live_action(s, gid, uid, {"type": "life", "seat_id": seats[0], "delta": -4}, TABLE)
    assert json.loads(live.state)["lives"][str(seats[0])] == 36
    assert live.version == 2
    s.close()


def test_the_game_map_is_bounded(db_engine):
    """A diagnostic must not leak memory across a long-lived pod."""
    _reset_state()
    cap = live_game_service._LIVE_OVERLAP_MAX_GAMES
    for gid in range(cap + 50):
        live_game_service._record_live_action(gid, 1, "life", 1, 2, "2026-07-26T00:00:00", 0.0)
    assert len(live_game_service._last_written_version) == cap
    # Oldest evicted, newest retained.
    assert 0 not in live_game_service._last_written_version
    assert (cap + 49) in live_game_service._last_written_version
    _reset_state()
