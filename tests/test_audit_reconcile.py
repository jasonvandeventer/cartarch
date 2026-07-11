"""Physical Audit Mode — reconciliation (issue #73, Phase 3).

compute_delta / validate_snapshot / apply_reconciliation, plus the reconcile
and complete routes.
"""

from __future__ import annotations

import itertools

from app import audit_service
from app.models import (
    AuditLog,
    AuditSession,
    Card,
    InventoryRow,
    StorageLocation,
    TransactionLog,
    User,
)

_seq = itertools.count(1)


def _card(db, *, name="Some Card", set_code="aaa") -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code=set_code,
        set_name="Test",
        collector_number=str(next(_seq)),
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


def _loc(db, user_id, name="Box A", type_="box") -> StorageLocation:
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


# -- compute_delta -------------------------------------------------------------


def test_compute_delta_classifies_seen_missing_extra_partial(db, user):
    loc = _loc(db, user.id)
    seen_card = _card(db, name="Sol Ring")
    _row(db, user.id, seen_card, loc.id, qty=1)
    missing_card = _card(db, name="Mana Crypt")
    _row(db, user.id, missing_card, loc.id, qty=2)
    partial_expected = _card(db, name="Lightning Bolt", set_code="mkc")
    _row(db, user.id, partial_expected, loc.id, qty=1)
    partial_scanned = _card(db, name="Lightning Bolt", set_code="fdn")
    stranger = _card(db, name="Black Lotus")
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    audit_service.record_scan(db, audit.id, user.id, seen_card.id, "normal", 1)  # match
    audit_service.record_scan(db, audit.id, user.id, missing_card.id, "normal", 1)  # 1 of 2
    audit_service.record_scan(db, audit.id, user.id, partial_scanned.id, "normal", 1)  # partial
    audit_service.record_scan(db, audit.id, user.id, stranger.id, "normal", 1)  # extra

    delta = audit_service.compute_delta(db, audit.id, user.id)
    assert [d["card_name"] for d in delta.seen] == ["Sol Ring"]
    missing = {d["card_name"]: d for d in delta.missing}
    assert missing["Mana Crypt"]["quantity_missing"] == 1
    assert "Lightning Bolt" in missing  # 0 exact matches → still missing the printing
    assert len(delta.extras) == 1 and delta.extras[0]["card_name"] == "Black Lotus"
    assert len(delta.partial_matches) == 1
    pm = delta.partial_matches[0]
    assert pm["expected_set"] == "MKC" and pm["scanned_set"] == "FDN"
    assert pm["scanned_card_id"] == partial_scanned.id


def test_compute_delta_extra_exists_elsewhere(db, user):
    loc = _loc(db, user.id)
    elsewhere = _loc(db, user.id, name="Box B")
    stranger = _card(db, name="Ponder")
    _row(db, user.id, stranger, elsewhere.id, qty=1)  # owned in another location
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.record_scan(db, audit.id, user.id, stranger.id, "normal", 1)

    delta = audit_service.compute_delta(db, audit.id, user.id)
    extra = delta.extras[0]
    assert extra["exists_elsewhere"] is True
    assert extra["sources"][0]["location_name"] == "Box B"


# -- validate_snapshot ---------------------------------------------------------


def test_validate_snapshot_unchanged(db, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db), loc.id, qty=1)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    ok, changes = audit_service.validate_snapshot(db, audit.id)
    assert ok is True and changes == []


def test_validate_snapshot_detects_quantity_add_remove(db, user):
    loc = _loc(db, user.id)
    row = _row(db, user.id, _card(db, name="Forest", set_code="fdn"), loc.id, qty=1)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    row.quantity = 2
    added = _row(db, user.id, _card(db, name="Island", set_code="fdn"), loc.id, qty=1)  # noqa: F841
    db.flush()

    ok, changes = audit_service.validate_snapshot(db, audit.id)
    assert ok is False
    joined = " | ".join(changes)
    assert "quantity changed from 1 to 2" in joined
    assert "New card added: Island (FDN)" in joined


# -- apply_reconciliation ------------------------------------------------------


def test_apply_rejected_on_snapshot_conflict(db, user):
    loc = _loc(db, user.id)
    row = _row(db, user.id, _card(db), loc.id, qty=1)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    row.quantity = 9  # inventory changed mid-audit
    db.flush()

    result = audit_service.apply_reconciliation(db, audit.id, user.id, [])
    assert result.success is False
    assert result.snapshot_conflict is True
    assert result.changes
    assert db.get(AuditSession, audit.id).status != "completed"  # not applied


def test_apply_mark_missing_logs_without_mutation(db, user):
    loc = _loc(db, user.id)
    row = _row(db, user.id, _card(db, name="Sol Ring"), loc.id, qty=2)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    result = audit_service.apply_reconciliation(
        db,
        audit.id,
        user.id,
        [{"type": "mark_missing", "inventory_row_id": row.id, "quantity": 2}],
    )
    assert result.success and result.actions_applied == 1
    assert db.get(InventoryRow, row.id).quantity == 2  # no mutation
    logs = db.query(TransactionLog).filter_by(event_type="audit_missing").all()
    assert len(logs) == 1 and "Sol Ring" in logs[0].note
    completed = db.get(AuditSession, audit.id)
    assert completed.status == "completed"
    assert db.query(AuditLog).filter_by(audit_session_id=audit.id).count() == 1


def test_apply_add_extra_creates_row(db, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db, name="Anchor"), loc.id, qty=1)  # keeps location non-empty
    extra_card = _card(db, name="Brainstorm")
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    result = audit_service.apply_reconciliation(
        db,
        audit.id,
        user.id,
        [{"type": "add_extra", "card_id": extra_card.id, "finish": "normal", "quantity": 3}],
    )
    assert result.success and result.actions_applied == 1
    new_row = (
        db.query(InventoryRow).filter_by(card_id=extra_card.id, storage_location_id=loc.id).one()
    )
    assert new_row.quantity == 3
    assert db.query(TransactionLog).filter_by(event_type="audit_extra_added").count() == 1


def test_apply_move_extra_here(db, user):
    dest = _loc(db, user.id, name="Drawer 1")
    source = _loc(db, user.id, name="Box B")
    _row(db, user.id, _card(db, name="Anchor"), dest.id, qty=1)
    moving_card = _card(db, name="Counterspell")
    source_row = _row(db, user.id, moving_card, source.id, qty=1)
    audit, _ = audit_service.start_audit(db, user.id, dest.id)

    result = audit_service.apply_reconciliation(
        db,
        audit.id,
        user.id,
        [
            {
                "type": "move_extra_here",
                "card_id": moving_card.id,
                "finish": "normal",
                "inventory_row_id": source_row.id,
                "source_location_id": source.id,
            }
        ],
    )
    assert result.success
    assert db.get(InventoryRow, source_row.id).storage_location_id == dest.id
    assert db.query(TransactionLog).filter_by(event_type="audit_extra_moved").count() == 1


def test_apply_update_printing_changes_card_id(db, user):
    loc = _loc(db, user.id)
    expected = _card(db, name="Lightning Bolt", set_code="mkc")
    row = _row(db, user.id, expected, loc.id, qty=1)
    scanned = _card(db, name="Lightning Bolt", set_code="fdn")
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    result = audit_service.apply_reconciliation(
        db,
        audit.id,
        user.id,
        [{"type": "update_printing", "inventory_row_id": row.id, "card_id": scanned.id}],
    )
    assert result.success
    assert db.get(InventoryRow, row.id).card_id == scanned.id
    assert db.query(TransactionLog).filter_by(event_type="audit_printing_corrected").count() == 1


def test_apply_ignore_actions_are_noops(db, user):
    loc = _loc(db, user.id)
    row = _row(db, user.id, _card(db), loc.id, qty=1)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    result = audit_service.apply_reconciliation(
        db,
        audit.id,
        user.id,
        [{"type": "ignore_missing", "inventory_row_id": row.id}],
    )
    assert result.success and result.actions_applied == 0
    assert db.query(TransactionLog).count() == 0
    assert db.get(AuditSession, audit.id).status == "completed"  # still completes


# -- Routes --------------------------------------------------------------------


def test_route_reconcile_get_and_apply_flow(db, client, user):
    loc = _loc(db, user.id)
    row = _row(db, user.id, _card(db, name="Sol Ring"), loc.id, qty=1)
    db.commit()
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    db.commit()

    r = client.get(f"/audit/{audit.id}/reconcile")
    assert r.status_code == 200
    assert "Missing" in r.text  # the unseen Sol Ring row

    r = client.post(
        f"/audit/{audit.id}/reconcile",
        data={f"missing_action_{row.id}": "mark_missing"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/audit/{audit.id}/complete"
    db.expire_all()
    assert db.get(AuditSession, audit.id).status == "completed"

    r = client.get(f"/audit/{audit.id}/complete")
    assert r.status_code == 200
    assert "Audit complete" in r.text


def test_route_reconcile_apply_blocked_on_conflict(db, client, user):
    loc = _loc(db, user.id)
    row = _row(db, user.id, _card(db), loc.id, qty=1)
    db.commit()
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    db.commit()
    row.quantity = 5  # change inventory after the snapshot
    db.commit()

    r = client.post(
        f"/audit/{audit.id}/reconcile",
        data={f"missing_action_{row.id}": "mark_missing"},
        follow_redirects=False,
    )
    assert r.status_code == 200  # re-rendered, not redirected
    assert "changed during your audit" in r.text
    db.expire_all()
    assert db.get(AuditSession, audit.id).status != "completed"


def test_route_reconcile_renders_and_applies_move_and_printing(db, client, user):
    dest = _loc(db, user.id, name="Drawer 1")
    source = _loc(db, user.id, name="Box B")
    # A partial: expected MKC bolt, will scan FDN bolt.
    expected_bolt = _card(db, name="Lightning Bolt", set_code="mkc")
    bolt_row = _row(db, user.id, expected_bolt, dest.id, qty=1)
    scanned_bolt = _card(db, name="Lightning Bolt", set_code="fdn")
    # An extra owned elsewhere, so the move-source select renders.
    moving = _card(db, name="Counterspell")
    source_row = _row(db, user.id, moving, source.id, qty=1)
    db.commit()

    audit, _ = audit_service.start_audit(db, user.id, dest.id)
    audit_service.record_scan(db, audit.id, user.id, scanned_bolt.id, "normal", 1)  # partial
    audit_service.record_scan(db, audit.id, user.id, moving.id, "normal", 1)  # extra
    db.commit()

    r = client.get(f"/audit/{audit.id}/reconcile")
    assert r.status_code == 200
    assert "Partial matches" in r.text and "Extras" in r.text
    assert "Move here from" in r.text  # source select rendered

    r = client.post(
        f"/audit/{audit.id}/reconcile",
        data={
            f"partial_action_{bolt_row.id}_{scanned_bolt.id}": "update_printing",
            f"extra_action_{moving.id}_normal": "move_extra_here",
            f"extra_source_{moving.id}_normal": str(source_row.id),
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    assert db.get(InventoryRow, bolt_row.id).card_id == scanned_bolt.id  # printing corrected
    assert db.get(InventoryRow, source_row.id).storage_location_id == dest.id  # moved here


def test_route_reconcile_non_owner_404(db, client, user):
    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.flush()
    loc = _loc(db, other.id)
    audit = AuditSession(
        user_id=other.id,
        storage_location_id=loc.id,
        status="active",
        snapshot_hash="x",
        started_at=audit_service.utc_now(),
    )
    db.add(audit)
    db.commit()

    assert client.get(f"/audit/{audit.id}/reconcile").status_code == 404
    assert client.post(f"/audit/{audit.id}/reconcile", follow_redirects=False).status_code == 404
    assert client.get(f"/audit/{audit.id}/complete").status_code == 404
