"""A rejected trade proposal must survive the error (SaintWacko, 2026-08-21).

"The error should be displayed... but the pending trade proposal should be left
alone to be fixed." It was a 303 to a bare ``/trades/new``, and the in-progress
selection lives ONLY in the page's JS Maps, so the recipient, the playgroup and
every pick were gone by the time the message was readable — a validation error
cost you the whole proposal.

The POST now RE-RENDERS the construction page with the error and the picks.
"""

from __future__ import annotations

import itertools
import json
import re

from app.models import (
    Card,
    InventoryRow,
    PlaygroupMember,
    Share,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    User,
)
from app.playgroup_service import create_playgroup

_seq = itertools.count(1)


def _card(db, name):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Creature",
        # The picker renders a thumb only for a card that HAS art, so a card
        # without image_url would make the srcset assertion below vacuous.
        image_url="https://img.example.invalid/x.jpg",
    )
    db.add(c)
    db.flush()
    return c


def _own(db, user_id, card, location_id=None):
    row = InventoryRow(
        card_id=card.id,
        user_id=user_id,
        quantity=1,
        finish="normal",
        is_pending=False,
        storage_location_id=location_id,
    )
    db.add(row)
    db.flush()
    return row


def _setup(db, user):
    """`user` (the authed proposer) and a co-member who shares a showcase."""
    other = User(username=f"other-{next(_seq)}@x.com", password_hash="x", display_name="Other")
    db.add(other)
    db.flush()
    pg = create_playgroup(db, user.id, "Pod")
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=other.id, role="member"))

    binder = StorageLocation(user_id=user.id, name="Binder", type="binder", mode="managed")
    their_box = StorageLocation(user_id=other.id, name="Box", type="box", mode="manual")
    db.add_all([binder, their_box])
    db.flush()

    mine = _own(db, user.id, _card(db, "Llanowar Elves"), binder.id)
    theirs = _own(db, other.id, _card(db, "Rhystic Study"), their_box.id)
    sc = Showcase(user_id=other.id, name="Trades")
    db.add(sc)
    db.flush()
    si = ShowcaseItem(showcase_id=sc.id, inventory_row_id=theirs.id, quantity_offered=1)
    db.add(si)
    db.add(Share(user_id=other.id, showcase_id=sc.id, playgroup_id=pg.id))
    db.commit()
    return other, pg, mine, si


def _restore_blob(page: str) -> dict:
    """#184 — the blob is now HYDRATED: each entry carries the name, price and
    cap the tray needs, because a paged picker cannot rely on the pick's tile
    being on screen to read them off."""
    m = re.search(r'id="pick-restore">(.*?)</script>', page, re.S)
    assert m, "the page carries no restore blob"
    return json.loads(m.group(1))


def test_a_rejected_proposal_comes_back_with_its_picks(client, db, user):
    """One offered card, NO requested card — the A6 rule rejects it.

    The page must come back with the message AND the offered pick still made,
    rather than a blank form.
    """
    other, pg, mine, _si = _setup(db, user)

    resp = client.post(
        "/trades",
        data={
            "recipient_user_id": other.id,
            "playgroup_id": pg.id,
            "offered_json": json.dumps([{"inventory_row_id": mine.id, "quantity": 1}]),
            "requested_json": "[]",
            "proposer_note": "",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 200, "a rejected proposal must not redirect away from the page"
    assert "at least one requested item" in resp.text.lower()
    # The recipient context survived, so the requested-side picker is usable.
    assert "Rhystic Study" in resp.text
    (kept,) = _restore_blob(resp.text)["offered"]
    assert (kept["kind"], kept["id"], kept["quantity"]) == ("inventory_row_id", mine.id, 1)
    assert kept["name"] == "Llanowar Elves", "the tray has to draw it without its tile"
    # Same render carries the picker grid, so it also pins the thumb's srcset
    # (see test_thumb_srcset.py — this is the surface that reported the weight).
    assert "/small.jpg 146w" in resp.text and 'sizes="146px"' in resp.text


def test_quantities_survive_and_junk_entries_are_dropped(client, db, user):
    """The payload already failed validation once, so it is treated as hostile:
    ids and quantities only, anything unparseable skipped rather than raising a
    second error on the page whose job is to show the first."""
    other, pg, mine, si = _setup(db, user)

    resp = client.post(
        "/trades",
        data={
            "recipient_user_id": other.id,
            "playgroup_id": pg.id,
            "offered_json": json.dumps(
                [
                    {"inventory_row_id": mine.id, "quantity": 3},
                    {"inventory_row_id": "not-an-id", "quantity": 1},
                    "garbage",
                ]
            ),
            # Requested is present but not from their shared showcase -> rejected.
            "requested_json": json.dumps([{"showcase_item_id": si.id + 9999, "quantity": 1}]),
            "proposer_note": "",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 200
    blob = _restore_blob(resp.text)
    (kept,) = blob["offered"]
    assert (kept["id"], kept["quantity"]) == (mine.id, 3), "quantities survive"
    # The bogus requested id names nothing pickable, so it is dropped — the same
    # answer the validator gives it, rather than a tray entry for a card that
    # cannot be traded.
    assert blob["requested"] == []


def test_a_malformed_submission_still_renders_the_page(client, db, user):
    other, pg, _mine, _si = _setup(db, user)
    resp = client.post(
        "/trades",
        data={
            "recipient_user_id": other.id,
            "playgroup_id": pg.id,
            "offered_json": "{not json",
            "requested_json": "[]",
            "proposer_note": "",
            "csrf_token": "x",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "malformed" in resp.text.lower()
