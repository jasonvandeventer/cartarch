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
import os
import threading

import pytest
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


def test_a_stale_reader_no_longer_loses_the_other_writers_change(db_engine):
    """THE FIX (v4.14.1). This is the exact setup that used to lose a write.

    Two sessions each hold the live row at version 1 — strong refs, because the
    identity map is weak and an unreferenced instance would be silently re-read.
    The second action is computed from a snapshot taken before the first
    committed, which is precisely the production race: five of these were
    recorded on 2026-08-16, every one a `life` tap.

    ``apply_live_action`` now re-reads the row under a lock (``populate_existing``
    on a ``with_for_update`` query), so the second writer applies -3 on top of the
    committed -5 instead of on top of its own stale copy. 40 - 5 - 3 = 32; the bug
    produced 37, having thrown the -5 away.
    """
    _reset_state()
    Sess, (gid, uid, seats) = _setup(db_engine)
    a, b = Sess(), Sess()
    held = [s.query(GameLiveState).filter(GameLiveState.game_id == gid).first() for s in (a, b)]
    assert [h.version for h in held] == [1, 1], "both sessions must start from the same version"

    apply_live_action(a, gid, uid, {"type": "life", "seat_id": seats[0], "delta": -5}, TABLE)
    apply_live_action(b, gid, uid, {"type": "life", "seat_id": seats[0], "delta": -3}, TABLE)

    chk = Sess()
    assert json.loads(chk.get(Game, gid).live_state.state)["lives"][str(seats[0])] == 32
    assert chk.get(Game, gid).live_state.version == 3, "both writes landed"
    chk.close()
    a.close()
    b.close()


def test_the_detector_still_reports_a_clobber_if_one_ever_happens(db_engine, caplog):
    """The instrumentation outlives the fix until it has soaked, so it still has
    to work. Driven at the recorder, because the write path can no longer produce
    the condition — which is the point of the change above."""
    _reset_state()
    with caplog.at_level(logging.INFO, logger="app.live_game_service"):
        live_game_service._record_live_action(99, 1, "life", 1, 2, "start", 0.0, 1)
        live_game_service._record_live_action(99, 2, "life", 1, 2, "start", 0.0, 1)

    recs = _records(caplog)
    assert len(recs) == 2
    assert "lost_update=False" in recs[0].getMessage()
    assert "lost_update=True" in recs[1].getMessage()
    w = _warnings(caplog)
    assert len(w) == 1
    assert "v_read=1" in w[0].getMessage() and "already_written=2" in w[0].getMessage()


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


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="genuine concurrency needs Postgres — SQLite serialises writers itself, "
    "so this can only be proven where prod actually runs",
)
def test_two_concurrent_writers_lose_nothing_on_postgres(db_engine):
    """The lock, under real threads. A single-process stale-read test cannot see
    this: it proves the re-read, not the serialisation.

    Two threads tap the same seat 25 times each. Every tap is a read-mutate-write
    of one JSON blob, so without ``FOR UPDATE`` the two transactions interleave
    (READ COMMITTED lets both read the same version) and taps go missing — the
    production signature. With it, the second writer waits, re-reads, and applies
    on top: 40 - 50 = -10, and 50 version bumps.
    """
    Sess, (gid, uid, seats) = _setup(db_engine)
    errors: list[Exception] = []

    def tap(n):
        s = Sess()
        try:
            for _ in range(n):
                apply_live_action(
                    s, gid, uid, {"type": "life", "seat_id": seats[0], "delta": -1}, TABLE
                )
        except Exception as exc:  # surfaced below — a thread's raise is invisible
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=tap, args=(25,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"a writer failed: {errors[0]!r}"
    chk = Sess()
    live = chk.get(Game, gid).live_state
    assert json.loads(live.state)["lives"][str(seats[0])] == -10, "a tap was lost"
    assert live.version == 51, "every write must bump the version exactly once"
    chk.close()
