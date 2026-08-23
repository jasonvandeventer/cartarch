"""The trade picker is searched and paged (#184).

The construction page used to render every card of the other party's showcase
AND every row of your own inventory. On the reported data — a 4,472-card share
plus 1,607 owned rows — that was 6,094 tiles and 11.04 MB of HTML, so the page
took over a minute to settle and every filter change rebuilt all of it.

Two things make paging safe here, and both are pinned below:

  * the pick carries its own price and name (`trade-picker.js`), so a card
    picked on page 1 still counts toward the balance from page 2, where its
    tile no longer exists — verified in Chromium as well;
  * the pane endpoint answers with the SAME partial the page renders, so a
    searched pane and a first render cannot drift.
"""

from __future__ import annotations

import itertools
import json
import re

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

_seq = itertools.count(1)


@pytest.fixture
def world(db, user):
    """A 120-card share and 60 owned rows — three pages and two, at 50."""
    sharer = User(username=f"s{next(_seq)}@x.com", password_hash="x", display_name="Sharer")
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
    theirs = StorageLocation(user_id=sharer.id, name="Binder", type="binder", mode="managed")
    mine = StorageLocation(user_id=user.id, name="Mine", type="binder", mode="managed")
    sc = Showcase(user_id=sharer.id, name="Trades")
    db.add_all([theirs, mine, sc])
    db.flush()

    def _card(name):
        c = Card(
            scryfall_id=f"sid-{next(_seq)}",
            name=name,
            set_code="tst",
            collector_number="1",
            type_line="Creature" if "Their" in name else "Artifact",
            image_url="https://img.example.invalid/x.jpg",
            price_usd="2.00",
        )
        db.add(c)
        db.flush()
        return c

    for i in range(120):
        row = InventoryRow(
            user_id=sharer.id,
            card_id=_card(f"Their Card {i:03d}").id,
            quantity=1,
            finish="normal",
            is_pending=False,
            storage_location_id=theirs.id,
        )
        db.add(row)
        db.flush()
        db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1))
    for i in range(60):
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=_card(f"My Card {i:03d}").id,
                quantity=1,
                finish="normal",
                is_pending=False,
                storage_location_id=mine.id,
            )
        )
    share = Share(user_id=sharer.id, showcase_id=sc.id, playgroup_id=pg.id)
    db.add(share)
    db.commit()
    return {"sharer": sharer, "pg": pg, "share": share}


def _tiles(html: str) -> list[str]:
    return re.findall(r'data-name="([^"]+)"', html)


def _base(world) -> str:
    return f"recipient_user_id={world['sharer'].id}&playgroup_id={world['pg'].id}"


def test_the_page_renders_one_page_per_side_not_the_whole_collection(client, db, user, world):
    page = client.get(f"/trades/new?{_base(world)}")
    assert page.status_code == 200
    names = _tiles(page.text)
    assert len([n for n in names if n.startswith("Their")]) == 50
    assert len([n for n in names if n.startswith("My")]) == 50
    # The cap is stated on both sides — a silent one reads as "this is all".
    assert page.text.count("Search to narrow it down.") == 2


def test_the_pane_endpoint_pages(client, db, user, world):
    first = client.get(f"/trades/picker/requested?{_base(world)}")
    second = client.get(f"/trades/picker/requested?{_base(world)}&page=2")
    assert "Their Card 000" in first.text and "Their Card 000" not in second.text
    assert "Their Card 050" in second.text
    assert len(_tiles(second.text)) == 50
    assert "Showing 51–100 of 120" in second.text


def test_the_pane_endpoint_searches_server_side(client, db, user, world):
    """The app's own query language, over the WHOLE set — not a filter over
    whatever happened to be rendered."""
    by_name = client.get(f"/trades/picker/requested?{_base(world)}&q=Their Card 099")
    assert _tiles(by_name.text) == ["Their Card 099"]

    # And the language, not just substrings: their side is all creatures, so a
    # type search matches everything and an artifact search matches nothing.
    typed = client.get(f"/trades/picker/requested?{_base(world)}&q=t:artifact")
    assert _tiles(typed.text) == []


def test_the_offered_pane_searches_your_own_inventory(client, db, user, world):
    resp = client.get(f"/trades/picker/offered?{_base(world)}&q=My Card 042")
    assert _tiles(resp.text) == ["My Card 042"]


def test_a_pane_never_leaks_a_showcase_you_cannot_see(client, db, user, world):
    """Authorisation is not re-invented in the endpoint: it asks the same option
    builder the page does, so a playgroup you are not in resolves to an empty
    list rather than to a 403 that would confirm the share exists."""
    outsider = User(username=f"out{next(_seq)}@x.com", password_hash="x")
    db.add(outsider)
    db.commit()
    from app import main
    from app.dependencies import get_current_user

    main.app.dependency_overrides[get_current_user] = lambda: outsider
    try:
        resp = client.get(f"/trades/picker/requested?{_base(world)}")
        assert resp.status_code == 200
        assert _tiles(resp.text) == []
    finally:
        main.app.dependency_overrides[get_current_user] = lambda: user


def test_the_literal_picker_path_wins_over_the_trade_id_route(client, db, user, world):
    """`/trades/picker/...` and `/trades/{trade_id}` overlap; registration order
    is what keeps "picker" from being parsed as a trade id."""
    resp = client.get(f"/trades/picker/requested?{_base(world)}")
    assert resp.status_code == 200, "the picker path is being swallowed by /trades/{id}"


def test_an_unknown_side_is_redirected_not_500(client, db, user, world):
    resp = client.get(f"/trades/picker/sideways?{_base(world)}", follow_redirects=False)
    assert resp.status_code == 303


def test_a_prefilled_pick_arrives_hydrated_for_the_tray(client, db, user, world):
    """The tray must be able to draw a pick whose tile is not on the page, so
    the blob carries the name, price and cap rather than a bare id."""
    si = db.query(ShowcaseItem).order_by(ShowcaseItem.id.desc()).first()
    page = client.get(f"/trades/new?from_showcase_item={si.id}")
    blob = json.loads(re.search(r'id="pick-restore">(.*?)</script>', page.text, re.S).group(1))
    (entry,) = blob["requested"]
    assert entry["name"].startswith("Their Card")
    assert entry["price"] == 2.0
    assert entry["available"] >= 1
