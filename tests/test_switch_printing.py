"""Switch Printing owned-swap tests (issue #58).

`switch_deck_row_printing` used to rewrite `card_id`/`finish` in place, bypassing
all inventory accounting. It now performs a quantity-conserving two-leg swap:
return the old printing to the collection as pending, consume an owned LOOSE copy
of the target, merge/log with the same discipline as pull/return.

Invoke via:  pytest tests/test_switch_printing.py
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import (
    legacy_tables as _legacy_tables,  # noqa: F401 — binds raw bracket tables to Base.metadata
)
from app.db import Base
from app.deck_service import list_user_printings_for_card, switch_deck_row_printing
from app.models import (
    Card,
    Deck,
    DeckCardShare,
    InventoryRow,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    TransactionLog,
    User,
    VariantGroup,
)

_seq = itertools.count(1)


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _user(s) -> User:
    u = User(username=f"u{next(_seq)}@x.com", password_hash="x")
    s.add(u)
    s.flush()
    return u


def _deck(s, user_id, name="Deck", group_id=None) -> Deck:
    loc = StorageLocation(user_id=user_id, name=name, type="deck", mode="managed")
    s.add(loc)
    s.flush()
    d = Deck(user_id=user_id, name=name, storage_location_id=loc.id, variant_group_id=group_id)
    s.add(d)
    s.flush()
    return d


def _loc(s, user_id, name, type_) -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    s.add(loc)
    s.flush()
    return loc


def _card(s, name="Sol Ring") -> Card:
    n = next(_seq)
    c = Card(
        scryfall_id=f"sid-{n}",
        name=name,
        set_code=f"s{n}",
        set_name="Test Set",
        collector_number=str(n),
        type_line="Artifact",
    )
    s.add(c)
    s.flush()
    return c


def _row(s, user_id, card, *, loc_id=None, qty=1, finish="normal", pending=False, proxy=False):
    r = InventoryRow(
        card_id=card.id,
        user_id=user_id,
        storage_location_id=loc_id,
        finish=finish,
        quantity=qty,
        is_pending=pending,
        is_proxy=proxy,
    )
    s.add(r)
    s.flush()
    return r


def _logs(s, user_id):
    return (
        s.query(TransactionLog)
        .filter(TransactionLog.user_id == user_id, TransactionLog.event_type == "switch_printing")
        .all()
    )


def _qty_for(s, user_id, card_id):
    return sum(
        int(r.quantity or 0)
        for r in s.query(InventoryRow).filter_by(user_id=user_id, card_id=card_id).all()
    )


# --------------------------------------------------------------------------- #
# Core swap
# --------------------------------------------------------------------------- #


def test_happy_path_owned_loose_swap():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder A", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id)
    _row(s, u.id, b, loc_id=binder.id)
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is True

    s.refresh(deck_row)
    assert deck_row.card_id == b.id and deck_row.finish == "normal"
    # old printing A back as pending
    pend = s.query(InventoryRow).filter_by(user_id=u.id, card_id=a.id, is_pending=True).one()
    assert pend.quantity == 1 and pend.storage_location_id is None
    # conservation: 1 of each printing before and after
    assert _qty_for(s, u.id, a.id) == 1 and _qty_for(s, u.id, b.id) == 1
    logs = _logs(s, u.id)
    assert len(logs) == 2 and logs[0].note == logs[1].note


def test_unowned_target_rejected():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")  # b in catalog, unowned
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id)
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is False
    s.refresh(deck_row)
    assert deck_row.card_id == a.id
    assert _logs(s, u.id) == []
    assert _qty_for(s, u.id, b.id) == 0  # no phantom row


def test_no_loose_copies_rejected():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id, "Main")
    other = _deck(s, u.id, "Other")
    a, b = _card(s, "Card A"), _card(s, "Card B")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id)
    _row(s, u.id, b, loc_id=other.storage_location_id)  # only copy is deck-resident
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is False
    s.refresh(deck_row)
    assert deck_row.card_id == a.id
    assert _logs(s, u.id) == []


def test_no_op_same_printing():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a = _card(s, "Card A")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id)
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, a.scryfall_id, "normal") is True
    s.refresh(deck_row)
    assert deck_row.card_id == a.id and deck_row.finish == "normal"
    assert _logs(s, u.id) == []


def test_proxy_row_rejected():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, proxy=True)
    binder = _loc(s, u.id, "Binder", "binder")
    _row(s, u.id, b, loc_id=binder.id)
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is False
    s.refresh(deck_row)
    assert deck_row.card_id == a.id
    assert _logs(s, u.id) == []


def test_card_not_in_local_db():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a = _card(s, "Card A")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id)
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, "does-not-exist", "normal") is (
        False
    )
    s.refresh(deck_row)
    assert deck_row.card_id == a.id


# --------------------------------------------------------------------------- #
# Quantity & multi-row
# --------------------------------------------------------------------------- #


def test_multi_quantity_row():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=3)
    _row(s, u.id, b, loc_id=binder.id, qty=3)
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is True
    s.refresh(deck_row)
    assert deck_row.card_id == b.id and deck_row.quantity == 3
    pend = s.query(InventoryRow).filter_by(user_id=u.id, card_id=a.id, is_pending=True).one()
    assert pend.quantity == 3
    # loose B fully consumed
    assert (
        s.query(InventoryRow)
        .filter_by(user_id=u.id, card_id=b.id, storage_location_id=binder.id)
        .first()
        is None
    )


def test_insufficient_aggregate_loose():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=3)
    _row(s, u.id, b, loc_id=binder.id, qty=2)  # only 2 loose
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is False
    s.refresh(deck_row)
    assert deck_row.card_id == a.id and deck_row.quantity == 3
    assert _logs(s, u.id) == []


def test_loose_split_across_rows():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    drawer = _loc(s, u.id, "Drawer", "drawer")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=3)
    _row(s, u.id, b, loc_id=drawer.id, qty=1)
    _row(s, u.id, b, loc_id=binder.id, qty=1)
    _row(s, u.id, b, qty=1, pending=True)  # pending / no location
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is True
    # all three loose sources consumed — only the deck row holds B now
    # (assert by location, not PK: SQLite reuses a deleted row's id for the new pending row)
    loose_b = [
        r
        for r in s.query(InventoryRow).filter_by(user_id=u.id, card_id=b.id).all()
        if r.storage_location_id != deck.storage_location_id
    ]
    assert loose_b == []
    assert _qty_for(s, u.id, b.id) == 3


def test_consumption_priority_order():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    drawer = _loc(s, u.id, "Drawer", "drawer")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=1)
    _row(s, u.id, b, loc_id=drawer.id, qty=1)
    pending_row = _row(s, u.id, b, qty=1, pending=True)
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is True
    # drawer (tier 0) consumed first; the pending B copy (tier 4) is untouched
    assert (
        s.query(InventoryRow)
        .filter_by(user_id=u.id, card_id=b.id, storage_location_id=drawer.id)
        .first()
        is None
    )
    s.refresh(pending_row)
    assert pending_row.quantity == 1 and pending_row.card_id == b.id


# --------------------------------------------------------------------------- #
# Merge & metadata
# --------------------------------------------------------------------------- #


def test_duplicate_deck_row_merge():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    row_a = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=1)
    row_b = _row(s, u.id, b, loc_id=deck.storage_location_id, qty=1)
    _row(s, u.id, b, loc_id=binder.id, qty=1)  # loose B to consume
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, row_a.id, b.scryfall_id, "normal") is True
    s.refresh(row_b)
    assert row_b.quantity == 2  # source row merged into target
    # A no longer in the deck; exactly one B row in the deck (no duplicate)
    assert (
        s.query(InventoryRow)
        .filter_by(user_id=u.id, card_id=a.id, storage_location_id=deck.storage_location_id)
        .count()
        == 0
    )
    deck_b = (
        s.query(InventoryRow)
        .filter_by(user_id=u.id, card_id=b.id, storage_location_id=deck.storage_location_id)
        .all()
    )
    assert len(deck_b) == 1


def test_merge_metadata_target_survives():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    row_a = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=1)
    row_a.role, row_a.tags, row_a.notes = "draw", "Ramp", "keep this"
    row_b = _row(s, u.id, b, loc_id=deck.storage_location_id, qty=1)
    row_b.role, row_b.tags = "removal", "Removal"
    _row(s, u.id, b, loc_id=binder.id, qty=1)
    s.commit()

    switch_deck_row_printing(s, u.id, deck.id, row_a.id, b.scryfall_id, "normal")
    s.refresh(row_b)
    assert row_b.role == "removal" and row_b.tags == "Removal" and row_b.notes is None


def test_metadata_preserved_no_merge():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=1)
    deck_row.role, deck_row.tags, deck_row.notes = "commander", "Voltron", "sig card"
    _row(s, u.id, b, loc_id=binder.id, qty=1)
    s.commit()

    switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal")
    s.refresh(deck_row)
    assert deck_row.card_id == b.id
    assert (
        deck_row.role == "commander" and deck_row.tags == "Voltron" and deck_row.notes == "sig card"
    )


# --------------------------------------------------------------------------- #
# Audit & reference cleanup
# --------------------------------------------------------------------------- #


def test_transaction_log_legs():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id, "MyDeck")
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=2)
    _row(s, u.id, b, loc_id=binder.id, qty=2)
    s.commit()

    switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal")
    logs = _logs(s, u.id)
    ret = next(x for x in logs if x.card_id == a.id)
    con = next(x for x in logs if x.card_id == b.id)
    assert ret.quantity_delta == 2 and ret.source_location == "deck:MyDeck"
    assert ret.destination_location == "collection"
    assert con.quantity_delta == -2 and con.source_location == "collection"
    assert con.destination_location == "deck:MyDeck"
    assert ret.note == con.note


def test_deck_card_share_cleanup():
    s = _session()
    u = _user(s)
    grp = VariantGroup(user_id=u.id, name="G")
    s.add(grp)
    s.flush()
    deck = _deck(s, u.id, "Main", group_id=grp.id)
    sib = _deck(s, u.id, "Sib", group_id=grp.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=1)
    _row(s, u.id, b, loc_id=binder.id, qty=1)
    share = DeckCardShare(
        inventory_row_id=deck_row.id,
        source_deck_id=deck.id,
        target_deck_id=sib.id,
        variant_group_id=grp.id,
    )
    s.add(share)
    s.commit()

    switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal")
    assert s.query(DeckCardShare).count() == 0


def test_source_row_reference_cleanup():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=1)
    src = _row(s, u.id, b, loc_id=binder.id, qty=1)
    sc = Showcase(user_id=u.id, name="SC")
    s.add(sc)
    s.flush()
    item = ShowcaseItem(showcase_id=sc.id, inventory_row_id=src.id, quantity_offered=1)
    s.add(item)
    s.commit()

    switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal")
    # source row consumed to zero → its ShowcaseItem cleaned up
    assert (
        s.query(InventoryRow)
        .filter_by(user_id=u.id, card_id=b.id, storage_location_id=binder.id)
        .first()
        is None
    )
    assert s.query(ShowcaseItem).count() == 0


def test_existing_pending_old_printing_increment():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=1)
    pend_a = _row(s, u.id, a, qty=2, pending=True)  # existing pending of old printing
    _row(s, u.id, b, loc_id=binder.id, qty=1)
    s.commit()

    switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal")
    pend_rows = s.query(InventoryRow).filter_by(user_id=u.id, card_id=a.id, is_pending=True).all()
    assert len(pend_rows) == 1 and pend_rows[0].id == pend_a.id and pend_rows[0].quantity == 3


def test_other_deck_copy_guard():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id, "Main")
    other = _deck(s, u.id, "Other")
    a, b = _card(s, "Card A"), _card(s, "Card B")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id)
    _row(s, u.id, b, loc_id=other.storage_location_id)  # only copy deck-resident
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is False


# --------------------------------------------------------------------------- #
# Conservation
# --------------------------------------------------------------------------- #


def test_conservation_merge_and_rewrite_paths():
    # rewrite path
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=2)
    _row(s, u.id, b, loc_id=binder.id, qty=2)
    s.commit()
    before = _qty_for(s, u.id, a.id) + _qty_for(s, u.id, b.id)
    switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal")
    assert _qty_for(s, u.id, a.id) + _qty_for(s, u.id, b.id) == before

    # merge path
    s2 = _session()
    u2 = _user(s2)
    d2 = _deck(s2, u2.id)
    a2, b2 = _card(s2, "Card A"), _card(s2, "Card B")
    bind2 = _loc(s2, u2.id, "Binder", "binder")
    ra = _row(s2, u2.id, a2, loc_id=d2.storage_location_id, qty=1)
    _row(s2, u2.id, b2, loc_id=d2.storage_location_id, qty=1)
    _row(s2, u2.id, b2, loc_id=bind2.id, qty=1)
    s2.commit()
    before2 = _qty_for(s2, u2.id, a2.id) + _qty_for(s2, u2.id, b2.id)
    switch_deck_row_printing(s2, u2.id, d2.id, ra.id, b2.scryfall_id, "normal")
    assert _qty_for(s2, u2.id, a2.id) + _qty_for(s2, u2.id, b2.id) == before2


def test_no_duplicate_pending_after_repeated_swaps():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    a, b = _card(s, "Card A"), _card(s, "Card B")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, a, loc_id=deck.storage_location_id, qty=1)
    _row(s, u.id, b, loc_id=binder.id, qty=1)
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, b.scryfall_id, "normal") is True
    # now old A is loose pending; swap back consumes it
    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, a.scryfall_id, "normal") is True
    # no printing has more than one pending row
    for card in (a, b):
        pend = s.query(InventoryRow).filter_by(user_id=u.id, card_id=card.id, is_pending=True).all()
        assert len(pend) <= 1


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


def test_same_name_different_printing_finish():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    sol1, sol2 = _card(s, "Sol Ring"), _card(s, "Sol Ring")  # two printings
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, sol1, loc_id=deck.storage_location_id, finish="normal")
    _row(s, u.id, sol2, loc_id=binder.id, finish="foil")
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, sol2.scryfall_id, "foil") is True
    s.refresh(deck_row)
    assert deck_row.card_id == sol2.id and deck_row.finish == "foil"


def test_same_card_id_different_finish():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    sol = _card(s, "Sol Ring")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, sol, loc_id=deck.storage_location_id, finish="normal")
    _row(s, u.id, sol, loc_id=binder.id, finish="foil")  # loose foil of SAME printing
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, sol.scryfall_id, "foil") is True
    s.refresh(deck_row)
    assert deck_row.card_id == sol.id and deck_row.finish == "foil"


def test_finish_normalization():
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    sol = _card(s, "Sol Ring")
    binder = _loc(s, u.id, "Binder", "binder")
    deck_row = _row(s, u.id, sol, loc_id=deck.storage_location_id, finish="normal")
    _row(s, u.id, sol, loc_id=binder.id, finish="foil")
    s.commit()

    assert switch_deck_row_printing(s, u.id, deck.id, deck_row.id, sol.scryfall_id, "Foil") is True
    s.refresh(deck_row)
    assert deck_row.finish == "foil"


def test_loose_quantity_annotation():
    # list_user_printings_for_card advisory annotation: deck copies are not loose.
    s = _session()
    u = _user(s)
    deck = _deck(s, u.id)
    binder = _loc(s, u.id, "Binder", "binder")
    a = _card(s, "Card A")
    _row(s, u.id, a, loc_id=deck.storage_location_id, qty=1)  # in deck → not loose
    _row(s, u.id, a, loc_id=binder.id, qty=2)  # loose
    s.commit()

    entries = list_user_printings_for_card(s, u.id, "Card A")
    assert len(entries) == 1
    assert entries[0]["quantity"] == 3 and entries[0]["loose_quantity"] == 2


def test_crafted_post_disabled_option(client, db, user):
    # Route-level: a POST for a printing with 0 loose copies → 400, no mutation.
    deck = _deck(db, user.id, "Main")
    other = _deck(db, user.id, "Other")
    a, b = _card(db, "Card A"), _card(db, "Card B")
    deck_row = _row(db, user.id, a, loc_id=deck.storage_location_id)
    _row(db, user.id, b, loc_id=other.storage_location_id)  # only copy deck-resident
    db.commit()

    resp = client.post(
        f"/decks/{deck.id}/rows/{deck_row.id}/switch-printing",
        data={"scryfall_id": b.scryfall_id, "finish": "normal", "csrf_token": "x"},
    )
    assert resp.status_code == 400
    assert "loose copy" in resp.text
    db.refresh(deck_row)
    assert deck_row.card_id == a.id
