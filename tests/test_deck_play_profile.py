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


def test_seed_play_profiles(tmp_path):
    """Seed inserts auto rows, never touches custom rows, tolerates missing decks."""
    s = _fresh_session()
    user = _user(s)
    seeded_deck = _deck(s, user)
    custom_deck = _deck(s, user)
    deck_service.save_play_profile(s, custom_deck, {"pilot": True}, is_custom=True)

    seed = {
        str(seeded_deck.id): {"primary_plan": ["seeded"]},
        str(custom_deck.id): {"primary_plan": ["should not land"]},
        "999999": {"primary_plan": ["no such deck"]},
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed), encoding="utf-8")

    stats = deck_service.seed_play_profiles(s, seed_path=str(path))
    assert stats == {"seeded": 1, "skipped_custom": 1, "missing_decks": 1}
    row = deck_service.get_play_profile(s, seeded_deck.id)
    assert row.is_custom is False
    assert json.loads(row.profile_data) == {"primary_plan": ["seeded"]}
    assert json.loads(deck_service.get_play_profile(s, custom_deck.id).profile_data) == {
        "pilot": True
    }

    # Re-run is a no-op upsert, not an error or a duplicate.
    stats = deck_service.seed_play_profiles(s, seed_path=str(path))
    assert stats["seeded"] == 1
    assert s.query(DeckPlayProfile).count() == 2

    # Missing file (fresh checkout without the data file) is a clean no-op.
    assert deck_service.seed_play_profiles(s, seed_path=str(tmp_path / "absent.json")) == {
        "seeded": 0,
        "skipped_custom": 0,
        "missing_decks": 0,
    }


def test_shipped_seed_file_is_valid():
    """CI-validates the real seed data: parseable, and every profile passes the
    same validation save_play_profile applies (dict, under the size cap)."""
    with open(deck_service.PLAY_PROFILE_SEED, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data, "shipped seed file is empty"
    for deck_id, profile in data.items():
        int(deck_id)
        assert isinstance(profile, dict), f"deck {deck_id}: profile is not an object"
        size = len(json.dumps(profile, ensure_ascii=False).encode("utf-8"))
        assert size <= deck_service.PLAY_PROFILE_MAX_BYTES, f"deck {deck_id}: {size} bytes"


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
