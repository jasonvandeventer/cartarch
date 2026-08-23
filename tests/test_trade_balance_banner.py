"""The trade balance banner: viewer-relative, pinned, and on a proposed trade.

Three things came out of one round of feedback on 2026-08-23:

1. A PROPOSED trade had no banner at all — only per-side totals — so the one
   number you want (which way does this lean, and by how much) had to be worked
   out by eye.
2. The banner labelled SIDES, not viewers. "Offered" always means the proposer's
   cards, so on the counter editor a recipient saw "You give $0.15" when $0.15
   was what the other party was giving — and "in your favour" therefore said the
   exact opposite of the truth.
3. It did not stick below 769px, where `html, body { overflow-x: hidden }` makes
   body a scroll container and quietly disables `position: sticky` everywhere
   inside it. Covered by test_sticky_survives_mobile_overflow.py.
"""

from __future__ import annotations

import itertools
import re

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
def world(db, user):
    """ALEX proposes: he offers a $1 card and asks for two $3 cards of ours.

    So `user` is the RECIPIENT — the case that was inverted. From our side the
    trade costs $6.00 and returns $1.00.
    """
    alex = User(username=f"alex{next(_seq)}@x.com", password_hash="x", display_name="Alex")
    db.add(alex)
    db.flush()
    pg = Playgroup(name="Pod", created_by=alex.id)
    db.add(pg)
    db.flush()
    db.add_all(
        [
            PlaygroupMember(playgroup_id=pg.id, user_id=alex.id, role="owner"),
            PlaygroupMember(playgroup_id=pg.id, user_id=user.id, role="member"),
        ]
    )
    theirs = StorageLocation(user_id=alex.id, name="Theirs", type="binder", mode="managed")
    mine = StorageLocation(user_id=user.id, name="Mine", type="binder", mode="managed")
    sc_them = Showcase(user_id=alex.id, name="Theirs")
    sc_me = Showcase(user_id=user.id, name="Mine")
    db.add_all([theirs, mine, sc_them, sc_me])
    db.flush()

    def _row(owner_id, name, loc, price):
        c = Card(
            scryfall_id=f"sid-{next(_seq)}",
            name=name,
            set_code="tst",
            collector_number="1",
            type_line="Creature",
            image_url="https://img.example.invalid/x.jpg",
            price_usd=price,
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

    their_row = _row(alex.id, "Germinating Wurm", theirs, "1.00")
    my_rows = [_row(user.id, f"Mine {i}", mine, "3.00") for i in (1, 2)]
    my_items = []
    for r in my_rows:
        si = ShowcaseItem(showcase_id=sc_me.id, inventory_row_id=r.id, quantity_offered=1)
        db.add(si)
        db.flush()
        my_items.append(si)
    db.add(ShowcaseItem(showcase_id=sc_them.id, inventory_row_id=their_row.id, quantity_offered=1))
    db.add_all(
        [
            Share(user_id=alex.id, showcase_id=sc_them.id, playgroup_id=pg.id),
            Share(user_id=user.id, showcase_id=sc_me.id, playgroup_id=pg.id),
        ]
    )
    db.commit()

    trade = ts.create_trade(
        db,
        proposer_user_id=alex.id,
        recipient_user_id=user.id,
        playgroup_id=pg.id,
        offered=[{"inventory_row_id": their_row.id, "quantity": 1}],
        requested=[{"showcase_item_id": si.id, "quantity": 1} for si in my_items],
    )
    return {"alex": alex, "pg": pg, "trade": trade}


def _bar(html: str) -> str:
    m = re.search(
        r'<div class="trade-balance[^"]*"(.*?)</div>\s*</?(?:section|div|form)', html, re.S
    )
    assert m, "no balance bar on the page"
    return m.group(0)


# --------------------------------------------------------------------------
# The summary itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "offered,requested,is_proposer,give,receive,note",
    [
        # The proposer gives what is OFFERED.
        (10.0, 4.0, True, 10.0, 4.0, "$6.00 in their favour"),
        # The recipient gives what is REQUESTED — the inversion that shipped.
        (10.0, 4.0, False, 4.0, 10.0, "$6.00 in your favour"),
        # Within a dollar reads as even, from either side.
        (10.0, 9.5, True, 10.0, 9.5, "Even — within $0.50"),
        (10.0, 9.5, False, 9.5, 10.0, "Even — within $0.50"),
        # Exactly a dollar is still even — the boundary the rounding protects.
        (11.0, 10.0, True, 11.0, 10.0, "Even — within $1.00"),
    ],
)
def test_the_summary_is_viewer_relative(offered, requested, is_proposer, give, receive, note):
    s = ts.trade_balance_summary(offered, requested, is_proposer)
    assert (s["give"], s["receive"], s["note"]) == (give, receive, note)


def test_the_key_is_receive_not_get():
    """`bal.get` resolves to the dict's own `get` METHOD in Jinja, which the
    template then tries to format as money. Same family as `.items`."""
    assert "get" not in ts.trade_balance_summary(1.0, 2.0, True)


# --------------------------------------------------------------------------
# The pages
# --------------------------------------------------------------------------


def test_a_proposed_trade_shows_the_banner_from_YOUR_side(client, db, user, world):
    page = client.get(f"/trades/{world['trade'].id}")
    assert page.status_code == 200
    bar = _bar(page.text)
    # We are the recipient: we give our two $3 cards and receive their $1 one.
    assert "$6.00" in bar and "$1.00" in bar
    assert "$5.00 in their favour" in bar
    assert 'data-give-side="requested"' in bar


def test_the_counter_editor_orients_the_bar_for_whoever_is_editing(client, db, user, world):
    """The bug in the report: as the recipient, the picker's bar said we were
    giving the cheap side."""
    page = client.get(f"/trades/{world['trade'].id}/counter")
    assert page.status_code == 200
    assert 'data-give-side="requested"' in _bar(page.text)


def test_the_construction_page_gives_the_offered_side(client, db, user, world):
    """There the viewer is always the proposer, so offered IS what you give."""
    page = client.get(
        f"/trades/new?recipient_user_id={world['alex'].id}&playgroup_id={world['pg'].id}"
    )
    assert 'data-give-side="offered"' in _bar(page.text)


def test_the_even_threshold_reaches_the_client_from_the_server(client, db, user, world):
    """One definition of "even": the static banner computes it in Python and the
    live one reads the same number off the bar."""
    page = client.get(f"/trades/{world['trade'].id}")
    assert f'data-even-within="{ts.TRADE_EVEN_WITHIN:.2f}"' in _bar(page.text)
    js = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app"
        / "static"
        / "trade-picker.js"
    ).read_text()
    assert "data-even-within" in js, "the client no longer reads the shared threshold"
