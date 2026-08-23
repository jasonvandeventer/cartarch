"""The share view is a PAGE (#184).

Reported 2026-08-22 against a 1,426-card share: *"it also takes a long time to
render when changing the filters."* The page rendered every card of the
showcase, so a search or a sort rebuilt ~1,400 tiles and re-fetched an image for
each — the same structural problem as the trade picker, one page over, but far
easier here because a read-only view has no in-progress selection to preserve.

Paged with the SAME helper and page size as the location grid (v4.13.27), and
the same rule as that page: the hero figures describe the whole share, only the
grid is a page, and the cap is STATED rather than silent.
"""

from __future__ import annotations

import itertools

import pytest

from app.models import (
    Card,
    InventoryRow,
    Playgroup,
    PlaygroupMember,
    Share,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    User,
)
from app.routes.collections import LOCATION_PAGE_SIZE

_seq = itertools.count(1)


@pytest.fixture
def big_share(db, user):
    """A share of 120 cards — enough for three pages at 50."""
    sharer = User(username=f"s-{next(_seq)}@x.com", password_hash="x", display_name="Sharer")
    db.add(sharer)
    db.flush()
    pg = Playgroup(name="Pod", created_by=sharer.id)
    db.add(pg)
    db.flush()
    db.add_all(
        [
            PlaygroupMember(playgroup_id=pg.id, user_id=sharer.id, role="owner"),
            PlaygroupMember(playgroup_id=pg.id, user_id=user.id, role="member"),
        ]
    )
    loc = StorageLocation(user_id=sharer.id, name="Binder", type="binder", mode="managed")
    sc = Showcase(user_id=sharer.id, name="Trades")
    db.add_all([loc, sc])
    db.flush()
    for i in range(120):
        c = Card(
            scryfall_id=f"sid-{next(_seq)}",
            # Zero-padded so "Card 000" sorts first and the page boundary is
            # checkable by name rather than by id.
            name=f"Card {i:03d}",
            set_code="tst",
            collector_number=str(i),
            type_line="Creature" if i % 2 == 0 else "Instant",
            image_url="https://img.example.invalid/x.jpg",
            price_usd="1.00",
        )
        db.add(c)
        db.flush()
        row = InventoryRow(
            user_id=sharer.id,
            card_id=c.id,
            quantity=1,
            finish="normal",
            is_pending=False,
            storage_location_id=loc.id,
        )
        db.add(row)
        db.flush()
        db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1))
    share = Share(user_id=sharer.id, showcase_id=pg.id and sc.id, playgroup_id=pg.id)
    db.add(share)
    db.commit()
    return share


def _tile_count(html: str) -> int:
    return html.count('class="share-view-item-wrap"')


def test_the_grid_is_one_page_not_the_whole_showcase(client, db, user, big_share):
    page = client.get(f"/shares/{big_share.id}?sort=name&direction=asc")
    assert page.status_code == 200
    assert _tile_count(page.text) == LOCATION_PAGE_SIZE, "the whole showcase is still rendering"
    # The cap is STATED — a silent one reads as "this is everything".
    assert "Showing 1–50 of 120" in page.text
    assert "Card 000" in page.text and "Card 049" in page.text
    assert "Card 050" not in page.text


def test_later_pages_carry_the_search_and_sort(client, db, user, big_share):
    page = client.get(f"/shares/{big_share.id}?page=2&sort=name&direction=asc")
    assert "Card 050" in page.text and "Card 000" not in page.text
    assert "Showing 51–100 of 120" in page.text
    # Paging must not silently reset the view — that is the whole reason the
    # pager rebuilds the query string.
    assert "sort=name" in page.text and "direction=asc" in page.text


def test_a_search_narrows_before_the_page_is_taken(client, db, user, big_share):
    """The filter runs server-side, BEFORE the projection and before the slice,
    so the count reflects the search rather than the showcase."""
    page = client.get(f"/shares/{big_share.id}?search=t:instant&sort=name&direction=asc")
    assert "of 60" in page.text, "the count should describe the matching set"
    assert _tile_count(page.text) == LOCATION_PAGE_SIZE


def test_the_headline_total_still_describes_the_WHOLE_share(client, db, user, big_share):
    """Only the grid is a page. A total that quietly became per-page would be a
    wrong number rather than a smaller one — the location page's rule."""
    page = client.get(f"/shares/{big_share.id}")
    assert "120 cards" in page.text
    assert "$120.00" in page.text


def test_an_out_of_range_page_lands_on_the_last_one(client, db, user, big_share):
    page = client.get(f"/shares/{big_share.id}?page=99&sort=name&direction=asc")
    assert page.status_code == 200
    assert "Showing 101–120 of 120" in page.text


def test_a_junk_page_param_does_not_500(client, db, user, big_share):
    page = client.get(f"/shares/{big_share.id}?page=twelve")
    assert page.status_code == 200
    assert "Showing 1–50 of 120" in page.text


def test_a_small_share_is_unchanged_and_shows_no_pager(client, db, user):
    """The common case must not grow a pager or a truncation notice."""
    sharer = User(username=f"s-{next(_seq)}@x.com", password_hash="x", display_name="S")
    db.add(sharer)
    db.flush()
    pg = Playgroup(name="Pod2", created_by=sharer.id)
    db.add(pg)
    db.flush()
    db.add_all(
        [
            PlaygroupMember(playgroup_id=pg.id, user_id=sharer.id, role="owner"),
            PlaygroupMember(playgroup_id=pg.id, user_id=user.id, role="member"),
        ]
    )
    loc = StorageLocation(user_id=sharer.id, name="B", type="binder", mode="managed")
    sc = Showcase(user_id=sharer.id, name="Small")
    db.add_all([loc, sc])
    db.flush()
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name="Sol Ring",
        set_code="tst",
        collector_number="1",
        type_line="Artifact",
        image_url="https://img.example.invalid/x.jpg",
    )
    db.add(c)
    db.flush()
    row = InventoryRow(
        user_id=sharer.id,
        card_id=c.id,
        quantity=1,
        finish="normal",
        is_pending=False,
        storage_location_id=loc.id,
    )
    db.add(row)
    db.flush()
    db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1))
    share = Share(user_id=sharer.id, showcase_id=sc.id, playgroup_id=pg.id)
    db.add(share)
    db.commit()

    page = client.get(f"/shares/{share.id}")
    assert _tile_count(page.text) == 1
    assert "1 card" in page.text
    assert "Showing" not in page.text
    assert "Page 1 of" not in page.text
