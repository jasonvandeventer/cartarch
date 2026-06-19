"""FK-safety of the merge + import-undo delete paths — run under PRAGMA foreign_keys=ON.

Before v3.39.x the merge path (`move_inventory_row_to_location` / placement-merge)
and the import-undo paths deleted inventory rows WITHOUT cleaning referencing
``showcase_items`` (NOT NULL FK) / ``trade_items`` (nullable FK) — silent orphans
under SQLite, a hard FK error under Postgres. All such ``session.delete(row)``
sites now route through ``clean_inventory_row_references``. These tests prove the
paths are safe with **enforcement enabled** (the ``fk_db`` fixture). A negative
control proves the enforcement is real (no false-green).
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy.exc import IntegrityError

from app import inventory_service
from app.models import (
    Card,
    ImportBatch,
    InventoryRow,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    Trade,
    TradeItem,
    TransactionLog,
    User,
)

_seq = itertools.count(1)


def _seed_user(s, username):
    u = User(username=username, password_hash="x")
    s.add(u)
    s.flush()
    return u


def _seed_card(s):
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name="Test Card",
        set_code="TST",
        collector_number=str(next(_seq)),
        price_usd="5.00",
    )
    s.add(c)
    s.flush()
    return c


def _seed_row(s, user_id, card_id, loc_id, qty=1):
    r = InventoryRow(
        card_id=card_id,
        user_id=user_id,
        quantity=qty,
        finish="normal",
        is_pending=False,
        is_proxy=False,
        storage_location_id=loc_id,
    )
    s.add(r)
    s.flush()
    return r


def _seed_loc(s, user_id, name):
    loc = StorageLocation(user_id=user_id, name=name, type="box", mode="manual", sort_order=0)
    s.add(loc)
    s.flush()
    return loc


def _reference_row(s, user, card, row):
    """Attach a ShowcaseItem and a pending-Trade TradeItem to ``row``."""
    sc = Showcase(user_id=user.id, name="SC")
    s.add(sc)
    s.flush()
    si = ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1)
    s.add(si)
    other = _seed_user(s, f"other-{next(_seq)}@example.com")
    t = Trade(proposer_user_id=user.id, recipient_user_id=other.id, status="proposed")
    s.add(t)
    s.flush()
    ti = TradeItem(
        trade_id=t.id,
        side="offered",
        inventory_row_id=row.id,
        card_id=card.id,
        finish="normal",
        quantity=1,
    )
    s.add(ti)
    s.commit()
    return si, ti


def test_fk_enforcement_is_actually_on(fk_db):
    """Negative control: an UNCLEAN delete of a referenced row must raise under FK
    enforcement — proves the fixture really enforces (else the tests below false-green).

    Deletes a ``Showcase`` that still has a child ``ShowcaseItem``: the
    ``showcase_items.showcase_id`` FK is plain NO ACTION, so the parent delete must
    raise. (The earlier control deleted the *inventory row*, but as of the Gate-#4
    baseline ``showcase_items.inventory_row_id`` is ``ondelete=CASCADE`` and
    ``trade_items.inventory_row_id`` is ``SET NULL`` — neither raises now; the merge/
    undo tests below still assert the app-side cleanup produces the right end state.)"""
    s = fk_db
    u = _seed_user(s, "owner@example.com")
    c = _seed_card(s)
    loc = _seed_loc(s, u.id, "Box")
    row = _seed_row(s, u.id, c.id, loc.id)
    sc = Showcase(user_id=u.id, name="SC2")
    s.add(sc)
    s.flush()
    s.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1))
    s.commit()

    with pytest.raises(IntegrityError):
        s.query(Showcase).filter(Showcase.id == sc.id).delete(synchronize_session=False)
        s.flush()
    s.rollback()


def test_merge_path_is_fk_safe(fk_db):
    """Moving a showcased+traded row into a location with a matching row merges it
    away (deletes the row) — must NOT FK-error, and must clean the references."""
    s = fk_db
    u = _seed_user(s, "owner@example.com")
    c = _seed_card(s)
    loc1 = _seed_loc(s, u.id, "Box1")
    loc2 = _seed_loc(s, u.id, "Box2")
    row_a = _seed_row(s, u.id, c.id, loc1.id)  # the showcased/traded row (gets merged away)
    _seed_row(s, u.id, c.id, loc2.id)  # matching destination row (survives)
    si, ti = _reference_row(s, u, c, row_a)
    si_id, ti_id, a_id = si.id, ti.id, row_a.id

    # Merge A into the loc2 row — deletes A. No IntegrityError expected.
    inventory_service.move_inventory_row_to_location(
        s, row_id=a_id, user_id=u.id, location_id=loc2.id
    )
    s.expire_all()

    assert s.query(InventoryRow).filter(InventoryRow.id == a_id).first() is None
    assert s.query(ShowcaseItem).filter(ShowcaseItem.id == si_id).first() is None  # deleted
    ti_after = s.query(TradeItem).filter(TradeItem.id == ti_id).first()
    assert ti_after is not None and ti_after.inventory_row_id is None  # NULLed, trade kept


def test_undo_path_is_fk_safe(fk_db):
    """Undoing an import that deletes a showcased+traded row must NOT FK-error and
    must clean the references."""
    s = fk_db
    u = _seed_user(s, "owner@example.com")
    c = _seed_card(s)
    loc = _seed_loc(s, u.id, "Box")
    row = _seed_row(s, u.id, c.id, loc.id, qty=1)
    batch = ImportBatch(filename="paste", row_count=1, user_id=u.id)
    s.add(batch)
    s.flush()
    s.add(
        TransactionLog(
            user_id=u.id,
            event_type="import",
            card_id=c.id,
            finish="normal",
            quantity_delta=1,
            inventory_row_id=row.id,
            batch_id=batch.id,
        )
    )
    s.commit()
    si, ti = _reference_row(s, u, c, row)
    si_id, ti_id, row_id, batch_id = si.id, ti.id, row.id, batch.id

    # Undo the batch — decrements the row to 0 and deletes it. No IntegrityError.
    inventory_service.undo_last_batch(s, batch_id=batch_id, user_id=u.id)
    s.expire_all()

    assert s.query(InventoryRow).filter(InventoryRow.id == row_id).first() is None
    assert s.query(ShowcaseItem).filter(ShowcaseItem.id == si_id).first() is None
    ti_after = s.query(TradeItem).filter(TradeItem.id == ti_id).first()
    assert ti_after is not None and ti_after.inventory_row_id is None
