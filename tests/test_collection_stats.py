"""#74 — Collection Overview pill classification.

Cards in a non-drawer StorageLocation (deck/binder/box/other) must be tallied
under their location, NOT counted as Unassigned. Pending is excluded from the
placement pills (D2). Covers REQ-001..REQ-009 plus the defensive edge cases.
"""

from __future__ import annotations

import pytest

from app.inventory_service import get_inventory_row_stats
from app.models import Card, InventoryRow, StorageLocation


@pytest.fixture
def make_card(db):
    counter = {"n": 0}

    def _make(name="Sol Ring", price="1.00"):
        counter["n"] += 1
        c = Card(
            scryfall_id=f"sid-{counter['n']}",
            name=name,
            set_code="cmr",
            collector_number=str(counter["n"]),
            price_usd=price,
        )
        db.add(c)
        db.flush()
        return c

    return _make


@pytest.fixture
def make_loc(db, user):
    def _make(name, type_="other"):
        loc = StorageLocation(user_id=user.id, name=name, type=type_)
        db.add(loc)
        db.flush()
        return loc

    return _make


def _row(db, user, card, **kw):
    kw.setdefault("finish", "normal")
    kw.setdefault("quantity", 1)
    kw.setdefault("is_pending", False)
    r = InventoryRow(card_id=card.id, user_id=user.id, **kw)
    db.add(r)
    db.flush()
    return r


def _stats(db, user):
    return get_inventory_row_stats(db, user_id=user.id)


def test_drawer_row_counted_in_drawer(db, user, make_card, make_loc):
    # REQ-003 + test #1
    d = make_loc("Drawer 3", type_="drawer")
    _row(db, user, make_card(), drawer="3", slot="1", storage_location_id=d.id)
    s = _stats(db, user)
    assert s["drawer_counts"]["3"] == 1
    assert s["non_drawer_location_counts"] == {}
    assert s["unassigned_count"] == 0


def test_deck_row_counted_under_location(db, user, make_card, make_loc):
    # REQ-001 + test #2
    deck = make_loc("Atraxa", type_="deck")
    _row(db, user, make_card(), drawer=None, storage_location_id=deck.id)
    s = _stats(db, user)
    assert s["non_drawer_location_counts"] == {"Atraxa": 1}
    assert s["unassigned_count"] == 0


def test_binder_row_counted_under_location(db, user, make_card, make_loc):
    # REQ-002 + test #3
    binder = make_loc("Trade Binder", type_="binder")
    _row(db, user, make_card(), drawer=None, storage_location_id=binder.id)
    s = _stats(db, user)
    assert s["non_drawer_location_counts"] == {"Trade Binder": 1}
    assert s["unassigned_count"] == 0


def test_truly_unassigned(db, user, make_card):
    # REQ-004 + test #4
    _row(db, user, make_card(), drawer=None, storage_location_id=None)
    s = _stats(db, user)
    assert s["unassigned_count"] == 1
    assert s["non_drawer_location_counts"] == {}


def test_drawer_wins_over_mismatched_location(db, user, make_card, make_loc):
    # test #5 — defensive: drawer set + a NON-matching deck location, drawer wins
    deck = make_loc("Some Deck", type_="deck")
    _row(db, user, make_card(), drawer="3", storage_location_id=deck.id)
    s = _stats(db, user)
    assert s["drawer_counts"]["3"] == 1
    assert "Some Deck" not in s["non_drawer_location_counts"]


def test_nonstandard_drawer_no_location_is_unassigned(db, user, make_card):
    # test #6 — drawer="7" is not in 1-6 and no location → unassigned
    _row(db, user, make_card(), drawer="7", storage_location_id=None)
    s = _stats(db, user)
    assert s["unassigned_count"] == 1


def test_nonstandard_drawer_with_location_falls_to_location(db, user, make_card, make_loc):
    # test #7 — drawer="7" falls through, location catches it
    box = make_loc("Overflow Box", type_="box")
    _row(db, user, make_card(), drawer="7", storage_location_id=box.id)
    s = _stats(db, user)
    assert s["non_drawer_location_counts"] == {"Overflow Box": 1}
    assert s["unassigned_count"] == 0


def test_two_rows_same_location_aggregate_by_quantity(db, user, make_card, make_loc):
    # REQ-006 + tests #8/#9
    binder = make_loc("Binder")
    _row(db, user, make_card(), storage_location_id=binder.id, quantity=3)
    _row(db, user, make_card(), storage_location_id=binder.id, quantity=2)
    s = _stats(db, user)
    assert s["non_drawer_location_counts"] == {"Binder": 5}


def test_same_name_different_ids_merge(db, user, make_card, make_loc):
    # REQ-007 — by-name aggregation
    a = make_loc("Cube")
    b = make_loc("Cube")
    assert a.id != b.id
    _row(db, user, make_card(), storage_location_id=a.id, quantity=2)
    _row(db, user, make_card(), storage_location_id=b.id, quantity=4)
    s = _stats(db, user)
    assert s["non_drawer_location_counts"] == {"Cube": 6}


def test_orphaned_location_does_not_crash(db, user, make_card):
    # REQ-008 + test #10 — non-null FK that doesn't resolve (FK enforcement off)
    _row(db, user, make_card(), storage_location_id=999999)
    s = _stats(db, user)
    assert s["non_drawer_location_counts"] == {"[orphaned location]": 1}


def test_pending_excluded_from_placement_pills(db, user, make_card):
    # REQ-009 (D2) — pending counted only in pending_cards
    _row(db, user, make_card(), drawer=None, storage_location_id=None, is_pending=True, quantity=2)
    s = _stats(db, user)
    assert s["pending_cards"] == 2
    assert s["unassigned_count"] == 0
    assert s["non_drawer_location_counts"] == {}


def test_route_pill_ordering(client, db, user, make_card, make_loc):
    # REQ-005 + test #12 — drawers, then alpha locations, then Unassigned.
    d = make_loc("Drawer 1", type_="drawer")
    _row(db, user, make_card(), drawer="1", slot="1", storage_location_id=d.id)
    _row(db, user, make_card(), storage_location_id=make_loc("Zed Box", "box").id)
    _row(db, user, make_card(), storage_location_id=make_loc("Alpha Deck", "deck").id)
    _row(db, user, make_card(), storage_location_id=None)  # unassigned
    db.commit()

    resp = client.get("/collection")
    assert resp.status_code == 200
    body = resp.text
    order = [body.index(x) for x in ("Drawer 1", "Alpha Deck", "Zed Box", "Unassigned")]
    assert order == sorted(order), body
