"""The location card grid is paged; the bulk lists deliberately are NOT.

Reported 2026-08-14: opening a 1,409-row location ("Unused Cards") to reach the
quick-add modal left the page uninteractable. Measured: 9.5 MB of HTML and
1,412 lazy <img> elements, DOMContentLoaded 1.34 min. The grid renders a full
``inventory_card`` per row with its actions drawer inline (~6 KB each) and was
unpaginated, unlike the Collection grid it otherwise mirrors.

The scope half is the part that is easy to get wrong: paging "Select all" down
to the visible 50 would silently narrow a bulk move/delete, and nothing on
screen would say so.
"""

import re

from app.models import Card, InventoryRow, StorageLocation
from app.routes.collections import LOCATION_PAGE_SIZE

ROWS = 120  # > 2 pages at 50


def _seed(db, user, n=ROWS):
    loc = StorageLocation(user_id=user.id, name="Unused Cards", type="other", mode="managed")
    # A second movable location, or the Bulk Move panel is not rendered at all.
    other = StorageLocation(user_id=user.id, name="Box B", type="box", mode="manual")
    db.add_all([loc, other])
    db.flush()
    for i in range(n):
        c = Card(
            scryfall_id=f"loc-pg-{i}",
            name=f"Card Name {i:04d}",
            set_code="tst",
            collector_number=str(i),
            type_line="Creature — Human Wizard",
            image_url=f"http://x/{i}.png",
        )
        db.add(c)
        db.flush()
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=c.id,
                finish="normal",
                quantity=1,
                is_pending=False,
                storage_location_id=loc.id,
            )
        )
    db.commit()
    return loc


def _grid_names(html):
    """Card names inside the grid section only (not the bulk checkbox lists)."""
    i = html.find('id="location-card-list"')
    assert i > 0, "grid container missing"
    j = html.find("Page ", i)  # the pager panel follows the grid
    return set(re.findall(r"Card Name \d{4}", html[i : j if j > i else len(html)]))


def test_the_grid_renders_one_page_not_the_whole_location(db, client, user):
    loc = _seed(db, user)
    html = client.get(f"/locations/{loc.id}").text
    assert len(_grid_names(html)) == LOCATION_PAGE_SIZE


def test_page_two_shows_different_cards(db, client, user):
    loc = _seed(db, user)
    p1 = _grid_names(client.get(f"/locations/{loc.id}?page=1").text)
    p2 = _grid_names(client.get(f"/locations/{loc.id}?page=2").text)
    assert len(p2) == LOCATION_PAGE_SIZE
    assert not (p1 & p2)


def test_an_out_of_range_page_clamps_instead_of_rendering_empty(db, client, user):
    loc = _seed(db, user)
    html = client.get(f"/locations/{loc.id}?page=999").text
    # Clamped to the last page (120 rows / 50 = 3 pages, last holds 20).
    assert len(_grid_names(html)) == ROWS - 2 * LOCATION_PAGE_SIZE
    assert "No inventory rows assigned" not in html


def test_bulk_lists_still_cover_every_row_in_the_location(db, client, user):
    """The scope guard. Paging these would silently shrink Select-all."""
    loc = _seed(db, user)
    html = client.get(f"/locations/{loc.id}").text
    assert len(re.findall(r'class="bulk-cb"', html)) == ROWS
    assert len(re.findall(r'class="bulk-delete-cb"', html)) == ROWS


def test_hero_totals_describe_the_location_not_the_page(db, client, user):
    loc = _seed(db, user)
    html = client.get(f"/locations/{loc.id}").text
    stats = html[html.find('id="location-stats"') :][:600]
    assert f">{ROWS}<" in stats  # Rows stat counts all 120, not the visible 50


def test_the_pager_renders_and_marks_the_current_page(db, client, user):
    loc = _seed(db, user)
    html = client.get(f"/locations/{loc.id}?page=2").text
    assert "Page 2 of 3" in html
    assert f"/locations/{loc.id}?page=3" in html
    # The emphasis is the current-page indicator; on every number it marks nothing.
    assert len(re.findall(r"font-weight: bold; opacity: 0\.8;", html)) == 1


def test_no_pager_when_everything_fits_on_one_page(db, client, user):
    loc = _seed(db, user, n=10)
    html = client.get(f"/locations/{loc.id}").text
    assert "Page 1 of" not in html


def test_the_quick_add_htmx_refresh_is_paged_too(db, client, user):
    """Otherwise every add through the modal re-ships the whole grid."""
    loc = _seed(db, user)
    target = Card(
        scryfall_id="loc-pg-new",
        name="Freshly Added Card",
        set_code="tst",
        collector_number="999",
        type_line="Artifact",
    )
    db.add(target)
    db.commit()

    resp = client.post(
        f"/locations/{loc.id}/add-card",
        data={
            "scryfall_id": "loc-pg-new",
            "finish": "normal",
            "quantity": "1",
            "csrf_token": "x",
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert len(set(re.findall(r"Card Name \d{4}", resp.text))) == LOCATION_PAGE_SIZE
    # ...while the out-of-band Rows stat still reports the whole location.
    assert f">{ROWS + 1}<" in resp.text
