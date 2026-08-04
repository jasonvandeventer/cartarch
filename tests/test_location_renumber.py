"""#132 — a browsable, slot-numbered Bulk, and the ordering that makes it readable.

The issue asks for "internal set/collector slotting like drawers 2–5". Checking
what drawers 2–5 actually DO decided the shape: every drawer row carries a
DISTINCT slot (prod: 1,042 rows → 1,042 distinct slots), written by
``resort_collection`` as ``str(index)`` after sorting each bucket. So a slot is a
physical position, not a label — and Bulk had 1,744 rows and **zero** of them.

Checking it also surfaced a live defect the issue never mentions: ``slot`` is the
DEFAULT sort on a location page and was compared LEXICALLY, so a real drawer
displayed ``1, 10, 100, 1000, …, 2, 20``.
"""

from __future__ import annotations

import pytest

from app import sort_spec
from app.inventory_service import renumber_location_slots, shelf_sort_key
from app.models import Card, InventoryRow, StorageLocation


@pytest.fixture
def box(db, user):
    loc = StorageLocation(user_id=user.id, name="Bulk", type="box", mode="manual")
    db.add(loc)
    db.commit()
    return loc


def _card(db, name, set_code, collector):
    c = Card(
        scryfall_id=f"sid-{set_code}-{collector}-{name}",
        name=name,
        set_code=set_code,
        collector_number=collector,
        type_line="Creature",
    )
    db.add(c)
    db.commit()
    return c


def _place(db, user, card, loc, *, slot=None):
    row = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=1,
        finish="normal",
        storage_location_id=loc.id,
        is_pending=False,
        slot=slot,
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------
# The live defect: slot is the DEFAULT location sort and was lexical.
# --------------------------------------------------------------------------


def test_slot_sorts_numerically_not_lexically():
    """Drawer 3 holds 1,042 slotted rows in prod, and ``slot`` is the default sort.

    Lexically that renders ``1, 10, 100, 1000, …, 2, 20`` — useless for walking a
    physical drawer, which is the only purpose the ordering has. This was live
    before #132 and is not something the issue asked for.
    """

    class _C:
        name = "x"

    class _R:
        def __init__(self, slot, rid):
            self.slot, self.id, self.card = slot, rid, _C()

    rows = [_R(str(n), n) for n in (1, 2, 7, 10, 99, 100, 101, 1042)]
    ordered = sort_spec.sort_inventory_rows(rows, "slot", "asc")
    assert [r.slot for r in ordered] == ["1", "2", "7", "10", "99", "100", "101", "1042"]


def test_unslotted_rows_sort_after_every_numbered_one():
    """A box mid-refile has both; the unnumbered must not interleave with the run."""

    class _C:
        name = "x"

    class _R:
        def __init__(self, slot, rid):
            self.slot, self.id, self.card = slot, rid, _C()

    rows = [_R(None, 1), _R("2", 2), _R("", 3), _R("10", 4)]
    ordered = sort_spec.sort_inventory_rows(rows, "slot", "asc")
    assert [r.slot for r in ordered][:2] == ["2", "10"]


# --------------------------------------------------------------------------
# The shelf ordering — ONE definition, shared with drawer_sort_key.
# --------------------------------------------------------------------------


def test_shelf_key_orders_by_set_then_collector_numerically(db, user, box):
    """2 before 10 — a box filed lexically is a box nobody can walk."""
    rows = [
        _place(db, user, _card(db, "Ten", "abc", "10"), box),
        _place(db, user, _card(db, "Two", "abc", "2"), box),
        _place(db, user, _card(db, "Aardvark", "zzz", "1"), box),
        _place(db, user, _card(db, "Suffix", "abc", "2a"), box),
    ]
    rows.sort(key=shelf_sort_key)
    assert [r.card.name for r in rows] == ["Two", "Suffix", "Ten", "Aardvark"]


def test_drawer_sort_key_uses_the_same_definition(db, user, box):
    """``drawer_sort_key`` returns ``shelf_sort_key`` for drawers 2-5.

    If these ever diverge, a re-filed box and a sorted drawer are ordered by two
    different rules while both claim to be "set and collector order".
    """
    from app.inventory_service import drawer_sort_key

    card = _card(db, "Ordinary", "abc", "7")
    row = _place(db, user, card, box)
    assert drawer_sort_key(row) == shelf_sort_key(row)


# --------------------------------------------------------------------------
# Renumbering.
# --------------------------------------------------------------------------


def test_renumbering_writes_slots_one_through_n_in_shelf_order(db, user, box):
    _place(db, user, _card(db, "Ten", "abc", "10"), box)
    _place(db, user, _card(db, "Two", "abc", "2"), box)
    _place(db, user, _card(db, "Zed", "zzz", "1"), box)

    assert renumber_location_slots(db, box.id, user.id) == 3

    rows = db.query(InventoryRow).filter(InventoryRow.storage_location_id == box.id).all()
    rows.sort(key=shelf_sort_key)
    assert [r.slot for r in rows] == ["1", "2", "3"]
    assert [r.card.name for r in rows] == ["Two", "Ten", "Zed"]


def test_renumbering_is_idempotent_and_reports_zero(db, user, box):
    """ "0 changed" is a real answer — already in order — not a failure."""
    _place(db, user, _card(db, "A", "abc", "1"), box)
    _place(db, user, _card(db, "B", "abc", "2"), box)

    assert renumber_location_slots(db, box.id, user.id) == 2
    assert renumber_location_slots(db, box.id, user.id) == 0


def test_pending_rows_are_not_numbered(db, user, box):
    """A pending row has no place in the box yet; numbering it would claim it does."""
    placed = _place(db, user, _card(db, "Placed", "abc", "1"), box)
    pending = _place(db, user, _card(db, "Pending", "abc", "2"), box)
    pending.is_pending = True
    db.commit()

    assert renumber_location_slots(db, box.id, user.id) == 1
    db.refresh(placed)
    db.refresh(pending)
    assert placed.slot == "1"
    assert pending.slot is None


def test_a_drawer_is_refused(db, user):
    """The sorter owns drawer slots. Two writers on one column is a race.

    ``resort_collection`` renumbers drawers from its own ordering; a second
    entry point writing the same column would fight it.
    """
    drawer = StorageLocation(user_id=user.id, name="Drawer 2", type="drawer", mode="managed")
    db.add(drawer)
    db.commit()

    with pytest.raises(ValueError, match="drawer"):
        renumber_location_slots(db, drawer.id, user.id)


@pytest.mark.parametrize("loc_type", ["deck", "considering"])
def test_deck_locations_are_refused(db, user, loc_type):
    """A deck is not filed by set — its ordering is the decklist."""
    loc = StorageLocation(user_id=user.id, name="Deck", type=loc_type, mode="manual")
    db.add(loc)
    db.commit()

    with pytest.raises(ValueError, match=loc_type):
        renumber_location_slots(db, loc.id, user.id)


def test_another_users_location_is_refused(db, user, box):
    """Owner scoping — this writes to rows."""
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()

    with pytest.raises(ValueError, match="not found"):
        renumber_location_slots(db, box.id, other.id)


def test_renumbering_leaves_other_locations_alone(db, user, box):
    """A box is filed on its own; nothing outside it moves."""
    other_box = StorageLocation(user_id=user.id, name="Binder", type="binder", mode="manual")
    db.add(other_box)
    db.commit()
    elsewhere = _place(db, user, _card(db, "Elsewhere", "abc", "1"), other_box, slot="99")
    _place(db, user, _card(db, "Here", "abc", "1"), box)

    renumber_location_slots(db, box.id, user.id)
    db.refresh(elsewhere)
    assert elsewhere.slot == "99"


# --------------------------------------------------------------------------
# The route.
# --------------------------------------------------------------------------


def test_the_route_renumbers_and_reports(client, db, user, box):
    _place(db, user, _card(db, "Ten", "abc", "10"), box)
    _place(db, user, _card(db, "Two", "abc", "2"), box)

    resp = client.post(f"/locations/{box.id}/renumber", follow_redirects=False)
    assert resp.status_code == 303
    assert "renumbered=2" in resp.headers["location"]

    rows = db.query(InventoryRow).filter(InventoryRow.storage_location_id == box.id).all()
    assert sorted(r.slot for r in rows) == ["1", "2"]


def test_the_button_renders_on_a_box_but_not_on_a_drawer(client, db, user, box):
    """Route-level — the control has to reach the page (the #152 mode)."""
    page = client.get(f"/locations/{box.id}").text
    assert f"/locations/{box.id}/renumber" in page

    drawer = StorageLocation(user_id=user.id, name="Drawer 2", type="drawer", mode="managed")
    db.add(drawer)
    db.commit()
    drawer_page = client.get(f"/locations/{drawer.id}").text
    assert f"/locations/{drawer.id}/renumber" not in drawer_page
