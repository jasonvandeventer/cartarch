"""Physical Audit Mode — Phase 4 (issue #73): thumbnails, resume banner,
audit history, and lazy 24h session timeout."""

from __future__ import annotations

import itertools

from app import audit_service
from app.models import AuditSession, Card, InventoryRow, StorageLocation
from app.timeutil import utc_now

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


# -- 7a: scryfall_id threaded through the delta --------------------------------


def test_compute_delta_includes_scryfall_id_everywhere(db, user):
    loc = _loc(db, user.id)
    seen_c = _card(db, name="Sol Ring")
    _row(db, user.id, seen_c, loc.id, qty=1)
    missing_c = _card(db, name="Mana Crypt")
    _row(db, user.id, missing_c, loc.id, qty=1)
    exp_bolt = _card(db, name="Lightning Bolt", set_code="mkc")
    _row(db, user.id, exp_bolt, loc.id, qty=1)
    scanned_bolt = _card(db, name="Lightning Bolt", set_code="fdn")
    stranger = _card(db, name="Black Lotus")
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.record_scan(db, audit.id, user.id, seen_c.id, "normal", 1)  # seen
    audit_service.record_scan(db, audit.id, user.id, scanned_bolt.id, "normal", 1)  # partial
    audit_service.record_scan(db, audit.id, user.id, stranger.id, "normal", 1)  # extra

    delta = audit_service.compute_delta(db, audit.id, user.id)
    assert all(d["scryfall_id"] for d in delta.seen)
    assert all(d["scryfall_id"] for d in delta.missing)
    assert all(e["scryfall_id"] for e in delta.extras)
    for p in delta.partial_matches:
        assert p["expected_scryfall_id"] and p["scanned_scryfall_id"]
    # And the scan feedback carries it too.
    r = audit_service.record_scan(db, audit.id, user.id, seen_c.id, "normal", 1)
    assert r.scryfall_id == seen_c.scryfall_id


# -- 7b: resume banner ---------------------------------------------------------


def test_resume_banner_data_source_get_active_audit(db, user):
    # render()'s _active_audit_banner_for uses the global SessionLocal (untestable
    # against the temp engine, same as pending_count), so we test its data source —
    # get_active_audit — plus the partial's rendering separately below.
    loc = _loc(db, user.id, name="Drawer 3")
    _row(db, user.id, _card(db), loc.id, qty=1)
    assert audit_service.get_active_audit(db, user.id) is None  # none yet → no banner

    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.pause_audit(db, audit.id, user.id)
    active = audit_service.get_active_audit(db, user.id)
    assert active is not None and active.storage_location_id == loc.id


def test_resume_banner_partial_renders():
    from app.dependencies import templates

    tmpl = templates.get_template("_audit_resume_banner.html")
    banner = {"audit_id": 7, "location_id": 3, "location_name": "Drawer 3", "status": "paused"}
    html = tmpl.render(audit_banner=banner, csrf_token="x")
    assert "audit-resume-banner" in html
    assert "Drawer 3" in html and "paused" in html
    assert "/audit/start?location_id=3" in html and "/audit/7/abandon" in html
    # No active audit → nothing rendered.
    assert tmpl.render(audit_banner=None, csrf_token="x").strip() == ""


# -- 7c: audit history on the location detail page -----------------------------


def test_location_detail_shows_audit_history(db, client, user):
    loc = _loc(db, user.id)
    card = _card(db, name="Sol Ring")
    _row(db, user.id, card, loc.id, qty=1)
    db.commit()
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.apply_reconciliation(
        db,
        audit.id,
        user.id,
        [
            {
                "type": "mark_missing",
                "inventory_row_id": db.query(InventoryRow).filter_by(card_id=card.id).one().id,
                "quantity": 1,
            }
        ],
    )
    db.commit()

    page = client.get(f"/locations/{loc.id}").text
    assert "Audit history" in page
    assert "Last audited" in page
    # counts row present: expected 1, seen 0, missing 1, 1 action applied
    history = audit_service.list_audit_history(db, user.id, loc.id)
    assert len(history) == 1 and history[0].cards_missing == 1


# -- 7d: lazy 24h session timeout ---------------------------------------------


def test_session_timeout_auto_abandons_after_24h(db, user):
    from datetime import timedelta

    loc = _loc(db, user.id)
    _row(db, user.id, _card(db), loc.id, qty=1)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit.started_at = utc_now() - timedelta(hours=25)
    db.flush()

    assert audit_service.get_active_audit(db, user.id) is None
    assert db.get(AuditSession, audit.id).status == "abandoned"
    # auto-abandon is logged
    from app.models import TransactionLog

    assert db.query(TransactionLog).filter_by(event_type="audit_auto_abandoned").count() == 1


def test_session_within_24h_not_abandoned(db, user):
    from datetime import timedelta

    loc = _loc(db, user.id)
    _row(db, user.id, _card(db), loc.id, qty=1)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit.started_at = utc_now() - timedelta(hours=23)
    db.flush()

    active = audit_service.get_active_audit(db, user.id)
    assert active is not None and active.id == audit.id
    assert db.get(AuditSession, audit.id).status == "active"
