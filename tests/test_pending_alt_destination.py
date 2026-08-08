"""A drawer-sorter user can file a pending card somewhere other than its drawer.

Reported 2026-08-08: "why does Bulk not show up on the Pending Placement page?"
Because `routes/collections.py` set `locations = []` for sorter users, so the
destination select only ever rendered on the non-sorter branch. That was
deliberate when written — a sorter user's rows go to their assigned drawer — but
v3.38.0's drawer-vs-bulk routing made "this surplus copy belongs in Bulk" a
normal outcome with no way to say it from this page.

Decks stay excluded: that route must go through `pull_card_to_deck`, the same
guard the non-sorter branch and the bulk-move route already apply.
"""

import app.legacy_tables  # noqa
from app.inventory_service import confirm_pending_row
from app.models import Card, InventoryRow, StorageLocation


def _sorter_user_with_pending(db, user):
    """A drawer setup (so `has_sortable_setup` is true) plus a Bulk box, a deck
    location, and one pending row the sorter has assigned to drawer 3."""
    drawer = StorageLocation(user_id=user.id, name="Drawer 3", type="drawer", mode="managed")
    bulk = StorageLocation(user_id=user.id, name="Bulk", type="box", mode="manual")
    deck_loc = StorageLocation(user_id=user.id, name="A Deck", type="deck", mode="manual")
    db.add_all([drawer, bulk, deck_loc])
    card = Card(
        name="Surplus Common",
        scryfall_id="sf-alt-1",
        set_code="tst",
        set_name="T",
        collector_number="1",
        rarity="common",
    )
    db.add(card)
    db.flush()
    row = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=1,
        finish="normal",
        is_pending=True,
        drawer="3",
        slot="12",
    )
    db.add(row)
    db.commit()
    return row, bulk, deck_loc


def test_bulk_is_offered_to_a_sorter_user(client, db, user):
    row, bulk, deck_loc = _sorter_user_with_pending(db, user)
    html = client.get("/pending").text
    assert "Bulk (box)" in html, "the whole point of the report"
    assert f'value="{bulk.id}"' in html


def test_decks_are_not_offered(client, db, user):
    """A deck destination would bypass pull_card_to_deck reconciliation."""
    row, bulk, deck_loc = _sorter_user_with_pending(db, user)
    html = client.get("/pending").text
    assert f'value="{deck_loc.id}"' not in html
    assert "A Deck" not in html


def test_the_assigned_drawer_stays_the_default(client, db, user):
    """Value 0 → the drawer path in confirm_pending_row. Changing the default
    would silently redirect every confirm a sorter user makes."""
    row, bulk, _ = _sorter_user_with_pending(db, user)
    html = client.get("/pending").text
    i = html.index('name="location_id"')
    first_option = html.index("<option", i)
    assert 'value="0"' in html[first_option : first_option + 60]


def test_confirming_to_bulk_clears_the_drawer_assignment(db, user):
    """THE trap. `get_inventory_row_stats` is a drawer-FIRST cascade, so a row
    filed to Bulk while still carrying `drawer=3` would be counted under Drawer 3
    on the Overview pills — and the audit line would claim it went to a drawer it
    never reached."""
    row, bulk, _ = _sorter_user_with_pending(db, user)
    confirm_pending_row(db, row_id=row.id, user_id=user.id, location_id=bulk.id)
    db.refresh(row)
    assert row.storage_location_id == bulk.id
    assert row.is_pending is False
    assert row.drawer is None and row.slot is None


def test_confirming_to_the_drawer_keeps_the_slot(db, user):
    """The default path is untouched — the drawer/slot are how the card is found."""
    row, _, _ = _sorter_user_with_pending(db, user)
    confirm_pending_row(db, row_id=row.id, user_id=user.id, location_id=None)
    db.refresh(row)
    assert row.is_pending is False
    assert (row.drawer, row.slot) == ("3", "12")


def test_the_audit_line_names_the_real_destination(db, user):
    """`dest` used to read `drawer=3 slot=12` for a card that went to Bulk."""
    from app.models import TransactionLog

    row, bulk, _ = _sorter_user_with_pending(db, user)
    confirm_pending_row(db, row_id=row.id, user_id=user.id, location_id=bulk.id)
    log = (
        db.query(TransactionLog)
        .filter(TransactionLog.inventory_row_id == row.id)
        .order_by(TransactionLog.id.desc())
        .first()
    )
    assert log is not None
    assert log.destination_location == "Bulk"
