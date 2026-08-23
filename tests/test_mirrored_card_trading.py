"""A mirrored card is tradeable (2026-08-22).

#135 made showcase membership computed: a card can be in a Showcase because its
LOCATION is mirrored, with no ShowcaseItem of its own. The share page has always
shown those cards — it renders from `resolve_showcase_rows`, THE membership
answer — but everything on the trade side still read the `showcase_items` table
directly, so:

  * the trade picker listed only curated cards, and
  * "Propose trade for this" on a mirrored card emitted
    `?from_showcase_item=None` and answered 422.

Mirroring a box therefore made its cards visible and untradeable at the same
time, which is most of the point of putting them in a showcase. Reported the day
after the reporter mirrored a 1,416-card location.
"""

from __future__ import annotations

import itertools
import json

import pytest

from app import share_service
from app import trade_service as ts
from app.models import (
    Card,
    InventoryRow,
    Playgroup,
    PlaygroupMember,
    Share,
    Showcase,
    ShowcaseItem,
    ShowcaseLocationSource,
    StorageLocation,
    User,
)

_seq = itertools.count(1)


def _card(db, name):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Creature",
        image_url="https://img.example.invalid/x.jpg",
    )
    db.add(c)
    db.flush()
    return c


def _row(db, user_id, card, loc, qty=2):
    r = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        quantity=qty,
        finish="normal",
        is_pending=False,
        storage_location_id=loc.id,
    )
    db.add(r)
    db.flush()
    return r


@pytest.fixture
def world(db, user):
    """`user` is the VIEWER. The sharer curates one card and mirrors a box
    holding another, then shares the showcase with their common playgroup."""
    sharer = User(username=f"sharer-{next(_seq)}@x.com", password_hash="x", display_name="Sharer")
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
    binder = StorageLocation(user_id=sharer.id, name="Binder", type="binder", mode="managed")
    box = StorageLocation(user_id=sharer.id, name="Trade box", type="box", mode="manual")
    unshared = StorageLocation(user_id=sharer.id, name="Private", type="box", mode="manual")
    sc = Showcase(user_id=sharer.id, name="Trades")
    db.add_all([binder, box, unshared, sc])
    db.flush()

    curated_row = _row(db, sharer.id, _card(db, "Rhystic Study"), binder)
    mirrored_row = _row(db, sharer.id, _card(db, "Mystic Remora"), box)
    private_row = _row(db, sharer.id, _card(db, "Secret Card"), unshared)
    db.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=curated_row.id, quantity_offered=1))
    db.add(ShowcaseLocationSource(showcase_id=sc.id, storage_location_id=box.id))
    db.add(Share(user_id=sharer.id, showcase_id=sc.id, playgroup_id=pg.id))

    # The viewer needs something of their own to offer.
    my_loc = StorageLocation(user_id=user.id, name="Mine", type="binder", mode="managed")
    db.add(my_loc)
    db.flush()
    my_row = _row(db, user.id, _card(db, "Sol Ring"), my_loc)
    db.commit()
    return {
        "sharer": sharer,
        "pg": pg,
        "showcase": sc,
        "curated_row": curated_row,
        "mirrored_row": mirrored_row,
        "private_row": private_row,
        "my_row": my_row,
    }


def _names(items):
    return sorted(i["card"].name for i in items)


# --------------------------------------------------------------------------
# The picker
# --------------------------------------------------------------------------


def test_the_picker_offers_mirrored_cards_as_well_as_curated(db, user, world):
    opts = ts.get_construction_options(db, user.id, world["sharer"].id, world["pg"].id)
    assert _names(opts["recipient_share_items"]) == ["Mystic Remora", "Rhystic Study"]

    by_name = {i["card"].name: i for i in opts["recipient_share_items"]}
    # Each entry names itself, because the two id spaces would collide.
    assert by_name["Rhystic Study"]["pick_kind"] == "showcase_item_id"
    assert by_name["Mystic Remora"]["pick_kind"] == "inventory_row_id"
    assert by_name["Mystic Remora"]["showcase_item_id"] is None
    # ...and the picker shows exactly what the SHARE PAGE shows. That is the
    # invariant that broke: two surfaces, one membership question.
    shown = share_service.build_share_display_items(db, world["showcase"])
    assert _names(opts["recipient_share_items"]) == sorted(i["card"].name for i in shown)


def test_the_picker_still_excludes_what_is_not_shared(db, user, world):
    opts = ts.get_construction_options(db, user.id, world["sharer"].id, world["pg"].id)
    assert "Secret Card" not in _names(opts["recipient_share_items"])


# --------------------------------------------------------------------------
# Creating the trade
# --------------------------------------------------------------------------


def test_a_mirrored_card_can_be_requested_in_a_trade(db, user, world):
    trade = ts.create_trade(
        db,
        proposer_user_id=user.id,
        recipient_user_id=world["sharer"].id,
        playgroup_id=world["pg"].id,
        offered=[{"inventory_row_id": world["my_row"].id, "quantity": 1}],
        requested=[{"inventory_row_id": world["mirrored_row"].id, "quantity": 1}],
    )
    (item,) = ts._items_by_side(trade, "requested")
    assert item.card.name == "Mystic Remora"
    assert item.inventory_row_id == world["mirrored_row"].id
    # No ShowcaseItem to point at — a shape the column already allows (C1: the
    # link is navigation-only, and §10 cleanup nulls it anyway).
    assert item.showcase_item_id is None


def test_a_curated_card_keeps_its_provenance_link(db, user, world):
    """The client sends both ids; the server must prefer the ShowcaseItem, or
    every curated trade would quietly lose the link it has always carried."""
    si = db.query(ShowcaseItem).first()
    trade = ts.create_trade(
        db,
        proposer_user_id=user.id,
        recipient_user_id=world["sharer"].id,
        playgroup_id=world["pg"].id,
        offered=[{"inventory_row_id": world["my_row"].id, "quantity": 1}],
        requested=[
            {
                "showcase_item_id": si.id,
                "inventory_row_id": world["curated_row"].id,
                "quantity": 1,
            }
        ],
    )
    (item,) = ts._items_by_side(trade, "requested")
    assert item.showcase_item_id == si.id


def test_a_row_that_is_owned_but_NOT_shared_is_refused(db, user, world):
    """The row id is client-supplied, so membership is re-checked server-side
    through the same resolver — naming a card they own but do not share must not
    reach into it."""
    with pytest.raises(ValueError, match="not in the recipient's Showcase"):
        ts.create_trade(
            db,
            proposer_user_id=user.id,
            recipient_user_id=world["sharer"].id,
            playgroup_id=world["pg"].id,
            offered=[{"inventory_row_id": world["my_row"].id, "quantity": 1}],
            requested=[{"inventory_row_id": world["private_row"].id, "quantity": 1}],
        )


def test_a_stranger_row_id_is_refused(db, user, world):
    """Someone else's row entirely — not the recipient's at all."""
    with pytest.raises(ValueError, match="not in the recipient's Showcase"):
        ts.create_trade(
            db,
            proposer_user_id=user.id,
            recipient_user_id=world["sharer"].id,
            playgroup_id=world["pg"].id,
            offered=[{"inventory_row_id": world["my_row"].id, "quantity": 1}],
            requested=[{"inventory_row_id": world["my_row"].id, "quantity": 1}],
        )


# --------------------------------------------------------------------------
# The share page's propose link
# --------------------------------------------------------------------------


def test_the_share_page_links_a_mirrored_card_to_its_row_not_to_None(client, db, user, world):
    share = db.query(Share).first()
    page = client.get(f"/shares/{share.id}")
    assert page.status_code == 200
    assert "from_showcase_item=None" not in page.text, "the 422 link is back"
    assert f"from_showcase_row={world['mirrored_row'].id}" in page.text
    si = db.query(ShowcaseItem).first()
    assert f"from_showcase_item={si.id}" in page.text, "a curated card keeps its own link"


def test_proposing_from_a_mirrored_card_prefills_the_trade(client, db, user, world):
    resp = client.get(
        f"/trades/new?from_showcase_row={world['mirrored_row'].id}", follow_redirects=False
    )
    assert resp.status_code == 200
    assert "Mystic Remora" in resp.text
    # #184 — a prefilled pick arrives as a hydrated TRAY entry (its tile need not
    # be on the first page), and the tile itself names the row id it is picked by.
    assert f'data-pick-id="{world["mirrored_row"].id}"' in resp.text
    assert '"kind": "inventory_row_id"' in resp.text


def test_a_row_you_cannot_see_degrades_to_a_plain_page(client, db, user, world):
    """Same posture as the item version: no leak, no error — just an unprefilled
    construction page."""
    resp = client.get(
        f"/trades/new?from_showcase_row={world['private_row'].id}", follow_redirects=False
    )
    assert resp.status_code == 200
    assert "Secret Card" not in resp.text


def test_the_end_to_end_route_creates_the_trade(client, db, user, world):
    resp = client.post(
        "/trades",
        data={
            "recipient_user_id": world["sharer"].id,
            "playgroup_id": world["pg"].id,
            "offered_json": json.dumps([{"inventory_row_id": world["my_row"].id, "quantity": 1}]),
            "requested_json": json.dumps(
                [{"inventory_row_id": world["mirrored_row"].id, "quantity": 1}]
            ),
            "proposer_note": "",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:400]
    assert "/trades/" in resp.headers["location"]
