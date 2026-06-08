"""Service-layer enum enforcement (Session B — v4-baseline characterization).

These pin the constrained-value columns that architecture.md ENFORCES IN PYTHON
AT THE SERVICE LAYER WITH NO DB CHECK (the CHECK is deferred to the v4 Postgres
rebuild). Until then, this service code is the ONLY guard between a bad value and
the database — so these tests are the stand-in for the constraints not yet added.
Characterization style: pin current observed behaviour (the v4 baseline).

invariant: architecture.md → "Constrained-value columns are enforced at the
service layer, never with a DB CHECK" (VALID_LOCATION_TYPES / VALID_LOCATION_MODES,
CANONICAL_GAME_FORMATS + normalize_game_format)
"""

from __future__ import annotations

import pytest

from app.game_service import (
    DEFAULT_GAME_FORMAT,
    DEFAULT_GAME_STATUS,
    normalize_game_format,
    normalize_game_status,
)
from app.location_service import create_location, update_location
from app.models import StorageLocation

# ── normalize_game_format ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "Commander"),  # None → default, never blocks creation
        ("", "Commander"),  # empty → default
        ("   ", "Commander"),  # whitespace-only → default
        ("commander", "Commander"),  # case-fold
        ("COMMANDER", "Commander"),
        ("  Modern  ", "Modern"),  # trim + match
        ("draft", "Draft"),
        ("Other", "Other"),  # 'Other' IS canonical (backfill catch-all)
        ("Pauper", "Commander"),  # non-empty unknown → default unknown_to
    ],
)
def test_normalize_game_format(raw, expected):
    assert normalize_game_format(raw) == expected


def test_normalize_game_format_unknown_to_override():
    # The migration backfill passes unknown_to="Other" to PRESERVE unrecognized
    # historical free-text as a distinct signal rather than collapse to default.
    assert normalize_game_format("Pauper", unknown_to="Other") == "Other"
    # …but empty / None always resolve to DEFAULT regardless of unknown_to.
    assert normalize_game_format(None, unknown_to="Other") == DEFAULT_GAME_FORMAT
    assert normalize_game_format("", unknown_to="Other") == DEFAULT_GAME_FORMAT


# ── normalize_game_status (same shape) ───────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "created"),
        ("", "created"),
        ("FINALIZED", "finalized"),  # case-fold
        ("in_progress", "in_progress"),
        ("garbage", "created"),  # unknown → default unknown_to
    ],
)
def test_normalize_game_status(raw, expected):
    assert normalize_game_status(raw) == expected


def test_normalize_game_status_unknown_to_override():
    assert normalize_game_status("garbage", unknown_to="abandoned") == "abandoned"
    assert normalize_game_status(None, unknown_to="abandoned") == DEFAULT_GAME_STATUS


# ── create_location: VALID_LOCATION_TYPES / VALID_LOCATION_MODES ──────────────


def test_create_location_valid(db, user):
    loc = create_location(db, user.id, name="Shelf A", type="binder", mode="managed")
    assert loc.type == "binder"
    assert loc.mode == "managed"


def test_create_location_lowercases_type_and_mode(db, user):
    loc = create_location(db, user.id, name="Shout", type="BINDER", mode="MANAGED")
    assert loc.type == "binder"
    assert loc.mode == "managed"


def test_create_location_empty_type_defaults_to_other(db, user):
    loc = create_location(db, user.id, name="Blank Type", type="")
    assert loc.type == "other"


def test_create_location_invalid_type_rejected(db, user):
    with pytest.raises(ValueError):
        create_location(db, user.id, name="Bad", type="vault")


def test_create_location_invalid_mode_rejected(db, user):
    with pytest.raises(ValueError):
        create_location(db, user.id, name="BadMode", type="binder", mode="frozen")


def test_create_location_deck_type_rejected(db, user):
    # type="deck" must go through deck_service.create_deck so the paired Deck
    # row lands atomically (the v3.3 architectural invariant / v3.30.17 guard).
    with pytest.raises(ValueError):
        create_location(db, user.id, name="Sneaky Deck", type="deck")


def test_create_location_blank_name_rejected(db, user):
    with pytest.raises(ValueError):
        create_location(db, user.id, name="   ", type="binder")


def test_create_location_duplicate_name_rejected(db, user):
    create_location(db, user.id, name="Dup", type="box")
    with pytest.raises(ValueError):
        create_location(db, user.id, name="Dup", type="binder")


# ── update_location: same enum enforcement + deck/root protection ─────────────


def test_update_location_valid_changes_type_and_mode(db, user):
    loc = create_location(db, user.id, name="Edit Me", type="binder", mode="managed")
    updated = update_location(
        db, location_id=loc.id, user_id=user.id, name="Edit Me", type="box", mode="manual"
    )
    assert updated.type == "box"
    assert updated.mode == "manual"


def test_update_location_invalid_type_rejected(db, user):
    loc = create_location(db, user.id, name="E2", type="binder")
    with pytest.raises(ValueError):
        update_location(db, location_id=loc.id, user_id=user.id, name="E2", type="vault")


def test_update_location_invalid_mode_rejected(db, user):
    loc = create_location(db, user.id, name="E3", type="binder")
    with pytest.raises(ValueError):
        update_location(
            db, location_id=loc.id, user_id=user.id, name="E3", type="binder", mode="frozen"
        )


@pytest.mark.parametrize("bad_type", ["deck", "root"])
def test_update_location_cannot_set_reserved_type(db, user, bad_type):
    loc = create_location(db, user.id, name="E4", type="binder")
    with pytest.raises(ValueError):
        update_location(db, location_id=loc.id, user_id=user.id, name="E4", type=bad_type)


def test_update_location_deck_location_not_editable(db, user):
    # A deck-type StorageLocation is managed through the Decks page; update_location
    # refuses it outright (created here raw, since create_location refuses deck).
    deck_loc = StorageLocation(user_id=user.id, name="My Deck", type="deck", mode="managed")
    db.add(deck_loc)
    db.commit()
    with pytest.raises(ValueError):
        update_location(db, location_id=deck_loc.id, user_id=user.id, name="My Deck", type="binder")
