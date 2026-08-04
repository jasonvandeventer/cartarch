"""Status facet: the "Not in deck" option (v4.13.1).

`not_in_deck` must include rows in non-deck locations AND rows with no
location at all (NOT IN alone silently drops NULLs), and must exclude rows
living in deck-type locations. Counts mirror the filter.

    pytest tests/test_collection_status_facets.py
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.inventory_service import apply_collection_facet_filters, get_collection_facet_counts
from app.models import Card, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _card(s, name):
    c = Card(
        name=name,
        scryfall_id=f"sf-{next(_seq)}",
        set_code="tst",
        set_name="Test",
        collector_number=str(next(_seq)),
        rarity="common",
    )
    s.add(c)
    s.flush()
    return c


def test_not_in_deck_filter_and_counts():
    s = _fresh_session()
    u = User(username="u", password_hash="x")
    s.add(u)
    s.flush()
    deck_loc = StorageLocation(user_id=u.id, name="deck loc", type="deck")
    box_loc = StorageLocation(user_id=u.id, name="box", type="box")
    s.add_all([deck_loc, box_loc])
    s.flush()

    in_deck = InventoryRow(
        user_id=u.id, card_id=_card(s, "In Deck").id, storage_location_id=deck_loc.id, quantity=1
    )
    in_box = InventoryRow(
        user_id=u.id, card_id=_card(s, "In Box").id, storage_location_id=box_loc.id, quantity=1
    )
    nowhere = InventoryRow(
        user_id=u.id, card_id=_card(s, "Nowhere").id, storage_location_id=None, quantity=1
    )
    s.add_all([in_deck, in_box, nowhere])
    s.commit()

    base = s.query(InventoryRow).join(Card).filter(InventoryRow.user_id == u.id)
    got = {
        r.card.name
        for r in apply_collection_facet_filters(s, base, u.id, facet_status="not_in_deck")
    }
    assert got == {"In Box", "Nowhere"}

    # in_deck and not_in_deck are complements over the same rows.
    got_in = {
        r.card.name for r in apply_collection_facet_filters(s, base, u.id, facet_status="in_deck")
    }
    assert got_in == {"In Deck"}

    counts = get_collection_facet_counts(s, u.id)
    assert counts["status"]["in_deck"] == 1
    assert counts["status"]["not_in_deck"] == 2
