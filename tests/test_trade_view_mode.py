"""Grid/List for the cards on a trade (2026-08-23).

The third surface to get one, and the third separate column: a trade holds a
handful of cards where a showcase holds thousands, so wanting art on one and a
dense list on the other is ordinary rather than contradictory. What they share
is the WRITER — `set_view_pref` — because three hand-rolled "validate and
commit" routes is how one of them ends up accepting a mode the others reject.

The list markup shares the `.card-list-*` family the showcase list introduced.
A trade row and a showcase row are the same shape and differ only in what sits
at the end, so a second CSS block would be two things to keep in step.
"""

from __future__ import annotations

import itertools

import pytest

from app import trade_service as ts
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
def trade(db, user):
    """`user` proposes: one real card offered, one foil proxy, one requested."""
    other = User(username=f"o{next(_seq)}@x.com", password_hash="x", display_name="Other")
    db.add(other)
    db.flush()
    pg = Playgroup(name="Pod", created_by=user.id)
    db.add(pg)
    db.flush()
    db.add_all(
        [
            PlaygroupMember(playgroup_id=pg.id, user_id=user.id, role="owner"),
            PlaygroupMember(playgroup_id=pg.id, user_id=other.id, role="member"),
        ]
    )
    mine = StorageLocation(user_id=user.id, name="Mine", type="binder", mode="managed")
    theirs = StorageLocation(user_id=other.id, name="Theirs", type="binder", mode="managed")
    sc = Showcase(user_id=other.id, name="Trades")
    db.add_all([mine, theirs, sc])
    db.flush()

    def _row(owner_id, name, loc, *, proxy=False, finish="normal", price="10.00"):
        c = Card(
            scryfall_id=f"sid-{next(_seq)}",
            name=name,
            set_code="tst",
            collector_number="7",
            type_line="Creature",
            image_url="https://img.example.invalid/x.jpg",
            price_usd=price,
            price_usd_foil=price,
        )
        db.add(c)
        db.flush()
        r = InventoryRow(
            user_id=owner_id,
            card_id=c.id,
            quantity=1,
            finish=finish,
            is_proxy=proxy,
            is_pending=False,
            storage_location_id=loc.id,
        )
        db.add(r)
        db.flush()
        return r

    real = _row(user.id, "Sol Ring", mine)
    proxy = _row(user.id, "Mana Crypt", mine, proxy=True)
    theirs_row = _row(other.id, "Rhystic Study", theirs, finish="foil")
    si = ShowcaseItem(showcase_id=sc.id, inventory_row_id=theirs_row.id, quantity_offered=1)
    db.add(si)
    db.add(Share(user_id=other.id, showcase_id=sc.id, playgroup_id=pg.id))
    db.commit()

    return ts.create_trade(
        db,
        proposer_user_id=user.id,
        recipient_user_id=other.id,
        playgroup_id=pg.id,
        offered=[
            {"inventory_row_id": real.id, "quantity": 1},
            {"inventory_row_id": proxy.id, "quantity": 1},
        ],
        requested=[{"showcase_item_id": si.id, "quantity": 1}],
    )


def test_the_toggle_switches_both_sides_and_the_choice_sticks(client, db, user, trade):
    grid = client.get(f"/trades/{trade.id}")
    assert grid.status_code == 200
    assert "inventory-grid trade-items-grid" in grid.text
    assert "card-list-row" not in grid.text

    switch = client.post(
        "/trades/account/trade-view-pref",
        data={"view": "list", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert switch.status_code == 303
    assert user.trade_view_mode == "list"

    listed = client.get(f"/trades/{trade.id}")
    assert "inventory-grid trade-items-grid" not in listed.text
    # BOTH sides switch — a toggle that changed one half would be worse than none.
    assert listed.text.count('<ul class="card-list"') == 2
    assert listed.text.count("card-list-row") >= 3
    for name in ("Sol Ring", "Mana Crypt", "Rhystic Study"):
        assert name in listed.text


def test_the_list_keeps_the_facts_that_change_what_a_trade_is_worth(client, db, user, trade):
    client.post("/trades/account/trade-view-pref", data={"view": "list", "csrf_token": "x"})
    page = client.get(f"/trades/{trade.id}").text
    row = page[page.index("Mana Crypt") - 400 : page.index("Mana Crypt") + 400]
    # A proxy priced as the real card would misprice the whole trade
    # (ADR proxy-valuation) — the grid says so and the list has to as well.
    assert "PROXY" in row and "$0.00" in row
    foil = page[page.index("Rhystic Study") - 200 : page.index("Rhystic Study") + 400]
    assert "Foil" in foil, "the finish changes the price, so it stays visible"


def test_the_view_is_a_preference_not_a_url_axis(client, db, user, trade):
    page = client.get(f"/trades/{trade.id}?view=list")
    assert "card-list-row" not in page.text, "a URL param must not steer the view"


def test_an_unknown_mode_is_ignored(client, db, user, trade):
    client.post("/trades/account/trade-view-pref", data={"view": "spreadsheet", "csrf_token": "x"})
    assert user.trade_view_mode == "grid"


def test_the_three_view_prefs_are_independent(client, db, user, trade):
    """Separate columns, on purpose: a 1,400-card showcase and a three-card trade
    want different things, and one shared column would make each surface silently
    change the other."""
    client.post("/trades/account/trade-view-pref", data={"view": "list", "csrf_token": "x"})
    assert user.trade_view_mode == "list"
    assert user.showcase_view_mode == "grid"
    assert user.deck_view_mode == "grid"
