"""Regression checks from the desktop/mobile usability walkthrough."""

import json

import pytest

from app import deck_service, main
from app.dependencies import get_optional_current_user
from app.models import Card, Deck, DeckSimResult, InventoryRow, StorageLocation


def test_no_matches_is_not_empty_account(client, db, user):
    card = Card(
        scryfall_id="review-card",
        set_code="tst",
        collector_number="1",
        name="Forest",
        type_line="Basic Land — Forest",
    )
    loc = StorageLocation(user_id=user.id, name="Binder", type="binder", mode="manual")
    db.add_all([card, loc])
    db.flush()
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=card.id,
            quantity=3,
            storage_location_id=loc.id,
            is_pending=False,
        )
    )
    db.commit()
    page = client.get("/collection?search=NoSuchCard")
    assert page.status_code == 200
    assert "No matching inventory" in page.text
    assert "Welcome to Cartarch" not in page.text
    assert "Matching placed cards" in page.text
    assert "Forest" in client.get("/collection").text


def test_onboarding_follows_remaining_work(client, db, user):
    main.app.dependency_overrides[get_optional_current_user] = lambda: user
    try:
        assert "Welcome to Cartarch" in client.get("/").text
        card = Card(scryfall_id="onboard-card", set_code="tst", collector_number="2", name="Forest")
        db.add(card)
        db.flush()
        row = InventoryRow(user_id=user.id, card_id=card.id, quantity=1, is_pending=True)
        db.add(row)
        db.commit()
        page = client.get("/").text
        assert "Finish setting up your collection" in page
        assert "Place your imported cards" in page
        assert "Create your first deck" in page
        location = StorageLocation(user_id=user.id, name="First deck", type="deck", mode="manual")
        db.add(location)
        db.flush()
        db.add(Deck(user_id=user.id, name="First deck", storage_location_id=location.id))
        row.storage_location_id = location.id
        row.is_pending = False
        db.commit()
        assert "Finish setting up your collection" not in client.get("/").text
    finally:
        main.app.dependency_overrides.pop(get_optional_current_user, None)


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {"owner_username": "someone-else", "deck_name": "Review deck"},
        {"owner_username": "tester@example.com", "deck_name": "Other deck"},
    ],
)
def test_seed_identity_collision_preserves_existing_rows(db, user, tmp_path, identity):
    deck = Deck(user_id=user.id, name="Review deck")
    db.add(deck)
    db.flush()
    deck_service.save_play_profile(db, deck, {"primary_plan": ["My plan"]}, is_custom=False)
    db.add(DeckSimResult(deck_id=deck.id, run_label="review", strategy="core", wins=1, games=4))
    db.commit()
    profile = {"primary_plan": ["Wrong plan"]}
    result = {"deck_id": deck.id, "run_label": "review", "strategy": "core", "wins": 4, "games": 4}
    if identity is not None:
        profile["_identity"] = result["_identity"] = identity
    pp = tmp_path / "profiles.json"
    sim = tmp_path / "sim.json"
    pp.write_text(json.dumps({str(deck.id): profile}))
    sim.write_text(json.dumps([result]))
    assert deck_service.seed_play_profiles(db, str(pp))["skipped_identity"] == 1
    assert deck_service.seed_sim_results(db, str(sim))["skipped_identity"] == 1
    assert "My plan" in deck_service.get_play_profile(db, deck.id).profile_data
    assert db.query(DeckSimResult).filter_by(deck_id=deck.id).one().wins == 1


def test_shipped_seed_does_not_attach_to_fresh_deck(db, user):
    deck = Deck(id=1, user_id=user.id, name="Smoke Deck")
    db.add(deck)
    db.commit()
    assert deck_service.seed_play_profiles(db)["seeded"] == 0
    assert deck_service.seed_sim_results(db)["seeded"] == 0
    assert deck_service.get_play_profile(db, deck.id) is None
    assert db.query(DeckSimResult).count() == 0
