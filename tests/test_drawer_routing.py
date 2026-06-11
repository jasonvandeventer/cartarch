"""Drawer-vs-Bulk routing predicate (v3.38.0), Phase 1.

``should_keep_in_drawer`` is the single intrinsic-protection predicate shared by
both routing call sites (retroactive cull + intake routing). Per-printing grain
== ``(card_id, finish)`` (owner F2 decision 2026-06-10; no oracle_id grouping).

This suite pins each layer in isolation:
  - Layer 1: basic land (any kind / finish) -> keep
  - Layer 3: in any deck (card_id, finish-agnostic) -> keep
  - Layer 4: cached effective_price STRICTLY > threshold -> keep; == threshold
    is NOT kept; threshold is a parameter, not a literal
  - Otherwise (cheap, non-basic, not in a deck) -> False (bulk-eligible)
  - Layer precedence (basic wins even when cheap; in-deck wins even when cheap)
  - deck_member_card_ids: deck rows only, distinct, scoped to the user
  - No request-path network: predicate reads only cached columns

Layer 2 (keeper / keep-one) and Layer 5 (manual keep-list) are deliberately NOT
in this predicate — see the module docstring in inventory_service.
"""

from __future__ import annotations

import itertools

from app.inventory_service import (
    DRAWER_KEEP_PRICE_THRESHOLD,
    deck_member_card_ids,
    should_keep_in_drawer,
)
from app.models import Card, InventoryRow, StorageLocation

_seq = itertools.count(1)


def _card(
    db,
    *,
    name="Some Card",
    type_line="Creature — Goblin",
    price="0.10",
    price_foil=None,
) -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="Test",
        collector_number=str(next(_seq)),
        rarity="common",
        type_line=type_line,
        oracle_text="x",
        image_url="http://x/img.png",
        color_identity="",
        set_type="expansion",
        price_usd=price,
        price_usd_foil=price_foil,
    )
    db.add(c)
    db.flush()
    return c


def _loc(db, user_id, name, type_="box", mode="manual") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode=mode)
    db.add(loc)
    db.flush()
    return loc


def _row(db, user_id, card, *, finish="normal", qty=1, loc_id=None) -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        finish=finish,
        quantity=qty,
        storage_location_id=loc_id,
        is_pending=False,
    )
    db.add(row)
    db.flush()
    return row


# -- Layer 1: basic land --------------------------------------------------------


def test_basic_land_kept(db, user):
    card = _card(db, name="Island", type_line="Basic Land — Island", price="0.05")
    row = _row(db, user.id, card)
    assert should_keep_in_drawer(db, row, user_id=user.id, deck_card_ids=set()) is True


def test_snow_basic_land_kept(db, user):
    # Snow basics read "Basic Snow Land — Forest" (no "Basic Land" substring).
    card = _card(db, name="Snow-Covered Forest", type_line="Basic Snow Land — Forest", price="0.20")
    row = _row(db, user.id, card)
    assert should_keep_in_drawer(db, row, user_id=user.id, deck_card_ids=set()) is True


def test_nonbasic_land_not_kept_when_cheap(db, user):
    # A nonbasic land is NOT layer-1 protected; cheap + no deck -> bulk-eligible.
    card = _card(db, name="Evolving Wilds", type_line="Land", price="0.10")
    row = _row(db, user.id, card)
    assert should_keep_in_drawer(db, row, user_id=user.id, deck_card_ids=set()) is False


# -- Layer 3: in a deck ---------------------------------------------------------


def test_in_deck_card_kept_even_when_cheap(db, user):
    card = _card(db, name="Command Tower", type_line="Land", price="0.15")
    deck = _loc(db, user.id, "My Deck", type_="deck")
    _row(db, user.id, card, loc_id=deck.id)
    # A surplus copy of the SAME printing elsewhere, cheap:
    surplus = _row(db, user.id, card, loc_id=None)
    deck_ids = deck_member_card_ids(db, user.id)
    assert card.id in deck_ids
    assert should_keep_in_drawer(db, surplus, user_id=user.id, deck_card_ids=deck_ids) is True


def test_in_deck_is_finish_agnostic(db, user):
    # Running the NORMAL finish in a deck protects the FOIL printing too
    # (layer 3 keys on card_id, finish-agnostic).
    card = _card(db, name="Sol Ring", type_line="Artifact", price="0.50", price_foil="0.90")
    deck = _loc(db, user.id, "Deck", type_="deck")
    _row(db, user.id, card, finish="normal", loc_id=deck.id)
    foil_surplus = _row(db, user.id, card, finish="foil")
    deck_ids = deck_member_card_ids(db, user.id)
    assert should_keep_in_drawer(db, foil_surplus, user_id=user.id, deck_card_ids=deck_ids) is True


def test_card_in_nondeck_location_does_not_protect(db, user):
    # Owning a copy in a box is NOT the in-deck signal.
    card = _card(db, name="Random Common", price="0.10")
    box = _loc(db, user.id, "Box", type_="box")
    _row(db, user.id, card, loc_id=box.id)
    surplus = _row(db, user.id, card)
    assert card.id not in deck_member_card_ids(db, user.id)
    assert should_keep_in_drawer(db, surplus, user_id=user.id) is False


# -- Layer 4: value -------------------------------------------------------------


def test_value_above_threshold_kept(db, user):
    card = _card(db, name="Pricey", price="1.01")
    row = _row(db, user.id, card)
    assert should_keep_in_drawer(db, row, user_id=user.id, deck_card_ids=set()) is True


def test_value_at_threshold_not_kept(db, user):
    # STRICTLY greater — exactly $1.00 is bulk-eligible.
    card = _card(db, name="At Floor", price="1.00")
    row = _row(db, user.id, card)
    assert should_keep_in_drawer(db, row, user_id=user.id, deck_card_ids=set()) is False


def test_value_uses_finish_price(db, user):
    # Cheap normal, valuable foil — a foil copy is kept on its foil price.
    card = _card(db, name="Foil Spike", price="0.20", price_foil="3.00")
    normal = _row(db, user.id, card, finish="normal")
    foil = _row(db, user.id, card, finish="foil")
    assert should_keep_in_drawer(db, normal, user_id=user.id, deck_card_ids=set()) is False
    assert should_keep_in_drawer(db, foil, user_id=user.id, deck_card_ids=set()) is True


def test_threshold_is_a_parameter(db, user):
    card = _card(db, name="Fifty Cent", price="0.50")
    row = _row(db, user.id, card)
    # Default $1.00 -> bulk; a $0.25 floor -> kept.
    assert should_keep_in_drawer(db, row, user_id=user.id, deck_card_ids=set()) is False
    assert (
        should_keep_in_drawer(db, row, user_id=user.id, price_threshold=0.25, deck_card_ids=set())
        is True
    )


def test_default_threshold_constant():
    assert DRAWER_KEEP_PRICE_THRESHOLD == 1.0


# -- Otherwise / precedence -----------------------------------------------------


def test_cheap_nonbasic_not_in_deck_is_bulk_eligible(db, user):
    card = _card(db, name="Bulk Rare", type_line="Creature — Beast", price="0.10")
    row = _row(db, user.id, card)
    assert should_keep_in_drawer(db, row, user_id=user.id, deck_card_ids=set()) is False


def test_missing_card_is_conservatively_kept(db, user):
    # A row whose .card is unset cannot be classified -> keep (never bulk blind).
    row = InventoryRow(
        user_id=user.id, card_id=999999, finish="normal", quantity=1, is_pending=False
    )
    assert should_keep_in_drawer(db, row, user_id=user.id, deck_card_ids=set()) is True


def test_auto_resolves_deck_ids_when_not_passed(db, user):
    # The convenience path (deck_card_ids omitted) resolves the set itself.
    card = _card(db, name="Esper Sentinel", type_line="Artifact Creature", price="0.30")
    deck = _loc(db, user.id, "Deck", type_="deck")
    _row(db, user.id, card, loc_id=deck.id)
    surplus = _row(db, user.id, card)
    assert should_keep_in_drawer(db, surplus, user_id=user.id) is True


# -- deck_member_card_ids -------------------------------------------------------


def test_deck_member_card_ids_scopes_to_decks_and_user(db, user):
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.flush()

    in_deck = _card(db, name="Deck Card")
    in_box = _card(db, name="Box Card")
    other_deck_card = _card(db, name="Other User Deck Card")

    deck = _loc(db, user.id, "Deck", type_="deck")
    box = _loc(db, user.id, "Box", type_="box")
    other_deck = _loc(db, other.id, "Their Deck", type_="deck")

    _row(db, user.id, in_deck, loc_id=deck.id)
    _row(db, user.id, in_box, loc_id=box.id)
    _row(db, other.id, other_deck_card, loc_id=other_deck.id)

    ids = deck_member_card_ids(db, user.id)
    assert in_deck.id in ids
    assert in_box.id not in ids
    assert other_deck_card.id not in ids
