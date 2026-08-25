"""Deck row quantity is SET, not stepped, and the hero numbers ride along.

Reported 2026-08-25 as "raising or lowering land counts is painful": the
basic-land control was a +/- pair, so going from 4 to 12 Forests meant eight
POSTs, each one a full card-list re-render (and, in grid view, one that closed
the actions drawer the buttons live in). The control is now a quantity box
posting an absolute number to /decks/{id}/rows/{row}/set-qty.

The second half is the honesty half: the swap replaces #deck-card-list only,
so before this the deck's "Total Cards" hero number kept reporting the old
count until the page was reloaded — the very reload the request asked to
avoid. The route now piggybacks an out-of-band swap of #deck-hero-numbers.

Invoke via:  pytest tests/test_deck_row_set_qty.py
"""

from __future__ import annotations

import pytest

from app import (
    legacy_tables as _legacy_tables,  # noqa: F401 — binds raw bracket tables to Base.metadata
)
from app.models import Card, Deck, InventoryRow, StorageLocation, User


@pytest.fixture
def deck_with_forest(db, user):
    """A deck holding 4 Forests (basic) and one Sol Ring (not basic)."""
    loc = StorageLocation(user_id=user.id, name="Lands Deck", type="deck", mode="managed")
    db.add(loc)
    db.flush()
    deck = Deck(user_id=user.id, name="Lands Deck", storage_location_id=loc.id, format="commander")
    db.add(deck)

    rows = {}
    for name, type_line, qty in (
        ("Forest", "Basic Land — Forest", 4),
        ("Sol Ring", "Artifact", 1),
    ):
        card = Card(
            scryfall_id=f"sf-{name.lower()}",
            name=name,
            set_code="tst",
            set_name="Test Set",
            collector_number=str(len(rows) + 1),
            type_line=type_line,
            image_url="https://example.invalid/x.jpg",
        )
        db.add(card)
        db.flush()
        row = InventoryRow(
            user_id=user.id,
            card_id=card.id,
            quantity=qty,
            finish="normal",
            storage_location_id=loc.id,
            is_pending=False,
        )
        db.add(row)
        db.flush()
        rows[name] = row
    db.commit()
    return deck, rows


def _post_qty(client, deck, row, qty, htmx=True):
    return client.post(
        f"/decks/{deck.id}/rows/{row.id}/set-qty",
        data={"quantity": qty},
        headers={"HX-Request": "true"} if htmx else {},
        follow_redirects=False,
    )


def test_setting_a_quantity_writes_it_in_one_post(client, db, deck_with_forest):
    deck, rows = deck_with_forest
    resp = _post_qty(client, deck, rows["Forest"], 12)
    assert resp.status_code == 200
    db.refresh(rows["Forest"])
    assert rows["Forest"].quantity == 12


def test_zero_removes_the_row(client, db, deck_with_forest):
    deck, rows = deck_with_forest
    row_id = rows["Forest"].id
    resp = _post_qty(client, deck, rows["Forest"], 0)
    assert resp.status_code == 200
    assert db.query(InventoryRow).filter(InventoryRow.id == row_id).first() is None


def test_an_absurd_quantity_is_clamped_not_rejected(client, db, deck_with_forest):
    deck, rows = deck_with_forest
    resp = _post_qty(client, deck, rows["Forest"], 500)
    assert resp.status_code == 200
    db.refresh(rows["Forest"])
    assert rows["Forest"].quantity == 99


def test_another_users_row_is_a_404(client, db, deck_with_forest):
    """The row must belong to a deck this user owns — ownership is the guard."""
    deck, rows = deck_with_forest
    other = User(username="someone@example.invalid", password_hash="x")
    db.add(other)
    db.flush()
    other_loc = StorageLocation(user_id=other.id, name="Theirs", type="deck", mode="managed")
    db.add(other_loc)
    db.flush()
    other_deck = Deck(
        user_id=other.id, name="Theirs", storage_location_id=other_loc.id, format="commander"
    )
    db.add(other_deck)
    db.commit()
    resp = _post_qty(client, other_deck, rows["Forest"], 7)
    assert resp.status_code == 404


def test_the_htmx_response_carries_the_new_hero_total(client, deck_with_forest):
    """The list swap alone would leave "Total Cards" on the old number.

    4 Forests + 1 Sol Ring = 5 before; setting Forests to 12 makes it 13.
    """
    deck, rows = deck_with_forest
    page = client.get(f"/decks/{deck.id}")
    assert "5 Total Cards" in page.text

    resp = _post_qty(client, deck, rows["Forest"], 12)
    body = resp.text
    assert 'id="deck-hero-numbers"' in body
    assert 'hx-swap-oob="true"' in body
    assert "13 Total Cards" in body
    # The flag is set inside the shared helper, so switch-printing and
    # add-card (its other two callers, both mutations) carry it too.


def test_the_search_partial_sends_no_out_of_band_hero(client, deck_with_forest):
    """A search changes no total, so it must not swap the hero numbers."""
    deck, _rows = deck_with_forest
    resp = client.get(f"/decks/{deck.id}/cards-partial", params={"search": "Forest"})
    assert resp.status_code == 200
    assert "hx-swap-oob" not in resp.text


@pytest.mark.parametrize("view_mode", ["grid", "list"])
def test_both_views_render_the_quantity_box_for_a_basic_only(
    client, db, user, deck_with_forest, view_mode
):
    """The control is offered on basics (no inventory accounting) — and it has
    to exist in BOTH views, which render from two different templates."""
    deck, rows = deck_with_forest
    user.deck_view_mode = view_mode
    db.commit()

    body = client.get(f"/decks/{deck.id}").text
    assert f"/decks/{deck.id}/rows/{rows['Forest'].id}/set-qty" in body
    assert f"/decks/{deck.id}/rows/{rows['Sol Ring'].id}/set-qty" not in body
    assert 'name="quantity"' in body
