from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.models import (
    AuditLog,
    AuditScan,
    AuditSession,
    Card,
    ImportBatch,
    InventoryRow,
    StorageLocation,
    TransactionLog,
)
from app.timeutil import utc_now


def create_import_batch(
    session: Session,
    user_id: int,
    filename: str,
    row_count: int,
    note: str | None = None,
) -> ImportBatch:
    batch = ImportBatch(
        user_id=user_id,
        filename=filename,
        row_count=row_count,
        note=note,
    )
    session.add(batch)
    session.flush()
    return batch


def log_transaction(
    session: Session,
    user_id: int,
    event_type: str,
    card_id: int | None,
    finish: str | None,
    quantity_delta: int,
    source_location: str | None = None,
    destination_location: str | None = None,
    batch_id: int | None = None,
    inventory_row_id: int | None = None,
    note: str | None = None,
    flush: bool = False,
) -> TransactionLog:
    log = TransactionLog(
        user_id=user_id,
        event_type=event_type,
        card_id=card_id,
        finish=finish,
        quantity_delta=quantity_delta,
        source_location=source_location,
        destination_location=destination_location,
        batch_id=batch_id,
        inventory_row_id=inventory_row_id,
        note=note,
    )
    session.add(log)
    if flush:
        session.flush()
    return log


def recent_location_activity(
    session: Session,
    user_id: int,
    location_name: str,
    limit: int = 10,
) -> list[dict]:
    """Recent inventory events touching a location, newest first.

    Exists because a user could not see what a quick-add had just done. The
    2026-08-15 Stoneskin report was "adding a non-foil overwrote my foil"; the
    record showed the foil was fine and a non-foil row had existed since May,
    on a page of a ~1,400-row location the reporter had no reason to scroll to.
    Nothing on the page could have told him that, so the page tells him now.

    Matched on the location NAME in either direction, because ``TransactionLog``
    stores free text and has no location FK — so this deliberately catches
    cards that left as well as cards that arrived (the foil's departure is the
    entry that would have explained the whole report). The cost is that
    renaming a location orphans its history here; that is the schema's
    constraint, not a choice, and it fails quiet rather than wrong.
    """
    rows = (
        session.query(TransactionLog, Card.name, Card.set_code)
        .outerjoin(Card, TransactionLog.card_id == Card.id)
        .filter(
            TransactionLog.user_id == user_id,
            or_(
                TransactionLog.source_location == location_name,
                TransactionLog.destination_location == location_name,
            ),
        )
        .order_by(TransactionLog.created_at.desc(), TransactionLog.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": t.id,
            "event_type": t.event_type,
            "created_at": t.created_at,
            "finish": t.finish,
            "quantity_delta": t.quantity_delta,
            "source_location": t.source_location,
            "destination_location": t.destination_location,
            "card_name": card_name,
            "set_code": set_code,
            # Which way the card went, resolved server-side: the template must
            # not re-derive it and get a third answer.
            "left_here": t.source_location == location_name,
        }
        for t, card_name, set_code in rows
    ]


def list_transaction_logs(session: Session, user_id: int) -> list[TransactionLog]:
    return (
        session.query(TransactionLog)
        .filter(TransactionLog.user_id == user_id)
        .order_by(TransactionLog.id.desc())
        .all()
    )


# --- Physical Audit Mode (issue #73) -----------------------------------------
# A session that reconciles one storage location against the DB. Nothing mutates
# inventory here — Phase 1 is session lifecycle + expected-list + snapshot only;
# the reconciliation changeset (extra/missing → apply) lands in Phase 2.

_ACTIVE_STATUSES = ("active", "paused")


def _location_rows(session: Session, user_id: int, storage_location_id: int) -> list[InventoryRow]:
    """The location's current inventory rows (the audit snapshot's contents)."""
    return (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id == storage_location_id,
        )
        .all()
    )


def _parse_scope(scope_json: str | None) -> set[str] | None:
    """Uppercased set codes an audit is scoped to, or ``None`` for a full audit
    (also None when the JSON is malformed or the code list is empty)."""
    if not scope_json:
        return None
    try:
        data = json.loads(scope_json)
    except (ValueError, TypeError):
        return None
    codes = data.get("set_codes") if isinstance(data, dict) else None
    if not codes:
        return None
    normalized = {str(c).strip().upper() for c in codes if str(c).strip()}
    return normalized or None


def scope_set_codes(audit: AuditSession) -> list[str] | None:
    """Sorted set codes an audit is scoped to (for headers/badges), or ``None``
    for a full audit."""
    scope = _parse_scope(audit.scope)
    return sorted(scope) if scope else None


def _in_scope(row: InventoryRow, scope: set[str] | None) -> bool:
    """A row is in scope when the audit is unscoped, or its card's set is listed."""
    if scope is None:
        return True
    return bool(row.card) and (row.card.set_code or "").upper() in scope


def _scoped_rows(rows: list[InventoryRow], scope: set[str] | None) -> list[InventoryRow]:
    return [r for r in rows if _in_scope(r, scope)] if scope is not None else rows


def _audit_expected_rows(session: Session, audit: AuditSession) -> list[InventoryRow]:
    """The audit's expected inventory rows — the location's rows filtered to the
    audit's scope (all rows for a full audit). The single source of truth for the
    expected set across snapshot, scan matching, progress, and reconciliation."""
    rows = _location_rows(session, audit.user_id, audit.storage_location_id)
    return _scoped_rows(rows, _parse_scope(audit.scope))


def _snapshot_hash(rows: list[InventoryRow]) -> str:
    """Deterministic fingerprint of a location's inventory: sorted (row id,
    quantity) pairs → sha256. Same inventory ⇒ same hash regardless of row
    order; any quantity/row change flips it (optimistic-concurrency check)."""
    payload = ";".join(f"{r.id}:{r.quantity}" for r in sorted(rows, key=lambda r: r.id))
    return hashlib.sha256(payload.encode()).hexdigest()


def _card_label(card: Card | None) -> str:
    """ "Name (SET)" label used in snapshot-change descriptions and audit notes."""
    if card is None:
        return "?"
    return f"{card.name} ({(card.set_code or '?').upper()})"


def _snapshot_detail(rows: list[InventoryRow]) -> str:
    """JSON baseline paired with ``_snapshot_hash`` — per-row (id, qty, label) so
    reconciliation can itemize what changed, not just that the hash differs."""
    detail = [
        {"row_id": r.id, "qty": r.quantity, "label": _card_label(r.card)}
        for r in sorted(rows, key=lambda r: r.id)
    ]
    return json.dumps(detail)


def _expected_order(location: StorageLocation, rows: list[InventoryRow]) -> list[InventoryRow]:
    """Rows in the order the operator physically encounters them: drawer-sorter
    order for ``type="drawer"`` locations, else alphabetical by card name."""
    if location.type == "drawer":
        # Local import: inventory_service imports log_transaction from this module.
        from app.inventory_service import drawer_sort_key

        return sorted(rows, key=drawer_sort_key)
    return sorted(rows, key=lambda r: ((r.card.name or "").lower(), r.id))


def _owned_audit(session: Session, audit_session_id: int, user_id: int) -> AuditSession:
    """Fetch an audit session, enforcing owner authorization."""
    audit = session.get(AuditSession, audit_session_id)
    if audit is None:
        raise ValueError(f"audit session {audit_session_id} not found")
    if audit.user_id != user_id:
        raise PermissionError("audit session belongs to another user")
    return audit


_SESSION_TIMEOUT = timedelta(hours=24)


def get_active_audit(session: Session, user_id: int) -> AuditSession | None:
    """The user's current active-or-paused audit session, if any (at most one).

    Lazy timeout: a session whose ``started_at`` is >24h old is auto-abandoned
    here (on the next audit action / page load — no background task) and ``None``
    is returned, so a stale session never blocks a fresh audit."""
    audit = (
        session.query(AuditSession)
        .filter(AuditSession.user_id == user_id, AuditSession.status.in_(_ACTIVE_STATUSES))
        .order_by(AuditSession.id.desc())
        .first()
    )
    if audit is None:
        return None
    if utc_now() - audit.started_at > _SESSION_TIMEOUT:
        abandon_audit(session, audit.id, user_id)
        log_transaction(
            session=session,
            user_id=user_id,
            event_type="audit_auto_abandoned",
            card_id=None,
            finish=None,
            quantity_delta=0,
            note=f"Auto-abandoned: session exceeded 24h timeout (audit session {audit.id})",
            flush=True,
        )
        return None
    return audit


def start_audit(
    session: Session,
    user_id: int,
    storage_location_id: int,
    set_codes: list[str] | None = None,
) -> tuple[AuditSession, list[InventoryRow]]:
    """Open an audit for a location. Returns the session and the expected card
    list in physical-encounter order. Raises if the user already has an active
    OR paused audit (the paused one must be explicitly abandoned first).

    ``set_codes`` scopes the audit to those sets: the snapshot and expected set
    cover ONLY the matching rows, so concurrent changes to unscoped cards at the
    same location don't invalidate it. ``None``/empty = a full-location audit
    (unchanged behavior). A scope that matches no rows is rejected (ValueError)."""
    location = session.get(StorageLocation, storage_location_id)
    if location is None:
        raise ValueError(f"storage location {storage_location_id} not found")
    if location.user_id != user_id:
        raise PermissionError("storage location belongs to another user")

    if get_active_audit(session, user_id) is not None:
        raise ValueError("an audit is already active or paused; abandon it before starting another")

    all_rows = _location_rows(session, user_id, storage_location_id)
    scope = {str(c).strip().upper() for c in (set_codes or []) if str(c).strip()} or None
    scope_json = None
    if scope is not None:
        rows = _scoped_rows(all_rows, scope)
        if not rows:
            raise ValueError("No cards from those sets at this location")
        scope_json = json.dumps({"set_codes": sorted(scope)})
    else:
        rows = all_rows

    audit = AuditSession(
        user_id=user_id,
        storage_location_id=storage_location_id,
        status="active",
        snapshot_hash=_snapshot_hash(rows),
        snapshot_detail=_snapshot_detail(rows),
        scope=scope_json,
        started_at=utc_now(),
    )
    session.add(audit)
    session.flush()
    return audit, _expected_order(location, rows)


def list_location_sets(session: Session, user_id: int, storage_location_id: int) -> list[dict]:
    """Sets present at a location — ``set_code``, ``set_name`` (if known), and
    ``card_count`` (summed quantities) — sorted by count desc. Powers the scope
    picker at audit start."""
    rows = (
        session.query(
            Card.set_code,
            Card.set_name,
            func.coalesce(func.sum(InventoryRow.quantity), 0),
        )
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id == storage_location_id,
        )
        .group_by(Card.set_code, Card.set_name)
        .all()
    )
    agg: dict[str, dict] = {}
    for code, name, count in rows:
        key = (code or "").upper()
        entry = agg.setdefault(key, {"set_code": key, "set_name": None, "card_count": 0})
        entry["card_count"] += int(count)
        if name and not entry["set_name"]:
            entry["set_name"] = name
    out = list(agg.values())
    out.sort(key=lambda d: (-d["card_count"], d["set_code"]))
    return out


def pause_audit(session: Session, audit_session_id: int, user_id: int) -> AuditSession:
    audit = _owned_audit(session, audit_session_id, user_id)
    audit.status = "paused"
    audit.paused_at = utc_now()
    session.flush()
    return audit


def resume_audit(
    session: Session, audit_session_id: int, user_id: int
) -> tuple[AuditSession, dict]:
    """Reactivate a paused audit and return current scan progress."""
    audit = _owned_audit(session, audit_session_id, user_id)
    audit.status = "active"
    audit.paused_at = None
    session.flush()
    return audit, _scan_progress(session, audit_session_id)


def abandon_audit(session: Session, audit_session_id: int, user_id: int) -> AuditSession:
    """Abandon an audit and discard its scans (SQLite FKs are off, so delete
    explicitly rather than relying on the CASCADE)."""
    audit = _owned_audit(session, audit_session_id, user_id)
    session.query(AuditScan).filter(AuditScan.audit_session_id == audit_session_id).delete()
    audit.status = "abandoned"
    session.flush()
    return audit


def complete_audit(
    session: Session,
    audit_session_id: int,
    user_id: int,
    *,
    cards_expected: int,
    cards_seen: int,
    cards_missing: int,
    cards_extra: int,
    actions_applied: list | None = None,
) -> tuple[AuditSession, AuditLog]:
    """Close an audit and write its log record. Phase 1 is the state transition +
    log write; the caller supplies the reconciliation counts (the changeset math
    that produces them lands in Phase 2)."""
    # ponytail: reconciliation counts passed in, not computed — Phase 2 owns the delta math.
    audit = _owned_audit(session, audit_session_id, user_id)
    audit.status = "completed"
    audit.completed_at = utc_now()
    log = AuditLog(
        audit_session_id=audit.id,
        user_id=user_id,
        storage_location_id=audit.storage_location_id,
        cards_expected=cards_expected,
        cards_seen=cards_seen,
        cards_missing=cards_missing,
        cards_extra=cards_extra,
        actions_applied=json.dumps(actions_applied or []),
        scope=audit.scope,  # copy the scope so history/staleness can distinguish scoped audits
        completed_at=audit.completed_at,
    )
    session.add(log)
    session.flush()
    return audit, log


def _scan_progress(session: Session, audit_session_id: int) -> dict:
    scans = session.query(AuditScan).filter(AuditScan.audit_session_id == audit_session_id).all()
    return {
        "scans": scans,
        "seen": sum(s.quantity_scanned for s in scans if s.scan_type == "match"),
        "extra": sum(1 for s in scans if s.scan_type == "extra"),
        "partial": sum(1 for s in scans if s.scan_type == "partial_match"),
    }


# --- Phase 2: scan loop ------------------------------------------------------


@dataclass
class ScanResult:
    scan_type: str  # match | extra | partial_match | out_of_scope
    audit_scan_id: int
    matched_row: InventoryRow | None
    message: str
    scryfall_id: str | None = None  # scanned card, for the feedback thumbnail


@dataclass
class AuditProgress:
    total_expected: int
    total_seen: int
    total_remaining: int
    total_extras: int
    total_out_of_scope: int = 0
    expected_cards: list[dict] = field(default_factory=list)


def _seen_by_row(session: Session, audit_session_id: int) -> dict[int, int]:
    """Per-expected-row seen count: sum of ``quantity_scanned`` for match +
    partial_match scans (both carry an ``inventory_row_id``; extras don't)."""
    rows = (
        session.query(
            AuditScan.inventory_row_id,
            func.coalesce(func.sum(AuditScan.quantity_scanned), 0),
        )
        .filter(
            AuditScan.audit_session_id == audit_session_id,
            AuditScan.inventory_row_id.isnot(None),
        )
        .group_by(AuditScan.inventory_row_id)
        .all()
    )
    return {rid: int(total) for rid, total in rows}


def _add_scan(
    session: Session,
    audit_session_id: int,
    inventory_row_id: int | None,
    card_id: int,
    finish: str,
    scan_type: str,
    quantity: int,
) -> AuditScan:
    scan = AuditScan(
        audit_session_id=audit_session_id,
        inventory_row_id=inventory_row_id,
        card_id=card_id,
        finish=finish,
        scan_type=scan_type,
        quantity_scanned=quantity,
        scanned_at=utc_now(),
    )
    session.add(scan)
    session.flush()
    return scan


def record_scan(
    session: Session,
    audit_session_id: int,
    user_id: int,
    card_id: int,
    finish: str,
    quantity: int = 1,
) -> ScanResult:
    """Classify a scanned card against the audit's expected set and record it.

    Precedence: exact ``card_id`` + ``finish`` with unseen copies remaining →
    ``match``; else same card *name* (different printing/finish) with unseen
    copies → ``partial_match``; else ``extra`` (no matching expected row)."""
    audit = _owned_audit(session, audit_session_id, user_id)
    quantity = max(1, int(quantity))
    finish = (finish or "normal").strip() or "normal"

    card = session.get(Card, card_id)
    if card is None:
        raise ValueError(f"card {card_id} not found")

    # 0. Scope gate (scoped audits only): the SCANNED card's own set decides. A
    # card whose set isn't in scope is out_of_scope — acknowledged, not an extra,
    # no reconciliation — even if an in-scope printing of the same name is
    # expected (the operator scanned what's physically in hand). Runs BEFORE
    # match/partial/extra.
    scope = _parse_scope(audit.scope)
    if scope is not None and (card.set_code or "").upper() not in scope:
        scan = _add_scan(session, audit_session_id, None, card_id, finish, "out_of_scope", quantity)
        return ScanResult(
            "out_of_scope", scan.id, None, "Out of scope — not part of this audit", card.scryfall_id
        )

    expected = _scoped_rows(_location_rows(session, user_id, audit.storage_location_id), scope)
    seen = _seen_by_row(session, audit_session_id)

    # 1. Exact printing + finish with a copy still unseen.
    match_row = next(
        (
            r
            for r in expected
            if r.card_id == card_id and r.finish == finish and seen.get(r.id, 0) < r.quantity
        ),
        None,
    )
    if match_row is not None:
        scan = _add_scan(
            session, audit_session_id, match_row.id, card_id, finish, "match", quantity
        )
        msg = f"Matched: {card.name} ({(card.set_code or '?').upper()}) x{quantity}"
        return ScanResult("match", scan.id, match_row, msg, card.scryfall_id)

    # 2. Same name, different printing or finish, with a copy still unseen.
    partial_row = next(
        (
            r
            for r in expected
            if r.card
            and r.card.name == card.name
            and (r.card_id != card_id or r.finish != finish)
            and seen.get(r.id, 0) < r.quantity
        ),
        None,
    )
    if partial_row is not None:
        scan = _add_scan(
            session, audit_session_id, partial_row.id, card_id, finish, "partial_match", quantity
        )
        exp_set = (partial_row.card.set_code or "?").upper()
        got_set = (card.set_code or "?").upper()
        msg = f"Partial match: different printing — expected {exp_set}, scanned {got_set}"
        return ScanResult("partial_match", scan.id, partial_row, msg, card.scryfall_id)

    # 3. Not expected here (or all expected copies already seen) → extra.
    scan = _add_scan(session, audit_session_id, None, card_id, finish, "extra", quantity)
    return ScanResult("extra", scan.id, None, "Extra: not in expected set", card.scryfall_id)


def get_scan_progress(session: Session, audit_session_id: int, user_id: int) -> AuditProgress:
    """Live audit progress: totals plus the expected card list in physical order,
    each row carrying its expected/seen counts and a ``unseen|partial|complete``
    status."""
    audit = _owned_audit(session, audit_session_id, user_id)
    location = session.get(StorageLocation, audit.storage_location_id)
    rows = _expected_order(location, _audit_expected_rows(session, audit))
    seen = _seen_by_row(session, audit_session_id)

    total_extras = (
        session.query(func.coalesce(func.sum(AuditScan.quantity_scanned), 0))
        .filter(
            AuditScan.audit_session_id == audit_session_id,
            AuditScan.scan_type == "extra",
        )
        .scalar()
    )
    total_out_of_scope = (
        session.query(func.coalesce(func.sum(AuditScan.quantity_scanned), 0))
        .filter(
            AuditScan.audit_session_id == audit_session_id,
            AuditScan.scan_type == "out_of_scope",
        )
        .scalar()
    )

    expected_cards: list[dict] = []
    total_expected = 0
    total_seen = 0
    for r in rows:
        qty_expected = r.quantity
        qty_seen = seen.get(r.id, 0)
        total_expected += qty_expected
        total_seen += qty_seen
        if qty_seen <= 0:
            status = "unseen"
        elif qty_seen < qty_expected:
            status = "partial"
        else:
            status = "complete"
        expected_cards.append(
            {
                "inventory_row_id": r.id,
                "card_name": r.card.name if r.card else "",
                "set_code": (r.card.set_code or "").upper() if r.card else "",
                "collector_number": r.card.collector_number if r.card else "",
                "finish": r.finish,
                "quantity_expected": qty_expected,
                "quantity_seen": qty_seen,
                "status": status,
            }
        )

    return AuditProgress(
        total_expected=total_expected,
        total_seen=total_seen,
        total_remaining=max(0, total_expected - total_seen),
        total_extras=int(total_extras or 0),
        total_out_of_scope=int(total_out_of_scope or 0),
        expected_cards=expected_cards,
    )


def list_extras(session: Session, audit_session_id: int, user_id: int) -> list[dict]:
    """Scanned cards that aren't in the expected set, aggregated by printing +
    finish (for the workspace's Extras panel)."""
    _owned_audit(session, audit_session_id, user_id)
    scans = (
        session.query(AuditScan)
        .options(joinedload(AuditScan.card))
        .filter(
            AuditScan.audit_session_id == audit_session_id,
            AuditScan.scan_type == "extra",
        )
        .all()
    )
    agg: dict[tuple[int, str], dict] = {}
    for s in scans:
        key = (s.card_id, s.finish)
        entry = agg.get(key)
        if entry is None:
            agg[key] = {
                "card_id": s.card_id,
                "card_name": s.card.name if s.card else "",
                "set_code": (s.card.set_code or "").upper() if s.card else "",
                "collector_number": s.card.collector_number if s.card else "",
                "finish": s.finish,
                "quantity_scanned": s.quantity_scanned,
                "scryfall_id": s.card.scryfall_id if s.card else None,
            }
        else:
            entry["quantity_scanned"] += s.quantity_scanned
    return list(agg.values())


def list_out_of_scope(session: Session, audit_session_id: int, user_id: int) -> list[dict]:
    """Scanned cards whose set wasn't in a scoped audit's scope, aggregated by
    printing + finish. Acknowledged-only: shown collapsed, offered no actions."""
    _owned_audit(session, audit_session_id, user_id)
    scans = (
        session.query(AuditScan)
        .options(joinedload(AuditScan.card))
        .filter(
            AuditScan.audit_session_id == audit_session_id,
            AuditScan.scan_type == "out_of_scope",
        )
        .all()
    )
    agg: dict[tuple[int, str], dict] = {}
    for s in scans:
        key = (s.card_id, s.finish)
        entry = agg.get(key)
        if entry is None:
            agg[key] = {
                "card_id": s.card_id,
                "card_name": s.card.name if s.card else "",
                "set_code": (s.card.set_code or "").upper() if s.card else "",
                "collector_number": s.card.collector_number if s.card else "",
                "finish": s.finish,
                "quantity_scanned": s.quantity_scanned,
                "scryfall_id": s.card.scryfall_id if s.card else None,
            }
        else:
            entry["quantity_scanned"] += s.quantity_scanned
    return list(agg.values())


# --- Phase 3: reconciliation -------------------------------------------------


@dataclass
class AuditDelta:
    seen: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)
    partial_matches: list[dict] = field(default_factory=list)
    out_of_scope: list[dict] = field(default_factory=list)


@dataclass
class ReconciliationResult:
    success: bool
    actions_applied: int
    snapshot_conflict: bool
    changes: list[str] = field(default_factory=list)
    audit_log_id: int | None = None


def _seen_by_row_and_type(
    session: Session, audit_session_id: int
) -> tuple[dict[int, int], dict[int, int]]:
    """(match_seen, partial_seen) per expected row. A row accrues match OR partial
    scans up to its capacity (record_scan's first-come rule), never both past the
    expected quantity — so match_seen + partial_seen ≤ expected."""
    rows = (
        session.query(
            AuditScan.inventory_row_id,
            AuditScan.scan_type,
            func.coalesce(func.sum(AuditScan.quantity_scanned), 0),
        )
        .filter(
            AuditScan.audit_session_id == audit_session_id,
            AuditScan.inventory_row_id.isnot(None),
        )
        .group_by(AuditScan.inventory_row_id, AuditScan.scan_type)
        .all()
    )
    match: dict[int, int] = {}
    partial: dict[int, int] = {}
    for rid, scan_type, total in rows:
        if scan_type == "match":
            match[rid] = int(total)
        elif scan_type == "partial_match":
            partial[rid] = int(total)
    return match, partial


def compute_delta(session: Session, audit_session_id: int, user_id: int) -> AuditDelta:
    """The reconciliation changeset: seen (no action), missing (expected but not
    exact-matched), extras (scanned, not expected), and partial_matches (same card,
    different printing/finish). Built from audit_scans against the location's
    expected rows."""
    audit = _owned_audit(session, audit_session_id, user_id)
    location = session.get(StorageLocation, audit.storage_location_id)
    expected = {r.id: r for r in _audit_expected_rows(session, audit)}
    ordered = _expected_order(location, list(expected.values()))
    match_seen, _partial_seen = _seen_by_row_and_type(session, audit_session_id)

    delta = AuditDelta()
    for r in ordered:
        seen = match_seen.get(r.id, 0)
        base = {
            "card_name": r.card.name if r.card else "",
            "set_code": (r.card.set_code or "").upper() if r.card else "",
            "finish": r.finish,
            "quantity_expected": r.quantity,
            "quantity_seen": seen,
            "scryfall_id": r.card.scryfall_id if r.card else None,
        }
        if seen >= r.quantity:
            delta.seen.append(base)
        else:
            delta.missing.append(
                {"inventory_row_id": r.id, **base, "quantity_missing": r.quantity - seen}
            )

    # Partial matches: each (expected row, scanned printing, finish), aggregated.
    partial_scans = (
        session.query(AuditScan)
        .options(joinedload(AuditScan.card))
        .filter(
            AuditScan.audit_session_id == audit_session_id,
            AuditScan.scan_type == "partial_match",
        )
        .all()
    )
    partial_agg: dict[tuple, dict] = {}
    for s in partial_scans:
        exp_row = expected.get(s.inventory_row_id)
        key = (s.inventory_row_id, s.card_id, s.finish)
        entry = partial_agg.get(key)
        if entry is None:
            partial_agg[key] = {
                "inventory_row_id": s.inventory_row_id,
                "expected_card_name": exp_row.card.name if exp_row and exp_row.card else "",
                "expected_set": (exp_row.card.set_code or "").upper()
                if exp_row and exp_row.card
                else "",
                "expected_finish": exp_row.finish if exp_row else "",
                "expected_scryfall_id": exp_row.card.scryfall_id
                if exp_row and exp_row.card
                else None,
                "scanned_card_id": s.card_id,
                "scanned_set": (s.card.set_code or "").upper() if s.card else "",
                "scanned_finish": s.finish,
                "scanned_scryfall_id": s.card.scryfall_id if s.card else None,
                "quantity": s.quantity_scanned,
            }
        else:
            entry["quantity"] += s.quantity_scanned
    delta.partial_matches = list(partial_agg.values())

    # Extras: scanned but not expected; flag whether the user owns the card elsewhere.
    for extra in list_extras(session, audit_session_id, user_id):
        elsewhere = (
            session.query(InventoryRow.storage_location_id, StorageLocation.name, InventoryRow.id)
            .join(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.card_id == extra["card_id"],
                InventoryRow.storage_location_id != audit.storage_location_id,
            )
            .all()
        )
        delta.extras.append(
            {
                **extra,
                "exists_elsewhere": bool(elsewhere),
                "sources": [
                    {"location_id": loc_id, "location_name": loc_name, "row_id": row_id}
                    for loc_id, loc_name, row_id in elsewhere
                ],
            }
        )

    # Out-of-scope scans (scoped audits only) — display-only, no proposed actions.
    delta.out_of_scope = list_out_of_scope(session, audit_session_id, user_id)

    return delta


def validate_snapshot(session: Session, audit_session_id: int) -> tuple[bool, list[str]]:
    """Optimistic-concurrency check: recompute the location's inventory hash and
    compare to the one stored at audit start. Returns (True, []) if unchanged,
    else (False, [human-readable changes]) itemized from the stored baseline."""
    audit = session.get(AuditSession, audit_session_id)
    if audit is None:
        raise ValueError(f"audit session {audit_session_id} not found")

    # Scoped audits hash ONLY their in-scope rows, so unscoped changes at the same
    # location don't trip the concurrency check (in-scope changes still do).
    current_rows = _audit_expected_rows(session, audit)
    if _snapshot_hash(current_rows) == audit.snapshot_hash:
        return True, []

    if not audit.snapshot_detail:
        return False, ["The inventory at this location changed since the audit began."]

    baseline = {e["row_id"]: e for e in json.loads(audit.snapshot_detail)}
    current = {r.id: r for r in current_rows}
    changes: list[str] = []
    for row_id, e in baseline.items():
        cur = current.get(row_id)
        if cur is None:
            changes.append(f"Card removed: {e['label']}")
        elif cur.quantity != e["qty"]:
            changes.append(f"{e['label']} quantity changed from {e['qty']} to {cur.quantity}")
    for row_id, r in current.items():
        if row_id not in baseline:
            changes.append(f"New card added: {_card_label(r.card)}")
    return False, changes or ["The inventory at this location changed since the audit began."]


def _owned_row(session: Session, row_id: int | None, user_id: int) -> InventoryRow | None:
    if row_id is None:
        return None
    return (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == user_id)
        .first()
    )


def _apply_mark_missing(session, audit, user_id, action) -> None:
    """Record that expected copies weren't found. No inventory mutation — the
    audit trail is the whole point (spec: mark_missing writes a log, nothing else)."""
    row = _owned_row(session, action.get("inventory_row_id"), user_id)
    location = session.get(StorageLocation, audit.storage_location_id)
    card = row.card if row else session.get(Card, action.get("card_id"))
    qty = int(action.get("quantity") or (row.quantity if row else 1))
    log_transaction(
        session=session,
        user_id=user_id,
        event_type="audit_missing",
        card_id=card.id if card else None,
        finish=action.get("finish") or (row.finish if row else None),
        quantity_delta=0,  # no mutation; the missing count lives in the note
        source_location=location.name if location else None,
        inventory_row_id=row.id if row else None,
        note=(
            f"Audit: marked {qty} missing — {_card_label(card)} in "
            f"{location.name if location else '?'} (audit session {audit.id})"
        ),
        flush=True,
    )


def _apply_add_extra(session, audit, user_id, action) -> None:
    """Create (or merge into) an InventoryRow at the audited location for a
    scanned extra."""
    from app.import_service import normalize_finish

    card = session.get(Card, action.get("card_id"))
    if card is None:
        return
    finish = normalize_finish(action.get("finish"))
    qty = max(1, int(action.get("quantity") or 1))
    location = session.get(StorageLocation, audit.storage_location_id)
    now = utc_now()

    existing = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.card_id == card.id,
            InventoryRow.finish == finish,
            func.coalesce(InventoryRow.language, "en") == "en",
            InventoryRow.is_proxy.is_(False),
            InventoryRow.storage_location_id == audit.storage_location_id,
            InventoryRow.is_pending.is_(False),
        )
        .first()
    )
    if existing is not None:
        existing.quantity += qty
        existing.updated_at = now
        row = existing
    else:
        row = InventoryRow(
            user_id=user_id,
            card_id=card.id,
            storage_location_id=audit.storage_location_id,
            finish=finish,
            quantity=qty,
            language="en",
            is_proxy=False,
            is_pending=False,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()

    log_transaction(
        session=session,
        user_id=user_id,
        event_type="audit_extra_added",
        card_id=card.id,
        finish=finish,
        quantity_delta=qty,
        destination_location=location.name if location else None,
        inventory_row_id=row.id,
        note=f"Audit: added extra — {_card_label(card)} x{qty} (audit session {audit.id})",
        flush=True,
    )


def _apply_move_extra(session, audit, user_id, action) -> None:
    """Repoint an existing row from its current location into the audited one.
    Reuses ``move_inventory_row_to_location`` (handles merge + FK cleanup), then
    marks the move in the audit trail."""
    from app.inventory_service import move_inventory_row_to_location

    source_row_id = action.get("inventory_row_id")
    if source_row_id is None:
        # Fall back to (card, finish, source_location) if the row id wasn't given.
        row = (
            session.query(InventoryRow)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.card_id == action.get("card_id"),
                InventoryRow.storage_location_id == action.get("source_location_id"),
            )
            .first()
        )
        source_row_id = row.id if row else None
    if source_row_id is None:
        return

    card = session.get(Card, action.get("card_id"))
    location = session.get(StorageLocation, audit.storage_location_id)
    move_inventory_row_to_location(session, source_row_id, user_id, audit.storage_location_id)
    log_transaction(
        session=session,
        user_id=user_id,
        event_type="audit_extra_moved",
        card_id=action.get("card_id"),
        finish=action.get("finish"),
        quantity_delta=0,
        destination_location=location.name if location else None,
        inventory_row_id=source_row_id,
        note=f"Audit: moved extra here — {_card_label(card)} (audit session {audit.id})",
        flush=True,
    )


def _apply_update_printing(session, audit, user_id, action) -> None:
    """Correct an expected row's printing to the scanned one (partial → match).
    Direct card_id update + audit trail — no Scryfall round-trip needed since the
    scanned card_id is already the operator-confirmed local printing."""
    row = _owned_row(session, action.get("inventory_row_id"), user_id)
    new_card = session.get(Card, action.get("card_id"))
    if row is None or new_card is None:
        return
    old_card = row.card
    row.card_id = new_card.id
    row.updated_at = utc_now()
    log_transaction(
        session=session,
        user_id=user_id,
        event_type="audit_printing_corrected",
        card_id=new_card.id,
        finish=row.finish,
        quantity_delta=0,
        inventory_row_id=row.id,
        note=(
            f"Audit: printing corrected {_card_label(old_card)} → "
            f"{_card_label(new_card)} (audit session {audit.id})"
        ),
        flush=True,
    )


_APPLIERS = {
    "mark_missing": _apply_mark_missing,
    "add_extra": _apply_add_extra,
    "move_extra_here": _apply_move_extra,
    "update_printing": _apply_update_printing,
}
_IGNORE_TYPES = {"ignore_missing", "ignore_extra", "ignore_partial"}


def apply_reconciliation(
    session: Session, audit_session_id: int, user_id: int, actions: list[dict]
) -> ReconciliationResult:
    """Apply the operator-approved changeset. Rejects the whole batch up front if
    the location's inventory changed since the audit began (optimistic
    concurrency), then applies each mutating action, and finally completes the
    audit + writes its log."""
    audit = _owned_audit(session, audit_session_id, user_id)

    ok, changes = validate_snapshot(session, audit_session_id)
    if not ok:
        return ReconciliationResult(
            success=False, actions_applied=0, snapshot_conflict=True, changes=changes
        )

    # Counts reflect what the audit FOUND (pre-mutation state); capture before
    # add/move actions change the location's inventory.
    progress = get_scan_progress(session, audit_session_id, user_id)

    applied = 0
    for action in actions:
        atype = (action or {}).get("type")
        if atype in _IGNORE_TYPES:
            continue
        applier = _APPLIERS.get(atype)
        if applier is None:
            continue
        applier(session, audit, user_id, action)
        applied += 1

    _completed, log = complete_audit(
        session,
        audit_session_id,
        user_id,
        cards_expected=progress.total_expected,
        cards_seen=progress.total_seen,
        cards_missing=progress.total_remaining,
        cards_extra=progress.total_extras,
        actions_applied=actions,
    )
    return ReconciliationResult(
        success=True,
        actions_applied=applied,
        snapshot_conflict=False,
        changes=[],
        audit_log_id=log.id,
    )


def list_audit_history(
    session: Session, user_id: int, storage_location_id: int, limit: int = 10
) -> list[AuditLog]:
    """Completed-audit records for a location, most recent first (for the
    location detail page's audit history + "last audited" line)."""
    return (
        session.query(AuditLog)
        .filter(
            AuditLog.user_id == user_id,
            AuditLog.storage_location_id == storage_location_id,
        )
        .order_by(AuditLog.completed_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )


# --- Audit hub (cross-location overviews) ------------------------------------


def list_auditable_locations(session: Session, user_id: int) -> list[dict]:
    """Every storage location the user owns, with its card count and last-audit
    summary — the Audit hub's "Locations" table.

    Sort: never-audited first (by card_count desc — the biggest un-audited piles
    surface at the top), then audited by ``last_audited_at`` ascending (stalest
    first, so the longest-unverified location is next in line)."""
    locations = session.query(StorageLocation).filter(StorageLocation.user_id == user_id).all()
    counts = dict(
        session.query(
            InventoryRow.storage_location_id,
            func.coalesce(func.sum(InventoryRow.quantity), 0),
        )
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id.isnot(None),
        )
        .group_by(InventoryRow.storage_location_id)
        .all()
    )
    # Most-recent completed audit per location (first seen in desc order wins),
    # split by scope: staleness ("last audited" / "Never") is driven by the last
    # FULL audit; scoped audits are surfaced separately and never reset staleness.
    last_full: dict[int, AuditLog] = {}
    last_scoped: dict[int, AuditLog] = {}
    for log in (
        session.query(AuditLog)
        .filter(AuditLog.user_id == user_id)
        .order_by(AuditLog.completed_at.desc(), AuditLog.id.desc())
        .all()
    ):
        target = last_scoped if log.scope else last_full
        target.setdefault(log.storage_location_id, log)

    out: list[dict] = []
    for loc in locations:
        last = last_full.get(loc.id)
        scoped = last_scoped.get(loc.id)
        out.append(
            {
                "location_id": loc.id,
                "name": loc.name,
                "type": loc.type,
                "card_count": int(counts.get(loc.id, 0)),
                "last_audited_at": last.completed_at if last else None,
                "last_scoped_audit_at": scoped.completed_at if scoped else None,
                "last_audit_summary": (
                    {
                        "seen": last.cards_seen,
                        "missing": last.cards_missing,
                        "extras": last.cards_extra,
                    }
                    if last
                    else None
                ),
            }
        )

    out.sort(
        key=lambda d: (
            0 if d["last_audited_at"] is None else 1,  # never-audited first
            -d["card_count"] if d["last_audited_at"] is None else 0,  # then biggest pile
            d["last_audited_at"] or datetime.min.replace(tzinfo=UTC),  # then stalest first
        )
    )
    return out


def list_all_audit_history(session: Session, user_id: int, limit: int = 25) -> list[dict]:
    """Completed audits across ALL locations, most recent first (the hub's Recent
    Audits table). Cross-location counterpart of :func:`list_audit_history`."""
    rows = (
        session.query(AuditLog, StorageLocation.name)
        .outerjoin(StorageLocation, AuditLog.storage_location_id == StorageLocation.id)
        .filter(AuditLog.user_id == user_id)
        .order_by(AuditLog.completed_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )
    result = []
    for log, loc_name in rows:
        scope = _parse_scope(log.scope)
        result.append(
            {
                "completed_at": log.completed_at,
                "location_name": loc_name or "—",
                "cards_expected": log.cards_expected,
                "cards_seen": log.cards_seen,
                "cards_missing": log.cards_missing,
                "cards_extra": log.cards_extra,
                "actions_count": len(json.loads(log.actions_applied or "[]")),
                "scope_sets": sorted(scope) if scope else None,
            }
        )
    return result
