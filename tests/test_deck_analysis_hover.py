"""Upgrade suggestions get the #169 hover preview, like every other card list.

Two things have to be true together, and the #168 lesson is that emitting one
without the other is a silent dead control: the rows carry the data attributes
AND the page loads `card-hover.js`.

The price in `data-card-info` comes from `effective_price` — a float — never
`Card.price_usd`, which is TEXT and 500s the page through `'%.2f'|format` (the
v4.13.12 crash on /pending and /drawers).
"""

import app.legacy_tables  # noqa
from app import deck_service
from app.models import Card, InventoryRow, StorageLocation


def _card(sid, name, *, type_line, oracle, price="2.50"):
    """Shapes copied from tests/test_recommendation_issue88.py, which is the
    fixture style the suggestion engine actually classifies — a hand-invented
    card silently produces zero suggestions and a vacuous test."""
    return Card(
        name=name,
        scryfall_id=sid,
        set_code="tst",
        set_name="T",
        collector_number=sid[-2:],
        rarity="rare",
        type_line=type_line,
        oracle_text=oracle,
        image_url="http://x/i.png",
        color_identity="W",
        cmc=3.0,
        price_usd=price,  # TEXT, exactly as the MTGJSON ingest writes it
    )


def _deck_with_a_suggestion(db, user):
    """A deck with a commander, plus owned removal sitting OUTSIDE it."""
    deck = deck_service.create_deck(db, user.id, "Analyzed", format_name="commander")
    commander = _card(
        "sf-cmdr-01",
        "Test White Commander",
        type_line="Legendary Creature — Human Soldier",
        oracle="Vigilance.",
    )
    db.add(commander)
    db.flush()
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=commander.id,
            storage_location_id=deck.storage_location_id,
            quantity=1,
            is_pending=False,
            is_proxy=False,
            role="commander",
        )
    )

    loose = StorageLocation(user_id=user.id, name="Drawer 1", type="drawer")
    db.add(loose)
    db.flush()
    for i in range(3):
        card = _card(
            f"sf-sugg-{i:02d}",
            f"Owned Removal {i}",
            type_line="Instant",
            oracle="Destroy target creature.",
        )
        db.add(card)
        db.flush()
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=card.id,
                quantity=1,
                finish="normal",
                is_pending=False,
                is_proxy=False,
                storage_location_id=loose.id,
            )
        )
    db.commit()
    return deck


def test_analysis_page_renders_with_priced_suggestions(client, db, user):
    """A TEXT price must not reach `'%.2f'|format` — that is a 500."""
    deck = _deck_with_a_suggestion(db, user)
    resp = client.get(f"/decks/{deck.id}/analysis")
    assert resp.status_code == 200


def test_suggestions_carry_hover_attrs_and_the_engine_is_loaded(client, db, user):
    deck = _deck_with_a_suggestion(db, user)
    html = client.get(f"/decks/{deck.id}/analysis").text

    # Assert the rows EXIST first. Without this the rest passes vacuously on a
    # fixture the suggestion engine rejects — which the first draft did.
    assert "upgrade-suggestion" in html, "fixture produced no suggestions; nothing was tested"
    assert "data-card-hover" in html, "container attr missing"
    assert "data-card-image=" in html, "row attrs missing"
    assert "card-hover.js" in html, "attrs emitted with no engine — a dead control (#168)"
