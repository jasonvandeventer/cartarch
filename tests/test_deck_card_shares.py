"""Service-layer tests for variant-group deck sharing — deck_card_shares (issue #27).

A DeckCardShare records that a physical InventoryRow (still stored in its
SOURCE deck's storage location — one-card-one-location PRESERVED, no row
duplication) is ALSO a member of a SIBLING build's decklist within the same
variant group. A reference, never a copy.

Covers the issue's acceptance criteria at the service layer (cross-deck UX is
verified manually per the issue QA scope):
  - share creates a reference and does NOT move storage_location_id
  - validation: not-self, must-share-within-a-group, same-group, ownership
  - idempotency on (inventory_row_id, target_deck_id)
  - inbound/outbound query helpers + the FULL decklist count (own + shares),
    while the physical InventoryRow set (collection) is counted once
  - reconciliation recognizes an already-shared card as covered (counted once,
    no double-count with the sibling-location tally)
  - cascade: deleting the physical row drops the share; a deck leaving the
    group drops its shares; delete_variant_group + delete_deck drop shares
  - decks NOT in a variant group are unaffected (regression guards)

Invoke via:  pytest tests/test_deck_card_shares.py
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import deck_service
from app import (
    legacy_tables as _legacy_tables,  # noqa: F401 — binds raw bracket tables to Base.metadata
)
from app.db import Base
from app.inventory_service import clean_inventory_row_references
from app.models import Card, Deck, DeckCardShare, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _user(s, username="u1") -> User:
    u = User(username=username, password_hash="x")
    s.add(u)
    s.flush()
    return u


def _deck(s, user_id, name, group_id=None) -> Deck:
    loc = StorageLocation(user_id=user_id, name=name, type="deck", mode="managed")
    s.add(loc)
    s.flush()
    deck = Deck(user_id=user_id, name=name, storage_location_id=loc.id, variant_group_id=group_id)
    s.add(deck)
    s.flush()
    return deck


def _card(s, name="Shared Card") -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="Test",
        collector_number=str(next(_seq)),
        type_line="Artifact",
    )
    s.add(c)
    s.flush()
    return c


def _place(s, user_id, card, location_id, qty=1, finish="normal", is_proxy=False) -> InventoryRow:
    row = InventoryRow(
        card_id=card.id,
        user_id=user_id,
        storage_location_id=location_id,
        finish=finish,
        quantity=qty,
        is_pending=False,
        is_proxy=is_proxy,
    )
    s.add(row)
    s.flush()
    return row


def _row_input(card, qty=1, finish="normal"):
    return {
        "line_number": 1,
        "scryfall_id": card.scryfall_id,
        "finish": finish,
        "quantity": qty,
    }


def _group_with_two_decks(s, user):
    g = deck_service.create_variant_group(s, user.id, "Mana base")
    a = _deck(s, user.id, "Build A", group_id=g.id)
    b = _deck(s, user.id, "Build B", group_id=g.id)
    return g, a, b


# --------------------------------------------------------------------------- #
# share_card_to_deck — happy path + invariants
# --------------------------------------------------------------------------- #


def test_share_creates_reference_without_moving_row():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    card = _card(s)
    row = _place(s, u.id, card, a.storage_location_id)

    share = deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    assert share.source_deck_id == a.id
    assert share.target_deck_id == b.id
    assert share.variant_group_id == a.variant_group_id
    # one-card-one-location PRESERVED: the physical row never moved.
    s.refresh(row)
    assert row.storage_location_id == a.storage_location_id
    # no row duplication — sharing creates zero new InventoryRows.
    assert s.query(InventoryRow).count() == 1


def test_share_is_idempotent():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)

    first = deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)
    second = deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    assert first.id == second.id
    assert s.query(DeckCardShare).count() == 1


def test_share_rejects_self_target():
    s = _fresh_session()
    u = _user(s)
    _g, a, _b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    with pytest.raises(ValueError):
        deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=a.id)


def test_share_rejects_different_group():
    s = _fresh_session()
    u = _user(s)
    _g, a, _b = _group_with_two_decks(s, u)
    other_group = deck_service.create_variant_group(s, u.id, "Other")
    outsider = _deck(s, u.id, "Outsider", group_id=other_group.id)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    with pytest.raises(ValueError):
        deck_service.share_card_to_deck(
            s, u.id, inventory_row_id=row.id, target_deck_id=outsider.id
        )


def test_share_rejects_no_group():
    s = _fresh_session()
    u = _user(s)
    a = _deck(s, u.id, "Standalone A")
    b = _deck(s, u.id, "Standalone B")
    row = _place(s, u.id, _card(s), a.storage_location_id)
    with pytest.raises(ValueError):
        deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)


def test_share_rejects_card_not_in_deck():
    s = _fresh_session()
    u = _user(s)
    _g, _a, b = _group_with_two_decks(s, u)
    # a pending row (no storage location) is not in any deck.
    pending = InventoryRow(
        card_id=_card(s).id,
        user_id=u.id,
        storage_location_id=None,
        finish="normal",
        quantity=1,
        is_pending=True,
    )
    s.add(pending)
    s.flush()
    with pytest.raises(ValueError):
        deck_service.share_card_to_deck(s, u.id, inventory_row_id=pending.id, target_deck_id=b.id)


def test_share_rejects_cross_user():
    s = _fresh_session()
    u1 = _user(s, "u1")
    u2 = _user(s, "u2")
    g = deck_service.create_variant_group(s, u1.id, "G")
    a = _deck(s, u1.id, "A", group_id=g.id)
    b = _deck(s, u1.id, "B", group_id=g.id)
    row = _place(s, u1.id, _card(s), a.storage_location_id)
    # u2 cannot share into u1's deck.
    with pytest.raises(ValueError):
        deck_service.share_card_to_deck(s, u2.id, inventory_row_id=row.id, target_deck_id=b.id)


# --------------------------------------------------------------------------- #
# query helpers + counts
# --------------------------------------------------------------------------- #


def test_inbound_outbound_helpers_and_counts():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    # B has one own card; A has a card shared into B.
    _place(s, u.id, _card(s, "B Own"), b.storage_location_id)
    shared_card = _card(s, "Shared")
    row = _place(s, u.id, shared_card, a.storage_location_id, finish="foil")
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    inbound = deck_service.get_inbound_shares_for_deck(s, b)
    assert len(inbound) == 1
    assert inbound[0]["card"].id == shared_card.id
    assert inbound[0]["finish"] == "foil"
    assert inbound[0]["source_deck_name"] == "Build A"

    assert deck_service.inbound_shared_row_ids_for_deck(s, b) == {row.id}
    assert deck_service.inbound_share_count_for_deck(s, b) == 1
    # A is the SOURCE — it has no inbound shares, but an outbound one.
    assert deck_service.get_inbound_shares_for_deck(s, a) == []
    assert deck_service.outbound_share_map(s, a) == {row.id: ["Build B"]}
    assert deck_service.outbound_share_map(s, b) == {}


def test_list_decks_count_includes_shares_collection_does_not():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    _place(s, u.id, _card(s, "B Own"), b.storage_location_id)
    row = _place(s, u.id, _card(s, "Shared"), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    decks = {d.name: d for d in deck_service.list_decks(s, u.id)}
    # FULL decklist: B = its own 1 card + 1 inbound share.
    assert decks["Build B"].card_count == 2
    # A still counts only its own physical card (the share is outbound, not its own).
    assert decks["Build A"].card_count == 1
    # Collection = physical InventoryRows ONLY — counted exactly once each.
    assert s.query(InventoryRow).count() == 2


def test_unshare_removes_membership_only():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    assert deck_service.unshare_card_from_deck(
        s, u.id, inventory_row_id=row.id, target_deck_id=b.id
    )
    assert s.query(DeckCardShare).count() == 0
    # second unshare is a no-op
    assert not deck_service.unshare_card_from_deck(
        s, u.id, inventory_row_id=row.id, target_deck_id=b.id
    )
    # physical row untouched
    s.refresh(row)
    assert row.storage_location_id == a.storage_location_id


# --------------------------------------------------------------------------- #
# unified decklist + deck-edit-popout helpers (issue #27 revision)
# --------------------------------------------------------------------------- #


def test_inbound_shared_rows_unifies_and_filters():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    shared = _card(s, "Shared Bolt")
    row = _place(s, u.id, shared, a.storage_location_id, finish="foil")
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    pairs = deck_service.inbound_shared_rows_for_deck(s, b)
    assert [(r.id, name) for r, name in pairs] == [(row.id, "Build A")]
    # search filter narrows the shared-in set in lock-step with own rows.
    assert deck_service.inbound_shared_rows_for_deck(s, b, search="Shared") != []
    assert deck_service.inbound_shared_rows_for_deck(s, b, search="t:land") == []
    # source deck A sees no inbound rows.
    assert deck_service.inbound_shared_rows_for_deck(s, a) == []


def test_build_deck_card_items_folds_shared_in():
    from app.routes.decks import _build_deck_card_items

    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    _place(s, u.id, _card(s, "B Own"), b.storage_location_id)
    row = _place(s, u.id, _card(s, "A Shared"), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    items, _value, total_cards = _build_deck_card_items(s, b, u.id, "", "name", "asc")
    by_name = {i["card"].name: i for i in items}
    # FULL decklist: own card + shared-in card, in ONE unified list.
    assert set(by_name) == {"B Own", "A Shared"}
    assert by_name["A Shared"]["is_shared_in"] is True
    assert by_name["A Shared"]["shared_from"] == "Build A"
    assert by_name["B Own"]["is_shared_in"] is False
    # count includes the share (the full list), and the shared-in role is cleared
    # so it never lands in this deck's commander split.
    assert total_cards == 2
    assert by_name["A Shared"]["role"] is None


def test_popout_share_helpers():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    card = _card(s, "Picker Card")
    row = _place(s, u.id, card, a.storage_location_id, finish="foil")

    opts = deck_service.own_deck_card_options(s, u.id, a)
    assert [(o["name"], o["finish"]) for o in opts] == [("Picker Card", "foil")]

    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)
    out = deck_service.get_outbound_shares_for_deck(s, a)
    assert out == [
        {
            "inventory_row_id": row.id,
            "card_name": "Picker Card",
            "target_deck_id": b.id,
            "target_deck_name": "Build B",
        }
    ]
    # B is the target, not a source → no outbound shares.
    assert deck_service.get_outbound_shares_for_deck(s, b) == []


# --------------------------------------------------------------------------- #
# reconciliation — already-shared card recognized as covered (counted once)
# --------------------------------------------------------------------------- #


def test_reconciliation_recognizes_shared_card_once():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    card = _card(s)
    row = _place(s, u.id, card, a.storage_location_id, qty=1)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    out = deck_service.find_inventory_matches_for_deck_import(
        s, u.id, b.id, [_row_input(card, qty=1)]
    )[0]
    # Covered by the group, NOT "missing" — and counted ONCE (not 2, even though
    # the row is both sibling-held and shared-in).
    assert out["recommended_action"] == "covered_by_variant"
    assert out["variant_covered_qty"] == 1
    assert out["recommended_new_qty"] == 0


def test_reconciliation_unshared_sibling_still_covered():
    # v3.33.0 baseline preserved: a sibling-held card (no share yet) is covered.
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    card = _card(s)
    _place(s, u.id, card, a.storage_location_id, qty=1)
    out = deck_service.find_inventory_matches_for_deck_import(
        s, u.id, b.id, [_row_input(card, qty=1)]
    )[0]
    assert out["recommended_action"] == "covered_by_variant"
    assert out["variant_covered_qty"] == 1


# --------------------------------------------------------------------------- #
# cascades
# --------------------------------------------------------------------------- #


def test_deleting_physical_row_drops_share():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    clean_inventory_row_references(s, [row.id])
    s.delete(row)
    s.commit()
    assert s.query(DeckCardShare).count() == 0


def test_leaving_group_drops_shares():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    # B leaves the group → shares touching B are dropped.
    deck_service.assign_deck_variant_group(s, u.id, b.id, None)
    assert s.query(DeckCardShare).count() == 0


def test_delete_variant_group_drops_shares():
    s = _fresh_session()
    u = _user(s)
    g, a, b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    deck_service.delete_variant_group(s, u.id, g.id)
    assert s.query(DeckCardShare).count() == 0


def test_delete_deck_drops_shares_as_source_and_target():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    # Deleting the TARGET deck (B) must drop the share even though A is source.
    deck_service.delete_deck(s, b.id, u.id)
    assert s.query(DeckCardShare).count() == 0


def test_delete_source_deck_drops_shares():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    deck_service.delete_deck(s, a.id, u.id)
    assert s.query(DeckCardShare).count() == 0


# --------------------------------------------------------------------------- #
# dangling-share defense — a MOVED physical card invalidates its share
# (issue #27 revision, red-team Flaw 1)
# --------------------------------------------------------------------------- #


def _other_location(s, user_id, name, type_="binder"):
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    s.add(loc)
    s.flush()
    return loc


def test_moving_physical_row_out_invalidates_and_prunes_share():
    from app.inventory_service import move_inventory_row_to_location

    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    card = _card(s, "Mover")
    row = _place(s, u.id, card, a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)
    assert deck_service.inbound_share_count_for_deck(s, b) == 1

    # Move the physical card OUT of deck A into a binder (NOT deleted).
    binder = _other_location(s, u.id, "Binder")
    move_inventory_row_to_location(s, row.id, u.id, binder.id)

    # The share is now stale — it no longer renders, counts, or is recognized by
    # reconciliation, AND it was pruned from the table (one-card-one-location: a
    # card living in a binder is not a member of any deck).
    assert deck_service.get_inbound_shares_for_deck(s, b) == []
    assert deck_service.inbound_share_count_for_deck(s, b) == 0
    assert deck_service.inbound_shared_row_ids_for_deck(s, b) == set()
    assert deck_service.outbound_share_map(s, a) == {}
    assert s.query(DeckCardShare).count() == 0
    decks = {d.name: d for d in deck_service.list_decks(s, u.id)}
    assert decks["Build B"].card_count == 0
    # the physical row itself is intact, just relocated.
    s.refresh(row)
    assert row.storage_location_id == binder.id


def test_read_guard_hides_stale_share_even_without_prune():
    # Bypass the move primitives entirely: mutate the row's location directly so
    # NO prune fires. The read-side validity guard must STILL hide the now-stale
    # share — correctness can't depend on every mover remembering to prune.
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    binder = _other_location(s, u.id, "Binder")
    row.storage_location_id = binder.id  # raw move, no pruning
    s.flush()

    # The DB row still exists (un-pruned) ...
    assert s.query(DeckCardShare).count() == 1
    # ... but every read helper treats it as invalid.
    assert deck_service.get_inbound_shares_for_deck(s, b) == []
    assert deck_service.inbound_share_count_for_deck(s, b) == 0
    assert deck_service.inbound_shared_row_ids_for_deck(s, b) == set()
    assert deck_service.outbound_share_map(s, a) == {}


def test_returning_shared_card_from_deck_drops_share():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    row = _place(s, u.id, _card(s), a.storage_location_id)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    # Return the physical card from deck A back to the collection (pending).
    assert deck_service.return_card_from_deck(s, u.id, row.id)
    assert s.query(DeckCardShare).count() == 0
    assert deck_service.inbound_share_count_for_deck(s, b) == 0


# --------------------------------------------------------------------------- #
# import share-materialization — exactly the covered quantity, no more/less
# (issue #27 revision, red-team Flaws 2 & 3)
# --------------------------------------------------------------------------- #


def _run_deck_import(s, user_id, deck, parsed_rows):
    """Reconcile + commit a deck import the way the routes do, using the
    recommended actions/quantities."""
    from app.routes.imports import _commit_deck_import_with_reconciliation

    recon = deck_service.find_inventory_matches_for_deck_import(s, user_id, deck.id, parsed_rows)
    actions = [r["recommended_action"] for r in recon]
    move_qtys = [r["recommended_move_qty"] for r in recon]
    new_qtys = [r["recommended_new_qty"] for r in recon]
    return _commit_deck_import_with_reconciliation(
        s, user_id, deck, parsed_rows, actions, move_qtys, new_qtys, "test.txt"
    )


def test_import_does_not_over_share_across_multiple_siblings():
    # Flaw 2: two sibling decks each hold one copy of the SAME printing. Importing
    # ONE copy into a third sibling must create EXACTLY one share — not one per
    # sibling row.
    s = _fresh_session()
    u = _user(s)
    g = deck_service.create_variant_group(s, u.id, "G")
    a = _deck(s, u.id, "A", group_id=g.id)
    b = _deck(s, u.id, "B", group_id=g.id)
    c = _deck(s, u.id, "C", group_id=g.id)
    card = _card(s)
    _place(s, u.id, card, a.storage_location_id, qty=1)
    _place(s, u.id, card, b.storage_location_id, qty=1)

    res = _run_deck_import(s, u.id, c, [_row_input(card, qty=1)])

    assert res["shared_count"] == 1
    assert s.query(DeckCardShare).count() == 1
    assert deck_service.inbound_share_count_for_deck(s, c) == 1
    # no physical row was created/moved into C (covered entirely by the share).
    c_own = (
        s.query(InventoryRow)
        .filter(InventoryRow.storage_location_id == c.storage_location_id)
        .count()
    )
    assert c_own == 0


def test_import_materializes_share_on_partial_coverage():
    # Flaw 3: a sibling covers only PART of the imported quantity. The covered
    # part must still become a share even though the row's action is move_existing
    # (not covered_by_variant). need=2: sibling covers 1, a drawer covers 1.
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    card = _card(s)
    _place(s, u.id, card, a.storage_location_id, qty=1)  # sibling-covered copy
    drawer = _other_location(s, u.id, "Drawer", type_="drawer")
    _place(s, u.id, card, drawer.id, qty=1)  # movable copy

    res = _run_deck_import(s, u.id, b, [_row_input(card, qty=2)])

    # one copy materialized as a share, one copy physically moved from the drawer.
    assert res["shared_count"] == 1
    assert res["moved_count"] == 1
    assert s.query(DeckCardShare).count() == 1
    assert deck_service.inbound_share_count_for_deck(s, b) == 1
    # B physically holds exactly the moved copy.
    b_own = (
        s.query(InventoryRow)
        .filter(
            InventoryRow.storage_location_id == b.storage_location_id,
            InventoryRow.is_pending.is_(False),
        )
        .all()
    )
    assert sum(r.quantity for r in b_own) == 1
    # the drawer copy is gone (fully pulled).
    drawer_left = (
        s.query(InventoryRow).filter(InventoryRow.storage_location_id == drawer.id).count()
    )
    assert drawer_left == 0


def test_import_full_coverage_creates_one_share_and_is_idempotent():
    # Full sibling coverage → covered_by_variant → exactly one share; re-import
    # creates nothing new (idempotent on the unique pair).
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    card = _card(s)
    _place(s, u.id, card, a.storage_location_id, qty=1)

    res1 = _run_deck_import(s, u.id, b, [_row_input(card, qty=1)])
    assert res1["shared_count"] == 1
    assert s.query(DeckCardShare).count() == 1

    res2 = _run_deck_import(s, u.id, b, [_row_input(card, qty=1)])
    assert res2["shared_count"] == 0
    assert s.query(DeckCardShare).count() == 1


def test_import_into_non_variant_deck_creates_no_shares():
    # Regression: a deck with no variant group never materializes shares and
    # pays no recheck for import_new rows.
    s = _fresh_session()
    u = _user(s)
    a = _deck(s, u.id, "Solo A")
    b = _deck(s, u.id, "Solo B")
    card = _card(s)
    _place(s, u.id, card, a.storage_location_id, qty=1)
    # Importing the card B already-elsewhere into B: no variant group → no share.
    drawer = _other_location(s, u.id, "Drawer", type_="drawer")
    _place(s, u.id, card, drawer.id, qty=1)
    res = _run_deck_import(s, u.id, b, [_row_input(card, qty=1)])
    assert res["shared_count"] == 0
    assert s.query(DeckCardShare).count() == 0


# --------------------------------------------------------------------------- #
# regression guards — non-variant decks unaffected
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# route-level smoke (uses the shared conftest client / db fixtures)
# --------------------------------------------------------------------------- #


def test_routes_share_render_and_export(client, db, user):
    """Share via the route, confirm the deck-detail panel renders the shared-in
    card, and the export includes it with its foil marker preserved."""
    g, a, b = _group_with_two_decks(db, user)
    card = _card(db, "Foil Sworn")
    row = _place(db, user.id, card, a.storage_location_id, finish="foil")
    db.commit()

    # Share A's foil card into B via the route.
    resp = client.post(
        f"/decks/{a.id}/share-card",
        data={"inventory_row_id": row.id, "target_deck_id": b.id},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.query(DeckCardShare).count() == 1

    # B's detail page renders the shared-in card with the SHARED FROM badge.
    page = client.get(f"/decks/{b.id}")
    assert page.status_code == 200
    assert "Foil Sworn" in page.text
    assert "SHARED FROM" in page.text

    # B's export is the FULL decklist including the shared foil (with *F* marker).
    export = client.get(f"/decks/{b.id}/export")
    assert export.status_code == 200
    assert "Foil Sworn" in export.text
    assert "*F*" in export.text

    # Unshare via the route removes it again.
    resp = client.post(
        f"/decks/{b.id}/unshare-card",
        data={"inventory_row_id": row.id, "target_deck_id": b.id},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.query(DeckCardShare).count() == 0


def test_non_variant_decks_unaffected():
    s = _fresh_session()
    u = _user(s)
    a = _deck(s, u.id, "Standalone")
    _place(s, u.id, _card(s), a.storage_location_id)
    # All share helpers short-circuit to empty for a deck with no group.
    assert deck_service.get_inbound_shares_for_deck(s, a) == []
    assert deck_service.inbound_shared_row_ids_for_deck(s, a) == set()
    assert deck_service.inbound_share_count_for_deck(s, a) == 0
    assert deck_service.outbound_share_map(s, a) == {}
    # And the deck count is just its own physical cards.
    decks = {d.name: d for d in deck_service.list_decks(s, u.id)}
    assert decks["Standalone"].card_count == 1
