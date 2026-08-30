"""A deck page shows its own recent activity (feature request, 2026-08-29).

The same panel /locations/{id} has carried since v4.13.31, now on the deck
page. The one thing that is NOT the same is the lookup key: `TransactionLog`
stores a free-text location and has no FK, and a deck is written under THREE
labels — the generic move primitive writes the bare location name, the deck
primitives write `deck:<name>`, and the holding area writes
`considering:<name>`. A test that seeds only one of those cannot tell a
correct lookup from a lucky one, so this seeds all three and drives the PAGE.
"""

from __future__ import annotations

from app.audit_service import log_transaction, recent_location_activity
from app.deck_service import create_deck
from app.models import Card


def _card(db, name: str) -> Card:
    c = Card(name=name, set_code="tst", collector_number="1", scryfall_id=f"sid-{name}")
    db.add(c)
    db.flush()
    return c


def _seed(db, user, deck_name: str):
    deck = create_deck(db, user.id, deck_name)
    moved = _card(db, "Sol Ring")
    pulled = _card(db, "Arcane Signet")
    considered = _card(db, "Lightning Greaves")
    left = _card(db, "Mana Crypt")
    # 1) generic move into the deck's location — bare location name.
    log_transaction(
        session=db,
        user_id=user.id,
        event_type="location_updated",
        card_id=moved.id,
        finish="normal",
        quantity_delta=0,
        source_location="Binder",
        destination_location=deck_name,
    )
    # 2) the deck primitive — "deck:<name>".
    log_transaction(
        session=db,
        user_id=user.id,
        event_type="pull_to_deck",
        card_id=pulled.id,
        finish="normal",
        quantity_delta=-1,
        source_location="collection",
        destination_location=f"deck:{deck_name}",
    )
    # 3) the Considering holding area — "considering:<name>".
    log_transaction(
        session=db,
        user_id=user.id,
        event_type="add_to_considering",
        card_id=considered.id,
        finish="normal",
        quantity_delta=-1,
        source_location="collection",
        destination_location=f"considering:{deck_name}",
    )
    # 4) a card that LEFT — the direction the location panel exists to show.
    log_transaction(
        session=db,
        user_id=user.id,
        event_type="return_from_deck",
        card_id=left.id,
        finish="normal",
        quantity_delta=1,
        source_location=f"deck:{deck_name}",
        destination_location="collection",
    )
    db.commit()
    return deck


def test_the_deck_page_renders_activity_under_all_three_labels(client, db, user):
    deck = _seed(db, user, "Atraxa Superfriends")

    page = client.get(f"/decks/{deck.id}").text
    assert "Recent activity" in page, "the activity panel must render on a deck"
    for name in ("Sol Ring", "Arcane Signet", "Lightning Greaves"):
        assert name in page, f"{name} arrived in this deck and must be listed"
    # A card that left is shown too, naming where it went — that departure is
    # the entry that explains "where did it go?", same as on a location page.
    assert "Mana Crypt" in page


def test_activity_is_deck_scoped_and_owner_scoped(client, db, user):
    """Free-text matching must not pull in a same-named place, another deck's
    events, or another account's history."""
    deck = _seed(db, user, "Atraxa Superfriends")
    other = _card(db, "Rhystic Study")
    log_transaction(
        session=db,
        user_id=user.id,
        event_type="pull_to_deck",
        card_id=other.id,
        finish="normal",
        quantity_delta=-1,
        source_location="collection",
        destination_location="deck:Some Other Deck",
    )
    db.commit()

    page = client.get(f"/decks/{deck.id}").text
    assert "Rhystic Study" not in page

    names = ["Atraxa Superfriends", "deck:Atraxa Superfriends", "considering:Atraxa Superfriends"]
    assert recent_location_activity(db, user.id, names)
    assert recent_location_activity(db, user.id + 999, names) == []


def test_a_string_name_still_works(db, user):
    """The location page passes one string; the list form must not break it."""
    _seed(db, user, "Atraxa Superfriends")
    one = recent_location_activity(db, user.id, "deck:Atraxa Superfriends")
    assert [e["card_name"] for e in one] == ["Mana Crypt", "Arcane Signet"]
    assert [e["left_here"] for e in one] == [True, False]
