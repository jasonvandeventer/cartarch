"""Physical Audit Mode — scan-loop routes (issue #73, Phase 2).

The audit workspace scopes a reconciliation pass to one storage location: the
operator scans each physical card, the app classifies it match/partial/extra
against the location's expected inventory, and progress updates over HTMX with
no full-page reload. Nothing mutates inventory here — the reconciliation
changeset lands in Phase 3 (``/audit/{id}/reconcile`` is a placeholder).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import audit_service
from app.dependencies import CsrfRequired, get_current_user, get_db_session, render
from app.import_service import normalize_finish
from app.location_service import get_location
from app.models import AuditLog, AuditSession, Card, InventoryRow, StorageLocation, User

router = APIRouter()


def _load_owned_audit(session: Session, session_id: int, user_id: int) -> AuditSession:
    """Fetch an audit owned by the user, or 404 (never leak another user's audit)."""
    audit = session.get(AuditSession, session_id)
    if audit is None or audit.user_id != user_id:
        raise HTTPException(status_code=404, detail="Audit not found")
    return audit


def _render_workspace(request: Request, session: Session, audit: AuditSession, user: User):
    location = session.get(StorageLocation, audit.storage_location_id)
    progress = audit_service.get_scan_progress(session, audit.id, user.id)
    extras = audit_service.list_extras(session, audit.id, user.id)
    return render(
        request,
        "audit_workspace.html",
        {
            "title": f"Auditing: {location.name}",
            "location": location,
            "audit": audit,
            "progress": progress,
            "extras": extras,
            "current_user": user,
        },
    )


@router.get("/audit")
def audit_hub(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Physical Audit Mode hub: any in-progress audit, the auditable-locations
    table, and cross-location recent-audit history."""
    # get_active_audit also triggers the lazy 24h timeout (may auto-abandon +
    # write), so commit to persist that cleanup.
    active = audit_service.get_active_audit(session, current_user.id)
    active_location = None
    active_progress = None
    if active is not None:
        active_location = session.get(StorageLocation, active.storage_location_id)
        active_progress = audit_service.get_scan_progress(session, active.id, current_user.id)
    session.commit()

    return render(
        request,
        "audit_hub.html",
        {
            "title": "Audit",
            "active_audit": active,
            "active_location": active_location,
            "active_progress": active_progress,
            "auditable_locations": audit_service.list_auditable_locations(session, current_user.id),
            "audit_history": audit_service.list_all_audit_history(
                session, current_user.id, limit=25
            ),
            "current_user": current_user,
        },
    )


@router.get("/audit/start")
def audit_start(
    request: Request,
    location_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    location = get_location(session, location_id=location_id, user_id=current_user.id)
    if location is None:
        raise HTTPException(status_code=404, detail="Location not found")

    active = audit_service.get_active_audit(session, current_user.id)
    if active is not None:
        if active.storage_location_id == location_id:
            # Same location — resume in place (reactivate if paused).
            if active.status == "paused":
                audit_service.resume_audit(session, active.id, current_user.id)
                session.commit()
            return _render_workspace(request, session, active, current_user)
        # A different location is mid-audit — confirm the switch before abandoning.
        active_location = session.get(StorageLocation, active.storage_location_id)
        return render(
            request,
            "audit_confirm_switch.html",
            {
                "title": "Audit in progress",
                "active_audit": active,
                "active_location": active_location,
                "target_location": location,
                "current_user": current_user,
            },
        )

    audit, _expected = audit_service.start_audit(session, current_user.id, location_id)
    session.commit()
    return _render_workspace(request, session, audit, current_user)


@router.post("/audit/{session_id}/scan")
def audit_scan(
    request: Request,
    session_id: int,
    card_id: int = Form(...),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    audit = _load_owned_audit(session, session_id, current_user.id)
    result = audit_service.record_scan(
        session,
        session_id,
        current_user.id,
        card_id=card_id,
        finish=normalize_finish(finish),
        quantity=quantity,
    )
    session.commit()

    progress = audit_service.get_scan_progress(session, audit.id, current_user.id)
    extras = audit_service.list_extras(session, audit.id, current_user.id)
    return render(
        request,
        "_audit_scan_response.html",
        {
            "result": result,
            "audit": audit,
            "progress": progress,
            "extras": extras,
            "current_user": current_user,
        },
    )


@router.post("/audit/{session_id}/pause")
def audit_pause(
    session_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    audit = _load_owned_audit(session, session_id, current_user.id)
    audit_service.pause_audit(session, session_id, current_user.id)
    session.commit()
    return RedirectResponse(url=f"/locations/{audit.storage_location_id}", status_code=303)


@router.post("/audit/{session_id}/abandon")
def audit_abandon(
    session_id: int,
    next: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    audit = _load_owned_audit(session, session_id, current_user.id)
    location_id = audit.storage_location_id
    audit_service.abandon_audit(session, session_id, current_user.id)
    session.commit()
    # `next` lets the "abandon & switch" confirmation land straight on the new
    # audit's start URL; only same-origin relative paths, else the location page.
    dest = (
        next
        if (next.startswith("/") and not next.startswith("//"))
        else f"/locations/{location_id}"
    )
    return RedirectResponse(url=dest, status_code=303)


@router.post("/audit/{session_id}/end")
def audit_end(
    session_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    _load_owned_audit(session, session_id, current_user.id)
    # Scanning is done; hand off to reconciliation (Phase 3 does the delta apply).
    return RedirectResponse(url=f"/audit/{session_id}/reconcile", status_code=303)


def _render_reconcile(request, session, audit, current_user, *, conflict=False, changes=None):
    delta = audit_service.compute_delta(session, audit.id, current_user.id)
    ok, snapshot_changes = audit_service.validate_snapshot(session, audit.id)
    location = session.get(StorageLocation, audit.storage_location_id)
    return render(
        request,
        "audit_reconcile.html",
        {
            "title": "Reconciliation",
            "audit": audit,
            "location": location,
            "delta": delta,
            "snapshot_ok": ok and not conflict,
            "changes": changes if changes is not None else snapshot_changes,
            "current_user": current_user,
        },
    )


@router.get("/audit/{session_id}/reconcile")
def audit_reconcile(
    request: Request,
    session_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    audit = _load_owned_audit(session, session_id, current_user.id)
    if audit.status == "completed":
        return RedirectResponse(url=f"/audit/{session_id}/complete", status_code=303)
    return _render_reconcile(request, session, audit, current_user)


@router.post("/audit/{session_id}/reconcile")
async def audit_reconcile_apply(
    request: Request,
    session_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    audit = _load_owned_audit(session, session_id, current_user.id)
    if audit.status == "completed":
        return RedirectResponse(url=f"/audit/{session_id}/complete", status_code=303)

    # Build the action list server-side from the delta + the operator's per-item
    # choices. Deriving from compute_delta (not raw form keys) means the form can
    # only pick a TYPE for a real delta item — it can't inject arbitrary row ids.
    form = await request.form()
    delta = audit_service.compute_delta(session, session_id, current_user.id)
    actions: list[dict] = []

    for m in delta.missing:
        rid = m["inventory_row_id"]
        atype = form.get(f"missing_action_{rid}", "mark_missing")
        actions.append({"type": atype, "inventory_row_id": rid, "quantity": m["quantity_missing"]})

    for e in delta.extras:
        key = f"{e['card_id']}_{e['finish']}"
        atype = form.get(f"extra_action_{key}", "ignore_extra")
        action = {
            "type": atype,
            "card_id": e["card_id"],
            "finish": e["finish"],
            "quantity": e["quantity_scanned"],
        }
        if atype == "move_extra_here":
            source_row_id = form.get(f"extra_source_{key}")
            action["inventory_row_id"] = int(source_row_id) if source_row_id else None
        actions.append(action)

    for p in delta.partial_matches:
        key = f"{p['inventory_row_id']}_{p['scanned_card_id']}"
        atype = form.get(f"partial_action_{key}", "ignore_partial")
        actions.append(
            {
                "type": atype,
                "inventory_row_id": p["inventory_row_id"],
                "card_id": p["scanned_card_id"],
            }
        )

    result = audit_service.apply_reconciliation(session, session_id, current_user.id, actions)
    if not result.success and result.snapshot_conflict:
        session.rollback()
        return _render_reconcile(
            request, session, audit, current_user, conflict=True, changes=result.changes
        )

    session.commit()
    return RedirectResponse(url=f"/audit/{session_id}/complete", status_code=303)


@router.get("/audit/{session_id}/complete")
def audit_complete(
    request: Request,
    session_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    audit = _load_owned_audit(session, session_id, current_user.id)
    log = (
        session.query(AuditLog)
        .filter(AuditLog.audit_session_id == session_id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    if log is None:
        # Not reconciled yet — send the operator back to finish the job.
        return RedirectResponse(url=f"/audit/{session_id}/reconcile", status_code=303)
    location = session.get(StorageLocation, audit.storage_location_id)
    return render(
        request,
        "audit_complete.html",
        {
            "title": "Audit complete",
            "audit": audit,
            "location": location,
            "log": log,
            "actions_applied": json.loads(log.actions_applied or "[]"),
            "current_user": current_user,
        },
    )


@router.get("/audit/api/card-search")
def audit_card_search(
    session_id: int,
    q: str = "",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Typeahead over the user's OWNED cards (bounded, unlike the global Scryfall
    autocomplete), flagging which are in this audit's expected set so the UI can
    highlight expected vs. not-expected. Returns local ``card_id`` for the scan
    form (the deck autocomplete returns Scryfall IDs, which don't fit here)."""
    audit = _load_owned_audit(session, session_id, current_user.id)
    q = q.strip()
    if len(q) < 2:
        return JSONResponse([])

    expected_card_ids = {
        r.card_id
        for r in session.query(InventoryRow.card_id)
        .filter(
            InventoryRow.user_id == current_user.id,
            InventoryRow.storage_location_id == audit.storage_location_id,
        )
        .all()
    }

    cards = (
        session.query(Card)
        .join(InventoryRow, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == current_user.id, Card.name.ilike(f"%{q}%"))
        .distinct()
        .order_by(Card.name.asc())
        .limit(30)
        .all()
    )
    out = [
        {
            "card_id": c.id,
            "name": c.name,
            "set_code": (c.set_code or "").upper(),
            "collector_number": c.collector_number,
            "expected": c.id in expected_card_ids,
        }
        for c in cards
    ]
    # Expected cards first, then alphabetical.
    out.sort(key=lambda r: (not r["expected"], r["name"]))
    return JSONResponse(out)
