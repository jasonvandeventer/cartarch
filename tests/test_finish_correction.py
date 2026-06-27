"""In-place finish correction (issue #52).

Pins the primary acceptance criterion: correcting a row's finish is an UPDATE
that preserves the row identity + every reference + history, never a
delete-and-readd.
"""

from __future__ import annotations

import pytest

from app.inventory_service import correct_inventory_row_finish
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
        scryfall_id=kw.pop("scryfall_id", "clb-520"),
        name=kw.pop("name", "Raised by Giants"),
        set_code=kw.pop("set_code", "clb"),
        collector_number=kw.pop("collector_number", "520"),
        **kw,
    )
    db.add(c)
    db.flush()
    return c


def _row(db, user_id, card, **kw):
    r = InventoryRow(
        card_id=card.id,
        user_id=user_id,
        quantity=kw.pop("quantity", 1),
        finish=kw.pop("finish", "foil"),
        is_pending=kw.pop("is_pending", False),
        **kw,
    )
    db.add(r)
    db.commit()
    return r


def test_update_preserves_row_identity_and_fields(db, user):
    loc = StorageLocation(user_id=user.id, name="Drawer 1", type="drawer", mode="managed")
    db.add(loc)
    db.flush()
    card = _card(db)
    row = _row(
        db,
        user.id,
        card,
        finish="foil",
        storage_location_id=loc.id,
        drawer="1",
        slot="5",
        tags="commander-staple",
        role="commander",
        notes="signed",
    )
    row_id, created = row.id, row.created_at

    correct_inventory_row_finish(db, row_id=row_id, user_id=user.id, new_finish="etched")
    db.refresh(row)

    assert row.id == row_id  # same row, not re-created
    assert row.finish == "etched"
    assert row.storage_location_id == loc.id
    assert row.drawer == "1" and row.slot == "5"
    assert row.tags == "commander-staple"
    assert row.role == "commander"
    assert row.notes == "signed"
    assert row.is_pending is False
    assert row.created_at == created
    assert row.updated_at >= created


def test_references_still_point_at_row(db, user):
    card = _card(db)
    row = _row(db, user.id, card, finish="foil")
    sc_item = ShowcaseItem(showcase_id=1, inventory_row_id=row.id, quantity_offered=1)
    tr_item = TradeItem(
        trade_id=1, side="offer", inventory_row_id=row.id, quantity=1, finish="foil"
    )
    # FK enforcement is off in the default `db` fixture, so placeholder deck/group
    # ids are fine — we only assert the inventory_row_id ref is left intact.
    dcs = DeckCardShare(
        inventory_row_id=row.id, source_deck_id=1, target_deck_id=2, variant_group_id=1
    )
    db.add_all([sc_item, tr_item, dcs])
    db.commit()

    correct_inventory_row_finish(db, row_id=row.id, user_id=user.id, new_finish="etched")

    assert db.get(ShowcaseItem, sc_item.id).inventory_row_id == row.id
    assert db.get(TradeItem, tr_item.id).inventory_row_id == row.id
    assert db.get(DeckCardShare, dcs.id).inventory_row_id == row.id


def test_price_resolves_to_etched_after_correction(db, user):
    # Raised by Giants clb 520: foil column empty, etched priced. foil → no price;
    # etched → resolves. effective_price reads the finish-matching Card column.
    from app.pricing import effective_price

    card = _card(db, price_usd_etched="3.50")
    row = _row(db, user.id, card, finish="foil")
    assert effective_price(card, row.finish) == 0.0  # foil has no price

    correct_inventory_row_finish(db, row_id=row.id, user_id=user.id, new_finish="etched")
    db.refresh(row)
    assert effective_price(card, row.finish) == 3.50


def test_writes_exactly_one_correction_event(db, user):
    card = _card(db)
    row = _row(db, user.id, card, finish="foil")
    correct_inventory_row_finish(db, row_id=row.id, user_id=user.id, new_finish="etched")

    logs = db.query(TransactionLog).filter(TransactionLog.inventory_row_id == row.id).all()
    assert len(logs) == 1
    assert logs[0].event_type == "correct_finish"
    assert logs[0].quantity_delta == 0


def test_invalid_finish_rejected(db, user):
    card = _card(db)
    row = _row(db, user.id, card, finish="foil")
    with pytest.raises(ValueError):
        correct_inventory_row_finish(db, row_id=row.id, user_id=user.id, new_finish="prerelease")
    db.refresh(row)
    assert row.finish == "foil"


def test_owner_only(db, user):
    card = _card(db)
    row = _row(db, user.id, card, finish="foil")
    with pytest.raises(ValueError, match="not found"):
        correct_inventory_row_finish(db, row_id=row.id, user_id=user.id + 999, new_finish="etched")


def test_route_corrects_finish(client, db, user):
    card = _card(db)
    row = _row(db, user.id, card, finish="foil")
    resp = client.post(
        f"/inventory/rows/{row.id}/correct-finish",
        data={"finish": "etched", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(row)
    assert row.finish == "etched"


def test_route_invalid_finish_is_400(client, db, user):
    card = _card(db)
    row = _row(db, user.id, card, finish="foil")
    resp = client.post(
        f"/inventory/rows/{row.id}/correct-finish",
        data={"finish": "bogus", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_route_other_users_row_is_404(client, db, user):
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    card = _card(db)
    row = _row(db, other.id, card, finish="foil")  # owned by someone else
    resp = client.post(
        f"/inventory/rows/{row.id}/correct-finish",
        data={"finish": "etched", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
