"""Collection sharing routes (v3.29.1).

APIRouter-based, mounted via ``app.include_router(sharing.router)`` in
``app/main.py``. GETs use ``get_current_user`` (anon → 303 to /login);
POSTs additionally take ``CsrfRequired``. All mutations are ownership
or membership-checked at the service layer.

**Non-leakage discipline.** ``GET /shares/{id}`` returns to the user's
``/shares`` page with an error code rather than 403 when the viewer is
not a member of the share's playgroup — keeps the existence of a
share with the given id non-leaky (same posture as
``/playgroups/{id}`` in :mod:`app.routes.playgroups`).

**Showcase ≠ Share.** Showcase routes (``/showcase``,
``/showcase/items/*``) manage the curated list. Share routes
(``/shares``, ``/shares/{id}/revoke``, ``/shares/{id}``) manage the
acts of exposing it.

**Add-to-Showcase entry point.** The inventory_card macro
(:mod:`app/templates/_macros.html`) gains a single
``POST /showcase/items/add`` form in its ``show_collection_actions``
block — the only way to add cards at v3.29.1 is from the card-action
drawer on /collection (per the spec's v1 scope).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import playgroup_service, share_service
from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
)
from app.models import User

router = APIRouter()


# ── Showcase management ─────────────────────────────────────────


@router.get("/showcase")
def showcase_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    data = share_service.get_showcase_with_items(session, current_user.id)
    error = request.query_params.get("error")
    success = request.query_params.get("success")
    return render(
        request,
        "showcase.html",
        {
            "title": "Showcase",
            "current_user": current_user,
            "showcase": data["showcase"],
            "items": data["items"],
            "error": error,
            "success": success,
        },
    )


@router.post("/showcase/edit")
def showcase_edit(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    share_service.update_showcase(session, current_user.id, name, description)
    return RedirectResponse(url="/showcase?success=updated", status_code=303)


@router.post("/showcase/items/add")
def showcase_item_add(
    request: Request,
    inventory_row_id: int = Form(...),
    quantity_offered: int = Form(1),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    item = share_service.add_showcase_item(
        session, current_user.id, inventory_row_id, quantity_offered
    )
    if item is None:
        # Row not owned by this user (or doesn't exist). Silently send
        # them back; we don't echo non-ownership signal.
        return RedirectResponse(url="/showcase?error=add_failed", status_code=303)
    # Inventory-card add path: redirect back to the page the user was
    # on. ``safe_redirect_url`` is not strictly needed here because the
    # POST originates from our own inventory_card macro; the
    # /showcase fallback gives a sane destination if the Referer is
    # missing.
    referer = request.headers.get("referer") or "/showcase"
    return RedirectResponse(url=referer + "?success=added", status_code=303)


@router.post("/showcase/items/{item_id}/quantity")
def showcase_item_quantity(
    item_id: int,
    request: Request,
    quantity_offered: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    share_service.update_quantity_offered(session, current_user.id, item_id, quantity_offered)
    return RedirectResponse(url="/showcase?success=quantity_updated", status_code=303)


@router.post("/showcase/items/{item_id}/remove")
def showcase_item_remove(
    item_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    share_service.remove_showcase_item(session, current_user.id, item_id)
    return RedirectResponse(url="/showcase?success=removed", status_code=303)


# ── Share management ────────────────────────────────────────────


@router.get("/shares")
def shares_index(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    my_shares = share_service.list_my_shares(session, current_user.id)
    # Picker source — playgroups the user is in (we can only share to
    # playgroups we're members of). The service-layer
    # ``create_share`` also enforces this; the UI filter just keeps
    # the dropdown honest.
    playgroup_rows = playgroup_service.list_playgroups_for_user(session, current_user.id)
    showcase = share_service.get_or_create_showcase(session, current_user.id)
    error = request.query_params.get("error")
    success = request.query_params.get("success")
    return render(
        request,
        "shares.html",
        {
            "title": "Shares",
            "current_user": current_user,
            "my_shares": my_shares,
            "playgroup_rows": playgroup_rows,
            "showcase": showcase,
            "error": error,
            "success": success,
        },
    )


@router.post("/shares")
def shares_create(
    request: Request,
    playgroup_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    share = share_service.create_share(session, current_user.id, playgroup_id)
    if share is None:
        # Non-member of the playgroup; should never trigger via the
        # picker (which is scoped to the user's playgroups), but a
        # tampered POST lands here.
        return RedirectResponse(url="/shares?error=not_a_member", status_code=303)
    return RedirectResponse(url="/shares?success=shared", status_code=303)


@router.post("/shares/{share_id}/revoke")
def shares_revoke(
    share_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    share_service.revoke_share(session, current_user.id, share_id)
    return RedirectResponse(url="/shares?success=revoked", status_code=303)


@router.get("/shares/{share_id}")
def shares_view(
    share_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Read-only shared view of someone's Showcase.

    Visibility-gated at the service layer via direct PlaygroupMember
    filter on Share.playgroup_id (decision E2). Non-members and
    non-existent share-ids alike redirect to /shares with the same
    error code — the existence of the share id is non-leaky (same
    pattern as ``/playgroups/{id}``).
    """
    view = share_service.get_share_view(session, current_user.id, share_id)
    if view is None:
        return RedirectResponse(url="/shares?error=share_unavailable", status_code=303)
    return render(
        request,
        "share_view.html",
        {
            "title": view["showcase"].name,
            "current_user": current_user,
            "share": view["share"],
            "showcase": view["showcase"],
            "sharer": view["sharer"],
            "playgroup": view["playgroup"],
            "items": view["items"],
        },
    )
