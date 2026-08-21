"""Quantity split (issue #77).

Pins the primary acceptance criterion: splitting N copies off a row creates a
NEW independent row and only decrements the original — the original keeps its
id, placement, metadata, and every FK reference. Never delete-and-readd.
"""

from __future__ import annotations

import pytest

from app.inventory_service import split_inventory_row
from app.models import (
    Card,
    DeckCardShare,
    InventoryRow,
    ShowcaseItem,
    StorageLocation,
    TradeItem,
    TransactionLog,
)


def _card(db, **kw):
    c = Card(
        scryfall_id=kw.pop("scryfall_id", "khm-1"),
        name=kw.pop("name", "Sol Ring"),
        set_code=kw.pop("set_code", "khm"),
        collector_number=kw.pop("collector_number", "1"),
        **kw,
    )
    db.add(c)
    db.flush()
    return c


def _row(db, user_id, card, **kw):
    r = InventoryRow(
        card_id=card.id,
        user_id=user_id,
        quantity=kw.pop("quantity", 4),
        finish=kw.pop("finish", "normal"),
        is_pending=kw.pop("is_pending", False),
        **kw,
    )
    db.add(r)
    db.commit()
    return r


def test_split_preserves_original_and_copies_fields(db, user):
    loc = StorageLocation(user_id=user.id, name="Drawer 1", type="drawer", mode="managed")
    db.add(loc)
    db.flush()
    card = _card(db)
    row = _row(
        db,
        user.id,
        card,
        quantity=4,
        finish="foil",
        storage_location_id=loc.id,
        drawer="1",
        slot="5",
        tags="ramp",
        role="commander",
        notes="signed",
        language="ja",
        is_proxy=True,
        from_drawer="2",
        from_slot="9",
    )
    row_id, created = row.id, row.created_at

    new_row = split_inventory_row(db, row_id=row_id, user_id=user.id, split_quantity=1)
    db.refresh(row)

    # original: same row, decremented, everything else untouched
    assert row.id == row_id
    assert row.quantity == 3
    assert row.created_at == created
    assert row.updated_at >= created

    # new row: independent id, split quantity, every non-quantity field copied
    assert new_row.id != row_id
    assert new_row.quantity == 1
    assert new_row.user_id == user.id
    assert new_row.card_id == card.id
    assert new_row.storage_location_id == loc.id
    assert new_row.finish == "foil"
    assert new_row.drawer == "1" and new_row.slot == "5"
    assert new_row.tags == "ramp"
    assert new_row.role == "commander"
    assert new_row.notes == "signed"
    assert new_row.language == "ja"
    assert new_row.is_proxy is True
    assert new_row.from_drawer == "2" and new_row.from_slot == "9"
    assert new_row.is_pending is False
    assert new_row.created_at >= created  # fresh timestamps, not copied


def test_split_quantity_invariant(db, user):
    card = _card(db)
    row = _row(db, user.id, card, quantity=7)
    new_row = split_inventory_row(db, row_id=row.id, user_id=user.id, split_quantity=3)
    db.refresh(row)
    assert row.quantity + new_row.quantity == 7


def test_split_copies_null_fields_as_null(db, user):
    card = _card(db)
    row = _row(db, user.id, card, quantity=2, notes=None, tags=None, role=None, slot=None)
    new_row = split_inventory_row(db, row_id=row.id, user_id=user.id, split_quantity=1)
    assert new_row.notes is None
    assert new_row.tags is None
    assert new_row.role is None
    assert new_row.slot is None
    assert new_row.storage_location_id is None


def test_references_stay_on_original_row(db, user, row_reference_parents):
    refs = row_reference_parents
    card = _card(db)
    row = _row(db, user.id, card, quantity=3)
    sc_item = ShowcaseItem(
        showcase_id=refs.showcase.id, inventory_row_id=row.id, quantity_offered=1
    )
    tr_item = TradeItem(
        trade_id=refs.trade.id,
        revision_id=refs.trade_revision.id,
        side="offer",
        inventory_row_id=row.id,
        quantity=1,
        finish="normal",
    )
    dcs = DeckCardShare(
        inventory_row_id=row.id,
        source_deck_id=refs.source_deck.id,
        target_deck_id=refs.target_deck.id,
        variant_group_id=refs.variant_group.id,
    )
    db.add_all([sc_item, tr_item, dcs])
    db.commit()

    new_row = split_inventory_row(db, row_id=row.id, user_id=user.id, split_quantity=1)

    assert db.get(ShowcaseItem, sc_item.id).inventory_row_id == row.id
    assert db.get(TradeItem, tr_item.id).inventory_row_id == row.id
    assert db.get(DeckCardShare, dcs.id).inventory_row_id == row.id
    # and nothing points at the new row
    assert new_row.id != row.id


def test_writes_two_net_zero_split_events(db, user):
    card = _card(db)
    row = _row(db, user.id, card, quantity=5)
    new_row = split_inventory_row(db, row_id=row.id, user_id=user.id, split_quantity=2)

    logs = db.query(TransactionLog).filter(TransactionLog.event_type == "split_row").all()
    assert len(logs) == 2
    assert sum(log.quantity_delta for log in logs) == 0
    by_row = {log.inventory_row_id: log.quantity_delta for log in logs}
    assert by_row == {row.id: -2, new_row.id: 2}
    assert logs[0].note == logs[1].note  # correlatable as one event


def test_invalid_split_quantities_rejected_and_nothing_changes(db, user):
    card = _card(db)
    row = _row(db, user.id, card, quantity=3)
    for bad in (0, -1, 3, 4):  # must be 1..quantity-1
        with pytest.raises(ValueError):
            split_inventory_row(db, row_id=row.id, user_id=user.id, split_quantity=bad)
    db.rollback()
    db.refresh(row)
    assert row.quantity == 3
    assert db.query(InventoryRow).filter(InventoryRow.user_id == user.id).count() == 1
    assert db.query(TransactionLog).count() == 0


def test_owner_only(db, user):
    card = _card(db)
    row = _row(db, user.id, card, quantity=3)
    with pytest.raises(ValueError, match="not found"):
        split_inventory_row(db, row_id=row.id, user_id=user.id + 999, split_quantity=1)


def test_split_repeated_rows_are_independent(db, user):
    card = _card(db)
    row = _row(db, user.id, card, quantity=4)
    first = split_inventory_row(db, row_id=row.id, user_id=user.id, split_quantity=1)
    second = split_inventory_row(db, row_id=row.id, user_id=user.id, split_quantity=1)
    db.refresh(row)
    assert row.quantity == 2
    assert first.quantity == 1 and second.quantity == 1
    assert len({row.id, first.id, second.id}) == 3


def test_route_splits_row(client, db, user):
    card = _card(db)
    row = _row(db, user.id, card, quantity=4)
    resp = client.post(
        f"/inventory/rows/{row.id}/split",
        data={"quantity": "3", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(row)
    assert row.quantity == 1
    rows = db.query(InventoryRow).filter(InventoryRow.user_id == user.id).all()
    assert sorted(r.quantity for r in rows) == [1, 3]


def test_route_invalid_quantity_is_400(client, db, user):
    card = _card(db)
    row = _row(db, user.id, card, quantity=2)
    resp = client.post(
        f"/inventory/rows/{row.id}/split",
        data={"quantity": "2", "csrf_token": "x"},  # == row quantity: nothing to peel
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_route_malformed_quantity_rejected(client, db, user):
    card = _card(db)
    row = _row(db, user.id, card, quantity=2)
    resp = client.post(
        f"/inventory/rows/{row.id}/split",
        data={"quantity": "one", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 422  # FastAPI form-int coercion
    db.refresh(row)
    assert row.quantity == 2


def test_route_other_users_row_is_404(client, db, user):
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    card = _card(db)
    row = _row(db, other.id, card, quantity=3)
    resp = client.post(
        f"/inventory/rows/{row.id}/split",
        data={"quantity": "1", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
