"""Orphan sweep — remediation + idempotency (scripts/sweep_fk_orphans.py).

Creates a real orphan (delete the inventory row under FK-OFF, leaving the
references dangling — exactly the pre-v3.39.x merge/undo failure mode), then
verifies the sweep deletes the orphaned showcase_item, NULLs the orphaned
trade_item's reference (keeping the trade record — decision A4), and that an
immediate second run remediates ZERO.
"""

from __future__ import annotations

from app.models import (
    Card,
    InventoryRow,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    Trade,
    TradeItem,
    User,
)
from scripts.sweep_fk_orphans import find_orphans, sweep_fk_orphans


def test_sweep_remediates_and_is_idempotent(db):
    s = db
    u = User(username="owner@example.com", password_hash="x")
    other = User(username="other@example.com", password_hash="x")
    s.add_all([u, other])
    s.flush()
    c = Card(scryfall_id="sid-sweep", name="C", set_code="TST", collector_number="1")
    s.add(c)
    s.flush()
    loc = StorageLocation(user_id=u.id, name="Box", type="box", mode="manual", sort_order=0)
    s.add(loc)
    s.flush()
    row = InventoryRow(
        card_id=c.id,
        user_id=u.id,
        quantity=1,
        finish="normal",
        is_pending=False,
        is_proxy=False,
        storage_location_id=loc.id,
    )
    s.add(row)
    s.flush()
    sc = Showcase(user_id=u.id, name="SC")
    s.add(sc)
    s.flush()
    si = ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1)
    s.add(si)
    t = Trade(proposer_user_id=u.id, recipient_user_id=other.id, status="proposed")
    s.add(t)
    s.flush()
    ti = TradeItem(
        trade_id=t.id,
        side="offered",
        inventory_row_id=row.id,
        card_id=c.id,
        finish="normal",
        quantity=1,
    )
    s.add(ti)
    s.commit()
    si_id, ti_id, row_id = si.id, ti.id, row.id

    # Orphan them: the default ``db`` fixture runs FK OFF, so a raw delete of the
    # inventory row leaves the references dangling (the pre-fix failure mode).
    s.query(InventoryRow).filter(InventoryRow.id == row_id).delete(synchronize_session=False)
    s.commit()

    orphans = find_orphans(s)
    assert orphans["showcase_items"] == [si_id]
    assert orphans["trade_items"] == [ti_id]

    # First sweep remediates: showcase_item deleted, trade_item NULLed.
    res1 = sweep_fk_orphans(s, apply=True)
    assert res1 == {"showcase_items_deleted": 1, "trade_items_nulled": 1}
    s.expire_all()
    assert s.query(ShowcaseItem).filter(ShowcaseItem.id == si_id).first() is None
    ti_after = s.query(TradeItem).filter(TradeItem.id == ti_id).first()
    assert ti_after is not None and ti_after.inventory_row_id is None  # trade kept, ref NULLed

    # Second sweep: zero deletions (idempotent).
    res2 = sweep_fk_orphans(s, apply=True)
    assert res2 == {"showcase_items_deleted": 0, "trade_items_nulled": 0}
