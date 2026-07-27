"""#153 — life-total consistency between the three artifacts, and a reproduction
of the mechanism that makes them disagree.

Three artifacts claim to know a seat's final life:
  blob       `finalized` bookend payload      SERVER, at end_game time
  replay     live_started + every life/cmd    SERVER, reconstructed
  final_life game_seats.final_life            CLIENT, posted by the End-game modal

`check_life_consistency` reports both gaps separately, because they have
different causes and only the first implies a server defect.
"""

from __future__ import annotations

import itertools
import json

from sqlalchemy.orm import sessionmaker

from app import live_game_service
from app.game_analytics_service import check_life_consistency, scan_life_consistency
from app.game_service import end_game
from app.models import Game, GameLiveState, GameSeat, User

TABLE = "TABLETOK"
_seq = itertools.count(1)


def _user(db) -> User:
    u = User(username=f"u{next(_seq)}@ex.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


def _game(db, owner_id, *, fmt="Commander", seats=3):
    g = Game(user_id=owner_id, format=fmt, status="created", client_token=TABLE)
    db.add(g)
    db.flush()
    objs = [
        GameSeat(game_id=g.id, seat_number=i, player_name=f"P{i}", starting_life=40)
        for i in range(1, seats + 1)
    ]
    db.add_all(objs)
    db.flush()
    return g, objs


def _act(db, g, owner, action):
    return live_game_service.apply_live_action(db, g.id, owner.id, action, TABLE)


def _finalize(db, g, owner, final_lives):
    end_game(db, g.id, owner.id, placements={}, final_lives=final_lives, turn_count=1, notes="")


def _seat(result, seat_id):
    return next(s for s in result["seats"] if s["seat_id"] == seat_id)


# ── the happy path: all three artifacts agree ────────────────────────────────


def test_clean_game_reconciles_on_all_three_artifacts(db):
    owner = _user(db)
    g, (s1, s2, s3) = _game(db, owner.id)
    live_game_service.start_live_game(db, g.id, owner.id)
    _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": -5})
    _act(
        db,
        g,
        owner,
        {"type": "cmd", "receiver_seat_id": s2.id, "attacker_seat_id": s1.id, "delta": 7},
    )
    blob = json.loads(db.get(Game, g.id).live_state.state)["lives"]
    _finalize(db, g, owner, {int(k): v for k, v in blob.items()})

    r = check_life_consistency(db, g.id)
    assert r is not None
    assert r["replay_diverged"] == []
    assert r["final_diverged"] == []
    assert _seat(r, s1.id)["replay"] == 35
    assert _seat(r, s2.id)["replay"] == 33  # coupled cmd hit, post-floor
    assert _seat(r, s3.id)["replay"] == 40
    for s in r["seats"]:
        assert s["replay"] == s["blob"] == s["final_life"]


# ── the mechanism: a lost update writes an event whose mutation does not survive ─


def _stale_write_pair(db_engine, delta_a, delta_b):
    """Two concurrent requests that both READ the blob before either COMMITS.

    Models the interleaving `apply_live_action` permits: it json.loads the whole
    blob, mutates, re-serialises and commits, with no optimistic locking (the
    `version` bump is for SSE ordering, not conflict rejection). Each request gets
    its own Session, so once both have loaded the GameLiveState row, the second to
    commit writes a state derived from the PRE-first-commit snapshot — discarding
    the first writer's mutation while its event row survives.

    Deterministic rather than threaded: holding a loaded, unexpired object in a
    second Session's identity map reproduces the same stale read without a race.
    Returns (game_id, seat_id, owner_id, baseline).
    """
    Sess = sessionmaker(bind=db_engine)
    setup = Sess()
    owner = _user(setup)
    g, (s1, _s2, _s3) = _game(setup, owner.id)
    live_game_service.start_live_game(setup, g.id, owner.id)
    gid, sid, oid = g.id, s1.id, owner.id
    baseline = json.loads(setup.get(Game, gid).live_state.state)["lives"][str(sid)]
    setup.commit()
    setup.close()

    a, b = Sess(), Sess()
    # BOTH load the live row before either writes — the concurrent-request window.
    # The `held` refs are load-bearing: SQLAlchemy's identity map is a
    # WeakValueDictionary, so an unreferenced instance is garbage-collected and the
    # next query silently re-reads fresh rows, dissolving the stale snapshot this
    # test depends on. A real request holds its loaded objects for its duration.
    held = [s.query(GameLiveState).filter(GameLiveState.game_id == gid).first() for s in (a, b)]
    assert [json.loads(h.state)["lives"][str(sid)] for h in held] == [baseline, baseline]

    live_game_service.apply_live_action(
        a, gid, oid, {"type": "life", "seat_id": sid, "delta": delta_a}, TABLE
    )
    # b still holds its pre-A snapshot, so its write is computed from the old blob.
    live_game_service.apply_live_action(
        b, gid, oid, {"type": "life", "seat_id": sid, "delta": delta_b}, TABLE
    )
    a.close()
    b.close()
    return gid, sid, oid, baseline


def test_concurrent_write_loses_a_mutation_while_keeping_its_event(db_engine):
    """The standing #153 hypothesis, reproduced: two events are recorded, only one
    mutation survives, so replay and blob disagree by exactly the lost delta."""
    gid, sid, oid, baseline = _stale_write_pair(db_engine, -5, -3)
    Sess = sessionmaker(bind=db_engine)
    s = Sess()
    g = s.get(Game, gid)
    blob_lives = json.loads(g.live_state.state)["lives"]
    assert int(blob_lives[str(sid)]) == baseline - 3  # A's -5 was overwritten
    _finalize(s, g, s.get(User, oid), {int(k): v for k, v in blob_lives.items()})

    r = check_life_consistency(s, gid)
    row = _seat(r, sid)
    assert row["replay"] == baseline - 8  # BOTH events replay
    assert row["blob"] == baseline - 3  # only one mutation landed
    assert row["replay_vs_blob"] == -5  # exactly the lost delta
    assert [x["seat_id"] for x in r["replay_diverged"]] == [sid]
    assert r["final_diverged"] == []  # final_life copied from the blob → clean
    s.close()


def test_lost_life_GAIN_makes_replay_read_higher_than_the_blob(db_engine):
    """#153 records divergence in BOTH directions. A lost gain inverts the sign,
    so direction alone does not distinguish this mechanism from another."""
    gid, sid, oid, baseline = _stale_write_pair(db_engine, +6, -2)
    Sess = sessionmaker(bind=db_engine)
    s = Sess()
    g = s.get(Game, gid)
    blob_lives = json.loads(g.live_state.state)["lives"]
    _finalize(s, g, s.get(User, oid), {int(k): v for k, v in blob_lives.items()})
    row = _seat(check_life_consistency(s, gid), sid)
    assert row["replay"] == baseline + 4
    assert row["blob"] == baseline - 2
    assert row["replay_vs_blob"] == 6  # replay HIGHER than the blob
    s.close()


def test_one_lost_write_can_diverge_several_seats_at_once(db_engine):
    """The losing writer re-serialises the WHOLE blob, so it discards every
    concurrent change, not just its own seat's. Predicts multi-seat divergence in
    a single game — which is what #153 observed."""
    Sess = sessionmaker(bind=db_engine)
    setup = Sess()
    owner = _user(setup)
    g, (s1, s2, s3) = _game(setup, owner.id)
    live_game_service.start_live_game(setup, g.id, owner.id)
    gid, oid = g.id, owner.id
    ids = [s1.id, s2.id, s3.id]
    setup.commit()
    setup.close()

    a, b = Sess(), Sess()
    # Strong refs — see the note in _stale_write_pair on the weak identity map.
    held = [s.query(GameLiveState).filter(GameLiveState.game_id == gid).first() for s in (a, b)]
    assert len(held) == 2
    # A hits two different seats; B (stale) then clobbers the blob for both.
    for seat_id in (ids[0], ids[1]):
        live_game_service.apply_live_action(
            a, gid, oid, {"type": "life", "seat_id": seat_id, "delta": -4}, TABLE
        )
    live_game_service.apply_live_action(
        b, gid, oid, {"type": "life", "seat_id": ids[2], "delta": -1}, TABLE
    )
    a.close()
    b.close()

    s = Sess()
    g = s.get(Game, gid)
    blob_lives = json.loads(g.live_state.state)["lives"]
    _finalize(s, g, s.get(User, oid), {int(k): v for k, v in blob_lives.items()})
    r = check_life_consistency(s, gid)
    assert sorted(x["seat_id"] for x in r["replay_diverged"]) == sorted(ids[:2])
    assert all(x["replay_vs_blob"] == -4 for x in r["replay_diverged"])
    s.close()


# ── the client-form gap is reported separately ───────────────────────────────


def test_client_submitted_final_life_gap_is_reported_without_a_server_defect(db):
    """final_life is posted by the End-game modal, not read from the blob. A stale
    or hand-edited value diverges with the server perfectly consistent — so it must
    not be counted as the #153 event defect."""
    owner = _user(db)
    g, (s1, _s2, _s3) = _game(db, owner.id)
    live_game_service.start_live_game(db, g.id, owner.id)
    _act(db, g, owner, {"type": "life", "seat_id": s1.id, "delta": -6})
    blob = {int(k): v for k, v in json.loads(db.get(Game, g.id).live_state.state)["lives"].items()}
    blob[s1.id] = blob[s1.id] + 1  # what a stale modal / typed correction submits
    _finalize(db, g, owner, blob)

    r = check_life_consistency(db, g.id)
    assert r["replay_diverged"] == []  # server side is clean
    assert [x["seat_id"] for x in r["final_diverged"]] == [s1.id]
    assert _seat(r, s1.id)["final_vs_blob"] == 1


# ── refusals + the scan wrapper ──────────────────────────────────────────────


def test_momir_game_is_not_judged(db):
    """momir_damage / combat move life with no reconstructable per-event delta, so
    replaying one would report false divergences."""
    owner = _user(db)
    g, _seats = _game(db, owner.id, fmt="momir")
    live_game_service.start_live_game(db, g.id, owner.id)
    assert check_life_consistency(db, g.id) is None


def test_game_without_events_is_not_judged(db):
    owner = _user(db)
    g, _seats = _game(db, owner.id)
    g.status = "finalized"
    db.flush()
    assert check_life_consistency(db, g.id) is None


def test_scan_reports_only_judgeable_games_and_finds_the_diverged_one(db_engine):
    gid, sid, oid, _baseline = _stale_write_pair(db_engine, -5, -3)
    Sess = sessionmaker(bind=db_engine)
    s = Sess()
    g = s.get(Game, gid)
    _finalize(s, g, s.get(User, oid), {})
    # A second, un-startable game contributes no events and must be skipped.
    _game(s, oid)
    s.commit()

    results = scan_life_consistency(s)
    assert [r["game_id"] for r in results] == [gid]
    assert results[0]["replay_diverged"]
    s.close()
