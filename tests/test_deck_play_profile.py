"""Per-deck play profile — piloting intent (deck_play_profiles).

DISTINCT from DeckStrategyProfile (deckbuilding targets): this is how to PILOT
the deck, consumed by the Forge AI-player simulation. Same is_custom contract as
the strategy profile: False = auto-seeded (regenerable), True = pilot-edited
(never silently overwritten by a re-seed).

Covers: save/get roundtrip, upsert, the is_custom overwrite contract, payload
validation, and delete_deck cleanup.

    pytest tests/test_deck_play_profile.py
"""

from __future__ import annotations

import itertools
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.legacy_tables  # noqa: F401 — registers the raw tables delete_deck cleans up
from app import deck_service
from app.db import Base
from app.models import DeckPlayProfile, StorageLocation, User

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


def _deck(s, user):
    loc = StorageLocation(user_id=user.id, name=f"deck loc {next(_seq)}", type="deck")
    s.add(loc)
    s.flush()
    d = deck_service.create_deck(s, user_id=user.id, name=f"Deck {next(_seq)}")
    return d


PROFILE = {
    "primary_plan": ["assemble the pair"],
    "hard_rules": ["protect the pair"],
    "threat_preferences": {"removal_targeting_the_assembled_pair": 100},
}


def test_save_and_get_roundtrip():
    s = _fresh_session()
    deck = _deck(s, _user(s))

    assert deck_service.get_play_profile(s, deck.id) is None
    row = deck_service.save_play_profile(s, deck, PROFILE)
    s.commit()

    got = deck_service.get_play_profile(s, deck.id)
    assert got is not None and got.id == row.id
    assert json.loads(got.profile_data) == PROFILE
    assert got.is_custom is True


def test_upsert_updates_in_place():
    s = _fresh_session()
    deck = _deck(s, _user(s))
    first = deck_service.save_play_profile(s, deck, {"v": 1}, is_custom=False)
    second = deck_service.save_play_profile(s, deck, {"v": 2}, is_custom=False)
    s.commit()

    assert first.id == second.id  # one row per deck, updated in place
    assert json.loads(deck_service.get_play_profile(s, deck.id).profile_data) == {"v": 2}
    assert s.query(DeckPlayProfile).count() == 1


def test_custom_row_survives_auto_reseed():
    """The is_custom contract: a pilot edit is never silently overwritten by a
    regeneration pass, but a pilot edit may replace anything."""
    s = _fresh_session()
    deck = _deck(s, _user(s))
    deck_service.save_play_profile(s, deck, {"pilot": True}, is_custom=True)

    deck_service.save_play_profile(s, deck, {"seeded": True}, is_custom=False)
    assert json.loads(deck_service.get_play_profile(s, deck.id).profile_data) == {"pilot": True}

    deck_service.save_play_profile(s, deck, {"pilot": 2}, is_custom=True)
    assert json.loads(deck_service.get_play_profile(s, deck.id).profile_data) == {"pilot": 2}


def test_payload_validation():
    s = _fresh_session()
    deck = _deck(s, _user(s))

    with pytest.raises(ValueError):
        deck_service.save_play_profile(s, deck, ["not", "an", "object"])  # type: ignore[arg-type]

    huge = {"x": "y" * (deck_service.PLAY_PROFILE_MAX_BYTES + 1)}
    with pytest.raises(ValueError):
        deck_service.save_play_profile(s, deck, huge)


def test_delete_deck_removes_profile():
    """SQLite enforces no FKs, so delete_deck must clean the row explicitly —
    same pattern as goals and the strategy profile."""
    s = _fresh_session()
    user = _user(s)
    deck = _deck(s, user)
    deck_service.save_play_profile(s, deck, PROFILE)
    s.commit()
    assert s.query(DeckPlayProfile).count() == 1

    deck_service.delete_deck(s, deck.id, user.id)
    s.commit()
    assert s.query(DeckPlayProfile).count() == 0
