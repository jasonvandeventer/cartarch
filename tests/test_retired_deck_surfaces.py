"""Retired identities remain for history, never as active decks or name reservations."""

import pytest

from app import deck_service
from app.import_service import auto_create_locations
from app.models import Deck


@pytest.mark.parametrize("surface", ["public", "siblings", "rename", "import", "goldfish"])
def test_retired_deck_is_absent_from_active_surfaces(client, db, user, surface):
    group = deck_service.create_variant_group(db, user.id, "Related")
    old = deck_service.create_deck(db, user.id, "Retired identity")
    live = deck_service.create_deck(db, user.id, "Live identity")
    for deck in (old, live):
        deck_service.assign_deck_variant_group(db, user.id, deck.id, group.id)
    token = deck_service.generate_deck_share_token(db, deck_id=old.id, user_id=user.id)
    assert client.get(f"/d/{token}").status_code == 200
    assert old.name in client.get(f"/decks/{live.id}").text
    assert deck_service.delete_deck(db, old.id, user.id)
    assert db.get(Deck, old.id).retired_at is not None

    if surface == "public":
        assert client.get(f"/d/{token}").status_code == 404
    elif surface == "siblings":
        page = client.get(f"/decks/{live.id}")
        assert page.status_code == 200
        assert f'href="/decks/{old.id}"' not in page.text
    elif surface == "rename":
        result = deck_service.update_deck(db, deck_id=live.id, user_id=user.id, name=old.name)
        assert result.name == old.name
    elif surface == "import":
        locations = auto_create_locations(db, user.id, [old.name], {old.name.lower(): "deck"})
        assert old.name.lower() in locations
        assert (
            db.query(Deck)
            .filter_by(storage_location_id=locations[old.name.lower()])
            .one()
            .retired_at
            is None
        )
    else:
        response = client.get(f"/decks/{old.id}/goldfish", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/decks"
