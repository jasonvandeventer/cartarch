"""Physical Audit Mode — scan loop (issue #73, Phase 2).

record_scan classification, get_scan_progress accounting, and the /audit routes
(start / scan / pause / abandon, owner-scoped).
"""

from __future__ import annotations

import itertools

from app import audit_service
from app.models import AuditScan, AuditSession, Card, InventoryRow, StorageLocation, User

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


# -- record_scan classification ------------------------------------------------


def test_scan_expected_card_is_match(db, user):
    loc = _loc(db, user.id)
    card = _card(db, name="Sol Ring")
    _row(db, user.id, card, loc.id, qty=1)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    result = audit_service.record_scan(db, audit.id, user.id, card.id, "normal", 1)
    assert result.scan_type == "match"
    assert result.matched_row is not None
    assert "Sol Ring" in result.message


def test_scan_up_to_quantity_still_match_then_extra(db, user):
    loc = _loc(db, user.id)
    card = _card(db, name="Sol Ring")
    _row(db, user.id, card, loc.id, qty=2)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    assert (
        audit_service.record_scan(db, audit.id, user.id, card.id, "normal", 1).scan_type == "match"
    )
    assert (
        audit_service.record_scan(db, audit.id, user.id, card.id, "normal", 1).scan_type == "match"
    )
    # Third scan exceeds the expected 2 → extra.
    third = audit_service.record_scan(db, audit.id, user.id, card.id, "normal", 1)
    assert third.scan_type == "extra"
    assert third.matched_row is None


def test_scan_same_name_different_printing_is_partial(db, user):
    loc = _loc(db, user.id)
    expected = _card(db, name="Lightning Bolt", set_code="mkc")
    _row(db, user.id, expected, loc.id, qty=1)
    other_printing = _card(
        db, name="Lightning Bolt", set_code="fdn"
    )  # different card_id, same name
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    result = audit_service.record_scan(db, audit.id, user.id, other_printing.id, "normal", 1)
    assert result.scan_type == "partial_match"
    assert result.matched_row.id == db.query(InventoryRow).filter_by(card_id=expected.id).one().id
    assert "MKC" in result.message and "FDN" in result.message


def test_scan_unexpected_card_is_extra(db, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db, name="Sol Ring"), loc.id, qty=1)
    stranger = _card(db, name="Black Lotus")
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    result = audit_service.record_scan(db, audit.id, user.id, stranger.id, "normal", 1)
    assert result.scan_type == "extra"
    assert result.matched_row is None


# -- get_scan_progress ---------------------------------------------------------


def test_progress_fresh_audit_all_unseen(db, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db, name="A Card"), loc.id, qty=2)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    p = audit_service.get_scan_progress(db, audit.id, user.id)
    assert p.total_expected == 2
    assert p.total_seen == 0
    assert p.total_remaining == 2
    assert p.total_extras == 0
    assert p.expected_cards[0]["status"] == "unseen"


def test_progress_counts_and_statuses_after_scans(db, user):
    loc = _loc(db, user.id)
    partial_card = _card(db, name="Counterspell")
    _row(db, user.id, partial_card, loc.id, qty=3)  # one row, 3 copies
    complete_card = _card(db, name="Brainstorm")
    _row(db, user.id, complete_card, loc.id, qty=1)
    audit, _ = audit_service.start_audit(db, user.id, loc.id)

    audit_service.record_scan(db, audit.id, user.id, partial_card.id, "normal", 1)
    audit_service.record_scan(db, audit.id, user.id, complete_card.id, "normal", 1)
    stranger = _card(db, name="Ponder")
    audit_service.record_scan(db, audit.id, user.id, stranger.id, "normal", 1)

    p = audit_service.get_scan_progress(db, audit.id, user.id)
    assert p.total_expected == 4
    assert p.total_seen == 2  # match + partial; extras excluded
    assert p.total_remaining == 2
    assert p.total_extras == 1
    by_name = {c["card_name"]: c for c in p.expected_cards}
    assert by_name["Counterspell"]["status"] == "partial"
    assert by_name["Brainstorm"]["status"] == "complete"


def test_extras_listed_separately(db, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db, name="Island"), loc.id, qty=1)
    stranger = _card(db, name="Mountain")
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.record_scan(db, audit.id, user.id, stranger.id, "normal", 2)

    extras = audit_service.list_extras(db, audit.id, user.id)
    assert len(extras) == 1
    assert extras[0]["card_name"] == "Mountain"
    assert extras[0]["quantity_scanned"] == 2


# -- Routes --------------------------------------------------------------------


def test_route_start_creates_session_and_scan_returns_partial(db, client, user):
    loc = _loc(db, user.id)
    card = _card(db, name="Sol Ring")
    _row(db, user.id, card, loc.id, qty=1)
    db.commit()

    # full=1 is the fast path that starts immediately (no scope picker).
    r = client.get(f"/audit/start?location_id={loc.id}&full=1")
    assert r.status_code == 200
    assert "Auditing" in r.text

    audit = db.query(AuditSession).filter_by(user_id=user.id).one()
    r = client.post(
        f"/audit/{audit.id}/scan",
        data={"card_id": card.id, "finish": "normal", "quantity": 1},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "Match" in r.text
    assert "audit-progress" in r.text  # OOB progress region present
    db.expire_all()
    assert db.query(AuditScan).filter_by(audit_session_id=audit.id).count() == 1


def test_route_start_resumes_paused_session(db, client, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db), loc.id, qty=1)
    db.commit()
    # Pre-existing paused audit for the same location.
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.pause_audit(db, audit.id, user.id)
    db.commit()

    r = client.get(f"/audit/start?location_id={loc.id}")
    assert r.status_code == 200
    db.expire_all()
    assert db.get(AuditSession, audit.id).status == "active"


def test_route_pause_and_abandon(db, client, user):
    loc = _loc(db, user.id)
    card = _card(db)
    _row(db, user.id, card, loc.id, qty=1)
    db.commit()
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    audit_service.record_scan(db, audit.id, user.id, card.id, "normal", 1)
    db.commit()

    r = client.post(f"/audit/{audit.id}/pause", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/locations/{loc.id}"
    db.expire_all()
    assert db.get(AuditSession, audit.id).status == "paused"

    r = client.post(f"/audit/{audit.id}/abandon", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(AuditSession, audit.id).status == "abandoned"
    assert db.query(AuditScan).filter_by(audit_session_id=audit.id).count() == 0


def test_route_start_other_location_shows_switch_confirmation(db, client, user):
    loc1 = _loc(db, user.id, name="Drawer 3")
    loc2 = _loc(db, user.id, name="Drawer 5")
    _row(db, user.id, _card(db), loc1.id, qty=1)
    db.commit()
    audit_service.start_audit(db, user.id, loc1.id)
    db.commit()

    r = client.get(f"/audit/start?location_id={loc2.id}")
    assert r.status_code == 200
    assert "Audit already in progress" in r.text
    assert "Drawer 5" in r.text  # target location named in the confirmation


def test_route_end_redirects_to_reconcile(db, client, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db), loc.id, qty=1)
    db.commit()
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    db.commit()

    r = client.post(f"/audit/{audit.id}/end", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/audit/{audit.id}/reconcile"

    r = client.get(f"/audit/{audit.id}/reconcile")
    assert r.status_code == 200
    assert "Reconcile" in r.text


def test_route_card_search_prioritizes_expected(db, client, user):
    loc = _loc(db, user.id)
    elsewhere = _loc(db, user.id, name="Box B")
    expected = _card(db, name="Goblin Guide")
    _row(db, user.id, expected, loc.id, qty=1)  # in the audited location
    owned_elsewhere = _card(db, name="Goblin Chainwhirler")
    _row(db, user.id, owned_elsewhere, elsewhere.id, qty=1)  # owned, not expected here
    db.commit()
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    db.commit()

    r = client.get(f"/audit/api/card-search?session_id={audit.id}&q=goblin")
    assert r.status_code == 200
    data = r.json()
    names = [c["name"] for c in data]
    assert set(names) == {"Goblin Guide", "Goblin Chainwhirler"}
    assert data[0]["name"] == "Goblin Guide" and data[0]["expected"] is True
    assert data[1]["expected"] is False


def test_route_card_search_cap_cannot_drop_the_exact_match(db, client, user):
    """Relevance decides which 30 survive the LIMIT; expected-first still owns display."""
    loc = _loc(db, user.id)
    for i in range(35):
        _row(db, user.id, _card(db, name=f"Aaa Filler Bolt {i:02d}"), loc.id, qty=1)
    _row(db, user.id, _card(db, name="Bolt"), loc.id, qty=1)
    db.commit()
    audit, _ = audit_service.start_audit(db, user.id, loc.id)
    db.commit()

    data = client.get(f"/audit/api/card-search?session_id={audit.id}&q=Bolt").json()
    assert len(data) == 30  # still capped
    assert "Bolt" in [c["name"] for c in data]  # the typed name is not truncated away


def test_route_non_owner_gets_404(db, client, user):
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

    # Pinned client user is `user`, not `other`.
    assert client.post(f"/audit/{audit.id}/pause", follow_redirects=False).status_code == 404
    assert (
        client.post(
            f"/audit/{audit.id}/scan",
            data={"card_id": 1, "finish": "normal"},
        ).status_code
        == 404
    )
    assert client.get(f"/audit/start?location_id={loc.id}").status_code == 404


# -- scoped audit routes (set-code subset) ------------------------------------


def test_route_start_no_full_shows_scope_picker(db, client, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db, name="Bolt", set_code="LTR"), loc.id, qty=2)
    _row(db, user.id, _card(db, name="Sol Ring", set_code="MH3"), loc.id, qty=1)
    db.commit()

    r = client.get(f"/audit/start?location_id={loc.id}")
    assert r.status_code == 200
    assert "Audit selected sets" in r.text  # the scope picker, not the workspace
    assert "LTR" in r.text and "MH3" in r.text
    # No session started by merely viewing the picker.
    assert db.query(AuditSession).filter_by(user_id=user.id).count() == 0


def test_route_start_scoped_post_starts_scoped_audit(db, client, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db, name="Bolt", set_code="LTR"), loc.id, qty=2)
    _row(db, user.id, _card(db, name="Sol Ring", set_code="MH3"), loc.id, qty=1)
    _row(db, user.id, _card(db, name="Boseiju", set_code="NEO"), loc.id, qty=1)
    db.commit()

    r = client.post(
        "/audit/start",
        data={"location_id": loc.id, "set_codes": ["LTR", "MH3"]},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Auditing" in r.text
    audit = db.query(AuditSession).filter_by(user_id=user.id).one()
    import json

    assert json.loads(audit.scope) == {"set_codes": ["LTR", "MH3"]}


def test_route_start_scoped_empty_reshows_picker_with_error(db, client, user):
    loc = _loc(db, user.id)
    _row(db, user.id, _card(db, name="Bolt", set_code="LTR"), loc.id, qty=1)
    db.commit()

    r = client.post(
        "/audit/start",
        data={"location_id": loc.id, "set_codes": ["ZZZ"]},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "No cards from those sets" in r.text
    assert db.query(AuditSession).filter_by(user_id=user.id).count() == 0
