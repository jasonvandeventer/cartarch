"""Per-deck goals — service layer (issue #46, Feature 1 of 2).

Goals are a custom, ordered "what this deck is trying to do" list, separate
from win rate and distinct from decks.intent_*. Removal is a soft-delete
(is_active=False); hard delete is a separate explicit action.

Covers: create / edit / reorder / deactivate (soft) / hard-delete / list-active,
ownership scoping, and delete_deck cleanup.

    pytest tests/test_deck_goals.py
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.legacy_tables  # noqa: F401 — registers the raw tables delete_deck cleans up
from app import deck_service
from app.db import Base
from app.models import Deck, DeckGoal, StorageLocation, User

_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _user(s, username=None) -> User:
    u = User(username=username or f"u{next(_seq)}", password_hash="x")
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


def test_create_appends_in_order_and_list_active():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g1 = deck_service.create_deck_goal(s, u.id, d.id, "Win by combo")
    g2 = deck_service.create_deck_goal(s, u.id, d.id, "Cast commander 3x", "flavor")
    assert g1.position < g2.position
    assert g2.description == "flavor"
    goals = deck_service.list_deck_goals(s, u.id, d.id)
    assert [g.label for g in goals] == ["Win by combo", "Cast commander 3x"]


def test_create_empty_label_raises():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    try:
        deck_service.create_deck_goal(s, u.id, d.id, "   ")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_edit_updates_label_and_description():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g = deck_service.create_deck_goal(s, u.id, d.id, "old")
    deck_service.edit_deck_goal(s, u.id, g.id, "new", "desc")
    s.refresh(g)
    assert g.label == "new"
    assert g.description == "desc"


def test_reorder_swaps_with_neighbour():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    a = deck_service.create_deck_goal(s, u.id, d.id, "A")
    b = deck_service.create_deck_goal(s, u.id, d.id, "B")
    c = deck_service.create_deck_goal(s, u.id, d.id, "C")
    assert deck_service.move_deck_goal(s, u.id, b.id, "up") is True
    assert [g.label for g in deck_service.list_deck_goals(s, u.id, d.id)] == ["B", "A", "C"]
    assert deck_service.move_deck_goal(s, u.id, b.id, "down") is True
    assert [g.label for g in deck_service.list_deck_goals(s, u.id, d.id)] == ["A", "B", "C"]
    # edge: top can't move up, bottom can't move down
    assert deck_service.move_deck_goal(s, u.id, a.id, "up") is False
    assert deck_service.move_deck_goal(s, u.id, c.id, "down") is False
    assert deck_service.move_deck_goal(s, u.id, a.id, "sideways") is False


def test_reorder_breaks_tied_positions():
    # Simulate a concurrent-create race: two goals end up sharing a position.
    # The move must still reorder (a naive value-swap of equal numbers is a no-op).
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    a = deck_service.create_deck_goal(s, u.id, d.id, "A")
    b = deck_service.create_deck_goal(s, u.id, d.id, "B")
    b.position = a.position  # force the tie
    s.commit()
    assert deck_service.move_deck_goal(s, u.id, b.id, "up") is True
    assert [g.label for g in deck_service.list_deck_goals(s, u.id, d.id)] == ["B", "A"]


def test_position_has_db_server_default():
    # The spec requires position NOT NULL default 0 at the DB level (non-ORM
    # inserts). Insert via raw SQL omitting position and confirm it lands as 0.
    from sqlalchemy import text

    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    s.execute(
        text(
            "INSERT INTO deck_goals (deck_id, label, is_active, created_at) "
            "VALUES (:d, 'raw', 1, '2026-01-01')"
        ),
        {"d": d.id},
    )
    s.commit()
    row = s.query(DeckGoal).filter_by(label="raw").one()
    assert row.position == 0


def test_soft_delete_hides_from_active_but_row_survives():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g = deck_service.create_deck_goal(s, u.id, d.id, "keep me as history")
    assert deck_service.deactivate_deck_goal(s, u.id, g.id) is True
    assert deck_service.list_deck_goals(s, u.id, d.id) == []
    # row still present (history preserved for Feature 2)
    assert s.get(DeckGoal, g.id) is not None
    assert deck_service.list_deck_goals(s, u.id, d.id, active_only=False) != []


def test_hard_delete_removes_row():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g = deck_service.create_deck_goal(s, u.id, d.id, "gone")
    assert deck_service.delete_deck_goal(s, u.id, g.id) is True
    assert s.get(DeckGoal, g.id) is None


def test_ownership_scoping():
    s = _fresh_session()
    owner = _user(s)
    other = _user(s)
    d = _deck(s, owner.id)
    g = deck_service.create_deck_goal(s, owner.id, d.id, "owner only")
    # other user can't see, create, edit, move, deactivate, or delete
    assert deck_service.list_deck_goals(s, other.id, d.id) == []
    assert deck_service.create_deck_goal(s, other.id, d.id, "x") is None
    assert deck_service.edit_deck_goal(s, other.id, g.id, "hacked") is None
    assert deck_service.move_deck_goal(s, other.id, g.id, "up") is False
    assert deck_service.deactivate_deck_goal(s, other.id, g.id) is False
    assert deck_service.delete_deck_goal(s, other.id, g.id) is False
    # untouched
    s.refresh(g)
    assert g.label == "owner only" and g.is_active is True


def test_delete_deck_removes_goals():
    s = _fresh_session()
    u = _user(s)
    d = _deck(s, u.id)
    g = deck_service.create_deck_goal(s, u.id, d.id, "doomed")
    gid = g.id
    assert deck_service.delete_deck(s, d.id, u.id) is True
    # bulk delete uses synchronize_session=False → evict the identity map so the
    # assert reads the DB, not a stale cached instance.
    s.expire_all()
    assert s.get(DeckGoal, gid) is None
