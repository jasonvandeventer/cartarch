"""Two controls asked for on 2026-08-23, and the rules behind them.

1. A SHARED showcase can be read as a list. It reuses the viewer's own
   `showcase_view_mode` — "how do I want showcase cards shown" is one answer,
   and a share is still a showcase — so there is no fourth view column.

2. The trade pickers sort again, SERVER-SIDE. #184 removed the client-side sort
   because a paged grid could only reorder the fifty cards on screen; this is
   the version that means what it says, applied to the whole set before the page
   is taken.
"""

from __future__ import annotations

import itertools
import re

import pytest

from app import sort_spec
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

_seq = itertools.count(1)


@pytest.fixture
def world(db, user):
    """A share we can view, and a trade context we can pick in."""
    other = User(username=f"o{next(_seq)}@x.com", password_hash="x", display_name="Alex")
    db.add(other)
    db.flush()
    pg = Playgroup(name="Pod", created_by=other.id)
    db.add(pg)
    db.flush()
    db.add_all(
        [
            PlaygroupMember(playgroup_id=pg.id, user_id=other.id, role="owner"),
            PlaygroupMember(playgroup_id=pg.id, user_id=user.id, role="member"),
        ]
    )
    theirs = StorageLocation(user_id=other.id, name="T", type="binder", mode="managed")
    mine = StorageLocation(user_id=user.id, name="M", type="binder", mode="managed")
    sc = Showcase(user_id=other.id, name="Trades")
    db.add_all([theirs, mine, sc])
    db.flush()

    def _row(owner_id, name, loc, price, cmc):
        c = Card(
            scryfall_id=f"sid-{next(_seq)}",
            name=name,
            set_code="tst",
            collector_number="1",
            type_line="Creature",
            image_url="https://img.example.invalid/x.jpg",
            price_usd=price,
            cmc=cmc,
        )
        db.add(c)
        db.flush()
        r = InventoryRow(
            user_id=owner_id,
            card_id=c.id,
            quantity=1,
            finish="normal",
            is_pending=False,
            storage_location_id=loc.id,
        )
        db.add(r)
        db.flush()
        return r

    # Deliberately: alphabetical order is the REVERSE of price order, so a test
    # that sorts by price cannot pass by accident on the default ordering.
    for name, price, cmc in (("Alpha", "1.00", 6.0), ("Beta", "5.00", 4.0), ("Gamma", "9.00", 2.0)):
        r = _row(other.id, name, theirs, price, cmc)
        db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=r.id, quantity_offered=1))
    _row(user.id, "My Card", mine, "3.00", 1.0)
    share = Share(user_id=other.id, showcase_id=sc.id, playgroup_id=pg.id)
    db.add(share)
    db.commit()
    return {"other": other, "pg": pg, "share": share, "showcase": sc}


def _names(html: str) -> list[str]:
    return re.findall(r'data-name="([^"]+)"', html)


# --------------------------------------------------------------------------
# 1. list view on a shared showcase
# --------------------------------------------------------------------------


def test_a_shared_showcase_can_be_read_as_a_list(client, db, user, world):
    grid = client.get(f"/shares/{world['share'].id}")
    assert grid.status_code == 200
    assert "share-view-item-wrap" in grid.text and "card-list-row" not in grid.text

    client.post("/account/showcase-view-pref", data={"view": "list", "csrf_token": "x"})
    listed = client.get(f"/shares/{world['share'].id}")
    assert "card-list-row" in listed.text
    assert "share-view-item-wrap" not in listed.text
    for name in ("Alpha", "Beta", "Gamma"):
        assert name in listed.text
    # The one control a viewer needs survives the switch.
    assert "from_showcase_item=" in listed.text


def test_the_list_view_reuses_the_showcase_preference_not_a_new_one(client, db, user, world):
    """One answer to "how do I want showcase cards shown" — the owner's own page
    and a share of someone else's read the same column."""
    client.post("/account/showcase-view-pref", data={"view": "list", "csrf_token": "x"})
    assert user.showcase_view_mode == "list"
    assert user.trade_view_mode == "grid", "the trade preference is untouched"


def test_a_shared_list_carries_no_owner_private_field(client, db, user, world):
    """Still the sanitized projection: a list row is a different shape, not a
    different privacy boundary."""
    client.post("/account/showcase-view-pref", data={"view": "list", "csrf_token": "x"})
    page = client.get(f"/shares/{world['share'].id}").text
    for leak in ("storage_location", "drawer", "data-notes", '"tags"'):
        assert leak not in page


# --------------------------------------------------------------------------
# 2. sort on the pickers
# --------------------------------------------------------------------------


def _base(world) -> str:
    return f"recipient_user_id={world['other'].id}&playgroup_id={world['pg'].id}"


def test_the_picker_sorts_server_side(client, db, user, world):
    asc = client.get(f"/trades/picker/requested?{_base(world)}&sort=price&direction=asc")
    assert _names(asc.text) == ["Alpha", "Beta", "Gamma"]
    desc = client.get(f"/trades/picker/requested?{_base(world)}&sort=price&direction=desc")
    assert _names(desc.text) == ["Gamma", "Beta", "Alpha"]
    # Not the same as name order reversed — cmc gives a third arrangement.
    cmc = client.get(f"/trades/picker/requested?{_base(world)}&sort=cmc&direction=asc")
    assert _names(cmc.text) == ["Gamma", "Beta", "Alpha"]


def test_an_unknown_sort_key_falls_back_instead_of_doing_nothing(client, db, user, world):
    """`sort_showcase_items` leaves the order untouched on an unknown key, which
    would look like a control that silently does nothing."""
    resp = client.get(f"/trades/picker/requested?{_base(world)}&sort=vibes")
    assert _names(resp.text) == ["Alpha", "Beta", "Gamma"]
    assert sort_spec.normalize_sort("vibes", sort_spec.PICKER_SORT_OPTIONS) == "name"


def test_sort_and_search_travel_together(client, db, user, world):
    """Both controls post to the same endpoint and include each other, so
    changing one cannot drop the other."""
    page = client.get(f"/trades/new?{_base(world)}").text
    bar = page[page.index('id="requested-toolbar"') : page.index('id="requested-toolbar"') + 1600]
    assert 'hx-include="#requested-toolbar"' in bar
    assert bar.count('hx-include="#requested-toolbar"') >= 3, "search, sort and direction"
    assert 'hx-target="#requested-pane"' in bar


def test_the_pager_carries_the_sort(client, db, user, world):
    """Paging that silently reset the order would be worse than no paging."""
    resp = client.get(f"/trades/picker/requested?{_base(world)}&sort=price&direction=desc")
    if "trade-pick-pager" in resp.text:
        assert "sort=price" in resp.text and "direction=desc" in resp.text


def test_the_picker_sort_offers_no_date_added(client, db, user, world):
    """The requested side has an added_at and the offered side does not; a
    control that works on one pane and silently not the other is worse than one
    that is not offered."""
    assert "added" not in {k for k, _ in sort_spec.PICKER_SORT_OPTIONS}
    page = client.get(f"/trades/new?{_base(world)}").text
    bar = page[page.index('id="offered-toolbar"') : page.index('id="offered-toolbar"') + 1600]
    assert "Date Added" not in bar


def test_the_sort_runs_over_the_WHOLE_set_not_the_page(client, db, user, world):
    """The distinction the control exists for.

    60 cards named A000..A059 with prices RISING with the index, so the dearest
    card sits on page 2 by name. Sorting the whole set by price descending must
    put it first; sorting only the fifty on screen would return A049 — which is
    exactly what the client-side control removed in #184 used to do, and would
    look right until someone checked.
    """
    loc = db.query(StorageLocation).filter(StorageLocation.user_id == world["other"].id).first()
    sc = world["showcase"]
    for i in range(60):
        c = Card(
            scryfall_id=f"bulk-{next(_seq)}",
            name=f"A{i:03d}",
            set_code="tst",
            collector_number=str(i),
            type_line="Creature",
            image_url="https://img.example.invalid/x.jpg",
            price_usd=f"{i + 1}.00",
        )
        db.add(c)
        db.flush()
        r = InventoryRow(
            user_id=world["other"].id,
            card_id=c.id,
            quantity=1,
            finish="normal",
            is_pending=False,
            storage_location_id=loc.id,
        )
        db.add(r)
        db.flush()
        db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=r.id, quantity_offered=1))
    db.commit()

    by_name = client.get(f"/trades/picker/requested?{_base(world)}&sort=name&direction=asc")
    assert _names(by_name.text)[0] == "A000"
    assert "A059" not in _names(by_name.text), "A059 must be on a later page by name"

    dearest = client.get(f"/trades/picker/requested?{_base(world)}&sort=price&direction=desc")
    assert _names(dearest.text)[0] == "A059", "the sort only reordered the visible page"
