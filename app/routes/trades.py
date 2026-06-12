"""Pairwise trading routes (v3.29.2).

APIRouter-based, mounted via ``app.include_router(trades.router)`` in
``app/main.py``. GETs use ``get_current_user`` (anon → 303 to /login);
POSTs additionally take ``CsrfRequired``. All mutations are authority-
gated at the service layer (``transition_trade`` checks the actor;
``create_trade`` checks both parties' playgroup membership + the
recipient's Share).

**Non-leakage discipline.** ``GET /trades/{id}`` returns to the user's
``/trades`` page with an error code rather than 403 when the viewer is
not a party — keeps the existence of a trade id non-leaky (same
posture as ``/playgroups/{id}`` + ``/shares/{id}``).

**Two initiation flows feed one construction page** (decision D2):

  - Standalone ``GET /trades/new`` — picker for recipient + playgroup
    (across all of the proposer's co-member Shares).
  - Propose-from-share ``GET /trades/new?from_showcase_item={id}`` —
    recipient + playgroup pre-resolved from the ShowcaseItem's
    Showcase and Share; the item pre-added to the requested side.

The propose-from-share entry is a per-card link on the v3.29.1
``share_view.html`` template; no route here owns it.

**Trade-item rendering reuses the v3.29.1 sanitized projection** (§8
of the spec). Both proposer + recipient see each other's items —
the privacy hard-flag (no InventoryRow private fields surfaced)
applies in both directions.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import trade_service
from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
)
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trades")


# ── Inbox ───────────────────────────────────────────────────────


@router.get("")
def trades_inbox(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Inbox: incoming pending, sent pending, recent terminal."""
    data = trade_service.list_trades_for_user(session, current_user.id)
    error = request.query_params.get("error")
    success = request.query_params.get("success")
    return render(
        request,
        "trades.html",
        {
            "title": "Trades",
            "current_user": current_user,
            "incoming": data["incoming"],
            "sent": data["sent"],
            "recent": data["recent"],
            "error": error,
            "success": success,
        },
    )


# ── Construction ────────────────────────────────────────────────


@router.get("/new")
def trades_new_page(
    request: Request,
    from_showcase_item: int | None = None,
    recipient_user_id: int | None = None,
    playgroup_id: int | None = None,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Construction page. Two initiation modes settle here.

    With ``from_showcase_item`` present, the recipient + playgroup are
    locked to that ShowcaseItem's context and the item is prefilled
    on the requested side. Without it, the page presents a picker for
    (recipient, playgroup) pairs across the proposer's co-member
    Shares; selecting one populates the requested-side picker.

    Unresolvable ``from_showcase_item`` (item gone, recipient + proposer
    share no playgroup, etc.) silently degrades to the standalone
    flow so the user gets a usable construction page rather than an
    error.
    """
    pre_recipient = None
    pre_playgroup = None
    pre_locked = False
    prefilled_requested = []

    if from_showcase_item:
        resolved = trade_service.resolve_propose_from_showcase_item(
            session, current_user.id, from_showcase_item
        )
        if resolved is not None:
            pre_recipient = resolved["recipient"]
            pre_playgroup = resolved["playgroup"]
            pre_locked = True
            # Push the showcase_item_id as the prefilled requested item.
            si = resolved["showcase_item"]
            inv = si.inventory_row
            if inv is not None and inv.card is not None:
                available = max(0, min(si.quantity_offered, inv.quantity))
                prefilled_requested.append(
                    {
                        "showcase_item_id": si.id,
                        "card": inv.card,  # raw access only inside the prefill summary
                        "finish": inv.finish,
                        "available": available,
                        "is_proxy": bool(inv.is_proxy),
                    }
                )

    # Override picker selection with explicit query params (when the user
    # picks from the standalone /trades/new selector and the page reloads).
    if not pre_locked and recipient_user_id and playgroup_id:
        # Confirm the pair is among the proposer's candidates before
        # presenting the requested-side picker.
        opts = trade_service.get_construction_options(
            session, current_user.id, recipient_user_id, playgroup_id
        )
        if opts["recipient_share_items"]:
            # Set the picker selection state.
            for cand in opts["recipients"]:
                if cand["user"].id == recipient_user_id and cand["playgroup"].id == playgroup_id:
                    pre_recipient = cand["user"]
                    pre_playgroup = cand["playgroup"]
                    break
        # Render with these options.
        return render(
            request,
            "trade_new.html",
            {
                "title": "New trade",
                "current_user": current_user,
                "pre_locked": False,
                "pre_recipient": pre_recipient,
                "pre_playgroup": pre_playgroup,
                "options": opts,
                "prefilled_requested": prefilled_requested,
                "error": request.query_params.get("error"),
            },
        )

    options = trade_service.get_construction_options(
        session,
        current_user.id,
        pre_recipient.id if pre_recipient else None,
        pre_playgroup.id if pre_playgroup else None,
    )
    return render(
        request,
        "trade_new.html",
        {
            "title": "New trade",
            "current_user": current_user,
            "pre_locked": pre_locked,
            "pre_recipient": pre_recipient,
            "pre_playgroup": pre_playgroup,
            "options": options,
            "prefilled_requested": prefilled_requested,
            "error": request.query_params.get("error"),
        },
    )


@router.post("")
def trades_create(
    request: Request,
    recipient_user_id: int = Form(...),
    playgroup_id: int = Form(...),
    # Each side is submitted as JSON-encoded array. The construction
    # template builds the JSON via hidden inputs in the client; this
    # keeps the multi-row item submission shape transport-independent
    # of FastAPI's list-from-form quirks (which require explicit
    # ``List[int] = Form(...)`` annotations and don't preserve grouping
    # across heterogenous fields).
    offered_json: str = Form("[]"),
    requested_json: str = Form("[]"),
    proposer_note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        offered = json.loads(offered_json or "[]")
        requested = json.loads(requested_json or "[]")
    except json.JSONDecodeError:
        return RedirectResponse(url="/trades/new?error=invalid_submission", status_code=303)
    if not isinstance(offered, list) or not isinstance(requested, list):
        return RedirectResponse(url="/trades/new?error=invalid_submission", status_code=303)
    try:
        trade = trade_service.create_trade(
            session,
            proposer_user_id=current_user.id,
            recipient_user_id=recipient_user_id,
            playgroup_id=playgroup_id,
            offered=offered,
            requested=requested,
            proposer_note=proposer_note,
        )
    except ValueError as err:
        logger.info("create_trade validation error: %s", err)
        return RedirectResponse(
            url=f"/trades/new?error={_safe_error_code(str(err))}",
            status_code=303,
        )
    return RedirectResponse(url=f"/trades/{trade.id}?success=proposed", status_code=303)


def _safe_error_code(message: str) -> str:
    """Compact a free-text error to a URL-safe code for ``?error=...``.
    Inverse of the templates' ``?error=foo`` → friendly-string switch.
    Keep it cheap; the template just renders the raw fallback when no
    code matches."""
    return (
        (
            message.strip()
            .lower()
            .replace(" ", "_")
            .replace(".", "")
            .replace(",", "")
            .replace("'", "")[:64]
        )
        or "validation_error"
    )


# ── Detail + transitions ────────────────────────────────────────


@router.get("/{trade_id}")
def trades_detail(
    trade_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    detail = trade_service.get_trade_detail(session, current_user.id, trade_id)
    if detail is None:
        return RedirectResponse(url="/trades?error=trade_unavailable", status_code=303)
    return render(
        request,
        "trade_detail.html",
        {
            "title": "Trade",
            "current_user": current_user,
            "trade": detail["trade"],
            "offered_items": detail["offered_items"],
            "requested_items": detail["requested_items"],
            "offered_total": detail["offered_total"],
            "requested_total": detail["requested_total"],
            "has_proxy": detail["has_proxy"],
            "viewer_is_proposer": detail["viewer_is_proposer"],
            "viewer_is_recipient": detail["viewer_is_recipient"],
            "error": request.query_params.get("error"),
            "success": request.query_params.get("success"),
        },
    )


@router.post("/{trade_id}/accept")
def trades_accept(
    trade_id: int,
    request: Request,
    recipient_note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        trade_service.transition_trade(
            session,
            trade_id=trade_id,
            actor_user_id=current_user.id,
            new_status="accepted",
            recipient_note=recipient_note,
        )
    except ValueError as err:
        logger.info("trade accept rejected: %s", err)
        return RedirectResponse(
            url=f"/trades/{trade_id}?error={_safe_error_code(str(err))}",
            status_code=303,
        )
    return RedirectResponse(url=f"/trades/{trade_id}?success=accepted", status_code=303)


@router.post("/{trade_id}/decline")
def trades_decline(
    trade_id: int,
    request: Request,
    recipient_note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        trade_service.transition_trade(
            session,
            trade_id=trade_id,
            actor_user_id=current_user.id,
            new_status="declined",
            recipient_note=recipient_note,
        )
    except ValueError as err:
        logger.info("trade decline rejected: %s", err)
        return RedirectResponse(
            url=f"/trades/{trade_id}?error={_safe_error_code(str(err))}",
            status_code=303,
        )
    return RedirectResponse(url=f"/trades/{trade_id}?success=declined", status_code=303)


@router.post("/{trade_id}/cancel")
def trades_cancel(
    trade_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        trade_service.transition_trade(
            session,
            trade_id=trade_id,
            actor_user_id=current_user.id,
            new_status="cancelled",
        )
    except ValueError as err:
        logger.info("trade cancel rejected: %s", err)
        return RedirectResponse(
            url=f"/trades/{trade_id}?error={_safe_error_code(str(err))}",
            status_code=303,
        )
    return RedirectResponse(url=f"/trades/{trade_id}?success=cancelled", status_code=303)
