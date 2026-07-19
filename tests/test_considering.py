"""#148 — the deck "Considering" holding area (a dedicated per-deck location).

Considering rows live in a SEPARATE StorageLocation (deck.considering_location_id),
so every "cards in this deck" query auto-excludes them. Placeholders are brew-only;
the #140 brew-placeholder exclusion must still hide brew considering placeholders
from the collection. These tests pin those invariants + the lifecycle.
"""

from __future__ import annotations

from app import legacy_tables as _legacy_tables  # noqa: F401
from app.deck_service import (
    _get_loose_source_rows,
    add_card_to_considering,
    considering_count,
    delete_deck,
    demote_to_considering,
    list_considering_rows,
    promote_from_considering,
    remove_from_considering,
    resolved_deck_rows,
)
from app.inventory_service import brew_placeholder_exclusion, is_brew_placeholder_row
from app.models import Card, Deck, InventoryRow, StorageLocation

_n = 0


def _card(db, name, type_line="Creature — Elf"):
    global _n
    _n += 1
    c = Card(
        scryfall_id=f"sf-{_n}",
        name=name,
        set_code="tst",
        set_name="Test",
        collector_number=str(_n),
        type_line=type_line,
    )
    db.add(c)
    db.flush()
    return c


def _deck(db, user, name, is_brew=False):
    loc = StorageLocation(user_id=user.id, name=name, type="deck", mode="managed")
    db.add(loc)
    db.flush()
    d = Deck(user_id=user.id, name=name, storage_location_id=loc.id, is_brew=is_brew)
    db.add(d)
    db.flush()
    return d


def _owned_loose(db, user, card, qty=1):
    r = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=qty,
        finish="normal",
        is_proxy=False,
        is_pending=True,
        storage_location_id=None,
    )
    db.add(r)
    db.flush()
    return r


def _main_row(db, user, deck, card, is_proxy=False, role=None):
    r = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=1,
        finish="normal",
        is_proxy=is_proxy,
        is_pending=False,
        storage_location_id=deck.storage_location_id,
        role=role,
    )
    db.add(r)
    db.flush()
    return r


# --------------------------------------------------------------------------- #
# Add: brew-only placeholders; owned pulls a loose copy
# --------------------------------------------------------------------------- #


def test_add_unowned_to_brew_makes_placeholder(db, user):
    deck = _deck(db, user, "Brew", is_brew=True)
    card = _card(db, "Unowned Bomb")
    assert add_card_to_considering(db, user.id, deck.id, card.id) is True
    rows = list_considering_rows(db, deck, user.id)
    assert len(rows) == 1 and rows[0].is_proxy is True
    assert considering_count(db, deck, user.id) == 1


def test_add_unowned_to_non_brew_is_refused(db, user):
    deck = _deck(db, user, "Real Deck", is_brew=False)
    card = _card(db, "Unowned")
    assert add_card_to_considering(db, user.id, deck.id, card.id) == "not_owned"
    assert list_considering_rows(db, deck, user.id) == []


def test_add_owned_pulls_a_loose_copy(db, user):
    deck = _deck(db, user, "Real Deck", is_brew=False)
    card = _card(db, "Owned Card")
    _owned_loose(db, user, card, qty=1)
    assert add_card_to_considering(db, user.id, deck.id, card.id) is True
    rows = list_considering_rows(db, deck, user.id)
    assert len(rows) == 1 and rows[0].is_proxy is False
    # ownership conserved: exactly one real copy exists and it's in considering,
    # with no loose (pending, unlocated) copy left. (Assert the end state, not a
    # row id — the consume frees an id SQLite may reuse for the placed row.)
    loose_left = (
        db.query(InventoryRow)
        .filter(
            InventoryRow.user_id == user.id,
            InventoryRow.card_id == card.id,
            InventoryRow.storage_location_id.is_(None),
        )
        .count()
    )
    total_real = (
        db.query(InventoryRow)
        .filter(
            InventoryRow.user_id == user.id,
            InventoryRow.card_id == card.id,
            InventoryRow.is_proxy.is_(False),
        )
        .count()
    )
    assert loose_left == 0 and total_real == 1


# --------------------------------------------------------------------------- #
# #140 — a brew considering placeholder stays out of the collection
# --------------------------------------------------------------------------- #


def test_brew_considering_placeholder_excluded_from_collection(db, user):
    deck = _deck(db, user, "Brew", is_brew=True)
    card = _card(db, "Placeholder Card")
    add_card_to_considering(db, user.id, deck.id, card.id)
    row = list_considering_rows(db, deck, user.id)[0]

    assert is_brew_placeholder_row(db, row) is True
    visible_ids = {
        r.id
        for r in db.query(InventoryRow)
        .filter(InventoryRow.user_id == user.id, brew_placeholder_exclusion(user.id))
        .all()
    }
    assert row.id not in visible_ids  # hidden from the collection


def test_considering_excluded_from_deck_rows_and_counts(db, user):
    deck = _deck(db, user, "Brew", is_brew=True)
    main_card = _card(db, "In Deck")
    _main_row(db, user, deck, main_card)
    cons_card = _card(db, "Just Considering")
    add_card_to_considering(db, user.id, deck.id, cons_card.id)
    db.commit()

    resolved_names = {r.card.name for r in resolved_deck_rows(db, deck, user.id)}
    assert "In Deck" in resolved_names
    assert "Just Considering" not in resolved_names  # never counts toward the deck

    # …and excluded from the public share view + the Decks-listing card count
    # (both filter on deck.storage_location_id, which considering is NOT in).
    from app.deck_service import build_public_deck_view, list_decks

    pub = build_public_deck_view(db, deck)
    assert pub["total_cards"] == 1
    listed = next(d for d in list_decks(db, user.id) if d.id == deck.id)
    assert listed.card_count == 1


# --------------------------------------------------------------------------- #
# Promote / demote (merge-aware moves between the deck's two locations)
# --------------------------------------------------------------------------- #


def test_promote_moves_into_deck(db, user):
    deck = _deck(db, user, "Brew", is_brew=True)
    card = _card(db, "Promote Me")
    add_card_to_considering(db, user.id, deck.id, card.id)
    row = list_considering_rows(db, deck, user.id)[0]

    assert promote_from_considering(db, user.id, row.id) is True
    assert list_considering_rows(db, deck, user.id) == []
    db.refresh(row)
    assert row.storage_location_id == deck.storage_location_id


def test_demote_moves_to_considering_and_clears_role(db, user):
    deck = _deck(db, user, "Real Deck")
    card = _card(db, "Demote Me")
    row = _main_row(db, user, deck, card, role="commander")

    assert demote_to_considering(db, user.id, row.id) is True
    db.refresh(row)
    assert row.storage_location_id == deck.considering_location_id
    assert row.role is None  # considering is not the deck
    assert len(list_considering_rows(db, deck, user.id)) == 1


# --------------------------------------------------------------------------- #
# Remove + delete disband rules (placeholder discarded, real returned)
# --------------------------------------------------------------------------- #


def test_remove_placeholder_discards(db, user):
    deck = _deck(db, user, "Brew", is_brew=True)
    card = _card(db, "Discard Me")
    add_card_to_considering(db, user.id, deck.id, card.id)
    row = list_considering_rows(db, deck, user.id)[0]

    assert remove_from_considering(db, user.id, row.id) is True
    assert db.get(InventoryRow, row.id) is None  # placeholder gone, not returned


def test_remove_real_returns_to_pending(db, user):
    deck = _deck(db, user, "Real Deck")
    card = _card(db, "Return Me")
    _owned_loose(db, user, card)
    add_card_to_considering(db, user.id, deck.id, card.id)
    row = list_considering_rows(db, deck, user.id)[0]

    assert remove_from_considering(db, user.id, row.id) is True
    db.refresh(row)
    assert row.is_pending is True and row.storage_location_id is None


def test_delete_deck_disbands_considering(db, user):
    deck = _deck(db, user, "Brew", is_brew=True)
    placeholder_card = _card(db, "Placeholder")
    owned_card = _card(db, "Owned")
    _owned_loose(db, user, owned_card)
    add_card_to_considering(db, user.id, deck.id, placeholder_card.id)  # proxy
    add_card_to_considering(db, user.id, deck.id, owned_card.id)  # real
    rows = list_considering_rows(db, deck, user.id)
    proxy_row = next(r for r in rows if r.is_proxy)
    real_row = next(r for r in rows if not r.is_proxy)
    cons_loc_id = deck.considering_location_id
    db.commit()

    assert delete_deck(db, deck.id, user.id) is True
    assert db.get(InventoryRow, proxy_row.id) is None  # placeholder discarded
    returned = db.get(InventoryRow, real_row.id)
    assert returned is not None and returned.is_pending and returned.storage_location_id is None
    assert db.get(StorageLocation, cons_loc_id) is None  # location dropped


# --------------------------------------------------------------------------- #
# Ripple: a considering copy is NOT loose/consumable
# --------------------------------------------------------------------------- #


def test_considering_row_is_not_a_loose_source(db, user):
    deck = _deck(db, user, "Real Deck")
    card = _card(db, "Staged Owned")
    _owned_loose(db, user, card)
    add_card_to_considering(db, user.id, deck.id, card.id)  # moves it into considering
    db.commit()
    # switch-printing / reconciliation must not be able to pull it back out
    assert _get_loose_source_rows(db, user.id, card.id, "normal") == []


# --------------------------------------------------------------------------- #
# HTTP surface (routes + template render together)
# --------------------------------------------------------------------------- #


def test_deck_page_renders_considering_section(client, db, user):
    deck = _deck(db, user, "Brew", is_brew=True)
    db.commit()
    resp = client.get(f"/decks/{deck.id}")
    assert resp.status_code == 200
    assert 'id="considering-section"' in resp.text
    assert "+ Considering" in resp.text  # the add-to-considering button


def test_considering_add_then_remove_over_http(client, db, user):
    deck = _deck(db, user, "Brew", is_brew=True)
    card = _card(db, "Http Placeholder")
    db.commit()

    # Add (HTMX) — the returned section partial shows the placeholder.
    resp = client.post(
        f"/decks/{deck.id}/considering/add",
        data={"scryfall_id": card.scryfall_id, "finish": "normal", "quantity": 1},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Http Placeholder" in resp.text and "Placeholder" in resp.text

    db.expire_all()
    deck = db.get(Deck, deck.id)
    rows = list_considering_rows(db, deck, user.id)
    assert len(rows) == 1 and rows[0].is_proxy is True
    rid = rows[0].id

    # Remove (HTMX) — placeholder discarded.
    resp2 = client.post(
        f"/decks/{deck.id}/considering/{rid}/remove", headers={"HX-Request": "true"}
    )
    assert resp2.status_code == 200
    db.expire_all()
    assert list_considering_rows(db, db.get(Deck, deck.id), user.id) == []


def test_considering_add_unowned_to_non_brew_over_http(client, db, user):
    """The route refuses an unowned add on a non-brew deck (nothing is created)."""
    deck = _deck(db, user, "Real Deck", is_brew=False)
    card = _card(db, "Http Unowned")
    db.commit()
    resp = client.post(
        f"/decks/{deck.id}/considering/add",
        data={"scryfall_id": card.scryfall_id, "finish": "normal", "quantity": 1},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200  # friendly partial, not a 500
    db.expire_all()
    assert list_considering_rows(db, db.get(Deck, deck.id), user.id) == []
