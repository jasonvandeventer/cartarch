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

from app import sort_spec, trade_service
from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
)
from app.models import User

# One page size, one slicing helper — the location grid solved this first
# (v4.13.27) and a second copy is how two surfaces drift.
from app.routes.collections import _paginate_items

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


def _pane_context(items: list[dict], endpoint: str, page: int = 1, view_mode: str = "grid") -> dict:
    """First-page context for one picker pane, in the shape the partial wants."""
    items = sort_spec.sort_showcase_items(list(items), "name", "asc")
    page_items, page, total_pages = _paginate_items(items, page, PICK_PAGE_SIZE)
    return {
        # NOT "items": Jinja resolves `.items` on a dict to the dict METHOD, so
        # `{% for x in pane.items %}` iterates a builtin and 500s the page. The
        # wishlist view learned this the same way (`cards`, never `items`).
        "cards": page_items,
        "page": page,
        "total_pages": total_pages,
        "total": len(items),
        "endpoint": endpoint,
        "view_mode": view_mode,
        "sort_options": sort_spec.PICKER_SORT_OPTIONS,
    }


def _hydrate_picks(items: list[dict], entries: list[dict]) -> list[dict]:
    """Turn submitted picks into tray entries carrying their own name/price.

    The tray has to show a pick whose TILE is not on screen — another page, or
    behind a different search — so the values come from the server's own list
    rather than from a DOM lookup that would find nothing. An entry naming
    something no longer pickable is dropped, which is the same answer the
    validator would give it.
    """
    from app.pricing import effective_price

    by_key = {(i["pick_kind"], i["pick_id"]): i for i in items}
    also_by_row = {i.get("inventory_row_id"): i for i in items if i.get("inventory_row_id")}
    also_by_item = {i.get("showcase_item_id"): i for i in items if i.get("showcase_item_id")}
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        found = None
        for kind in ("inventory_row_id", "showcase_item_id", "trade_item_id"):
            if not e.get(kind):
                continue
            found = by_key.get((kind, int(e[kind])))
            if found is None and kind == "inventory_row_id":
                found = also_by_row.get(int(e[kind]))
            if found is None and kind == "showcase_item_id":
                found = also_by_item.get(int(e[kind]))
            if found is not None:
                break
        if found is None:
            continue
        price = 0.0 if found["is_proxy"] else (effective_price(found["card"], found["finish"]) or 0)
        out.append(
            {
                "kind": found["pick_kind"],
                "id": found["pick_id"],
                "alt": found.get("pick_alt", ""),
                "name": found["card"].name,
                "price": price,
                "proxy": bool(found["is_proxy"]),
                "meta": f"{(found['card'].set_code or '?').upper()} "
                f"#{found['card'].collector_number or '?'}",
                "quantity": max(1, int(e.get("quantity") or 1)),
                "available": found.get("available") or 1,
            }
        )
    return out


def _construction_response(
    request: Request,
    current_user: User,
    options: dict,
    *,
    pre_locked: bool = False,
    pre_recipient=None,
    pre_playgroup=None,
    prefilled_requested: list | None = None,
    prefilled_offered_row_ids: list[int] | None = None,
    error: str | None = None,
    restore_picks: dict | None = None,
):
    """ONE context for the construction page — three callers reach it.

    The third is the POST's rejection path, which RE-RENDERS rather than
    redirecting (#: "an error when creating a trade proposal should not wipe out
    the trade"). A redirect to /trades/new dropped the recipient, the playgroup
    and every pick, because the in-progress selection lives only in the page's
    JS Maps. ``restore_picks`` hands those picks back to the page.
    """
    # #184 — each side is a paged pane now. The first page is rendered here so a
    # cold load needs no round-trip; everything after that is an HTMX swap of
    # the pane alone, which is what keeps the in-progress selection alive.
    requested_items = options.get("recipient_share_items") or []
    offered_items = options.get("proposer_inventory") or []
    endpoint_base = (
        f"?recipient_user_id={pre_recipient.id if pre_recipient else 0}"
        f"&playgroup_id={pre_playgroup.id if pre_playgroup else 0}"
    )
    # A prefilled or rejected pick becomes a TRAY entry, hydrated from the
    # server's own lists — its tile is very often not on the first page.
    view_mode = current_user.trade_view_mode or "grid"
    tray = restore_picks or {}
    if prefilled_requested:
        tray = dict(tray)
        tray.setdefault(
            "requested",
            [
                {
                    "showcase_item_id": it.get("showcase_item_id"),
                    "inventory_row_id": it.get("inventory_row_id"),
                    "quantity": 1,
                }
                for it in prefilled_requested
            ],
        )
    if prefilled_offered_row_ids:
        tray = dict(tray)
        tray.setdefault(
            "offered",
            [{"inventory_row_id": rid, "quantity": 1} for rid in prefilled_offered_row_ids],
        )
    hydrated = {
        "requested": _hydrate_picks(requested_items, tray.get("requested") or []),
        "offered": _hydrate_picks(offered_items, tray.get("offered") or []),
    }
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
            "requested_pane": _pane_context(
                requested_items, f"/trades/picker/requested{endpoint_base}", view_mode=view_mode
            ),
            "offered_pane": _pane_context(
                offered_items, f"/trades/picker/offered{endpoint_base}", view_mode=view_mode
            ),
            "view_mode": view_mode,
            "trade_even_within": trade_service.TRADE_EVEN_WITHIN,
            "prefilled_requested": prefilled_requested or [],
            "prefilled_offered_row_ids": prefilled_offered_row_ids or [],
            "error": error,
            "restore_picks": hydrated if (hydrated["requested"] or hydrated["offered"]) else None,
        },
    )


# ── Construction ────────────────────────────────────────────────


@router.get("/new")
def trades_new_page(
    request: Request,
    from_showcase_item: int | None = None,
    # A MIRRORED card has no ShowcaseItem, so the share page points at its ROW
    # instead. Same resolution, same guards — see resolve_propose_from_share_row.
    from_showcase_row: int | None = None,
    from_wishlist_user: int | None = None,
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
    prefilled_offered_row_ids: list[int] = []

    # Wishlist entry (#146/#147 follow-up): mirror of the propose-from-share
    # flow — locks the (recipient, playgroup) context and seeds the OFFERED
    # side. A6 unchanged: the user still picks >= 1 requested item here.
    if from_wishlist_user and not from_showcase_item:
        resolved = trade_service.resolve_propose_from_wishlist(
            session, current_user.id, from_wishlist_user
        )
        if resolved is not None:
            pre_recipient = resolved["recipient"]
            pre_playgroup = resolved["playgroup"]
            pre_locked = True
            prefilled_offered_row_ids = resolved["offered_row_ids"]

    if from_showcase_item or from_showcase_row:
        resolved = (
            trade_service.resolve_propose_from_showcase_item(
                session, current_user.id, from_showcase_item
            )
            if from_showcase_item
            else trade_service.resolve_propose_from_share_row(
                session, current_user.id, from_showcase_row
            )
        )
        if resolved is not None:
            pre_recipient = resolved["recipient"]
            pre_playgroup = resolved["playgroup"]
            pre_locked = True
            # Prefill the requested side. A mirrored card resolves with no
            # ShowcaseItem, so the row carries both the identity and the
            # available quantity.
            si = resolved.get("showcase_item")
            inv = si.inventory_row if si is not None else resolved.get("inventory_row")
            if inv is not None and inv.card is not None:
                available = (
                    max(0, min(si.quantity_offered, inv.quantity))
                    if si is not None
                    else inv.quantity
                )
                prefilled_requested.append(
                    {
                        "showcase_item_id": si.id if si is not None else None,
                        "inventory_row_id": inv.id,
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
        return _construction_response(
            request,
            current_user,
            opts,
            pre_recipient=pre_recipient,
            pre_playgroup=pre_playgroup,
            prefilled_requested=prefilled_requested,
            prefilled_offered_row_ids=prefilled_offered_row_ids,
            error=request.query_params.get("error"),
        )

    options = trade_service.get_construction_options(
        session,
        current_user.id,
        pre_recipient.id if pre_recipient else None,
        pre_playgroup.id if pre_playgroup else None,
    )
    return _construction_response(
        request,
        current_user,
        options,
        pre_locked=pre_locked,
        pre_recipient=pre_recipient,
        pre_playgroup=pre_playgroup,
        prefilled_requested=prefilled_requested,
        prefilled_offered_row_ids=prefilled_offered_row_ids,
        error=request.query_params.get("error"),
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
        return _reject_creation(
            request,
            session,
            current_user,
            recipient_user_id,
            playgroup_id,
            "invalid_submission",
            [],
            [],
        )
    if not isinstance(offered, list) or not isinstance(requested, list):
        return _reject_creation(
            request,
            session,
            current_user,
            recipient_user_id,
            playgroup_id,
            "invalid_submission",
            [],
            [],
        )
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
        return _reject_creation(
            request,
            session,
            current_user,
            recipient_user_id,
            playgroup_id,
            _safe_error_code(str(err)),
            offered,
            requested,
        )
    return RedirectResponse(url=f"/trades/{trade.id}?success=proposed", status_code=303)


def _pick_ids(entries: list, key: str) -> list[dict]:
    """Submitted picks → ``[{"id": int, "quantity": int}]``, junk dropped.

    The payload is client-built and has already failed validation once, so
    every entry is treated as hostile: a non-dict, a missing id or an
    unparseable quantity is skipped rather than raising a SECOND error on the
    page whose job is to show the FIRST one.
    """
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        try:
            item_id = int(e.get(key))
            qty = int(e.get("quantity") or 1)
        except (TypeError, ValueError):
            continue
        # Emit the KIND key, not a bare "id": the hydration step matches picks
        # against the server's own lists by (kind, id), and a bare id cannot say
        # which id space it belongs to.
        out.append({key: item_id, "quantity": max(1, qty)})
    return out


def _reject_creation(
    request: Request,
    session: Session,
    current_user: User,
    recipient_user_id: int | None,
    playgroup_id: int | None,
    error: str,
    offered: list,
    requested: list,
):
    """Re-render the construction page with the error AND the picks intact.

    Reported 2026-08-21: "an error when creating a trade proposal should not
    wipe out the trade". It redirected to a bare /trades/new, and the selection
    lives only in the page's JS Maps, so every pick — plus the recipient and
    playgroup — was gone by the time the message was readable.

    The picks are handed back as ``restore_picks``; the page seeds its Maps from
    it at boot. NOTHING is trusted from the payload beyond ids and quantities,
    and the ids only select among the options the server itself renders — an id
    for a card that is not on the page simply restores nothing.
    """
    options = trade_service.get_construction_options(
        session, current_user.id, recipient_user_id, playgroup_id
    )
    pre_recipient = None
    pre_playgroup = None
    for cand in options["recipients"]:
        if cand["user"].id == recipient_user_id and cand["playgroup"].id == playgroup_id:
            pre_recipient = cand["user"]
            pre_playgroup = cand["playgroup"]
            break
    restore = {
        "offered": _pick_ids(offered, "inventory_row_id"),
        "requested": _pick_ids(requested, "showcase_item_id"),
    }
    return _construction_response(
        request,
        current_user,
        options,
        pre_recipient=pre_recipient,
        pre_playgroup=pre_playgroup,
        error=error,
        restore_picks=restore,
    )


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


# ── The paged picker (#184) ─────────────────────────────────────

PICK_PAGE_SIZE = 50


def _pick_pane(
    request: Request,
    side: str,
    items: list[dict],
    page: int,
    search: str,
    endpoint: str,
    view_mode: str = "grid",
    sort: str = "name",
    direction: str = "asc",
):
    """Render ONE side's grid + status + pager.

    The same partial serves the full page and the HTMX swap, so a searched page
    and a first render cannot drift — the deck-card-list seam's lesson, applied
    to the picker.
    """
    # Sorted with the SHARED spec over the WHOLE set, before the page is taken —
    # sorting the fifty cards on screen would reorder a window, which is the
    # thing #184 removed the client-side control for.
    items = sort_spec.sort_showcase_items(list(items), sort, direction)
    page_items, page, total_pages = _paginate_items(items, page, PICK_PAGE_SIZE)
    return render(
        request,
        "_trade_pick_grid.html",
        {
            "pick_side": side,
            # Same stored preference the trade detail page uses — "how I want a
            # trade's cards shown" is one answer, not one per screen.
            "pick_view_mode": view_mode,
            "pick_items": page_items,
            "pick_page": page,
            "pick_total_pages": total_pages,
            "pick_total": len(items),
            "pick_search": search,
            "pick_endpoint": endpoint,
            "pick_sort": sort,
            "pick_direction": direction,
            "pick_sort_options": sort_spec.PICKER_SORT_OPTIONS,
        },
    )


def _pick_items_for(
    session: Session,
    current_user: User,
    side: str,
    *,
    recipient_user_id: int | None,
    playgroup_id: int | None,
    trade_id: int | None,
    search: str,
) -> tuple[list[dict], str]:
    """The list for one side, plus the endpoint its pager should call.

    Authorisation is NOT re-invented here: the construction and counter option
    builders already answer "what may this person pick from", including the
    membership and share checks, so an id that is not theirs to see resolves to
    an empty list rather than to a 403 that would confirm it exists.
    """
    if trade_id:
        detail = trade_service.get_trade_detail(session, current_user.id, trade_id)
        if detail is None:
            return [], ""
        opts = trade_service.counter_options(session, detail["trade"], current_user.id)
        endpoint = f"/trades/picker/{side}?trade_id={trade_id}"
        if side == "offered":
            items = opts["offered_rows"] or opts["offered_share_items"]
        else:
            items = opts["requested_share_items"]
        if search.strip():
            needle = search.strip().lower()
            items = [i for i in items if needle in (i["card"].name or "").lower()]
        return items, endpoint

    endpoint = (
        f"/trades/picker/{side}?recipient_user_id={recipient_user_id or 0}"
        f"&playgroup_id={playgroup_id or 0}"
    )
    if side == "offered":
        return trade_service.offered_pick_items(session, current_user.id, search), endpoint
    opts = trade_service.get_construction_options(
        session, current_user.id, recipient_user_id, playgroup_id, requested_search=search
    )
    return opts["recipient_share_items"], endpoint


@router.get("/picker/{side}")
def trade_picker_pane(
    side: str,
    request: Request,
    recipient_user_id: int | None = None,
    playgroup_id: int | None = None,
    trade_id: int | None = None,
    q: str = "",
    page: int = 1,
    sort: str = "name",
    direction: str = "asc",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """One side of a picker, searched, SORTED and paged — the HTMX endpoint
    behind both trade screens."""
    if side not in ("offered", "requested"):
        return RedirectResponse(url="/trades", status_code=303)
    items, endpoint = _pick_items_for(
        session,
        current_user,
        side,
        recipient_user_id=recipient_user_id,
        playgroup_id=playgroup_id,
        trade_id=trade_id,
        search=q,
    )
    return _pick_pane(
        request,
        side,
        items,
        page,
        q,
        endpoint,
        current_user.trade_view_mode or "grid",
        sort_spec.normalize_sort(sort, sort_spec.PICKER_SORT_OPTIONS),
        sort_spec.normalize_direction(direction),
    )


# ── Counter-proposals ───────────────────────────────────────────


@router.get("/{trade_id}/counter")
def trade_counter_page(
    trade_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """The counter editor: the construction picker, seeded with the trade as it
    currently stands, for either party.

    Non-parties get the same non-leaky redirect a non-party gets everywhere else
    — never a 403, which would confirm the trade exists.
    """
    detail = trade_service.get_trade_detail(session, current_user.id, trade_id)
    if detail is None:
        return RedirectResponse(url="/trades?error=trade_unavailable", status_code=303)
    trade = detail["trade"]
    if trade.status != "proposed":
        return RedirectResponse(url=f"/trades/{trade_id}?error=trade_is_closed", status_code=303)

    opts = trade_service.counter_options(session, trade, current_user.id)
    return _counter_response(request, current_user, trade, opts, session)


@router.post("/{trade_id}/counter")
def trade_counter_submit(
    trade_id: int,
    request: Request,
    offered_json: str = Form("[]"),
    requested_json: str = Form("[]"),
    note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    detail = trade_service.get_trade_detail(session, current_user.id, trade_id)
    if detail is None:
        return RedirectResponse(url="/trades?error=trade_unavailable", status_code=303)
    trade = detail["trade"]
    try:
        offered = json.loads(offered_json or "[]")
        requested = json.loads(requested_json or "[]")
    except json.JSONDecodeError:
        offered, requested = [], []
        return _reject_counter(request, session, current_user, trade, "invalid_submission", [], [])
    if not isinstance(offered, list) or not isinstance(requested, list):
        return _reject_counter(request, session, current_user, trade, "invalid_submission", [], [])
    try:
        trade_service.counter_trade(
            session,
            trade_id=trade_id,
            author_user_id=current_user.id,
            offered=offered,
            requested=requested,
            note=note,
        )
    except ValueError as err:
        logger.info("counter_trade validation error: %s", err)
        return _reject_counter(
            request,
            session,
            current_user,
            trade,
            _safe_error_code(str(err)),
            offered,
            requested,
        )
    return RedirectResponse(url=f"/trades/{trade_id}?success=countered", status_code=303)


@router.post("/{trade_id}/decline-counter")
def trade_decline_counter(
    trade_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Reject the open counter — the trade goes back to what it was before it."""
    try:
        trade_service.decline_counter(session, trade_id, current_user.id)
    except ValueError as err:
        logger.info("decline_counter error: %s", err)
        return RedirectResponse(
            url=f"/trades/{trade_id}?error={_safe_error_code(str(err))}", status_code=303
        )
    return RedirectResponse(url=f"/trades/{trade_id}?success=counter_declined", status_code=303)


def _counter_response(
    request: Request,
    current_user: User,
    trade,
    opts: dict,
    session: Session,
    *,
    error: str | None = None,
    restore_picks: dict | None = None,
):
    """ONE context for the counter editor — the GET and the rejection path both
    land here, for the same reason /trades/new has a single builder: two
    hand-assembled contexts for one template drift."""
    other = trade.recipient if current_user.id == trade.proposer_user_id else trade.proposer
    if restore_picks is None:
        restore_picks = trade_service.current_picks_for_restore(trade, opts["author_is_proposer"])
    view_mode = current_user.trade_view_mode or "grid"
    offered_items = opts["offered_rows"] or opts["offered_share_items"]
    requested_items = opts["requested_share_items"]
    # A recipient's counter names the trade's OWN lines for cards the proposer
    # does not publicly share, so those become pickable tiles in their own right.
    if not opts["author_is_proposer"]:
        lines = _counter_line_items(trade)
        # DEDUPE by the underlying row. A card the proposer both offered AND
        # shares publicly reaches this list twice — once as the trade's own line
        # and once as a showcase item — and picking both would put the same
        # physical card on the trade twice. The LINE wins: it is the handle that
        # works whether or not the card is shared.
        on_trade = {i["inventory_row_id"] for i in lines if i.get("inventory_row_id")}
        offered_items = lines + [
            i for i in offered_items if i.get("inventory_row_id") not in on_trade
        ]
    return render(
        request,
        "trade_counter.html",
        {
            "title": "Counter-proposal",
            "current_user": current_user,
            "trade": trade,
            "other_party": other,
            "options": opts,
            "offered_pane": _pane_context(
                offered_items, f"/trades/picker/offered?trade_id={trade.id}", view_mode=view_mode
            ),
            "requested_pane": _pane_context(
                requested_items,
                f"/trades/picker/requested?trade_id={trade.id}",
                view_mode=view_mode,
            ),
            "view_mode": view_mode,
            "trade_even_within": trade_service.TRADE_EVEN_WITHIN,
            "restore_picks": {
                "offered": _hydrate_picks(offered_items, restore_picks.get("offered") or []),
                "requested": _hydrate_picks(requested_items, restore_picks.get("requested") or []),
            },
            "error": error,
        },
    )


def _counter_line_items(trade) -> list[dict]:
    """The trade's existing offered lines, as pickable tiles.

    A countering RECIPIENT cannot name the proposer's inventory rows and can
    only see what the proposer has SHARED, so without these a counter would
    silently drop every offered card the proposer keeps private (v4.14.0's
    third identity, now rendered rather than implied).
    """
    from app.trade_service import _items_by_side, _ReadOnlyCardProjection

    out = []
    for it in _items_by_side(trade, "offered"):
        if it.card is None:
            continue
        out.append(
            {
                "pick_kind": "trade_item_id",
                "pick_id": it.id,
                "pick_alt": "",
                "available": it.quantity,
                "card": _ReadOnlyCardProjection(it.card),
                "finish": it.finish,
                "is_proxy": False,
                "inventory_row_id": it.inventory_row_id,
            }
        )
    return out


def _sanitize_picks(entries: list) -> list[dict]:
    """Submitted counter picks, reduced to the three identity keys the resolver
    understands plus a quantity. A pick names itself — the editor lists the
    proposer's inventory, the proposer's shared showcase and the trade's own
    lines in one grid, and those id spaces would collide if the key were
    implied by position rather than carried.

    Anything else is dropped: the payload has already failed validation once.
    """
    keys = ("inventory_row_id", "showcase_item_id", "trade_item_id")
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        key = next((k for k in keys if e.get(k)), None)
        if key is None:
            continue
        try:
            item_id = int(e[key])
            qty = max(1, int(e.get("quantity") or 1))
        except (TypeError, ValueError):
            continue
        out.append({key: item_id, "quantity": qty})
    return out


def _reject_counter(
    request: Request,
    session: Session,
    current_user: User,
    trade,
    error: str,
    offered: list,
    requested: list,
):
    """Same rule as a rejected proposal: show the message on the page that has
    the work, never a redirect that throws the picks away."""
    opts = trade_service.counter_options(session, trade, current_user.id)
    restore = {
        "offered": _sanitize_picks(offered),
        "requested": _sanitize_picks(requested),
    }
    return _counter_response(
        request, current_user, trade, opts, session, error=error, restore_picks=restore
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
            "viewer_can_respond": detail["viewer_can_respond"],
            "view_mode": current_user.trade_view_mode or "grid",
            # The banner on a PROPOSED trade is static (nothing changes on this
            # page), but it has to say the same thing the live one would — hence
            # the shared summary and the shared threshold.
            "bal": trade_service.trade_balance_summary(
                detail["offered_total"],
                detail["requested_total"],
                detail["viewer_is_proposer"],
            ),
            "trade_even_within": trade_service.TRADE_EVEN_WITHIN,
            # Counter-proposals. The template enumerates its context key by key
            # (failure mode 1), so every one of these needs its line here.
            "revision_count": len(detail["trade"].revisions),
            "current_revision": trade_service.current_revision(detail["trade"]),
            "revision_diff": trade_service.trade_revision_diff(detail["trade"]),
            "viewer_authored_current": (
                trade_service.current_revision(detail["trade"]) is not None
                and trade_service.current_revision(detail["trade"]).author_user_id
                == current_user.id
            ),
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
