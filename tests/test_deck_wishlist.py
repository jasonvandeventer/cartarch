"""#144 — "add to wishlist" on the deck buy-list.

The watchlist has no quantity, so adds are idempotent skip-duplicates (never a
second row, never an increment). Identity is printing-agnostic (card_name).
"""

from __future__ import annotations

import itertools

import app.legacy_tables  # noqa: F401 — registers deck_bracket_* so the deck page renders
from app import deck_service
from app.models import Card, InventoryRow, WatchlistItem
from app.watchlist_service import add_names_to_watchlist

_seq = itertools.count(1)


def _card(db, name):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Artifact",
        oracle_text="x",
        color_identity="",
    )
    db.add(c)
    db.flush()
    return c


def _watched_names(db, user_id):
    return {w.card_name for w in db.query(WatchlistItem).filter(WatchlistItem.user_id == user_id)}


# --------------------------------------------------------------------------- #
# Service — idempotent skip-duplicates
# --------------------------------------------------------------------------- #


def test_add_names_skips_duplicates(db, user):
    # a repeat within the same call is counted once
    r1 = add_names_to_watchlist(db, user.id, ["Sol Ring", "Cultivate", "Sol Ring", "  "])
    assert r1 == {"added": 2, "skipped": 1}  # blank dropped before counting

    # already-watched names skip on a later call
    r2 = add_names_to_watchlist(db, user.id, ["Sol Ring", "Rampant Growth"])
    assert r2 == {"added": 1, "skipped": 1}

    assert _watched_names(db, user.id) == {"Sol Ring", "Cultivate", "Rampant Growth"}


def test_add_names_empty_is_noop(db, user):
    assert add_names_to_watchlist(db, user.id, []) == {"added": 0, "skipped": 0}
    assert _watched_names(db, user.id) == set()


# --------------------------------------------------------------------------- #
# Route — per-card + section add, banner, idempotency
# --------------------------------------------------------------------------- #


def _brew_with_buylist(db, user):
    brew = deck_service.create_deck(db, user.id, "Brew", is_brew=True)
    loc = brew.storage_location_id
    # a proxy row = a "to buy" card; a real row = owned
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=_card(db, "Mana Vault").id,
            quantity=1,
            finish="normal",
            is_proxy=True,
            storage_location_id=loc,
            is_pending=False,
        )
    )
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=_card(db, "Arcane Signet").id,
            quantity=1,
            finish="normal",
            is_proxy=False,
            storage_location_id=loc,
            is_pending=False,
        )
    )
    db.commit()
    return brew


def test_wishlist_route_adds_and_is_idempotent(client, db, user):
    brew = _brew_with_buylist(db, user)

    # section-style add (two names)
    r = client.post(
        f"/decks/{brew.id}/wishlist",
        data={"card_name": ["Mana Vault", "Reliquary Tower"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "wl_added=2" in r.headers["location"] and "wl_skipped=0" in r.headers["location"]
    assert _watched_names(db, user.id) == {"Mana Vault", "Reliquary Tower"}

    # per-card add of an already-watched name → skipped
    r2 = client.post(
        f"/decks/{brew.id}/wishlist", data={"card_name": "Mana Vault"}, follow_redirects=False
    )
    assert "wl_added=0" in r2.headers["location"] and "wl_skipped=1" in r2.headers["location"]
    assert _watched_names(db, user.id) == {"Mana Vault", "Reliquary Tower"}


def test_deck_page_renders_wishlist_control(client, db, user):
    brew = _brew_with_buylist(db, user)
    body = client.get(f"/decks/{brew.id}").text
    # the To-buy section shows the proxy card + a wishlist button
    assert "Mana Vault" in body
    assert f"/decks/{brew.id}/wishlist" in body
    assert "+ wishlist" in body
