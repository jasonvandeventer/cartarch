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
