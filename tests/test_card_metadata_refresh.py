import pytest

from app import inventory_service
from app.models import Card


@pytest.mark.parametrize("supplied", [True, False])
def test_existing_card_refresh_updates_same_fields(db, monkeypatch, supplied):
    card = Card(
        scryfall_id="refresh",
        name="Before",
        set_code="old",
        collector_number="1",
        full_art=True,
        set_type="expansion",
    )
    db.add(card)
    db.commit()
    payload = dict(
        name="After",
        set_code="new",
        set_name="New Set",
        collector_number="2",
        rarity="rare",
        image_url="https://example.com/card.png",
        type_line="Creature",
        oracle_text="Flying",
        price_usd="1.00",
        price_usd_foil="2.00",
        price_usd_etched="3.00",
        colors="U",
        color_identity="U",
        mana_cost="{U}",
        cmc=1,
        layout="normal",
    )
    calls = []

    def fetch(sid):
        calls.append(sid)
        return payload

    monkeypatch.setattr(inventory_service, "fetch_card_by_scryfall_id", fetch)
    refreshed = inventory_service.get_or_create_card(
        db, card.scryfall_id, card_data=payload if supplied else None
    )
    assert refreshed is card
    for key, value in payload.items():
        assert getattr(card, key) == value
    assert card.full_art is True  # missing trait keys must not clear existing data
    assert calls == ([] if supplied else ["refresh"])
    inventory_service.get_or_create_card(db, card.scryfall_id)
    assert calls == ([] if supplied else ["refresh"])  # complete metadata never refetches
