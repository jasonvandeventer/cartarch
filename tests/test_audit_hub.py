"""Audit hub + import-history relocation (nav split).

Note: the old import-history URL was /audit, which is now REPURPOSED as the
Physical Audit Mode hub — the same URL can't both redirect and render, so there
is no 301 (a /audit bookmark now lands on the hub). Import history moved to
/imports/history and is reachable via links on the import surfaces.
"""

from __future__ import annotations

import itertools
from datetime import timedelta

from app import audit_service
from app.models import AuditLog, Card, InventoryRow, StorageLocation, User
from app.timeutil import utc_now

_seq = itertools.count(1)


def _user(db, name=None) -> User:
    u = User(username=(name or f"h{next(_seq)}@ex.com"), password_hash="x")
    db.add(u)
    db.flush()
    return u


def _card(db) -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=f"Card {next(_seq)}",
        set_code="tst",
        set_name="Test",
        collector_number=str(next(_seq)),
        rarity="common",
        type_line="Creature",
        oracle_text="x",
        image_url="http://x/i.png",
        color_identity="",
        set_type="expansion",
        price_usd="0.1",
        price_usd_foil=None,
    )
    db.add(c)
    db.flush()
    return c


def _loc(db, user_id, name="Box", type_="box") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    db.add(loc)
    db.flush()
    return loc


def _row(db, user_id, loc_id, qty) -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=_card(db).id,
        finish="normal",
        quantity=qty,
        storage_location_id=loc_id,
        is_pending=False,
    )
    db.add(row)
    db.flush()
    return row


def _log(db, user_id, loc_id, *, seen=0, missing=0, extras=0, expected=0, when, actions="[]"):
    log = AuditLog(
        audit_session_id=1,
        user_id=user_id,
        storage_location_id=loc_id,
        cards_expected=expected,
        cards_seen=seen,
        cards_missing=missing,
        cards_extra=extras,
        actions_applied=actions,
        completed_at=when,
    )
    db.add(log)
    db.flush()
    return log


# ── C1: import-history relocation ────────────────────────────────────────────


def test_imports_history_serves_the_old_content(db, client, user):
    r = client.get("/imports/history")
    assert r.status_code == 200
    assert "Import History" in r.text
    assert "Import Batches" in r.text and "Transaction Log" in r.text


def test_audit_url_is_now_the_hub_not_import_history(db, client, user):
    html = client.get("/audit").text
    assert "Physical Audit" in html  # the hub
    assert "Import Batches" not in html  # not the old import-history page


def test_import_page_links_to_history(db, client, user):
    _loc(db, user.id)  # avoids the "need a location" warning path
    db.commit()
    assert 'href="/imports/history"' in client.get("/import").text


def test_import_result_template_links_to_history():
    # The result page is only reachable via the multi-step commit flow; assert the
    # link at the template level.
    src = open("app/templates/import_result.html").read()
    assert "/imports/history" in src


def test_import_history_not_a_nav_item(db, client, user):
    # The nav's Audit item points to the hub, not import history.
    nav = client.get("/audit").text
    assert 'href="/imports/history"' not in nav
    assert 'href="/audit"' in nav  # Audit nav entry present (now ungated)


# ── C2: list_auditable_locations ─────────────────────────────────────────────


def test_auditable_locations_counts_and_ordering(db, user):
    now = utc_now()
    big = _loc(db, user.id, name="Big never")
    _row(db, user.id, big.id, 5)
    _row(db, user.id, big.id, 3)  # card_count 8, never audited
    small = _loc(db, user.id, name="Small never")
    _row(db, user.id, small.id, 1)  # card_count 1, never audited
    stale = _loc(db, user.id, name="Stale")
    _row(db, user.id, stale.id, 2)
    _log(
        db,
        user.id,
        stale.id,
        seen=2,
        missing=0,
        extras=1,
        expected=2,
        when=now - timedelta(days=30),
    )
    fresh = _loc(db, user.id, name="Fresh")
    _row(db, user.id, fresh.id, 2)
    _log(
        db, user.id, fresh.id, seen=2, missing=0, extras=0, expected=2, when=now - timedelta(days=1)
    )

    out = audit_service.list_auditable_locations(db, user.id)
    names = [d["name"] for d in out]
    # Never-audited first, biggest pile first; then audited stalest-first.
    assert names == ["Big never", "Small never", "Stale", "Fresh"]
    by_name = {d["name"]: d for d in out}
    assert by_name["Big never"]["card_count"] == 8
    assert by_name["Big never"]["last_audited_at"] is None
    assert by_name["Big never"]["last_audit_summary"] is None
    assert by_name["Stale"]["last_audit_summary"] == {"seen": 2, "missing": 0, "extras": 1}


def test_auditable_locations_uses_most_recent_audit(db, user):
    loc = _loc(db, user.id, name="Loc")
    _row(db, user.id, loc.id, 3)
    now = utc_now()
    _log(db, user.id, loc.id, seen=1, missing=2, extras=0, expected=3, when=now - timedelta(days=5))
    _log(db, user.id, loc.id, seen=3, missing=0, extras=1, expected=3, when=now - timedelta(days=1))
    out = {d["name"]: d for d in audit_service.list_auditable_locations(db, user.id)}
    # The most recent (yesterday) audit wins.
    assert out["Loc"]["last_audit_summary"] == {"seen": 3, "missing": 0, "extras": 1}


# ── C3: list_all_audit_history ───────────────────────────────────────────────


def test_all_audit_history_cross_location_recent_first_limited(db, user):
    now = utc_now()
    a = _loc(db, user.id, name="A")
    b = _loc(db, user.id, name="B")
    _log(db, user.id, a.id, seen=1, when=now - timedelta(days=3))
    _log(db, user.id, b.id, seen=2, when=now - timedelta(days=1))
    _log(
        db, user.id, a.id, seen=3, when=now - timedelta(days=2), actions='[{"type":"mark_missing"}]'
    )

    hist = audit_service.list_all_audit_history(db, user.id, limit=25)
    assert [h["location_name"] for h in hist] == ["B", "A", "A"]  # most recent first
    assert hist[1]["actions_count"] == 1  # the middle entry had 1 action

    assert len(audit_service.list_all_audit_history(db, user.id, limit=2)) == 2


# ── C4: GET /audit hub rendering ─────────────────────────────────────────────


def test_hub_no_active_audit(db, client, user):
    loc = _loc(db, user.id, name="Shelf")
    _row(db, user.id, loc.id, 4)
    db.commit()
    html = client.get("/audit").text
    assert "Audit in progress" not in html  # no in-progress section
    assert "Shelf" in html  # location listed
    assert "No completed audits yet." in html  # empty history


def test_hub_with_paused_audit_and_history(db, client, user):
    loc = _loc(db, user.id, name="Drawer 1")
    _row(db, user.id, loc.id, 2)
    _log(db, user.id, loc.id, seen=2, missing=0, extras=0, expected=2, when=utc_now())
    db.commit()
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.pause_audit(db, audit.id, user.id)
    db.commit()

    html = client.get("/audit").text
    assert "Audit in progress" in html and "Drawer 1" in html
    assert "Resume" in html and "Abandon" in html
    assert "Recent audits" in html and "No completed audits yet." not in html
