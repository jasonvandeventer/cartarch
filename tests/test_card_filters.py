"""Colour + type filter facets (SaintWacko, 2026-07-22).

ONE definition (app/card_filters.py) feeds every wishlist and trade surface;
the values are emitted server-side as data-colors / data-types and compared by
list-filter.js, so these are the tests for the SEMANTICS — the JS only compares.
"""

from __future__ import annotations

from app.card_filters import card_filter_tokens, color_filter_token, type_filter_token
from app.models import Card


def test_color_token_normalizes_to_sorted_wubrg_letters():
    assert color_filter_token("U B") == "BU"
    assert color_filter_token("bu") == "BU"
    # Colourless AND an unfetched NULL identity both read as "no colours", which
    # the UI's "Colourless" choice matches and every letter choice does not.
    assert color_filter_token("") == ""
    assert color_filter_token(None) == ""
    # Junk letters are dropped rather than passed through into the attribute.
    assert color_filter_token("WXZ") == "W"


def test_type_token_keeps_every_type_word_before_the_em_dash():
    # An Artifact Creature must match BOTH filters.
    assert type_filter_token("Artifact Creature — Golem") == "artifact creature"
    # Subtypes after the em dash never leak in (Forest is a subtype, not a type).
    assert type_filter_token("Land Creature — Forest Dryad") == "land creature"
    # Supertypes are dropped — the vocabulary is closed so the JS compares exactly.
    assert type_filter_token("Legendary Creature — Elf") == "creature"
    assert type_filter_token(None) == ""


def test_planeswalker_is_not_mistaken_for_a_plane_or_land():
    """Word-level matching, same care as inventory_service.is_oversized_card."""
    assert type_filter_token("Legendary Planeswalker — Teferi") == "planeswalker"
    assert "land" not in type_filter_token("Legendary Planeswalker — Teferi")


def test_card_filter_tokens_handles_a_missing_card():
    """A name-only wishlist watch has no Card; two empty tokens simply never
    match an active filter, rather than crashing the page."""
    assert card_filter_tokens(None) == ("", "")
    card = Card(
        scryfall_id="x",
        name="Atraxa",
        color_identity="W U B G",
        type_line="Legendary Creature — Phyrexian Angel Horror",
    )
    assert card_filter_tokens(card) == ("BGUW", "creature")


def test_wishlist_rows_carry_filter_tokens(db, user):
    """Both wishlist identity modes emit tokens — including the name-only watch,
    which resolves them from any printing of that name via the batched query
    list_watchlist already runs for the price floor."""
    from app.models import WatchlistItem
    from app.watchlist_service import build_public_wishlist_view, list_watchlist

    card = Card(
        scryfall_id="c1",
        name="Llanowar Elves",
        set_code="dom",
        collector_number="168",
        color_identity="G",
        type_line="Creature — Elf Druid",
    )
    db.add(card)
    db.commit()
    db.add(WatchlistItem(user_id=user.id, card_id=card.id))
    # A name watch for a card that HAS printings, and one that has none at all.
    db.add(WatchlistItem(user_id=user.id, card_name="Llanowar Elves"))
    db.add(WatchlistItem(user_id=user.id, card_name="Never Printed"))
    db.commit()

    by_mode = {}
    for row in list_watchlist(db, user.id):
        by_mode.setdefault(row["identity_mode"], []).append(row)
    assert by_mode["card"][0]["filter_colors"] == "G"
    assert by_mode["card"][0]["filter_types"] == "creature"
    name_rows = {r["display_name"]: r for r in by_mode["name"]}
    assert name_rows["Llanowar Elves"]["filter_colors"] == "G"
    assert name_rows["Never Printed"]["filter_colors"] == ""

    # The shared (names-only) projection carries them too — colour/type are
    # public card data, so filtering works for a viewer without exposing
    # anything private.
    view = build_public_wishlist_view(db, user.id)
    tokens = {c["name"]: (c["filter_colors"], c["filter_types"]) for c in view["cards"]}
    assert tokens["Llanowar Elves"] == ("G", "creature")
    for card_entry in view["cards"]:
        assert "note" not in card_entry and "target_price" not in card_entry
