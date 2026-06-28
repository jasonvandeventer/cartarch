"""Per-deck Total Value on the Decks listing (list_decks.total_value).

Value = sum(effective_price * quantity) over the deck's OWN rows, excluding
proxies and inbound variant-group shares (so a card is never double-counted
across sibling builds).
"""

from app import deck_service
from tests.test_deck_card_shares import (
    _card,
    _deck,
    _fresh_session,
    _group_with_two_decks,
    _place,
    _user,
)


def _priced_card(s, name, price, finish="normal"):
    c = _card(s, name=name)
    if finish == "foil":
        c.price_usd_foil = price
    elif finish == "etched":
        c.price_usd_etched = price
    else:
        c.price_usd = price
    s.flush()
    return c


def test_total_value_sums_price_times_quantity():
    s = _fresh_session()
    u = _user(s)
    deck = _deck(s, u.id, "Mono")
    _place(s, u.id, _priced_card(s, "A", "2.50"), deck.storage_location_id, qty=3)
    _place(
        s,
        u.id,
        _priced_card(s, "B", "10.00", finish="foil"),
        deck.storage_location_id,
        qty=1,
        finish="foil",
    )

    d = {x.name: x for x in deck_service.list_decks(s, u.id)}["Mono"]
    assert d.total_value == 17.50  # 2.50*3 + 10.00*1


def test_total_value_excludes_proxies():
    s = _fresh_session()
    u = _user(s)
    deck = _deck(s, u.id, "Brew")
    _place(s, u.id, _priced_card(s, "Real", "5.00"), deck.storage_location_id, qty=1)
    _place(
        s,
        u.id,
        _priced_card(s, "Proxy", "99.00"),
        deck.storage_location_id,
        qty=2,
        is_proxy=True,
    )

    d = {x.name: x for x in deck_service.list_decks(s, u.id)}["Brew"]
    assert d.total_value == 5.00


def test_total_value_excludes_inbound_shares():
    s = _fresh_session()
    u = _user(s)
    _g, a, b = _group_with_two_decks(s, u)
    card = _priced_card(s, "Shared", "20.00")
    row = _place(s, u.id, card, a.storage_location_id, qty=1)
    deck_service.share_card_to_deck(s, u.id, inventory_row_id=row.id, target_deck_id=b.id)

    decks = {x.name: x for x in deck_service.list_decks(s, u.id)}
    # value counts on the owning deck only — never double-counted on the sibling.
    assert decks["Build A"].total_value == 20.00
    assert decks["Build B"].total_value == 0.0


def test_total_value_uncached_price_contributes_zero():
    # A card with no price_usd* must contribute $0 and never crash the listing.
    s = _fresh_session()
    u = _user(s)
    deck = _deck(s, u.id, "Mixed")
    _place(s, u.id, _priced_card(s, "Priced", "4.00"), deck.storage_location_id, qty=2)
    _place(s, u.id, _card(s, name="NoPrice"), deck.storage_location_id, qty=5)

    d = {x.name: x for x in deck_service.list_decks(s, u.id)}["Mixed"]
    assert d.total_value == 8.00  # 4.00*2 + 0*5


def test_total_value_zero_for_empty_deck():
    s = _fresh_session()
    u = _user(s)
    _deck(s, u.id, "Empty")
    d = {x.name: x for x in deck_service.list_decks(s, u.id)}["Empty"]
    assert d.total_value == 0.0
