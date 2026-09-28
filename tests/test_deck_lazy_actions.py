"""Lazy menus retain the row/deck ownership boundary and render real controls."""

import pytest

from app.models import Card, Deck, InventoryRow, StorageLocation, User
from tests.test_deck_row_set_qty import deck_with_forest as deck_with_forest


@pytest.mark.parametrize("view", ["grid", "list"])
def test_lazy_menu_is_read_only_and_scoped(client, db, user, deck_with_forest, view):
    deck, rows = deck_with_forest
    user.deck_view_mode = view
    db.commit()
    page = client.get(f"/decks/{deck.id}")
    url = f"/decks/{deck.id}/rows/{rows['Forest'].id}/actions"
    assert url in page.text
    assert '<div class="card-actions-body">' not in page.text
    before = [(r.id, r.quantity, r.storage_location_id) for r in db.query(InventoryRow)]
    result = client.get(url)
    assert result.status_code == 200
    assert 'name="csrf_token"' in result.text
    assert 'name="tags"' in result.text
    assert "/toggle-commander" in result.text
    assert "/decks/return" in result.text
    assert 'name="quantity"' in result.text
    assert result.headers["cache-control"] == "no-store"
    db.expire_all()
    assert before == [(r.id, r.quantity, r.storage_location_id) for r in db.query(InventoryRow)]
    assert client.get(f"/decks/{deck.id}/rows/999999/actions").status_code == 404

    other = User(username="other@example.invalid", password_hash="x")
    db.add(other)
    db.flush()
    location = StorageLocation(user_id=other.id, name="Private", type="deck", mode="manual")
    db.add(location)
    db.flush()
    other_deck = Deck(user_id=other.id, name="Private", storage_location_id=location.id)
    row = InventoryRow(
        user_id=other.id,
        card_id=rows["Forest"].card_id,
        quantity=1,
        storage_location_id=location.id,
        finish="normal",
    )
    db.add_all([other_deck, row])
    db.commit()
    assert client.get(f"/decks/{other_deck.id}/rows/{row.id}/actions").status_code == 404
    assert client.get(f"/decks/{deck.id}/rows/{row.id}/actions").status_code == 404
    # An owned row in another location is not editable through this deck either.
    row.user_id = user.id
    db.commit()
    assert client.get(f"/decks/{deck.id}/rows/{row.id}/actions").status_code == 404


def test_large_list_initial_markup_stays_small(client, db, user, deck_with_forest):
    deck, _ = deck_with_forest
    for i in range(98):
        card = Card(
            scryfall_id=f"lazy-{i}",
            name=f"Artifact {i}",
            set_code="tst",
            collector_number=str(i),
            type_line="Artifact",
        )
        db.add(card)
        db.flush()
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=card.id,
                quantity=1,
                finish="normal",
                storage_location_id=deck.storage_location_id,
            )
        )
    user.deck_view_mode = "list"
    db.commit()
    page = client.get(f"/decks/{deck.id}")
    assert page.status_code == 200
    assert page.text.count('class="deck-actions-slot"') == 100
    assert '<div class="card-actions-body">' not in page.text
    assert len(page.content) < 600_000, len(page.content)
