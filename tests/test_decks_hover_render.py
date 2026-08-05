"""Decks page hover zoom (v4.13.7): each row previews its commander."""

import app.legacy_tables  # noqa
from app import deck_service
from app.models import Card, DeckCommander, StorageLocation


def test_decks_page_renders_commander_hover_attrs(client, db, user):
    loc = StorageLocation(user_id=user.id, name="deck loc", type="deck")
    db.add(loc)
    db.flush()
    deck = deck_service.create_deck(db, user_id=user.id, name="Hover Deck")
    card = Card(
        name="Cmdr",
        scryfall_id="sf-hover",
        set_code="tst",
        set_name="T",
        collector_number="1",
        rarity="rare",
        image_url="http://x/i.png",
    )
    db.add(card)
    db.flush()
    db.add(DeckCommander(deck_id=deck.id, card_id=card.id))
    db.commit()
    html = client.get("/decks").text
    assert "data-card-hover" in html, "container missing"
    n = html.count("data-card-image=")
    assert n >= 1, "no row attrs rendered"
