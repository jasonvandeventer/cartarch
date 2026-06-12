"""Wishlist add-by-search (v3.39.x).

The critical fix: a brand-new account owning ZERO cards must still be able to
add its first Wishlist card. The Wishlist page's add-by-search box posts an
**any-printing** add (by name) via the ``from_search`` branch of
``/watchlist/add`` — no owned cards required (the deck-builder autocomplete that
feeds it resolves unowned cards on the client side; the add path itself is
name-only). These tests pin the handler behavior; they do NOT hit Scryfall.
"""

from __future__ import annotations

from sqlalchemy import text

from app.models import WatchlistItem
from app.scryfall import autocomplete_cards_for_add

# Duplicate enforcement is a migration-created PARTIAL-unique index, not an ORM
# constraint, so Base.metadata.create_all (the conftest schema) lacks it. Recreate
# the v3.27.12 index so the dup branch is exercised exactly as in production.
_WATCHLIST_NAME_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_user_card_name "
    "ON watchlist(user_id, card_name) WHERE card_name IS NOT NULL"
)


def test_add_by_search_adds_any_printing_for_unowned_card(client, db, user):
    """A name-only add (the zero-owned case) creates an any-printing row."""
    r = client.post(
        "/watchlist/add",
        data={"card_name": "Black Lotus", "from_search": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/watchlist?wl=added")
    assert "Black%20Lotus" in r.headers["location"]

    rows = db.query(WatchlistItem).filter(WatchlistItem.user_id == user.id).all()
    assert len(rows) == 1
    # any-printing row: card_name set, card_id NULL (no ownership needed)
    assert rows[0].card_name == "Black Lotus"
    assert rows[0].card_id is None


def test_add_by_search_duplicate_is_skipped(client, db, user):
    """Re-adding the same card is a no-op with a ``wl=dup`` outcome, not an error."""
    db.execute(text(_WATCHLIST_NAME_UNIQUE))
    db.commit()
    client.post(
        "/watchlist/add",
        data={"card_name": "Sol Ring", "from_search": "1"},
        follow_redirects=False,
    )
    r2 = client.post(
        "/watchlist/add",
        data={"card_name": "Sol Ring", "from_search": "1"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert r2.headers["location"].startswith("/watchlist?wl=dup")

    rows = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user.id, WatchlistItem.card_name == "Sol Ring")
        .all()
    )
    assert len(rows) == 1


def test_add_by_search_empty_query_is_a_noop(client, db, user):
    """An empty/whitespace submit adds nothing and quietly returns to /watchlist."""
    r = client.post(
        "/watchlist/add",
        data={"card_name": "   ", "from_search": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/watchlist"  # plain no-op, no wl= outcome
    assert db.query(WatchlistItem).filter(WatchlistItem.user_id == user.id).count() == 0


def test_autocomplete_short_query_returns_empty_without_network():
    """Empty / sub-2-char queries short-circuit to [] (no Scryfall call) — the
    'empty query / no-result' guard the search box relies on."""
    assert autocomplete_cards_for_add("") == []
    assert autocomplete_cards_for_add(" ") == []
    assert autocomplete_cards_for_add("a") == []
