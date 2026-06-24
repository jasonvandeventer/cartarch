"""Game tracking + summary routes (extracted from main.py during the v4 reorg).

Read access is viewer-scoped (owner, seat-attributed players, members of a
linked playgroup — v3.32.0); all mutations stay owner-only, enforced inside
``game_service`` (``get_game`` is strict owner-only; ``get_viewable_game`` is
the widened read). Finalized games render the read-only ``game_summary.html``
instead of the live ``game_detail.html`` tracker (v3.33.2).

Behaviour is byte-identical to the pre-extraction handlers in main.py — this
move changes wiring only, not logic. ``gameFingerprint()`` is untouched.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
    safe_redirect_url,
)
from app.game_service import (
    create_game,
    delete_game,
    end_game,
    get_game,
    get_seat_commander_image_urls,
    get_viewable_game,
    list_games,
    normalize_game_format,
    set_game_playgroup,
    toggle_seat_art_background,
    update_game_notes,
    update_seat,
)
from app.models import Deck, User

router = APIRouter()


@router.get("/games")
def games_list_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    games = list_games(session, current_user.id)
    # "Wins" is the VIEWER's win count, not "every game that has a winner".
    # ``list_games`` returns the hybrid visibility set (owned + played-in +
    # playgroup-shared), and each finished game has exactly one ``placement==1``
    # seat — so the old unconditional ``placement == 1`` sum counted one win per
    # finished game regardless of who won, making every logged game look like a
    # win (issue #38). A seat counts as the viewer's win only when it is BOTH the
    # winning seat AND attributed to the viewer (``user_id``); the new-game picker
    # pre-selects the creator's own seat, so owner-logged wins carry that link.
    total_wins = sum(
        1 for g in games for s in g.seats if s.placement == 1 and s.user_id == current_user.id
    )
    return render(
        request,
        "games.html",
        {
            "title": "Game History",
            "games": games,
            "total_wins": total_wins,
            "current_user": current_user,
        },
    )


@router.get("/games/new")
def game_new_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    # v3.29.0 — picker scopes to the user's playgroup co-members via
    # ``playgroup_service.get_pickable_users``. C2 transition fallback:
    # when the user has no co-members (no playgroups yet, or alone in a
    # solo playgroup), the wrapper returns the global active-user list
    # — preserves pre-v3.29.0 behavior for users who haven't joined any
    # playgroup. The shared primitive ``co_members_of`` (consumed by
    # v3.29.1 sharing / v3.29.2 trading) does NOT carry this fallback —
    # only the people-picker does.
    from app import playgroup_service

    all_users = playgroup_service.get_pickable_users(session, current_user.id)
    all_decks = session.query(Deck).order_by(Deck.name).all()
    # JSON-safe: users list and deck lookup by user_id for JS filtering
    users_json = [{"id": u.id, "name": u.display_name or u.username} for u in all_users]
    decks_by_user_json = {}
    for d in all_decks:
        decks_by_user_json.setdefault(str(d.user_id), []).append({"id": d.id, "name": d.name})
    # v3.32.0 — optional playgroup link picker. Linking a game to a playgroup
    # lets every member view it (read-only). Only the user's own playgroups
    # are offered. Empty list → the template hides the picker.
    user_playgroups = playgroup_service.list_playgroups_for_user(session, current_user.id)
    return render(
        request,
        "game_new.html",
        {
            "title": "New Game",
            "users_json": users_json,
            "decks_by_user_json": decks_by_user_json,
            "user_playgroups": user_playgroups,
            "current_user": current_user,
            "current_user_id": current_user.id,
        },
    )


@router.post("/games")
def game_create(
    request: Request,
    player_count: int = Form(...),
    format: str = Form(""),
    player_names: list[str] = Form(...),
    deck_ids: list[str] = Form(...),
    user_ids: list[str] = Form(default=[]),
    grid_positions: list[str] = Form(default=[]),
    starting_life: int = Form(40),
    first_seat_number: int | None = Form(None),
    playgroup_id: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    seats = []
    for i in range(player_count):
        name = player_names[i].strip() if i < len(player_names) else f"Player {i + 1}"
        did_raw = deck_ids[i] if i < len(deck_ids) else ""
        try:
            deck_id = int(did_raw) if did_raw else None
        except ValueError:
            deck_id = None
        # v3.27.5 — seat→user attribution. ``user_ids`` has been submitted
        # by game_new.html since well before this patch but was silently
        # dropped by the route handler (the bug surfaced in v3.25.1 recon).
        # Parse as nullable int; invalid / absent / unauthorized values
        # resolve to None and the seat ships unattributed — game creation
        # never fails over an attribution problem (mirrors the v3.25.1
        # first_seat_number non-blocking philosophy). Validation that the
        # id refers to a real User happens inside ``_capture_user_attribution``
        # in game_service.py — same pattern as deck_id validation, and same
        # cross-user permissive stance (a seat may legitimately reference
        # another user's account, matching the existing all-decks dropdown
        # precedent in game_new.html).
        uid_raw = user_ids[i] if i < len(user_ids) else ""
        try:
            user_id = int(uid_raw) if uid_raw else None
        except ValueError:
            user_id = None
        pos_raw = grid_positions[i].strip() if i < len(grid_positions) else ""
        seats.append(
            {
                "player_name": name or f"Player {i + 1}",
                "deck_id": deck_id,
                "user_id": user_id,
                "starting_life": starting_life,
                "grid_position": pos_raw or None,
            }
        )

    # First-player pick is optional and non-critical: an absent or
    # out-of-range value falls back to None so the game tracker keeps its
    # existing clockwise-seat default rather than blocking game creation.
    fsn = first_seat_number
    if fsn is not None and not (1 <= fsn <= player_count):
        fsn = None

    # v3.27.0 — collision-proof localStorage key namespace. Generated
    # server-side exactly once per game and never regenerated. Pairs with
    # the bare ``games.id`` rowid (which SQLite reuses after a game is
    # deleted) to form ``mana-game-${gameId}-${clientToken}`` in the
    # tracker, so a recycled id cannot resurface a deleted game's saved
    # state. Key-only — NOT added to the saved-state blob; the
    # gameFingerprint() (``_fp``) value stays unchanged.
    # v3.27.2 — format normalization. Trim + case-fold + match against
    # CANONICAL_GAME_FORMATS; unknown / empty / form-tampered values
    # resolve to DEFAULT_GAME_FORMAT (Commander). Game creation must
    # never fail due to a format problem, matching the v3.25.1 non-
    # blocking philosophy for first_seat_number.
    canonical_format = normalize_game_format(format)

    game = create_game(
        session,
        user_id=current_user.id,
        format=canonical_format,
        seats=seats,
        first_seat_number=fsn,
        client_token=secrets.token_urlsafe(8),
    )
    # v3.32.0 — optional playgroup link. set_game_playgroup validates the
    # owner is a member of the target playgroup; a bad / non-member / empty
    # value simply leaves the game private (non-blocking, mirroring the
    # first_seat_number / format philosophy).
    pg_raw = playgroup_id.strip()
    if pg_raw:
        try:
            set_game_playgroup(session, game.id, current_user.id, int(pg_raw))
        except ValueError:
            pass
    return RedirectResponse(f"/games/{game.id}", status_code=303)


def _format_game_elapsed(game) -> str | None:
    """Human elapsed playtime for a finalized game ("1h 23m" / "45m" / "<1m"),
    or None when not computable (legacy game with no ``ended_at``, or a clock
    anomaly). ``played_at`` ≈ when live play started; ``ended_at`` is stamped
    once at finalize (v3.33.2)."""
    if not game.ended_at or not game.played_at:
        return None
    secs = (game.ended_at - game.played_at).total_seconds()
    if secs < 0:
        return None
    if secs < 60:
        return "<1m"
    minutes = int(secs // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


@router.get("/games/{game_id}")
def game_detail_page(
    request: Request,
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    # v3.32.0 — viewer-scoped: owner, seat-attributed players, and members of
    # a linked playgroup may all view. Mutation controls stay owner-only,
    # gated on ``is_owner`` in the template.
    game = get_viewable_game(session, game_id, current_user.id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    is_owner = game.user_id == current_user.id
    seat_commander_images = get_seat_commander_image_urls(session, game)
    # Owner-only controls need supporting data; participants get none of it.
    decks: list[Deck] = []
    pickable_users: list[User] = []
    user_playgroups: list[dict] = []
    if is_owner:
        from app import playgroup_service

        decks = (
            session.query(Deck).filter(Deck.user_id == current_user.id).order_by(Deck.name).all()
        )
        # People picker for retroactive seat→user attribution + playgroup
        # picker to open the game up to a group.
        pickable_users = playgroup_service.get_pickable_users(session, current_user.id)
        user_playgroups = playgroup_service.list_playgroups_for_user(session, current_user.id)

    ctx = {
        "title": f"Game {game_id}",
        "game": game,
        "decks": decks,
        "is_owner": is_owner,
        "pickable_users": pickable_users,
        "user_playgroups": user_playgroups,
        "current_user": current_user,
        "seat_commander_images": seat_commander_images,
    }

    # v3.33.2 — finalized games render a read-only summary (final standings,
    # turn count, elapsed playtime, full notes) instead of the frozen
    # full-screen life tracker, which read as a "non-functional tracker".
    if game.status == "finalized":
        ctx["standings"] = sorted(
            game.seats,
            key=lambda s: (s.placement is None, s.placement or 0, s.seat_number),
        )
        ctx["elapsed"] = _format_game_elapsed(game)
        return render(request, "game_summary.html", ctx)

    return render(request, "game_detail.html", ctx)


@router.post("/games/{game_id}/end")
async def game_end(
    request: Request,
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    form_data = await request.form()

    game = get_game(session, game_id, current_user.id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    placements: dict[int, int] = {}
    final_lives: dict[int, int | None] = {}
    for seat in game.seats:
        p_val = form_data.get(f"placement_{seat.id}", "")
        l_val = form_data.get(f"final_life_{seat.id}", "")
        if p_val:
            try:
                placements[seat.id] = int(p_val)
            except ValueError:
                pass
        if l_val:
            try:
                final_lives[seat.id] = int(l_val)
            except ValueError:
                pass

    turn_count_raw = form_data.get("turn_count", "")
    notes = str(form_data.get("notes", ""))
    try:
        tc = int(turn_count_raw) if str(turn_count_raw).strip() else None
    except ValueError:
        tc = None

    end_game(session, game_id, current_user.id, placements, final_lives, tc, notes)
    return RedirectResponse(f"/games/{game_id}", status_code=303)


@router.post("/games/{game_id}/seats/{seat_id}/art-toggle")
def game_seat_art_toggle(
    request: Request,
    game_id: int,
    seat_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Flip ``GameSeat.art_background_hidden`` for a single seat (v3.26.6).

    Per-seat opt-out for the v3.26.1 commander art panel background.
    Ownership enforced via :func:`toggle_seat_art_background` — game must
    belong to ``current_user`` and the seat must be on that game; either
    miss → 404.

    Returns 303 back to the game detail page; the v3.26.1 art-rendering
    JS reads the new value from the freshly-rendered ``seatDefs`` array
    on the next page paint.
    """
    new_value = toggle_seat_art_background(session, game_id, seat_id, current_user.id)
    if new_value is None:
        raise HTTPException(status_code=404, detail="Game or seat not found")
    return RedirectResponse(url=f"/games/{game_id}", status_code=303)


@router.post("/games/{game_id}/seats/{seat_id}")
def game_seat_edit(
    request: Request,
    game_id: int,
    seat_id: int,
    player_name: str = Form(""),
    user_id: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Owner-only: edit a seat's display name and/or attributed user (v3.32.1).

    The retroactive correction surface for a recorded game — rename a seat
    (fix a typo, turn "Player 2" into a real name) and/or link it to a user
    account (which lets that user view the game; empty/invalid ``user_id``
    clears the attribution back to name-only). A blank ``player_name`` leaves
    the existing name untouched. Ownership + seat membership enforced in
    :func:`update_seat`; either miss → 404. Works on finalized games.
    """
    uid_raw = user_id.strip()
    try:
        target_user_id = int(uid_raw) if uid_raw else None
    except ValueError:
        target_user_id = None
    result = update_seat(
        session,
        game_id,
        seat_id,
        current_user.id,
        player_name=player_name,
        target_user_id=target_user_id,
    )
    if result is None or result is False:
        raise HTTPException(status_code=404, detail="Game or seat not found")
    return RedirectResponse(url=f"/games/{game_id}", status_code=303)


@router.post("/games/{game_id}/playgroup")
def game_set_playgroup(
    request: Request,
    game_id: int,
    playgroup_id: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Owner-only: link the game to a playgroup, or clear the link (v3.32.0).

    Linking opens the game to every member of that playgroup (read-only).
    Empty value clears the link. :func:`set_game_playgroup` enforces that the
    caller owns the game AND is a member of the target playgroup; a violation
    → 404 (non-leaky, matching the game-not-found path).
    """
    pg_raw = playgroup_id.strip()
    try:
        target_pg_id = int(pg_raw) if pg_raw else None
    except ValueError:
        target_pg_id = None
    if not set_game_playgroup(session, game_id, current_user.id, target_pg_id):
        raise HTTPException(status_code=404, detail="Game or playgroup not found")
    return RedirectResponse(url=f"/games/{game_id}", status_code=303)


@router.post("/games/{game_id}/notes")
def game_update_notes(
    request: Request,
    game_id: int,
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Update ``Game.notes`` independent of finalization state (v3.26.0).

    Lets users revise notes after a game is finalized without touching
    placements/turn_count — :func:`end_game` couples notes to those fields
    and would clobber recorded results.

    Redirect target is referer-based via :func:`safe_redirect_url` so the
    games-list modal returns the user to ``/games``; the game-detail
    fallback default preserves prior behavior when Referer is missing or
    invalid.
    """
    game = get_game(session, game_id, current_user.id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    update_game_notes(session, game_id, current_user.id, notes)
    return RedirectResponse(
        url=safe_redirect_url(request, default=f"/games/{game_id}"), status_code=303
    )


@router.post("/games/{game_id}/delete")
def game_delete(
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_game(session, game_id, current_user.id)
    return RedirectResponse("/games", status_code=303)
