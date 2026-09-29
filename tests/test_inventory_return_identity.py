"""Returns must not convert real cards, proxies, or languages into each other."""

import pytest

from app import deck_service
from app.models import Card, InventoryRow


@pytest.mark.parametrize(
    "pending", [None, (False, "en"), (False, "ja"), (True, "en"), (True, "ja")]
)
def test_return_preserves_inventory_identity(client, db, user, pending):
    deck = deck_service.create_deck(db, user.id, "Return identity")
    card = Card(
        scryfall_id="return-identity", name="Return card", set_code="tst", collector_number="1"
    )
    db.add(card)
    db.flush()
    source = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        storage_location_id=deck.storage_location_id,
        quantity=2,
        finish="normal",
        is_proxy=True,
        language="ja",
        is_pending=False,
        notes="Physical proxy",
        tags='["Keep"]',
    )
    db.add(source)
    if pending is not None:
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=card.id,
                quantity=3,
                finish="normal",
                is_proxy=pending[0],
                language=pending[1],
                is_pending=True,
            )
        )
    db.commit()
    response = client.post("/decks/return", data={"deck_id": deck.id, "deck_row_id": source.id})
    assert response.status_code == 200
    rows = db.query(InventoryRow).filter_by(card_id=card.id).all()
    assert all(r.is_pending and r.storage_location_id is None for r in rows)
    proxies = [r for r in rows if r.is_proxy and r.language == "ja"]
    expected = 5 if pending == (True, "ja") else 2
    assert sum(r.quantity for r in proxies) == expected
    if pending != (True, "ja"):
        assert proxies[0].notes == "Physical proxy"
        assert proxies[0].tags == '["Keep"]'
    assert sum(r.quantity for r in rows if not r.is_proxy) == (
        3 if pending and not pending[0] else 0
    )


def test_switch_printing_does_not_merge_real_copies_into_proxies(db, user):
    deck = deck_service.create_deck(db, user.id, "Switch identity")
    old = Card(scryfall_id="old-identity", name="Card", set_code="old", collector_number="1")
    new = Card(scryfall_id="new-identity", name="Card", set_code="new", collector_number="1")
    db.add_all([old, new])
    db.flush()

    def row(card, location, proxy, quantity):
        r = InventoryRow(
            user_id=user.id,
            card_id=card.id,
            quantity=quantity,
            storage_location_id=location,
            is_pending=location is None,
            finish="normal",
            is_proxy=proxy,
            language="ja",
        )
        db.add(r)
        return r

    source = row(old, deck.storage_location_id, False, 1)
    row(new, None, False, 1)
    old_proxy = row(old, None, True, 3)
    new_proxy = row(new, deck.storage_location_id, True, 4)
    db.commit()
    assert deck_service.switch_deck_row_printing(
        db, user.id, deck.id, source.id, new.scryfall_id, "normal"
    )
    db.refresh(old_proxy)
    db.refresh(new_proxy)
    assert (old_proxy.quantity, new_proxy.quantity) == (3, 4)
    real = db.query(InventoryRow).filter_by(is_proxy=False).all()
    assert len(real) == 2
    assert {(r.card_id, r.storage_location_id, r.quantity, r.language) for r in real} == {
        (old.id, None, 1, "ja"),
        (new.id, deck.storage_location_id, 1, "ja"),
    }
