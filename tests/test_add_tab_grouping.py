"""#119 — Add tab: name grouping over scryfall_cards + printing auto-resolution."""

import itertools

from app.deck_service import (
    create_deck,
    grouped_card_search,
    resolve_add_printing,
)
from app.legacy_tables import scryfall_cards
from app.models import Card, InventoryRow, StorageLocation, VariantGroup

_seq = itertools.count(1)


def _sc(db, name, price=None, price_foil=None, layout="normal", set_type="expansion"):
    sid = f"sc-{next(_seq)}"
    db.execute(
        scryfall_cards.insert().values(
            scryfall_id=sid,
            name=name,
            set_code="tst",
            set_name="Test",
            collector_number=str(next(_seq)),
            image_url=f"http://x/{sid}.png",
            price_usd=price,
            price_usd_foil=price_foil,
            layout=layout,
            set_type=set_type,
        )
    )
    return sid


def _card(db, name, scryfall_id=None):
    c = Card(
        scryfall_id=scryfall_id or f"card-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Artifact",
        set_type="expansion",
        layout="normal",
    )
    db.add(c)
    db.flush()
    return c


def _loose_row(db, user_id, card, finish="normal", qty=1, loc_id=None, proxy=False):
    r = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        finish=finish,
        quantity=qty,
        is_pending=loc_id is None,
        storage_location_id=loc_id,
        is_proxy=proxy,
    )
    db.add(r)
    db.flush()
    return r


# --- grouped_card_search ------------------------------------------------------


def test_grouped_search_one_row_per_name_cheapest_first(db):
    _sc(db, "Lightning Bolt", price="3.00")
    cheap = _sc(db, "Lightning Bolt", price="0.50")
    _sc(db, "Lightning Bolt", price=None)
    _sc(db, "Lightning Strike", price="0.10")
    _sc(db, "Lightning Elemental", price="0.01", layout="token", set_type="token")
    db.commit()

    rows = grouped_card_search(db, "lightning")
    assert [r["name"] for r in rows] == ["Lightning Bolt", "Lightning Strike"]
    bolt = rows[0]
    assert bolt["scryfall_id"] == cheap  # cheapest printing represents the group
    assert bolt["from_price"] == 0.5
    assert bolt["printings"] == 3  # null-priced printings still counted


def test_grouped_search_finish_switches_price_column(db):
    _sc(db, "Sol Ring", price="1.00", price_foil="9.00")
    foil_cheap = _sc(db, "Sol Ring", price="2.00", price_foil="4.00")
    db.commit()

    rows = grouped_card_search(db, "sol ring", finish="foil")
    assert rows[0]["scryfall_id"] == foil_cheap
    assert rows[0]["from_price"] == 4.0


def test_grouped_search_short_query_is_empty(db):
    assert grouped_card_search(db, "a") == []


# --- resolve_add_printing -----------------------------------------------------


def test_rule3_cheapest_for_selected_finish(db, user):
    deck = create_deck(db, user.id, "Solo")
    _sc(db, "Brainstorm", price="2.00")
    cheap = _sc(db, "Brainstorm", price="0.25")
    _sc(db, "Brainstorm", price=None)
    db.commit()

    res = resolve_add_printing(db, user_id=user.id, deck=deck, name="Brainstorm", finish="normal")
    assert res["rule"] == "cheapest"
    assert res["scryfall_id"] == cheap
    assert res["price"] == 0.25
    assert res["finish_differs"] is False


def test_rule3_unknown_name_returns_none(db, user):
    deck = create_deck(db, user.id, "Solo")
    assert (
        resolve_add_printing(db, user_id=user.id, deck=deck, name="Nope", finish="normal") is None
    )


def test_rule2_owned_loose_copy_beats_cheapest_and_ignores_finish(db, user):
    deck = create_deck(db, user.id, "Solo")
    _sc(db, "Counterspell", price="0.10")
    owned_card = _card(db, "Counterspell")
    _loose_row(db, user.id, owned_card, finish="foil")  # pending, physical
    db.commit()

    res = resolve_add_printing(db, user_id=user.id, deck=deck, name="Counterspell", finish="normal")
    assert res["rule"] == "owned"
    assert res["scryfall_id"] == owned_card.scryfall_id
    assert res["finish"] == "foil"  # owned foil beats importing a nonfoil
    assert res["finish_differs"] is True


def test_rule2_skips_proxies_and_deck_resident_copies(db, user):
    deck = create_deck(db, user.id, "Solo")
    other_deck = create_deck(db, user.id, "Other")
    _sc(db, "Ponder", price="0.30")
    cheap = _sc(db, "Ponder", price="0.15")
    owned_card = _card(db, "Ponder")
    _loose_row(db, user.id, owned_card, proxy=True)  # proxy: not physical
    _loose_row(db, user.id, owned_card, loc_id=other_deck.storage_location_id)  # deck-resident
    db.commit()

    res = resolve_add_printing(db, user_id=user.id, deck=deck, name="Ponder", finish="normal")
    assert res["rule"] == "cheapest"
    assert res["scryfall_id"] == cheap


def test_rule1_variant_group_printing_identity(db, user):
    deck_a = create_deck(db, user.id, "Wyll A")
    deck_b = create_deck(db, user.id, "Wyll B")
    group = VariantGroup(user_id=user.id, name="Wyll")
    db.add(group)
    db.flush()
    deck_a.variant_group_id = group.id
    deck_b.variant_group_id = group.id

    _sc(db, "Wyll, Blade of Frontiers", price="0.05")
    sibling_card = _card(db, "Wyll, Blade of Frontiers")
    sibling_row = _loose_row(db, user.id, sibling_card, qty=1, loc_id=deck_b.storage_location_id)
    db.commit()

    res = resolve_add_printing(
        db, user_id=user.id, deck=deck_a, name="Wyll, Blade of Frontiers", finish="normal"
    )
    assert res["rule"] == "variant"
    assert res["scryfall_id"] == sibling_card.scryfall_id  # identity from the sibling
    # identity only — the sibling's physical copy stays deck-resident
    db.refresh(sibling_row)
    assert sibling_row.storage_location_id == deck_b.storage_location_id


# --- route: name-level add ----------------------------------------------------


def test_add_card_by_name_moves_owned_copy_and_reports(client, db, user):
    deck = create_deck(db, user.id, "Route Deck")
    _sc(db, "Opt", price="0.20")
    owned_card = _card(db, "Opt")
    box = StorageLocation(user_id=user.id, name="Drawer 3", type="box", mode="managed")
    db.add(box)
    db.flush()
    _loose_row(db, user.id, owned_card, loc_id=box.id)
    db.commit()

    resp = client.post(
        f"/decks/{deck.id}/add-card",
        data={"card_name": "Opt", "finish": "normal", "quantity": "1", "csrf_token": "x"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    toast = resp.headers.get("x-add-resolution", "")
    assert "moved from Drawer 3" in toast

    moved = db.query(InventoryRow).filter_by(user_id=user.id, card_id=owned_card.id).one()
    assert moved.storage_location_id == deck.storage_location_id


def test_add_card_by_unknown_name_404s(client, db, user):
    deck = create_deck(db, user.id, "Route Deck 2")
    db.commit()
    resp = client.post(
        f"/decks/{deck.id}/add-card",
        data={"card_name": "Not A Card", "finish": "normal", "quantity": "1", "csrf_token": "x"},
    )
    assert resp.status_code == 404


def test_grouped_autocomplete_endpoint(client, db):
    _sc(db, "Opt", price="0.20")
    db.commit()
    resp = client.get("/decks/api/card-autocomplete-grouped?q=opt&finish=normal")
    assert resp.status_code == 200
    data = resp.json()
    assert data and data[0]["name"] == "Opt"
    assert data[0]["from_price"] == 0.2
