"""Tests for shared game visibility + retroactive seat attribution (v3.32.0).

Pytest module (matches tests/test_share_service).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_game_visibility.py

Covers the v3.32.0 changes:
  - hybrid read visibility: owner, seat-attributed players, and members of a
    linked playgroup may VIEW a game; unrelated users may not
  - list_games returns the hybrid set + a transient is_owned_by_viewer flag
  - owner-only retroactive seat edit — rename + attribute/clear (update_seat)
  - owner-only playgroup link, gated on the owner's own membership
    (set_game_playgroup)
  - route layer: participants get a read-only 200, strangers 404; owner-only
    mutations (seat-edit, playgroup-set) 404 for non-owners
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import game_service
from app.db import Base
from app.models import Game, Playgroup, PlaygroupMember, User


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _make_user(s, username: str, display_name: str | None = None) -> User:
    u = User(username=username, password_hash="x", display_name=display_name)
    s.add(u)
    s.flush()
    return u


def _make_playgroup(s, owner: User, name: str, members: list[User] | None = None) -> Playgroup:
    pg = Playgroup(name=name, created_by=owner.id, join_code=f"code-{name}")
    s.add(pg)
    s.flush()
    s.add(PlaygroupMember(playgroup_id=pg.id, user_id=owner.id, role="owner"))
    for m in members or []:
        s.add(PlaygroupMember(playgroup_id=pg.id, user_id=m.id, role="member"))
    s.commit()
    return pg


def _make_game(s, owner: User, seat_specs: list[dict]) -> Game:
    """seat_specs: [{player_name, user_id?}] — minimal seats for the tests."""
    return game_service.create_game(
        s,
        user_id=owner.id,
        format="Draft",
        seats=[
            {"player_name": spec["player_name"], "user_id": spec.get("user_id")}
            for spec in seat_specs
        ],
    )


def test_hybrid_visibility():
    """get_viewable_game: owner, seat-attributed player, playgroup member yes;
    unrelated user no."""
    failed = 0
    s = _fresh_session()
    owner = _make_user(s, "owner")
    player = _make_user(s, "player")
    pg_member = _make_user(s, "pgmember")
    stranger = _make_user(s, "stranger")
    pg = _make_playgroup(s, owner, "pod", members=[pg_member])

    # Seat 2 is attributed to `player`; seat 1 is name-only.
    game = _make_game(
        s, owner, [{"player_name": "Owner"}, {"player_name": "P2", "user_id": player.id}]
    )

    if game_service.get_viewable_game(s, game.id, owner.id) is None:
        print("  [FAIL] owner cannot view own game")
        failed += 1
    else:
        print("  [OK] owner views own game")

    if game_service.get_viewable_game(s, game.id, player.id) is None:
        print("  [FAIL] seat-attributed player cannot view")
        failed += 1
    else:
        print("  [OK] seat-attributed player views")

    # Not yet linked to a playgroup → pg_member can't see it.
    if game_service.get_viewable_game(s, game.id, pg_member.id) is not None:
        print("  [FAIL] playgroup member saw a game before it was linked")
        failed += 1
    else:
        print("  [OK] unlinked game hidden from playgroup member")

    # Link it → pg_member can now see it.
    game_service.set_game_playgroup(s, game.id, owner.id, pg.id)
    if game_service.get_viewable_game(s, game.id, pg_member.id) is None:
        print("  [FAIL] playgroup member cannot view linked game")
        failed += 1
    else:
        print("  [OK] playgroup member views linked game")

    if game_service.get_viewable_game(s, game.id, stranger.id) is not None:
        print("  [FAIL] unrelated user could view the game")
        failed += 1
    else:
        print("  [OK] unrelated user cannot view")

    assert failed == 0


def test_list_games_hybrid_and_flag():
    """list_games returns owned + played-in; is_owned_by_viewer is set."""
    failed = 0
    s = _fresh_session()
    owner = _make_user(s, "owner")
    player = _make_user(s, "player")
    stranger = _make_user(s, "stranger")
    game = _make_game(
        s, owner, [{"player_name": "Owner"}, {"player_name": "P2", "user_id": player.id}]
    )

    owner_list = game_service.list_games(s, owner.id)
    if len(owner_list) != 1 or owner_list[0].id != game.id:
        print("  [FAIL] owner list missing own game")
        failed += 1
    elif owner_list[0].is_owned_by_viewer is not True:
        print("  [FAIL] is_owned_by_viewer not True for owner")
        failed += 1
    else:
        print("  [OK] owner list + is_owned_by_viewer=True")

    player_list = game_service.list_games(s, player.id)
    if len(player_list) != 1 or player_list[0].id != game.id:
        print("  [FAIL] participant list missing played-in game")
        failed += 1
    elif player_list[0].is_owned_by_viewer is not False:
        print("  [FAIL] is_owned_by_viewer not False for participant")
        failed += 1
    else:
        print("  [OK] participant list + is_owned_by_viewer=False")

    if game_service.list_games(s, stranger.id):
        print("  [FAIL] stranger list not empty")
        failed += 1
    else:
        print("  [OK] stranger list empty")

    assert failed == 0


def test_reassign_seat_user_owner_only():
    """Owner renames/attributes/clears a seat; non-owner rejected; unknown clears."""
    failed = 0
    s = _fresh_session()
    owner = _make_user(s, "owner")
    alex = _make_user(s, "alex", display_name="Alex")
    other = _make_user(s, "other")
    game = _make_game(s, owner, [{"player_name": "Player 1"}, {"player_name": "Bob"}])
    seat1 = game.seats[0]

    # Non-owner cannot edit.
    if (
        game_service.update_seat(s, game.id, seat1.id, other.id, target_user_id=alex.id)
        is not False
    ):
        print("  [FAIL] non-owner edited a seat")
        failed += 1
    else:
        print("  [OK] non-owner edit rejected")

    # Owner renames seat 1 AND attributes it to Alex in one call.
    ok = game_service.update_seat(
        s, game.id, seat1.id, owner.id, player_name="  Alexander  ", target_user_id=alex.id
    )
    s.refresh(seat1)
    if (
        ok is not True
        or seat1.player_name != "Alexander"
        or seat1.user_id != alex.id
        or seat1.user_name_at_game != "Alex"
    ):
        print(
            f"  [FAIL] rename+attribution wrong: ok={ok} name={seat1.player_name!r} "
            f"uid={seat1.user_id} snap={seat1.user_name_at_game!r}"
        )
        failed += 1
    else:
        print("  [OK] owner renames (trimmed) + attributes seat")

    # Blank player_name leaves the existing name untouched (NOT NULL column).
    game_service.update_seat(
        s, game.id, seat1.id, owner.id, player_name="   ", target_user_id=alex.id
    )
    s.refresh(seat1)
    if seat1.player_name != "Alexander":
        print(f"  [FAIL] blank name overwrote existing: {seat1.player_name!r}")
        failed += 1
    else:
        print("  [OK] blank name is a no-op")

    # Clearing attribution (None) resets both attribution columns; name stays.
    game_service.update_seat(s, game.id, seat1.id, owner.id, target_user_id=None)
    s.refresh(seat1)
    if (
        seat1.user_id is not None
        or seat1.user_name_at_game is not None
        or seat1.player_name != "Alexander"
    ):
        print("  [FAIL] clear didn't reset attribution / clobbered name")
        failed += 1
    else:
        print("  [OK] clear resets attribution, keeps name")

    # Unknown user id resolves to cleared (non-blocking), not an error.
    game_service.update_seat(s, game.id, seat1.id, owner.id, target_user_id=99999)
    s.refresh(seat1)
    if seat1.user_id is not None:
        print("  [FAIL] unknown id did not resolve to cleared")
        failed += 1
    else:
        print("  [OK] unknown id resolves to cleared (non-blocking)")

    # Seat not on this game → None.
    if game_service.update_seat(s, game.id, 99999, owner.id, target_user_id=alex.id) is not None:
        print("  [FAIL] missing seat did not return None")
        failed += 1
    else:
        print("  [OK] missing seat returns None")

    assert failed == 0


def test_set_game_playgroup():
    """Owner-only + member-only playgroup link."""
    failed = 0
    s = _fresh_session()
    owner = _make_user(s, "owner")
    other = _make_user(s, "other")
    pg = _make_playgroup(s, owner, "pod")  # owner is a member; other is not
    game = _make_game(s, owner, [{"player_name": "Owner"}])

    if game_service.set_game_playgroup(s, game.id, other.id, pg.id) is not False:
        print("  [FAIL] non-owner set a playgroup link")
        failed += 1
    else:
        print("  [OK] non-owner playgroup-link rejected")

    # Owner who isn't a member of a (different) playgroup can't link to it.
    foreign_owner = _make_user(s, "foreign")
    foreign_pg = _make_playgroup(s, foreign_owner, "theirs")
    if game_service.set_game_playgroup(s, game.id, owner.id, foreign_pg.id) is not False:
        print("  [FAIL] owner linked to a playgroup they don't belong to")
        failed += 1
    else:
        print("  [OK] link to non-member playgroup rejected")

    if game_service.set_game_playgroup(s, game.id, owner.id, pg.id) is not True:
        print("  [FAIL] owner could not link own playgroup")
        failed += 1
    else:
        s.refresh(game)
        if game.playgroup_id != pg.id:
            print("  [FAIL] playgroup_id not persisted")
            failed += 1
        else:
            print("  [OK] owner links own playgroup")

    # Clearing.
    game_service.set_game_playgroup(s, game.id, owner.id, None)
    s.refresh(game)
    if game.playgroup_id is not None:
        print("  [FAIL] clear didn't null playgroup_id")
        failed += 1
    else:
        print("  [OK] clear nulls playgroup_id")

    assert failed == 0


def test_games_list_total_wins_counts_only_viewer_wins():
    """The /games "Wins" stat counts only the viewer's own winning seats, not
    every game that has a winner (issue #38). Previously every finished game in
    the hybrid visibility set was counted as a win regardless of who won."""
    import re

    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    s = sm()
    owner = _make_user(s, "owner")
    rival = _make_user(s, "rival")

    # Game A: owner's seat won (placement 1).
    game_a = _make_game(
        s,
        owner,
        [
            {"player_name": "Owner", "user_id": owner.id},
            {"player_name": "Rival", "user_id": rival.id},
        ],
    )
    game_a.seats[0].placement = 1
    game_a.seats[1].placement = 2
    # Game B: owner played but the rival won.
    game_b = _make_game(
        s,
        owner,
        [
            {"player_name": "Owner", "user_id": owner.id},
            {"player_name": "Rival", "user_id": rival.id},
        ],
    )
    game_b.seats[0].placement = 2
    game_b.seats[1].placement = 1
    # Game C: owner played, an unattributed seat won (not the owner).
    game_c = _make_game(
        s, owner, [{"player_name": "Owner", "user_id": owner.id}, {"player_name": "Guest"}]
    )
    game_c.seats[0].placement = 2
    game_c.seats[1].placement = 1
    s.commit()

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    main.app.dependency_overrides[get_current_user] = lambda: owner
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    try:
        c = TestClient(main.app)
        r = c.get("/games")
        assert r.status_code == 200
        # The "Wins" stat-card value: owner won exactly 1 of the 3 games.
        m = re.search(r'stat-value">\s*(\d+)\s*</div>\s*<div class="stat-label">Wins</div>', r.text)
        assert m is not None, "could not find the Wins stat-card in the rendered page"
        assert m.group(1) == "1", f"expected 1 win for the viewer, got {m.group(1)}"
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
        main.app.dependency_overrides.pop(get_current_user, None)
        main.app.dependency_overrides.pop(require_csrf_token, None)


def test_routes_access_and_owner_only_mutations():
    """Route layer: participant 200 (read-only), stranger 404; owner-only
    mutations 404 for non-owners."""
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    failed = 0
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    s = sm()
    owner = _make_user(s, "owner")
    player = _make_user(s, "player")
    stranger = _make_user(s, "stranger")
    game = _make_game(
        s, owner, [{"player_name": "Owner"}, {"player_name": "P2", "user_id": player.id}]
    )
    game_id = game.id
    seat1_id = game.seats[0].id

    current = {"user": owner}

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    main.app.dependency_overrides[get_current_user] = lambda: current["user"]
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    try:
        c = TestClient(main.app)

        current["user"] = owner
        if c.get(f"/games/{game_id}").status_code != 200:
            print("  [FAIL] owner GET /games/{id} not 200")
            failed += 1
        else:
            print("  [OK] owner views game (200)")

        current["user"] = player
        r = c.get(f"/games/{game_id}")
        if r.status_code != 200:
            print(f"  [FAIL] participant GET -> {r.status_code} (expected 200)")
            failed += 1
        elif "read-only" not in r.text.lower():
            print("  [FAIL] participant view missing read-only banner")
            failed += 1
        else:
            print("  [OK] participant views read-only (200 + banner)")

        current["user"] = stranger
        if c.get(f"/games/{game_id}").status_code != 404:
            print("  [FAIL] stranger GET not 404")
            failed += 1
        else:
            print("  [OK] stranger GET 404")

        # Owner-only mutation routes reject non-owners with 404.
        current["user"] = stranger
        r = c.post(
            f"/games/{game_id}/seats/{seat1_id}",
            data={"player_name": "Hacked", "user_id": str(stranger.id)},
        )
        if r.status_code != 404:
            print(f"  [FAIL] non-owner seat-edit -> {r.status_code} (expected 404)")
            failed += 1
        else:
            print("  [OK] non-owner seat-edit 404")

        r = c.post(f"/games/{game_id}/playgroup", data={"playgroup_id": ""})
        if r.status_code != 404:
            print(f"  [FAIL] non-owner playgroup-set -> {r.status_code} (expected 404)")
            failed += 1
        else:
            print("  [OK] non-owner playgroup-set 404")
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
        main.app.dependency_overrides.pop(get_current_user, None)
        main.app.dependency_overrides.pop(require_csrf_token, None)
    assert failed == 0
