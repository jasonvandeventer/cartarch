"""Deck detail: commander stays out of the card list, list view offers actions.

Two defects reported 2026-07-16, both on /decks/{id}:

  1. The commander vanished from its own panel and appeared among the deck
     cards after applying a sort. Root cause: `_build_deck_card_items` returns
     commander rows alongside deck cards, and only the FULL-PAGE route split
     them out — the three HTMX partial paths (search/sort/group apply,
     bump-qty/switch-printing re-render, add-card) rendered the raw list. The
     split now lives in `_split_commanders`, which every path calls.

  2. List view had no way to reach a card's actions (grid view has the Actions
     drawer). The list rows now carry the same action set via the kebab
     dropdown the Collection rows already use, rendered from the shared
     `deck_actions_body` macro.

Invoke via:  pytest tests/test_deck_list_view_actions.py
"""

from __future__ import annotations

import pytest

from app import (
    legacy_tables as _legacy_tables,  # noqa: F401 — binds raw bracket tables to Base.metadata
)
from app.models import Card, Deck, InventoryRow, StorageLocation


@pytest.fixture
def deck_with_commander(db, user):
    """A deck holding one commander + one ordinary card."""
    loc = StorageLocation(user_id=user.id, name="Test Deck", type="deck", mode="managed")
    db.add(loc)
    db.flush()
    deck = Deck(user_id=user.id, name="Test Deck", storage_location_id=loc.id, format="commander")
    db.add(deck)

    cards = {}
    for name, type_line in (
        ("Mizzix of the Izmagnus", "Legendary Creature — Goblin Wizard"),
        ("Sol Ring", "Artifact"),
    ):
        card = Card(
            scryfall_id=f"sf-{name.lower().replace(' ', '-')}",
            name=name,
            set_code="tst",
            set_name="Test Set",
            collector_number=str(len(cards) + 1),
            type_line=type_line,
            image_url="https://example.invalid/x.jpg",
        )
        db.add(card)
        db.flush()
        cards[name] = card

    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=cards["Mizzix of the Izmagnus"].id,
            quantity=1,
            finish="normal",
            storage_location_id=loc.id,
            is_pending=False,
            role="commander",
        )
    )
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=cards["Sol Ring"].id,
            quantity=1,
            finish="normal",
            storage_location_id=loc.id,
            is_pending=False,
        )
    )
    db.commit()
    return deck


def _card_list_html(html: str) -> str:
    """The #deck-card-list region only — the commander panel renders above it."""
    start = html.index('id="deck-card-list"')
    return html[start:]


def test_commander_absent_from_card_list_on_full_page(client, deck_with_commander):
    resp = client.get(f"/decks/{deck_with_commander.id}")
    assert resp.status_code == 200
    body = _card_list_html(resp.text)
    assert "Sol Ring" in body
    assert "Mizzix of the Izmagnus" not in body


@pytest.mark.parametrize("sort", ["name", "cmc", "value"])
def test_commander_absent_from_cards_partial(client, deck_with_commander, sort):
    """The reported bug: re-sorting via the partial leaked the commander in.

    Covers several sort keys — the partial is what the Apply button hits. The
    commander split is view-independent (it runs before render), and view is no
    longer a URL axis (#149), so this exercises it in one view.
    """
    resp = client.get(
        f"/decks/{deck_with_commander.id}/cards-partial",
        params={"sort": sort, "direction": "asc"},
    )
    assert resp.status_code == 200
    assert "Sol Ring" in resp.text
    assert "Mizzix of the Izmagnus" not in resp.text


def test_commander_absent_from_add_card_partial(client, deck_with_commander, db):
    """The add-card HTMX re-render path shares the same builder."""
    from app.routes.decks import _build_deck_card_items, _split_commanders

    deck = db.query(Deck).get(deck_with_commander.id)
    items, _, _ = _build_deck_card_items(db, deck, deck.user_id, "", "name", "asc")
    commanders, deck_cards = _split_commanders(items)

    assert [i["card"].name for i in commanders] == ["Mizzix of the Izmagnus"]
    assert [i["card"].name for i in deck_cards] == ["Sol Ring"]


def test_list_view_rows_expose_card_actions(client, deck_with_commander, db, user):
    """List view must reach the same actions grid view offers."""
    user.deck_view_mode = "list"  # view is a stored pref, not a URL param (#149)
    db.add(user)
    db.commit()
    resp = client.get(f"/decks/{deck_with_commander.id}")
    assert resp.status_code == 200
    body = resp.text
    assert "deck-list-view" in body, "expected the list-view rendering"
    assert "collection-row-kebab-summary" in body, "expected a row actions trigger"
    # The action set itself, not just the trigger.
    assert "/toggle-commander" in body
    assert "/decks/return" in body
    assert "/tags" in body


def test_add_card_partial_honors_list_view_preference(client, deck_with_commander, db, user):
    """Adding a card while in list view must not swap the user back to grid.

    The add-card HTMX branch used to assemble its own render context and omit
    view_mode/group_by, so `_deck_card_list.html` fell through to its grid
    default. It now delegates to the shared partial renderer.
    """
    user.deck_view_mode = "list"
    db.add(user)
    db.commit()

    card = db.query(Card).filter_by(name="Sol Ring").one()
    resp = client.post(
        f"/decks/{deck_with_commander.id}/add-card",
        data={"scryfall_id": card.scryfall_id, "finish": "normal", "quantity": 1},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "deck-list-view" in resp.text, "expected list-view markup, got the grid default"
    assert "inventory-grid" not in resp.text


def test_grid_view_actions_unchanged_after_macro_extraction(client, deck_with_commander):
    """The drawer moved into a shared macro — grid must still render it."""
    resp = client.get(f"/decks/{deck_with_commander.id}")  # pref defaults to grid
    assert resp.status_code == 200
    body = resp.text
    assert "card-actions-drawer" in body
    assert "/toggle-commander" in body
    assert "/decks/return" in body
    assert "Switch Printing" in body


def test_stale_view_param_does_not_deadlock_toggle(client, deck_with_commander, db, user):
    """#149: after toggling to Grid, a stale ?view=list left in the URL (pushed
    there by the old search form's hidden `view` input) must neither pin the
    render mode nor get written back into the stored pref — otherwise the toggle
    deadlocks. Targets the two fixed seams directly (the toggle route itself is
    unchanged); asserting the toggle POST's own persistence isn't possible in this
    harness, where get_current_user and get_db_session are separate sessions.
    """
    user.deck_view_mode = "grid"  # the mode the user just toggled to
    db.add(user)
    db.commit()

    # AC#1 — the deck GET is pref-authoritative: ?view=list is ignored -> grid.
    page = client.get(f"/decks/{deck_with_commander.id}?view=list")
    assert page.status_code == 200
    assert "card-actions-drawer" in page.text
    assert "deck-list-view" not in page.text

    # AC#3 — cards-partial no longer back-writes the URL view into the pref, so a
    # partial re-render carrying the stale ?view=list can't overwrite grid.
    part = client.get(
        f"/decks/{deck_with_commander.id}/cards-partial",
        params={"view": "list", "sort": "name", "direction": "asc"},
    )
    assert part.status_code == 200
    db.refresh(user)
    assert user.deck_view_mode == "grid", "cards-partial must not overwrite the view pref (#149)"
