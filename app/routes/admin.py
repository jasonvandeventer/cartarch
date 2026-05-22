from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.dependencies import CsrfRequired, get_db_session, render, require_admin
from app.models import (
    Deck,
    GameSeat,
    ImportBatch,
    InventoryRow,
    StorageLocation,
    TransactionLog,
    User,
)

router = APIRouter(prefix="/admin")


def _build_user_rows(session: Session) -> list[dict]:
    users = session.query(User).order_by(User.username).all()

    card_counts = dict(
        session.query(InventoryRow.user_id, func.count(InventoryRow.id))
        .filter(InventoryRow.is_pending.is_(False))
        .group_by(InventoryRow.user_id)
        .all()
    )
    deck_counts = dict(
        session.query(Deck.user_id, func.count(Deck.id)).group_by(Deck.user_id).all()
    )

    # v3.27.4 — direct read of ``User.last_signed_in_at`` (set by POST /login).
    # Replaces the previous ``func.max(TransactionLog.created_at)`` aggregate
    # subquery, which was a misleading proxy for engagement: users who only
    # play games / edit decks / log in showed stale or NULL dates because
    # those activities don't write TransactionLog rows. Key renamed
    # ``last_activity`` → ``last_signed_in_at`` to match the new semantics;
    # the admin template's column header changes from "Last Activity" to
    # "Last Signed In" in parallel.
    return [
        {
            "user": u,
            "card_count": card_counts.get(u.id, 0),
            "deck_count": deck_counts.get(u.id, 0),
            "last_signed_in_at": u.last_signed_in_at,
        }
        for u in users
    ]


@router.get("")
def admin_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_admin),
):
    return render(
        request,
        "admin.html",
        {
            "title": "Admin",
            "current_user": current_user,
            "user_rows": _build_user_rows(session),
        },
    )


@router.post("/users/{user_id}/toggle-active")
def toggle_active(
    user_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_admin),
    _: None = CsrfRequired,
):
    if user_id == current_user.id:
        return RedirectResponse(url="/admin?error=cannot_deactivate_self", status_code=303)

    target = session.query(User).filter(User.id == user_id).first()
    if target:
        target.is_active = not target.is_active
        session.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/toggle-admin")
def toggle_admin(
    user_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_admin),
    _: None = CsrfRequired,
):
    if user_id == current_user.id:
        return RedirectResponse(url="/admin?error=cannot_remove_own_admin", status_code=303)

    target = session.query(User).filter(User.id == user_id).first()
    if target:
        target.is_admin = not target.is_admin
        session.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    new_password: str = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_admin),
    _: None = CsrfRequired,
):
    if len(new_password) < 8:
        return RedirectResponse(url="/admin?error=password_too_short", status_code=303)

    target = session.query(User).filter(User.id == user_id).first()
    if target:
        target.password_hash = hash_password(new_password)
        session.commit()
    return RedirectResponse(url="/admin?success=password_reset", status_code=303)


@router.post("/users/create")
def create_user(
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    is_admin: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_admin),
    _: None = CsrfRequired,
):
    username = username.strip().lower()
    display_name = display_name.strip()
    if not username:
        return RedirectResponse(url="/admin?error=username_required", status_code=303)
    if len(password) < 8:
        return RedirectResponse(url="/admin?error=password_too_short", status_code=303)
    if session.query(User).filter(User.username == username).first():
        return RedirectResponse(url="/admin?error=username_taken", status_code=303)

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name or None,
        is_active=True,
        is_admin=bool(is_admin),
    )
    session.add(user)
    session.commit()
    return RedirectResponse(url="/admin?success=user_created", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_user(
    user_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(require_admin),
    _: None = CsrfRequired,
):
    if user_id == current_user.id:
        return RedirectResponse(url="/admin?error=cannot_delete_self", status_code=303)

    target = session.query(User).filter(User.id == user_id).first()
    if not target:
        return RedirectResponse(url="/admin", status_code=303)

    # Cascade in FK-safe order
    session.query(TransactionLog).filter(TransactionLog.user_id == user_id).delete()
    session.query(InventoryRow).filter(InventoryRow.user_id == user_id).delete()
    session.query(ImportBatch).filter(ImportBatch.user_id == user_id).delete()
    session.query(Deck).filter(Deck.user_id == user_id).delete()
    session.query(StorageLocation).filter(StorageLocation.user_id == user_id).delete()
    # v3.27.5 — null seat→user FK on this user's historical seats. The
    # ``ondelete="SET NULL"`` clause on ``GameSeat.user_id`` is declared on
    # the model for documentation + v4 Postgres forward-compat but SQLite
    # doesn't enforce it (the project runs with ``PRAGMA foreign_keys`` OFF
    # — see app/db.py). This explicit UPDATE guarantees the outcome
    # regardless of engine: deleting a user nulls the FK on their seats,
    # leaving ``user_name_at_game`` untouched (the v3.27.5 snapshot column
    # SURVIVES deletion — that's its entire purpose). Seats in games owned
    # by OTHER users now correctly show the deleted user's historical name
    # via the snapshot.
    session.query(GameSeat).filter(GameSeat.user_id == user_id).update(
        {GameSeat.user_id: None}, synchronize_session=False
    )
    session.delete(target)
    session.commit()
    return RedirectResponse(url="/admin?success=user_deleted", status_code=303)
