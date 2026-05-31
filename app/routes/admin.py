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
    PasswordResetToken,
    PlaygroupMember,
    Share,
    Showcase,
    StorageLocation,
    Trade,
    TransactionLog,
    User,
    WatchlistItem,
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

    # v3.29.0 — playgroup pre-cleanup. For each playgroup the deleted
    # user owns: transfer ownership to the longest-tenured remaining
    # member (D3 auto-transfer), or hard-delete the playgroup if the
    # user is the sole member. After this call returns, ``user_id``
    # owns no playgroup, and the plain ``PlaygroupMember`` DELETE
    # below is safe. Owner-transfer was chosen here (rather than the
    # recon's original "block" recommendation) because in an admin
    # deletion the owner is not present to transfer themselves —
    # blocking would be unactionable.
    from app import playgroup_service, trade_service

    playgroup_service.handle_user_deletion(session, user_id)

    # v3.29.2 — pairwise-trading pre-cleanup. Done BEFORE the
    # InventoryRow / Share / Showcase cleanup so trade-item snapshots
    # can resolve against still-live data.
    #
    # (a) Collect every PROPOSED trade-id involving this user (both
    #     directions). Abandon them via the service helper (writes
    #     *_at_trade snapshots + status='abandoned' + closed_at).
    #     ORM-delete that exact set; cascade="all, delete-orphan" on
    #     Trade.items clears the TradeItem rows alongside each Trade.
    # (b) The terminal-trade SET-NULL pass is the §10 hook AFTER the
    #     GameSeat SET-NULL further below — keeps it adjacent to the
    #     v3.27.5 precedent for the same forward-compat pattern.
    pending_trade_ids_to_delete = [
        tid
        for (tid,) in session.query(Trade.id)
        .filter(
            Trade.status == "proposed",
            (Trade.proposer_user_id == user_id) | (Trade.recipient_user_id == user_id),
        )
        .all()
    ]
    if pending_trade_ids_to_delete:
        trade_service.abandon_pending_trades_involving_user(session, user_id)
        for trade in session.query(Trade).filter(Trade.id.in_(pending_trade_ids_to_delete)).all():
            session.delete(trade)
        session.flush()

    # v3.29.1 — collection-sharing cleanup. Order:
    #   1. Drop Share rows OWNED by this user (Share.user_id). These
    #      are the user's acts of exposing their Showcase to other
    #      playgroups; they go away with the account.
    #   2. Drop ALL the user's Showcases via ORM ``session.delete`` (NOT
    #      a bulk DELETE) so the ``cascade="all, delete-orphan"`` on
    #      Showcase.items takes the ShowcaseItem rows with it. A bulk
    #      ``query.delete()`` is DB-level only and would orphan the
    #      ShowcaseItem rows. Done BEFORE the InventoryRow DELETE so
    #      ShowcaseItem cascade resolves while the FK targets still
    #      exist (defense in depth; PRAGMA foreign_keys is OFF
    #      project-wide, but cleaner not to rely on that).
    #      v3.31.0 — multi-showcase: iterate every Showcase the user
    #      owns (was a single .first() under the one-per-user cap).
    # Note: handle_user_deletion (above) already deletes Share rows
    # targeting solo-owned playgroups that get auto-deleted. Other
    # users' Shares targeting playgroups the deleted user owned and
    # transferred stay — the new owner keeps that audience.
    session.query(Share).filter(Share.user_id == user_id).delete(synchronize_session=False)
    for user_showcase in session.query(Showcase).filter(Showcase.user_id == user_id).all():
        session.delete(user_showcase)
    session.flush()

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
    # v3.29.2 — same SET-NULL discipline for terminal trades involving
    # this user. The pending trades involving this user were ORM-
    # deleted above (cascade); what remains are TERMINAL trades
    # (accepted / declined / cancelled / pre-existing abandoned)
    # carrying the user_id on proposer_user_id or recipient_user_id.
    # SET-NULL the FKs; the v3.29.2 ``*_name_at_trade`` snapshot
    # columns preserve identity in the historical record. Same
    # forward-compat shape as the v3.27.5 ``GameSeat.user_id`` pattern.
    session.query(Trade).filter(Trade.proposer_user_id == user_id).update(
        {Trade.proposer_user_id: None},
        synchronize_session=False,
    )
    session.query(Trade).filter(Trade.recipient_user_id == user_id).update(
        {Trade.recipient_user_id: None},
        synchronize_session=False,
    )
    # v3.27.12 — watchlist rows are per-user with no historical retention
    # value (no "X was watching Y when their account was deleted" meaning
    # to preserve, unlike GameSeat.user_name_at_game). Plain DELETE here,
    # same shape as InventoryRow / Deck / etc. above.
    session.query(WatchlistItem).filter(WatchlistItem.user_id == user_id).delete()
    # v3.29.0 — playgroup membership rows. The pre-cleanup call above
    # has already transferred or auto-deleted any playgroups the user
    # owned; the remaining rows here are plain (demoted) memberships
    # with no retention value (no "X was a member of Y when their
    # account was deleted" snapshot to preserve, unlike GameSeat.
    # user_name_at_game). Plain DELETE.
    session.query(PlaygroupMember).filter(PlaygroupMember.user_id == user_id).delete()
    # v3.27.14 — password reset tokens. Same reasoning as watchlist:
    # no retention value (no "X reset Y's password" snapshot semantics).
    # Plain DELETE. Even already-used tokens (kept for the brief audit
    # breadcrumb the service-layer single-use enforcement uses) go away
    # with the user — no user, no point.
    session.query(PasswordResetToken).filter(PasswordResetToken.user_id == user_id).delete()
    session.delete(target)
    session.commit()
    return RedirectResponse(url="/admin?success=user_deleted", status_code=303)
