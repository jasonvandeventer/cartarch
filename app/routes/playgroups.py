"""Playgroup management routes (v3.29.0).

APIRouter-based, mounted under ``/playgroups`` via
``app.include_router(playgroups.router)`` in ``app/main.py``.

GETs use ``get_current_user`` (anon → 303 to /login); POSTs additionally
take ``CsrfRequired``. Management mutations are authority-gated via
``playgroup_service.require_membership`` — the route checks return
truthiness, errors propagate as redirect-with-error per the project's
established pattern (``/admin?error=cannot_delete_self`` etc.).

Detail (``GET /playgroups/{id}``) refuses non-members with a redirect
to ``/playgroups`` carrying an error code, NOT a 403 — keeps the
existence of a playgroup with the given id non-leaky.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import playgroup_service as svc
from app import share_service
from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
)
from app.models import Playgroup, User

router = APIRouter(prefix="/playgroups")


# ── Index + create ──────────────────────────────────────────────


@router.get("")
def playgroups_index(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    rows = svc.list_playgroups_for_user(session, current_user.id)
    error = request.query_params.get("error")
    success = request.query_params.get("success")
    return render(
        request,
        "playgroups.html",
        {
            "title": "Playgroups",
            "current_user": current_user,
            "playgroup_rows": rows,
            "error": error,
            "success": success,
        },
    )


@router.post("")
def playgroups_create(
    request: Request,
    name: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        playgroup = svc.create_playgroup(session, current_user.id, name=name, notes=notes or None)
    except ValueError:
        return RedirectResponse(url="/playgroups?error=name_required", status_code=303)
    return RedirectResponse(url=f"/playgroups/{playgroup.id}", status_code=303)


# ── Join ────────────────────────────────────────────────────────


@router.get("/join")
def playgroups_join_page(
    request: Request,
    code: str = "",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    # Pre-resolve to surface the playgroup name on the confirm view,
    # WITHOUT leaking member list / details to non-members. Bare
    # code-entry form when no code or unresolved.
    playgroup = svc.find_playgroup_by_code(session, code) if code else None
    error = request.query_params.get("error")
    return render(
        request,
        "playgroup_join.html",
        {
            "title": "Join a playgroup",
            "current_user": current_user,
            "code": code or "",
            "playgroup": playgroup,
            "error": error,
        },
    )


@router.post("/join")
def playgroups_join_submit(
    request: Request,
    code: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    playgroup = svc.join_by_code(session, current_user.id, code)
    if playgroup is None:
        return RedirectResponse(
            url=f"/playgroups/join?code={code}&error=invalid_code",
            status_code=303,
        )
    return RedirectResponse(url=f"/playgroups/{playgroup.id}?success=joined", status_code=303)


# ── Detail + management ────────────────────────────────────────


@router.get("/{playgroup_id}")
def playgroups_detail(
    playgroup_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    detail = svc.get_playgroup_detail(session, playgroup_id, current_user.id)
    if detail is None:
        # Non-member or unknown id; redirect — never expose detail.
        return RedirectResponse(url="/playgroups?error=not_a_member", status_code=303)
    error = request.query_params.get("error")
    success = request.query_params.get("success")
    # v3.29.1 — Shares targeting this playgroup, visible to the
    # viewing member. Service-layer filter is direct-PlaygroupMember
    # on Share.playgroup_id (decision E2). The viewer's membership is
    # already established by ``get_playgroup_detail`` above; the
    # ``list_shares_for_playgroup`` membership check is belt-and-suspenders.
    shares = share_service.list_shares_for_playgroup(session, current_user.id, playgroup_id)
    return render(
        request,
        "playgroup_detail.html",
        {
            "title": detail["playgroup"].name,
            "current_user": current_user,
            "playgroup": detail["playgroup"],
            "viewer_role": detail["viewer_role"],
            "members": detail["members"],
            "shares": shares,
            "error": error,
            "success": success,
        },
    )


@router.post("/{playgroup_id}/edit")
def playgroups_edit(
    playgroup_id: int,
    request: Request,
    name: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if name.strip():
        ok, err = svc.rename_playgroup(session, current_user.id, playgroup_id, name)
        if not ok:
            return RedirectResponse(
                url=f"/playgroups/{playgroup_id}?error={_slugify_error(err)}",
                status_code=303,
            )
    # Notes always updated (empty string clears them).
    ok, err = svc.update_notes(session, current_user.id, playgroup_id, notes)
    if not ok:
        return RedirectResponse(
            url=f"/playgroups/{playgroup_id}?error={_slugify_error(err)}",
            status_code=303,
        )
    return RedirectResponse(url=f"/playgroups/{playgroup_id}?success=updated", status_code=303)


@router.post("/{playgroup_id}/delete")
def playgroups_delete(
    playgroup_id: int,
    request: Request,
    confirm_name: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    # Type-the-name confirmation. Resolve the playgroup before delete
    # so we can compare; the require-owner check inside delete_playgroup
    # then runs as the actual authority gate.
    playgroup = session.query(Playgroup).filter(Playgroup.id == playgroup_id).first()
    if playgroup is None:
        return RedirectResponse(url="/playgroups", status_code=303)
    if (confirm_name or "").strip() != playgroup.name:
        return RedirectResponse(
            url=f"/playgroups/{playgroup_id}?error=name_mismatch",
            status_code=303,
        )
    ok, err = svc.delete_playgroup(session, current_user.id, playgroup_id)
    if not ok:
        return RedirectResponse(
            url=f"/playgroups/{playgroup_id}?error={_slugify_error(err)}",
            status_code=303,
        )
    return RedirectResponse(url="/playgroups?success=deleted", status_code=303)


@router.post("/{playgroup_id}/leave")
def playgroups_leave(
    playgroup_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    ok, err = svc.leave_playgroup(session, current_user.id, playgroup_id)
    if not ok:
        return RedirectResponse(
            url=f"/playgroups/{playgroup_id}?error={_slugify_error(err)}",
            status_code=303,
        )
    return RedirectResponse(url="/playgroups?success=left", status_code=303)


@router.post("/{playgroup_id}/transfer-ownership")
def playgroups_transfer_ownership(
    playgroup_id: int,
    request: Request,
    new_owner_user_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    ok, err = svc.transfer_ownership(session, current_user.id, playgroup_id, new_owner_user_id)
    if not ok:
        return RedirectResponse(
            url=f"/playgroups/{playgroup_id}?error={_slugify_error(err)}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/playgroups/{playgroup_id}?success=transferred",
        status_code=303,
    )


@router.post("/{playgroup_id}/members/{user_id}/remove")
def playgroups_remove_member(
    playgroup_id: int,
    user_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    ok, err = svc.remove_member(session, current_user.id, playgroup_id, user_id)
    if not ok:
        return RedirectResponse(
            url=f"/playgroups/{playgroup_id}?error={_slugify_error(err)}",
            status_code=303,
        )
    return RedirectResponse(url=f"/playgroups/{playgroup_id}?success=removed", status_code=303)


@router.post("/{playgroup_id}/regenerate-code")
def playgroups_regenerate_code(
    playgroup_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    ok, err = svc.regenerate_join_code(session, current_user.id, playgroup_id)
    if not ok:
        return RedirectResponse(
            url=f"/playgroups/{playgroup_id}?error={_slugify_error(err)}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/playgroups/{playgroup_id}?success=code_regenerated",
        status_code=303,
    )


@router.post("/{playgroup_id}/code-toggle")
def playgroups_code_toggle(
    playgroup_id: int,
    request: Request,
    enabled: str = Form("0"),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    is_enabled = enabled in ("1", "true", "True", "on")
    ok, err = svc.set_join_code_enabled(session, current_user.id, playgroup_id, is_enabled)
    if not ok:
        return RedirectResponse(
            url=f"/playgroups/{playgroup_id}?error={_slugify_error(err)}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/playgroups/{playgroup_id}?success=code_toggled",
        status_code=303,
    )


# ── Helpers ─────────────────────────────────────────────────────


def _slugify_error(message: str | None) -> str:
    """Compact a free-text error into a URL-safe slug for ?error= flash.

    Templates render the slug via a small ``ERROR_MESSAGES`` map or
    fall back to a generic line. Keeps URLs short; doesn't echo
    user-controllable text into the query string.
    """
    if not message:
        return "unknown"
    # Strip to alphanumerics + underscores; lowercase; first 40 chars.
    cleaned = "".join(c if c.isalnum() else "_" for c in message.lower())
    cleaned = "_".join(filter(None, cleaned.split("_")))[:40]
    return cleaned or "unknown"
