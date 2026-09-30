"""Shared rows offer a source path, never physical mutations; removal explains itself."""

import pytest

from app import deck_service as ds
from app.models import Card, DeckCardShare, InventoryRow, User


@pytest.fixture
def shared(db, user):
    group = ds.create_variant_group(db, user.id, "Related decks")
    source = ds.create_deck(db, user.id, "Source deck")
    target = ds.create_deck(db, user.id, "Target deck")
    ds.assign_deck_variant_group(db, user.id, source.id, group.id)
    ds.assign_deck_variant_group(db, user.id, target.id, group.id)
    card = Card(
        scryfall_id="shared-ux",
        name="Gaea's Cradle",
        set_code="tst",
        collector_number="1",
        type_line="Land",
    )
    db.add(card)
    db.flush()
    row = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=1,
        storage_location_id=source.storage_location_id,
        is_pending=False,
    )
    db.add(row)
    db.commit()
    ds.share_card_to_deck(db, user.id, row.id, target.id)
    return source, target, row


@pytest.mark.parametrize("view", ["grid", "list"])
def test_source_link_and_read_only_controls(client, db, user, shared, view):
    source, target, row = shared
    user.deck_view_mode = view
    db.commit()
    page = client.get(f"/decks/{target.id}")
    assert page.status_code == 200
    start = page.text.index('class="collection-row-kebab shared-card-controls')
    controls = page.text[start : page.text.index("</details>", start)]
    assert len(controls) > 500
    assert f'href="/decks/{source.id}"' in controls
    assert "Open source deck" in controls and "decklist only" in controls
    assert "Remove from this list" in controls
    assert "/actions" not in controls and "/return" not in controls and "/set-qty" not in controls
    outbound = client.get(f"/decks/{source.id}").text
    assert "ALSO" in outbound and "Physical card stays" in outbound
    db.refresh(row)
    assert row.quantity == 1 and row.storage_location_id == source.storage_location_id


def test_remove_feedback_is_once_and_preserves_inventory_and_view(client, db, shared):
    source, target, row = shared
    url = f"/decks/{target.id}/unshare-card"
    data = {"inventory_row_id": row.id, "target_deck_id": target.id}
    response = client.post(
        url,
        data=data,
        headers={"referer": f"http://testserver/decks/{target.id}?sort=price&direction=desc"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "sort=price" in response.headers["location"]
    page = client.get(response.headers["location"])
    assert "decklist only" in page.text and "was not moved or deleted" in page.text
    assert "Source deck" in page.text and "Gaea&#39;s Cradle" in page.text
    assert "was not moved or deleted" not in client.get(f"/decks/{target.id}").text
    assert db.query(DeckCardShare).count() == 0
    db.refresh(row)
    assert row.quantity == 1 and row.storage_location_id == source.storage_location_id
    assert "already removed" in client.post(url, data=data).text


def test_other_owner_cannot_unshare(client, db, shared):
    source, target, row = shared
    other = User(username="shared-ux-other", password_hash="x")
    db.add(other)
    db.flush()
    target.user_id = other.id
    db.commit()
    response = client.post(
        f"/decks/{source.id}/unshare-card",
        data={"inventory_row_id": row.id, "target_deck_id": target.id},
    )
    assert response.status_code == 404
    assert db.query(DeckCardShare).count() == 1
