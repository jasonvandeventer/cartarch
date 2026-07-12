"""Scoped audits — audit a subset of a location by set (issue #73 follow-up).

Null scope = today's full-audit behavior (regression-pinned here); scoped audits
narrow the expected set/snapshot to chosen sets and surface out-of-scope scans as
an acknowledged, action-less disposition.
"""

from __future__ import annotations

import itertools
import json

import pytest

from app import audit_service
from app.models import AuditLog, InventoryRow, StorageLocation

_seq = itertools.count(1)


def _card(db, *, name="Some Card", set_code="LTR", collector="1"):
    from app.models import Card

    c = Card(
        scryfall_id=f"sid-scope-{next(_seq)}",
        name=name,
        set_code=set_code,
        set_name=f"{set_code.upper()} Set",
        collector_number=collector,
        rarity="common",
        type_line="Creature",
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


def _loc(db, user_id, name="Drawer 3", type_="box"):
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    db.add(loc)
    db.flush()
    return loc


def _row(db, user_id, card, loc_id, *, qty=1, finish="normal"):
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


def _mixed_location(db, user):
    """A location holding LTR, MH3, and NEO cards (2+1+1 copies)."""
    loc = _loc(db, user.id)
    ltr = _card(db, name="Lightning Bolt", set_code="LTR", collector="10")
    mh3 = _card(db, name="Sol Ring", set_code="MH3", collector="20")
    neo = _card(db, name="Boseiju", set_code="NEO", collector="30")
    rows = {
        "ltr": _row(db, user.id, ltr, loc.id, qty=2),
        "mh3": _row(db, user.id, mh3, loc.id, qty=1),
        "neo": _row(db, user.id, neo, loc.id, qty=1),
    }
    return loc, {"ltr": ltr, "mh3": mh3, "neo": neo}, rows


# -- start / snapshot ----------------------------------------------------------


def test_scoped_start_expected_only_in_scope_and_hash_differs(db, user):
    loc, cards, rows = _mixed_location(db, user)

    full_audit, full_expected = audit_service.start_audit(db, user.id, loc.id)
    full_hash = full_audit.snapshot_hash
    assert len(full_expected) == 3 and full_audit.scope is None
    audit_service.abandon_audit(db, full_audit.id, user.id)

    audit, expected = audit_service.start_audit(db, user.id, loc.id, set_codes=["ltr", "MH3"])
    assert {r.card.set_code.upper() for r in expected} == {"LTR", "MH3"}  # NEO excluded
    assert len(expected) == 2
    assert json.loads(audit.scope) == {"set_codes": ["LTR", "MH3"]}
    assert audit.snapshot_hash != full_hash  # snapshot covers only scoped rows


def test_scoped_start_empty_result_rejected(db, user):
    loc, _cards, _rows = _mixed_location(db, user)
    with pytest.raises(ValueError, match="No cards from those sets"):
        audit_service.start_audit(db, user.id, loc.id, set_codes=["ZZZ"])


def test_list_location_sets_counts(db, user):
    loc, _cards, _rows = _mixed_location(db, user)
    sets = audit_service.list_location_sets(db, user.id, loc.id)
    by_code = {s["set_code"]: s for s in sets}
    assert by_code["LTR"]["card_count"] == 2
    assert by_code["MH3"]["card_count"] == 1
    assert by_code["NEO"]["card_count"] == 1
    assert [s["set_code"] for s in sets][0] == "LTR"  # sorted by count desc
    assert by_code["LTR"]["set_name"] == "LTR Set"


# -- validate_snapshot ---------------------------------------------------------


def test_unscoped_change_does_not_trip_validate_but_inscope_does(db, user):
    loc, cards, rows = _mixed_location(db, user)
    audit, _ = audit_service.start_audit(db, user.id, loc.id, set_codes=["LTR"])

    # Change an OUT-of-scope row (NEO) — must NOT invalidate the scoped audit.
    rows["neo"].quantity = 99
    db.flush()
    ok, changes = audit_service.validate_snapshot(db, audit.id)
    assert ok and changes == []

    # Change an IN-scope row (LTR) — must invalidate.
    rows["ltr"].quantity = 5
    db.flush()
    ok, changes = audit_service.validate_snapshot(db, audit.id)
    assert not ok and changes


# -- record_scan ---------------------------------------------------------------


def test_record_scan_out_of_scope_including_same_name_edge(db, user):
    loc = _loc(db, user.id)
    bolt_ltr = _card(db, name="Lightning Bolt", set_code="LTR", collector="10")
    bolt_neo = _card(db, name="Lightning Bolt", set_code="NEO", collector="11")  # same name!
    sol_mh3 = _card(db, name="Sol Ring", set_code="MH3", collector="20")
    _row(db, user.id, bolt_ltr, loc.id, qty=1)
    _row(db, user.id, sol_mh3, loc.id, qty=1)

    audit, _ = audit_service.start_audit(db, user.id, loc.id, set_codes=["LTR"])

    # Edge: an in-scope printing (LTR Bolt) is expected, but the scanned printing
    # is NEO → the scanned card's own set decides → out_of_scope.
    r = audit_service.record_scan(db, audit.id, user.id, card_id=bolt_neo.id, finish="normal")
    assert r.scan_type == "out_of_scope"
    assert "Out of scope" in r.message

    # A different set entirely (MH3, though present at the location) → out_of_scope.
    r = audit_service.record_scan(db, audit.id, user.id, card_id=sol_mh3.id, finish="normal")
    assert r.scan_type == "out_of_scope"

    # In-scope printing → normal match (unchanged behavior).
    r = audit_service.record_scan(db, audit.id, user.id, card_id=bolt_ltr.id, finish="normal")
    assert r.scan_type == "match"


# -- compute_delta / reconcile -------------------------------------------------


def test_compute_delta_out_of_scope_listed_and_universe_is_scoped(db, user):
    loc, cards, rows = _mixed_location(db, user)
    extra_ltr = _card(
        db, name="Fetchland", set_code="LTR", collector="99"
    )  # in scope, not owned here
    audit, _ = audit_service.start_audit(db, user.id, loc.id, set_codes=["LTR"])

    # NEO scan → out_of_scope; an in-scope card not expected here → extra.
    audit_service.record_scan(db, audit.id, user.id, card_id=cards["neo"].id, finish="normal")
    audit_service.record_scan(db, audit.id, user.id, card_id=extra_ltr.id, finish="normal")
    # Leave the expected LTR Bolt (qty 2) entirely unscanned → missing.

    delta = audit_service.compute_delta(db, audit.id, user.id)

    # Missing/extras computed over the SCOPED universe only (no MH3/NEO expected).
    assert [m["set_code"] for m in delta.missing] == ["LTR"]
    assert delta.missing[0]["quantity_missing"] == 2
    assert {e["set_code"] for e in delta.extras} == {"LTR"}  # the unowned LTR card
    # Out-of-scope surfaced separately, with no proposed action.
    assert len(delta.out_of_scope) == 1
    assert delta.out_of_scope[0]["set_code"] == "NEO"


def test_apply_reconciliation_unaffected_by_out_of_scope(db, user):
    loc, cards, rows = _mixed_location(db, user)
    audit, _ = audit_service.start_audit(db, user.id, loc.id, set_codes=["LTR"])
    audit_service.record_scan(db, audit.id, user.id, card_id=cards["neo"].id, finish="normal")

    rows_before = db.query(InventoryRow).filter_by(storage_location_id=loc.id).count()
    result = audit_service.apply_reconciliation(db, audit.id, user.id, actions=[])
    assert result.success
    # The out-of-scope scan created no action and no inventory change.
    assert db.query(InventoryRow).filter_by(storage_location_id=loc.id).count() == rows_before


# -- audit_log scope + history -------------------------------------------------


def test_audit_log_carries_scope_and_history_renders_badge(db, user):
    loc, cards, rows = _mixed_location(db, user)
    audit, _ = audit_service.start_audit(db, user.id, loc.id, set_codes=["LTR", "MH3"])
    audit_service.apply_reconciliation(db, audit.id, user.id, actions=[])

    log = db.query(AuditLog).filter_by(audit_session_id=audit.id).one()
    assert json.loads(log.scope) == {"set_codes": ["LTR", "MH3"]}

    history = audit_service.list_all_audit_history(db, user.id)
    assert history[0]["scope_sets"] == ["LTR", "MH3"]


# -- staleness (last FULL audit) -----------------------------------------------


def test_scoped_audit_does_not_reset_full_staleness(db, user):
    loc, cards, rows = _mixed_location(db, user)
    audit, _ = audit_service.start_audit(db, user.id, loc.id, set_codes=["LTR"])
    audit_service.apply_reconciliation(db, audit.id, user.id, actions=[])

    entry = next(
        d for d in audit_service.list_auditable_locations(db, user.id) if d["location_id"] == loc.id
    )
    assert entry["last_audited_at"] is None  # still "Never" for a FULL audit
    assert entry["last_scoped_audit_at"] is not None


# -- full-audit regression pin -------------------------------------------------


def test_full_audit_scope_null_is_unchanged(db, user):
    loc, cards, rows = _mixed_location(db, user)

    audit, expected = audit_service.start_audit(db, user.id, loc.id)  # no set_codes
    assert audit.scope is None
    assert audit.snapshot_hash == audit_service._snapshot_hash(
        audit_service._location_rows(db, user.id, loc.id)
    )
    assert len(expected) == 3  # every row

    audit_service.record_scan(db, audit.id, user.id, card_id=cards["neo"].id, finish="normal")
    delta = audit_service.compute_delta(db, audit.id, user.id)
    assert delta.out_of_scope == []  # no scope → never out_of_scope

    audit_service.apply_reconciliation(db, audit.id, user.id, actions=[])
    log = db.query(AuditLog).filter_by(audit_session_id=audit.id).one()
    assert log.scope is None
    entry = next(
        d for d in audit_service.list_auditable_locations(db, user.id) if d["location_id"] == loc.id
    )
    assert entry["last_audited_at"] is not None  # full audit resets staleness
