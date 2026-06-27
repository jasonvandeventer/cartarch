"""Per-game goal completion + per-deck stats (issue #47, Feature 2 of 2).

A GameGoalResult records whether a seat's deck achieved one of its goals in one
game. Grain is the SEAT. Written at finalize for goals ACTIVE at that time (no
backfill); the deck page shows a per-goal completion rate.

Covers: idempotent upsert + re-finalize, cascade on seat/game delete, explicit
cleanup on goal-hard-delete and deck-delete, stats aggregation (active + retired
goals), and the multiplayer rule (only the deck owner's goals apply to a seat).

    pytest tests/test_game_goal_results.py
"""

from __future__ import annotations

import itertools
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.legacy_tables  # noqa: F401 — registers raw tables delete_deck cleans up
from app import deck_service, game_service
from app.db import Base
from app.models import Deck, Game, GameGoalResult, GameSeat, StorageLocation, User

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


def _deck(s, user_id, name=None) -> Deck:
    name = name or f"d{next(_seq)}"
    loc = StorageLocation(user_id=user_id, name=name, type="deck", mode="manual")
    s.add(loc)
    s.flush()
    deck = Deck(user_id=user_id, name=name, storage_location_id=loc.id)
    s.add(deck)
    s.flush()
    return deck


def _game(s, user_id, **kw):
    g = Game(
        user_id=user_id,
        format="Commander",
        status="created",
        played_at=datetime(2026, 6, 1, 18),
        **kw,
    )
    s.add(g)
    s.flush()
    return g


def _seat(s, game_id, n, name, **kw) -> GameSeat:
    seat = GameSeat(game_id=game_id, seat_number=n, player_name=name, **kw)
    s.add(seat)
    s.flush()
    return seat


# ── upsert / idempotency ─────────────────────────────────────────


def test_upsert_writes_active_goals_and_is_idempotent_on_refinalize():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g1 = deck_service.create_deck_goal(s, u.id, d.id, "Win by combo")
    g2 = deck_service.create_deck_goal(s, u.id, d.id, "Cast commander 3x")
    game = _game(s, u.id)
    seat = _seat(s, game.id, 1, "Owner", deck_id=d.id)

    game_service.record_goal_results(s, game.id, u.id, {(seat.id, g1.id)})
    rows = s.query(GameGoalResult).all()
    assert {(r.deck_goal_id, r.achieved) for r in rows} == {(g1.id, True), (g2.id, False)}

    # Re-finalize with the OTHER goal checked — UNIQUE makes it an in-place update,
    # not new rows.
    game_service.record_goal_results(s, game.id, u.id, {(seat.id, g2.id)})
    rows = s.query(GameGoalResult).all()
    assert len(rows) == 2
    assert {(r.deck_goal_id, r.achieved) for r in rows} == {(g1.id, False), (g2.id, True)}


def test_inactive_goal_never_gets_a_row():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g_active = deck_service.create_deck_goal(s, u.id, d.id, "active")
    g_dead = deck_service.create_deck_goal(s, u.id, d.id, "retired")
    deck_service.deactivate_deck_goal(s, u.id, g_dead.id)
    game = _game(s, u.id)
    seat = _seat(s, game.id, 1, "Owner", deck_id=d.id)

    # Even if the form forges the retired goal as checked, it's not written.
    game_service.record_goal_results(s, game.id, u.id, {(seat.id, g_dead.id)})
    rows = s.query(GameGoalResult).all()
    assert {r.deck_goal_id for r in rows} == {g_active.id}


# ── multiplayer scoping ──────────────────────────────────────────


def test_only_deck_owners_goals_apply_to_a_seat():
    s = _fresh_session()
    owner = _user(s)
    friend = _user(s)
    own_deck = _deck(s, owner.id)
    friend_deck = _deck(s, friend.id)
    og = deck_service.create_deck_goal(s, owner.id, own_deck.id, "my goal")
    fg = deck_service.create_deck_goal(s, friend.id, friend_deck.id, "friend goal")
    game = _game(s, owner.id)  # owner records the game
    my_seat = _seat(s, game.id, 1, "Owner", deck_id=own_deck.id)
    friend_seat = _seat(s, game.id, 2, "Friend", deck_id=friend_deck.id)

    # Recorder ticks both, but only their own deck's goal is written.
    game_service.record_goal_results(
        s, game.id, owner.id, {(my_seat.id, og.id), (friend_seat.id, fg.id)}
    )
    rows = s.query(GameGoalResult).all()
    assert len(rows) == 1
    assert rows[0].game_seat_id == my_seat.id and rows[0].deck_goal_id == og.id


def test_non_owner_recorder_writes_nothing():
    s = _fresh_session()
    owner = _user(s)
    stranger = _user(s)
    d = _deck(s, owner.id)
    g = deck_service.create_deck_goal(s, owner.id, d.id, "goal")
    game = _game(s, owner.id)
    seat = _seat(s, game.id, 1, "Owner", deck_id=d.id)
    # game.user_id != stranger.id → record_goal_results bails.
    game_service.record_goal_results(s, game.id, stranger.id, {(seat.id, g.id)})
    assert s.query(GameGoalResult).count() == 0


# ── cascade on seat / game delete ────────────────────────────────


def test_cascade_on_seat_delete():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g = deck_service.create_deck_goal(s, u.id, d.id, "goal")
    game = _game(s, u.id)
    seat = _seat(s, game.id, 1, "Owner", deck_id=d.id)
    game_service.record_goal_results(s, game.id, u.id, {(seat.id, g.id)})
    assert s.query(GameGoalResult).count() == 1

    s.delete(seat)
    s.commit()
    assert s.query(GameGoalResult).count() == 0


def test_cascade_on_game_delete():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g = deck_service.create_deck_goal(s, u.id, d.id, "goal")
    game = _game(s, u.id)
    seat = _seat(s, game.id, 1, "Owner", deck_id=d.id)
    game_service.record_goal_results(s, game.id, u.id, {(seat.id, g.id)})

    assert game_service.delete_game(s, game.id, u.id) is True
    assert s.query(GameGoalResult).count() == 0


# ── explicit cleanup on goal / deck delete ───────────────────────


def test_goal_hard_delete_removes_its_results():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g1 = deck_service.create_deck_goal(s, u.id, d.id, "g1")
    g2 = deck_service.create_deck_goal(s, u.id, d.id, "g2")
    game = _game(s, u.id)
    seat = _seat(s, game.id, 1, "Owner", deck_id=d.id)
    game_service.record_goal_results(s, game.id, u.id, {(seat.id, g1.id), (seat.id, g2.id)})
    assert s.query(GameGoalResult).count() == 2

    assert deck_service.delete_deck_goal(s, u.id, g1.id) is True
    remaining = s.query(GameGoalResult).all()
    assert {r.deck_goal_id for r in remaining} == {g2.id}


def test_deck_delete_removes_goals_and_results():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g = deck_service.create_deck_goal(s, u.id, d.id, "goal")
    game = _game(s, u.id)
    seat = _seat(s, game.id, 1, "Owner", deck_id=d.id)
    game_service.record_goal_results(s, game.id, u.id, {(seat.id, g.id)})

    assert deck_service.delete_deck(s, d.id, u.id) is True
    assert s.query(GameGoalResult).count() == 0
    from app.models import DeckGoal

    assert s.query(DeckGoal).count() == 0


# ── stats aggregation ────────────────────────────────────────────


def test_stats_active_goal_aggregates_and_retired_goal_with_history_renders():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g_hit = deck_service.create_deck_goal(s, u.id, d.id, "win")
    g_mixed = deck_service.create_deck_goal(s, u.id, d.id, "ramp")

    # Two games: g_hit 2/2, g_mixed 1/2.
    for hit_mixed in [True, False]:
        game = _game(s, u.id)
        seat = _seat(s, game.id, 1, "Owner", deck_id=d.id)
        checked = {(seat.id, g_hit.id)}
        if hit_mixed:
            checked.add((seat.id, g_mixed.id))
        game_service.record_goal_results(s, game.id, u.id, checked)

    # A goal added AFTER both games is non-retroactive — no rows, renders 0/0.
    deck_service.create_deck_goal(s, u.id, d.id, "never played")
    # Retire g_mixed — its history must still render.
    deck_service.deactivate_deck_goal(s, u.id, g_mixed.id)

    stats = {st["goal"].label: st for st in deck_service.deck_goal_stats(s, u.id, d.id)}
    assert (
        stats["win"]["achieved"] == 2 and stats["win"]["total"] == 2 and stats["win"]["pct"] == 100
    )
    assert (
        stats["ramp"]["achieved"] == 1
        and stats["ramp"]["total"] == 2
        and stats["ramp"]["pct"] == 50
    )
    # Active goal that existed for NO games renders at 0/0 (non-retroactive).
    assert stats["never played"]["total"] == 0 and stats["never played"]["pct"] == 0


def test_stats_hides_retired_goal_with_no_history():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    deck_service.create_deck_goal(s, u.id, d.id, "active")
    g_dead = deck_service.create_deck_goal(s, u.id, d.id, "retired-empty")
    deck_service.deactivate_deck_goal(s, u.id, g_dead.id)
    labels = {st["goal"].label for st in deck_service.deck_goal_stats(s, u.id, d.id)}
    assert labels == {"active"}


def test_stats_empty_for_non_owner():
    s = _fresh_session()
    owner = _user(s)
    other = _user(s)
    d = _deck(s, owner.id)
    deck_service.create_deck_goal(s, owner.id, d.id, "goal")
    assert deck_service.deck_goal_stats(s, other.id, d.id) == []
