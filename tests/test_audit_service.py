"""Physical Audit Mode service layer (issue #73, Phase 1).

Covers session lifecycle, single-active enforcement, snapshot-hash determinism,
expected-list sort order, and owner authorization. No routes/reconciliation —
the changeset math is Phase 2.
"""

from __future__ import annotations

import itertools

import pytest

from app import audit_service
from app.models import AuditLog, AuditScan, Card, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _card(db, *, name="Some Card", set_code="aaa", collector="1") -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code=set_code,
        set_name="Test",
        collector_number=collector,
        rarity="common",
        type_line="Creature — Goblin",
        oracle_text="x",
        image_url="http://x/img.png",
        color_identity="",
        set_type="expansion",
        price_usd="0.10",
        price_usd_foil=None,
    )
    db.add(c)
    db.flush()
    return c


def _loc(db, user_id, name="Loc", type_="box") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    db.add(loc)
    db.flush()
    return loc


def _row(db, user_id, card, loc_id, *, qty=1, finish="normal") -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        finish=finish,
        quantity=qty,
        storage_location_id=loc_id,
        is_pending=False,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def user2(db):
    u = User(username="other@example.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


# -- Lifecycle ----------------------------------------------------------------


def test_lifecycle_start_pause_resume_complete(db, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db, name="Sol Ring"), loc.id, qty=3)

    audit, expected = audit_service.start_audit(db, user.id, loc.id)
    assert audit.status == "active"
    assert audit.started_at is not None
    assert len(expected) == 1

    paused = audit_service.pause_audit(db, audit.id, user.id)
    assert paused.status == "paused"
    assert paused.paused_at is not None

    resumed, progress = audit_service.resume_audit(db, audit.id, user.id)
    assert resumed.status == "active"
    assert resumed.paused_at is None
    assert progress["seen"] == 0

    done, log = audit_service.complete_audit(
        db,
        audit.id,
        user.id,
        cards_expected=3,
        cards_seen=2,
        cards_missing=1,
        cards_extra=0,
        actions_applied=[{"action": "mark_missing", "row_id": 1}],
    )
    assert done.status == "completed"
    assert done.completed_at is not None
    assert isinstance(log, AuditLog)
    assert log.cards_missing == 1
    assert db.query(AuditLog).count() == 1


def test_lifecycle_start_abandon_deletes_scans(db, user):
    loc = _loc(db, user.id)
    card = _row(db, user.id, _card(db), loc.id).card
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    db.add(
        AuditScan(
            audit_session_id=audit.id,
            card_id=card.id,
            finish="normal",
            scan_type="match",
            quantity_scanned=1,
        )
    )
    db.flush()
    assert db.query(AuditScan).filter_by(audit_session_id=audit.id).count() == 1

    abandoned = audit_service.abandon_audit(db, audit.id, user.id)
    assert abandoned.status == "abandoned"
    assert db.query(AuditScan).filter_by(audit_session_id=audit.id).count() == 0


# -- Single active enforcement ------------------------------------------------


def test_start_while_paused_raises(db, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db), loc.id)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.pause_audit(db, audit.id, user.id)

    with pytest.raises(ValueError):
        audit_service.start_audit(db, user.id, loc.id)

    # get_active_audit still surfaces the paused one.
    assert audit_service.get_active_audit(db, user.id).id == audit.id


# -- Snapshot hash ------------------------------------------------------------


def test_snapshot_hash_deterministic(db, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db), loc.id, qty=2)

    a1, _ = audit_service.start_audit(db, user.id, loc.id)
    h1 = a1.snapshot_hash
    audit_service.abandon_audit(db, a1.id, user.id)

    a2, _ = audit_service.start_audit(db, user.id, loc.id)
    assert a2.snapshot_hash == h1


def test_snapshot_hash_changes_with_inventory(db, user):
    loc = _loc(db, user.id)
    row = _row(db, user.id, _card(db), loc.id, qty=2)

    a1, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.abandon_audit(db, a1.id, user.id)

    row.quantity = 5
    db.flush()

    a2, _ = audit_service.start_audit(db, user.id, loc.id)
    assert a2.snapshot_hash != a1.snapshot_hash


# -- Expected-list sort order -------------------------------------------------


def test_expected_order_drawer_vs_alphabetical(db, user):
    # Card names sort opposite to their set/collector (drawer-sorter) order, so
    # the two orderings are distinguishable.
    zebra = _card(db, name="Zebra", set_code="aaa", collector="1")
    apple = _card(db, name="Apple", set_code="aaa", collector="2")

    drawer = _loc(db, user.id, name="Drawer 3", type_="drawer")
    _row(db, user.id, zebra, drawer.id)
    _row(db, user.id, apple, drawer.id)
    audit, expected = audit_service.start_audit(db, user.id, drawer.id)
    # Drawer-sorter order = by collector number → Zebra (1) before Apple (2).
    assert [r.card.name for r in expected] == ["Zebra", "Apple"]
    audit_service.abandon_audit(db, audit.id, user.id)  # free the single active slot

    box = _loc(db, user.id, name="Box A", type_="box")
    _row(db, user.id, zebra, box.id)
    _row(db, user.id, apple, box.id)
    _, expected_box = audit_service.start_audit(db, user.id, box.id)
    # Non-drawer → alphabetical by name.
    assert [r.card.name for r in expected_box] == ["Apple", "Zebra"]


# -- Owner authorization ------------------------------------------------------


def test_cannot_touch_another_users_audit(db, user, user2):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db), loc.id)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    for op in (audit_service.pause_audit, audit_service.resume_audit, audit_service.abandon_audit):
        with pytest.raises(PermissionError):
            op(db, audit.id, user2.id)
