"""The location grid's default sort resolves per location (v4.12.14).

`sort` defaulted to "slot", but only drawer-sorter drawers carry a slot. On a
Bulk box every row's slot is NULL, so a few hundred rows sorted by a uniformly
-NULL key and fell through to the name tiebreaker — ordered by accident rather
than by anything the page claimed.

Pinned here:
  - unslotted location -> resolves to "name"
  - slotted location   -> still resolves to "slot" (no regression for drawers)
  - an EXPLICIT sort always wins, including an explicit "slot" on a Bulk box
  - the route feeds the RESOLVED value to the template, so the Sort dropdown
    shows what the grid actually did (the #152 lesson: a service-level check
    cannot see whether the value reached the page)
"""

from __future__ import annotations

from app.models import Card, InventoryRow, StorageLocation
from app.routes.collections import _build_location_items


def _seed_location(db, user, *, name, slots):
    """A location holding one row per entry in ``slots`` (None = unslotted).

    Card names are seeded in reverse alphabetical order relative to insertion,
    so "insertion order" and "name order" are distinguishable in assertions.
    """
    loc = StorageLocation(user_id=user.id, name=name, type="box", mode="manual")
    db.add(loc)
    db.commit()

    for i, slot in enumerate(slots):
        card = Card(
            scryfall_id=f"{name}-{i}",
            name=f"{'ZYXWVUTSRQ'[i]} Card",
            set_code="tst",
            collector_number=str(i),
        )
        db.add(card)
        db.commit()
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=card.id,
                finish="normal",
                quantity=1,
                storage_location_id=loc.id,
                is_pending=False,
                slot=slot,
            )
        )
    db.commit()
    return loc


def test_unslotted_location_defaults_to_name(db, user):
    loc = _seed_location(db, user, name="Bulk", slots=[None, None, None])

    items, _, _, resolved = _build_location_items(db, loc.id, user.id)

    assert resolved == "name"
    assert [i["card"].name for i in items] == ["X Card", "Y Card", "Z Card"]


def test_slotted_location_still_defaults_to_slot(db, user):
    loc = _seed_location(db, user, name="Drawer 2", slots=["001", "002", "003"])

    items, _, _, resolved = _build_location_items(db, loc.id, user.id)

    assert resolved == "slot"
    assert [i["slot"] for i in items] == ["001", "002", "003"]


def test_a_partially_slotted_location_keeps_slot(db, user):
    """One slotted row is enough — slot is still the meaningful axis there."""
    loc = _seed_location(db, user, name="Half", slots=[None, "007", None])

    _, _, _, resolved = _build_location_items(db, loc.id, user.id)

    assert resolved == "slot"


def test_explicit_sort_always_wins(db, user):
    """Including an explicit "slot" on a box that has none — the user asked."""
    loc = _seed_location(db, user, name="Bulk", slots=[None, None, None])

    _, _, _, resolved = _build_location_items(db, loc.id, user.id, sort="slot")
    assert resolved == "slot"

    _, _, _, resolved = _build_location_items(db, loc.id, user.id, sort="set")
    assert resolved == "set"


def test_empty_location_does_not_blow_up(db, user):
    loc = _seed_location(db, user, name="Empty", slots=[])

    items, total_value, total_qty, resolved = _build_location_items(db, loc.id, user.id)

    assert items == []
    assert (total_value, total_qty) == (0.0, 0)
    assert resolved == "name"


def test_the_resolved_sort_reaches_the_page(client, db, user):
    """Route-level: the Sort dropdown must show the sort the grid actually used.

    A service-level assertion cannot see this — the route has to hand the
    resolved value to the template (see #152).
    """
    loc = _seed_location(db, user, name="Bulk", slots=[None, None])

    body = client.get(f"/locations/{loc.id}").text

    selected = body.split('name="sort"', 1)[1].split("</select>", 1)[0]
    assert '<option value="name" selected' in selected or 'value="name"  selected' in selected
