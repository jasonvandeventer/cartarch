"""Load-bearing route flows via the TestClient fixture (Session B).

Characterizes the user-facing flows through the real route → service → DB path:
the Collection search/filter grammar (name / set / color) and deck creation
(incl. the paired deck-type StorageLocation invariant). Storage-move
reassignment is already covered by tests/test_collection_bulk.py.

invariant: architecture.md → InventoryRow search grammar (apply_collection_search_filters)
invariant: architecture.md → every type="deck" StorageLocation has a paired Deck row
"""

from __future__ import annotations

from app.models import Card, Deck, InventoryRow, StorageLocation


def _placed(db, user, name, set_code, colors, color_identity):
    """Seed one placed (non-pending) InventoryRow for a card with given traits."""
    card = Card(
        scryfall_id=f"sid-{name}",
        name=name,
        set_code=set_code,
        collector_number="1",
        type_line="Creature",
        colors=colors,
        color_identity=color_identity,
    )
    db.add(card)
    db.flush()
    db.add(
        InventoryRow(
            card_id=card.id,
            user_id=user.id,
            quantity=1,
            finish="normal",
            is_pending=False,
            storage_location_id=None,
        )
    )
    db.commit()


def test_collection_search_by_name(client, db, user):
    _placed(db, user, "Lightning Bolt", "lea", "R", "R")
    _placed(db, user, "Counterspell", "lea", "U", "U")
    r = client.get("/collection?search=bolt")
    assert r.status_code == 200
    assert "Lightning Bolt" in r.text
    assert "Counterspell" not in r.text


def test_collection_search_by_set(client, db, user):
    _placed(db, user, "AlphaCard", "lea", "R", "R")
    _placed(db, user, "ModernCard", "mh3", "R", "R")
    r = client.get("/collection?search=s:mh3")
    assert r.status_code == 200
    assert "ModernCard" in r.text
    assert "AlphaCard" not in r.text


def test_collection_search_by_color(client, db, user):
    _placed(db, user, "RedOne", "tst", "R", "R")
    _placed(db, user, "BlueOne", "tst", "U", "U")
    r = client.get("/collection?search=c:r")
    assert r.status_code == 200
    assert "RedOne" in r.text
    assert "BlueOne" not in r.text


def test_deck_creation_route_creates_deck_and_paired_location(client, db, user):
    r = client.post(
        "/decks/create",
        data={
            "name": "My Commander Deck",
            "format_name": "commander",
            "notes": "",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    deck = db.query(Deck).filter(Deck.user_id == user.id, Deck.name == "My Commander Deck").first()
    assert deck is not None
    # The v3.3 invariant: every deck has a paired type="deck" StorageLocation.
    loc = db.query(StorageLocation).filter(StorageLocation.id == deck.storage_location_id).first()
    assert loc is not None
    assert loc.type == "deck"
