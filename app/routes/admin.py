from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.dependencies import CsrfRequired, get_db_session, render, require_admin
from app.models import (
    Deck,
    Game,
    GameSeat,
    ImportBatch,
    InventoryRow,
    PasswordResetToken,
    PlaygroupMember,
    Share,
    Showcase,
    StorageLocation,
    TokenInventory,
    Trade,
    TransactionLog,
    User,
    VariantGroup,
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
            # last_active_at — last authenticated request (stamped from the auth
            # dependency, throttled per-user). Distinct from last_signed_in_at
            # (last login); the gap between the two is the engagement signal.
            # NULL until a user's next authenticated request (no backfill).
            "last_active_at": u.last_active_at,
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

    # gate-#5 (Phase 2) — COMPOSE the per-entity delete helpers instead of bulk-
    # deleting around them. The old bulk deletes here (query(Deck).delete() /
    # query(InventoryRow).delete()) bypassed delete_deck and
    # clean_inventory_row_references, so they (a) orphaned the RAW deck_bracket_*
    # tables and never nulled game_seats.deck_id, (b) orphaned terminal cross-user
    # trade_items pointing at this user's rows, and (c) never deleted token_inventory
    # at all (orphaning token_inventory.user_id + .storage_location_id). The gate-#5
    # harness proved all three (plus the games.user_id crash, handled in step 1).
    # Composing the helpers also future-proofs the cascade: a child added to
    # decks/inventory/tokens later is cleaned by its own delete path, never re-leaked
    # here. All helpers run with commit=False — this stays a single transaction.
    from app import deck_service, token_service
    from app.inventory_service import clean_inventory_row_references

    # (1) Games this user RECORDED → SET NULL user_id, but SNAPSHOT the recorder's
    # name FIRST (gate-#5 amendment: games.user_id is now SET NULL — without the
    # snapshot the read-only banner degrades to "another player"). The game survives
    # as shared history for its seat-attributed players / linked playgroup. Explicit
    # (not relying on the DB SET NULL) so it is correct on prod SQLite (FK off) too.
    recorder_name = target.display_name or target.username
    for game in session.query(Game).filter(Game.user_id == user_id).all():
        if game.user_name_at_game is None:
            game.user_name_at_game = recorder_name
        game.user_id = None
    session.flush()

    # (2) Decks → delete_deck each: cleans the raw bracket tables +
    # deck_token_requirements + nulls game_seats.deck_id (gate-#5 fix) and disbands
    # the deck's inventory to pending (deleted in step 4).
    for (deck_id_,) in session.query(Deck.id).filter(Deck.user_id == user_id).all():
        deck_service.delete_deck(session, deck_id_, user_id, commit=False)

    # (3) Token inventory → delete_token each (nulls deck_token_requirements.
    # token_inventory_id; the decks' own requirements are already gone). BEFORE the
    # StorageLocation delete so token_inventory.storage_location_id never dangles.
    for (token_id_,) in (
        session.query(TokenInventory.id).filter(TokenInventory.user_id == user_id).all()
    ):
        token_service.delete_token(session, token_id_, user_id, commit=False)

    # (4) Remaining inventory (collection rows + rows just disbanded to pending by
    # delete_deck) → the shared clean-path: deletes referencing ShowcaseItems and
    # NULLs trade_items.inventory_row_id on ALL referencing trades (incl. terminal
    # cross-user trades — the *_at_trade snapshot is the durable record). Bulk for
    # large collections.
    inv_ids = [r for (r,) in session.query(InventoryRow.id).filter(InventoryRow.user_id == user_id)]
    if inv_ids:
        clean_inventory_row_references(session, inv_ids)
        session.query(InventoryRow).filter(InventoryRow.id.in_(inv_ids)).delete(
            synchronize_session=False
        )

    # (5) Now FK-safe to bulk-delete the user's remaining leaf rows. transaction_logs
    # BEFORE import_batches (transaction_logs.batch_id → import_batches). variant
    # groups: their only referencers (decks) are gone, nothing to null first. storage
    # locations last: inventory, decks, and tokens that referenced them are all gone.
    session.query(TransactionLog).filter(TransactionLog.user_id == user_id).delete()
    session.query(ImportBatch).filter(ImportBatch.user_id == user_id).delete()
    session.query(VariantGroup).filter(VariantGroup.user_id == user_id).delete()
    # storage_locations LEAF-FIRST (Gate #7, v3.39.x — PG-readiness hardening). A user
    # can NEST locations (``StorageLocation.parent_id`` → ``storage_locations``, a
    # self-ref FK declared NO ACTION). Account deletion legitimately takes the WHOLE
    # tree (unlike single ``delete_location``, which refuses while children exist).
    # NOTE: the prior single bulk ``query(...).delete()`` was NOT an active crash —
    # verified directly on PG18: a NO ACTION FK is checked at STATEMENT END, so deleting
    # the whole tree in one statement leaves no dangling reference at the check point and
    # passes (it differs from the gate-#5 ``games.user_id`` crash, where the child row
    # was left BEHIND referencing the deleted parent). This deletes leaf-first anyway as
    # defensive hardening that doesn't lean on that statement-end subtlety: repeatedly
    # remove the user's locations that are nobody's parent until none remain, so children
    # always go before parents regardless of FK deferral, ordering, or a future
    # RESTRICT/split. Terminating: each pass clears the current leaves; a (non-existent)
    # cycle falls through to a single delete rather than hang.
    remaining = {
        lid
        for (lid,) in session.query(StorageLocation.id).filter(StorageLocation.user_id == user_id)
    }
    while remaining:
        parent_ids = {
            pid
            for (pid,) in session.query(StorageLocation.parent_id).filter(
                StorageLocation.id.in_(remaining),
                StorageLocation.parent_id.isnot(None),
            )
        }
        leaves = remaining - parent_ids or remaining  # cycle-safety fallback
        session.query(StorageLocation).filter(StorageLocation.id.in_(leaves)).delete(
            synchronize_session=False
        )
        remaining -= leaves
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
