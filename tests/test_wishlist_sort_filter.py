"""Wishlist sort + filter (SaintWacko, Discord 2026-07-22).

/watchlist is fully materialized and unpaginated, so both run in Python over
the dicts list_watchlist already builds — no query change. Sort goes through
the SHARED sort_spec so direction, nulls-last, and the stable tiebreaker mean
the same thing here as on every other sorted list; the by-name search stays
client-side (list-filter.js), matching the Showcases/Shares indexes.
"""

from __future__ import annotations

import itertools

from app import sort_spec

_seq = itertools.count(1)


def _item(name, *, price=None, target=None, placed=0, pending=0, added=0):
    return {
        "id": next(_seq),
        "display_name": name,
        "current_min_price": price,
        "target_price": target,
        "placed_count": placed,
        "pending_count": pending,
        "added_at": added,
        "target_met": target is not None and price is not None and price <= target,
    }


def _names(items):
    return [it["display_name"] for it in items]


def test_sorts_by_name_price_and_owned():
    items = [
        _item("Sol Ring", price=1.5, placed=2),
        _item("Mana Crypt", price=120.0, placed=0),
        _item("Arcane Signet", price=0.5, placed=1, pending=3),
    ]
    assert _names(sort_spec.sort_wishlist_items(list(items), "name", "asc")) == [
        "Arcane Signet",
        "Mana Crypt",
        "Sol Ring",
    ]
    assert _names(sort_spec.sort_wishlist_items(list(items), "price", "desc"))[0] == "Mana Crypt"
    # Owned counts placed + pending, so the 1-placed/3-pending row leads.
    assert _names(sort_spec.sort_wishlist_items(list(items), "owned", "desc"))[0] == "Arcane Signet"


def test_unpriced_rows_sort_last_in_both_directions():
    """The nulls-last rule from sort_spec: a card with no cached price must
    never lead a descending price sort just because its value reads as zero."""
    items = [_item("No Price"), _item("Cheap", price=1.0), _item("Pricey", price=50.0)]
    for direction in ("asc", "desc"):
        assert _names(sort_spec.sort_wishlist_items(list(items), "price", direction))[-1] == (
            "No Price"
        )


def test_unknown_sort_key_leaves_order_untouched():
    items = [_item("B"), _item("A")]
    assert _names(sort_spec.sort_wishlist_items(list(items), "bogus", "asc")) == ["B", "A"]


def test_filters_partition_the_list(client, db, user):
    """The route's three filters, exercised through the real page."""
    from app.models import WatchlistItem

    db.add(WatchlistItem(user_id=user.id, card_name="Unowned Card"))
    db.add(WatchlistItem(user_id=user.id, card_name="Another Unowned"))
    db.commit()

    body = client.get("/watchlist?show=unowned").text
    assert "Unowned Card" in body and "Another Unowned" in body

    # Nothing is owned or target-met, so those views come back empty — and the
    # page still renders its controls rather than collapsing to the empty state.
    owned = client.get("/watchlist?show=owned").text
    assert "Unowned Card" not in owned
    assert "Show everything" in owned

    # An unknown filter value falls through to everything, never an empty page.
    assert "Unowned Card" in client.get("/watchlist?show=nonsense").text


def test_sort_params_reach_the_page(client, db, user):
    from app.models import WatchlistItem

    db.add(WatchlistItem(user_id=user.id, card_name="Zed Card"))
    db.add(WatchlistItem(user_id=user.id, card_name="Alpha Card"))
    db.commit()

    body = client.get("/watchlist?sort=name&direction=asc").text
    assert body.index("Alpha Card") < body.index("Zed Card")
    body_desc = client.get("/watchlist?sort=name&direction=desc").text
    assert body_desc.index("Zed Card") < body_desc.index("Alpha Card")

    # The controls themselves render: the shared sort partial, the Show filter,
    # and the client-side name box wired to the table.
    assert 'name="sort"' in body and 'name="direction"' in body
    assert 'name="show"' in body
    assert 'data-list-filter-target="#watchlist-table tbody tr"' in body
    assert "list-filter.js" in body
    # Colour + type facets ride the same target, so the three criteria compose
    # in one engine instead of clobbering each other's `hidden`.
    assert 'data-list-facet="colors"' in body and 'data-list-facet="types"' in body
