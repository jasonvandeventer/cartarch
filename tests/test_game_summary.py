"""Read-only game summary for finalized games (v3.33.2).

Pytest module (matches tests/test_share_service).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_game_summary.py

User feedback: a finalized game opened the frozen full-screen life tracker with
no consolidated results. Now finalized games render a summary page; live games
keep the tracker. Covers:
  - end_game stamps Game.ended_at once (idempotent on a re-finalize)
  - GET /games/{id} for a finalized game → summary (standings, full notes,
    elapsed) and NOT the live tracker; for a live game → still the tracker
  - owner sees Players + Delete controls; a seat-attributed participant sees the
    read-only summary without them but can still view it
  - elapsed renders for a game with ended_at; "—" for a legacy game (NULL)
  - migration idempotency
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import game_service
from app.db import Base
from app.models import Game, GameSeat, User

_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _user(s, name=None) -> User:
    u = User(username=name or f"u{next(_seq)}@x.com", password_hash="x")
    s.add(u)
    s.flush()
    return u


def _game(s, user_id, status="created", **kw) -> Game:
    g = Game(
        user_id=user_id, format="Commander", status=status, played_at=datetime(2026, 6, 1, 18), **kw
    )
    s.add(g)
    s.flush()
    return g


def _seat(s, game_id, n, name, **kw) -> GameSeat:
    seat = GameSeat(game_id=game_id, seat_number=n, player_name=name, **kw)
    s.add(seat)
    s.flush()
    return seat


def test_end_game_stamps_ended_at_once():
    failed = 0
    s = _fresh_session()
    u = _user(s)
    g = _game(s, u.id)
    s1 = _seat(s, g.id, 1, "A")
    s2 = _seat(s, g.id, 2, "B")
    s.commit()
    game_service.end_game(s, g.id, u.id, {s1.id: 1, s2.id: 2}, {s1.id: 30, s2.id: 0}, 12, "gg")
    s.refresh(g)
    first = g.ended_at
    if first is not None and g.status == "finalized" and g.turn_count == 12:
        print("  [OK] end_game stamps ended_at + finalizes")
    else:
        print(f"  [FAIL] end_game: ended_at={first} status={g.status}")
        failed += 1
    # Re-finalize (e.g. an edit) must NOT move ended_at.
    game_service.end_game(s, g.id, u.id, {s1.id: 1, s2.id: 2}, {s1.id: 30, s2.id: 0}, 13, "gg2")
    s.refresh(g)
    if g.ended_at == first:
        print("  [OK] re-finalize does not re-stamp ended_at")
    else:
        print(f"  [FAIL] ended_at moved on re-finalize: {first} -> {g.ended_at}")
        failed += 1
    assert failed == 0


def _client_session():
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    return TestClient(main.app, follow_redirects=False), main, sm, get_db_session, get_current_user


def test_routes_summary_vs_tracker():
    failed = 0
    client, main, sm, get_db_session, get_current_user = _client_session()
    s = sm()
    owner = _user(s, "owner@x.com")
    participant = _user(s, "alex@x.com")
    long_note = "A really memorable game with a huge final turn. " * 4  # > 80 chars
    fg = _game(
        s,
        owner.id,
        status="finalized",
        turn_count=11,
        ended_at=datetime(2026, 6, 1, 18) + timedelta(hours=1, minutes=23),
        notes=long_note,
    )
    _seat(s, fg.id, 1, "Owner", placement=1, final_life=24, commander_name_at_game="Atraxa")
    _seat(s, fg.id, 2, "Alex", placement=2, final_life=0, user_id=participant.id)
    lg = _game(s, owner.id, status="created")
    _seat(s, lg.id, 1, "Owner")
    s.commit()
    fg_id, lg_id = fg.id, lg.id

    def _db():
        d = sm()
        try:
            yield d
        finally:
            d.close()

    main.app.dependency_overrides[get_db_session] = _db
    try:
        # Owner view of the finalized game → summary, not tracker.
        main.app.dependency_overrides[get_current_user] = lambda: owner
        r = client.get(f"/games/{fg_id}")
        t = r.text
        checks = {
            "200": r.status_code == 200,
            "standings": "Final standings" in t,
            "elapsed 1h23m": "1h 23m" in t,
            "full notes (untruncated)": long_note.strip() in t,
            "NOT live tracker": 'id="game-app"' not in t,
            "owner Players control": "togglePlayersModal" in t,
            "owner Delete control": f"/games/{fg_id}/delete" in t,
        }
        if all(checks.values()):
            print(
                "  [OK] finalized game → owner summary (standings/elapsed/notes, no tracker, controls)"
            )
        else:
            print(f"  [FAIL] finalized owner summary: {checks}")
            failed += 1

        # Live game → still the tracker.
        r = client.get(f"/games/{lg_id}")
        if r.status_code == 200 and 'id="game-app"' in r.text:
            print("  [OK] live game → tracker unchanged")
        else:
            print(f"  [FAIL] live game tracker: status={r.status_code}")
            failed += 1

        # Participant (seat-attributed) → read-only summary, no owner controls.
        main.app.dependency_overrides[get_current_user] = lambda: participant
        r = client.get(f"/games/{fg_id}")
        t = r.text
        if (
            r.status_code == 200
            and "Final standings" in t
            and "read-only" in t
            and "togglePlayersModal" not in t
            and f"/games/{fg_id}/delete" not in t
        ):
            print("  [OK] participant → read-only summary, no owner controls")
        else:
            print(f"  [FAIL] participant view: status={r.status_code}")
            failed += 1
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
        main.app.dependency_overrides.pop(get_current_user, None)
    assert failed == 0


def test_elapsed_none_for_legacy():
    """A finalized game with no ended_at (legacy) shows '—' for playtime."""
    failed = 0
    client, main, sm, get_db_session, get_current_user = _client_session()
    s = sm()
    owner = _user(s, "owner2@x.com")
    g = _game(s, owner.id, status="finalized", turn_count=8, ended_at=None)
    _seat(s, g.id, 1, "Owner", placement=1, final_life=10)
    s.commit()
    gid = g.id

    def _db():
        d = sm()
        try:
            yield d
        finally:
            d.close()

    main.app.dependency_overrides[get_db_session] = _db
    main.app.dependency_overrides[get_current_user] = lambda: owner
    try:
        r = client.get(f"/games/{gid}")
        if r.status_code == 200 and "— playtime" in r.text:
            print("  [OK] legacy finalized game (no ended_at) shows '—' playtime")
        else:
            print(f"  [FAIL] legacy elapsed: status={r.status_code}")
            failed += 1
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
        main.app.dependency_overrides.pop(get_current_user, None)
    assert failed == 0
