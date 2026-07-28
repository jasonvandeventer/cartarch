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

import json
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import deck_service, game_service
from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
    safe_redirect_url,
)
from app.game_analytics_service import build_game_analytics
from app.game_service import (
    NEW_GAME_FORMAT_CHOICES,
    GameLockedError,
    create_game,
    delete_game,
    end_game,
    get_game,
    get_seat_commander_image_urls,
    get_seat_commander_scryfall_ids,
    get_viewable_game,
    list_games,
    list_user_decks_for_companion,
    log_game,
    normalize_game_format,
    record_goal_results,
    seat_commander_scryfall_id,
    set_game_playgroup,
    set_own_seat_deck,
    toggle_seat_art_background,
    update_game_notes,
    update_seat,
)
from app.live_game_service import valid_momir_mvs
from app.models import Deck, Game, GameEvent, GameSeat, User
from app.timeutil import utc_now


def _momir_valid_mvs(session, game) -> list[int]:
    """Sorted MVs with >=1 legal creature, for greying the Momir picker (#113).
    Empty for non-Momir games."""
    if game.format and game.format.lower() == "momir":
        return sorted(valid_momir_mvs(session))
    return []


router = APIRouter()


def _public_base_url() -> str:
    """#165 — lazy import: main.py imports this module, so a top-level import cycles."""
    from app.main import public_base_url

    return public_base_url()


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

    # #156 — decks are SCOPED to the people who can actually be seated. This used
    # to be `session.query(Deck).all()`, i.e. every deck in the system serialised
    # into the page for JS to filter client-side, so any user could read every
    # other user's deck names out of the page source. The seat picker only ever
    # offers `all_users`, so no other deck was reachable anyway — this narrows the
    # payload to what the UI can use and closes the disclosure.
    pickable_user_ids = {u.id for u in all_users} | {current_user.id}
    all_decks = (
        session.query(Deck).filter(Deck.user_id.in_(pickable_user_ids)).order_by(Deck.name).all()
    )
    # JSON-safe: users list and deck lookup by user_id for JS filtering
    users_json = [{"id": u.id, "name": u.display_name or u.username} for u in all_users]
    # #156 — a seat records a pilot and a deck independently, so a borrowed deck
    # is representable and the server already accepts it. Carry the owner's name
    # so the dropdown can offer other players' decks under a "Borrowed from…"
    # group rather than the UI silently implying you may only play your own.
    user_name_by_id = {u.id: (u.display_name or u.username) for u in all_users}
    user_name_by_id.setdefault(current_user.id, current_user.display_name or current_user.username)
    decks_by_user_json = {}
    for d in all_decks:
        decks_by_user_json.setdefault(str(d.user_id), []).append(
            {"id": d.id, "name": d.name, "owner": user_name_by_id.get(d.user_id, "")}
        )
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
    # #164 — per-seat commander entry. Used ONLY when that seat picked no deck:
    # typing a commander resolves to an existing deck of that commander, or
    # creates a placeholder. Parallel-indexed with the other seat arrays.
    commander_names: list[str] = Form(default=[]),
    user_ids: list[str] = Form(default=[]),
    grid_positions: list[str] = Form(default=[]),
    starting_life: int = Form(40),
    first_seat_number: int | None = Form(None),
    playgroup_id: str = Form(""),
    momir_physical: bool = Form(False),
    # #165 — mint the seat-claim code at creation, so the host lands on the game
    # page with the code and QR already up instead of hunting for a button. Default
    # ON: the code is inert until someone uses it, and a game nobody joins is
    # unaffected. The owner can still revoke it from the game page.
    enable_join: bool = Form(False),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    # v3.27.2 — format normalization (see the note at create_game below). Done
    # up front because the Momir life default depends on it.
    canonical_format = normalize_game_format(format)
    # Momir Basic starts at 24 life. The Commander default (40) means the user
    # left the life picker untouched, so swap in 24; an explicit 20/30 still wins.
    if canonical_format == "Momir" and starting_life == 40:
        starting_life = 24

    seats = []
    # #164 — names that matched no card in the local catalog. Reported back rather
    # than silently dropped, because a FLAVOR name lands here ("Buttercup,
    # Provincial Princess" is Sisay) and Cartarch stores no flavor names. Creating
    # a placeholder from an unmatched name would mint a deck under a wrong or empty
    # commander, which is worse than the blank we started with.
    unresolved_commanders: list[str] = []
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
        # #164 — a typed commander is a FALLBACK for a seat with no deck picked,
        # never an override: an explicit deck selection always wins. The deck is
        # created under the SEAT's user when known (that is whose deck it is), and
        # only under the creator as a last resort for an unattributed seat.
        cmd_raw = (commander_names[i] if i < len(commander_names) else "").strip()
        if deck_id is None and cmd_raw:
            resolved, missing = deck_service.resolve_commander_to_deck(
                session, user_id or current_user.id, cmd_raw, commit=False
            )
            if resolved is not None:
                deck_id = resolved.id
            unresolved_commanders.extend(missing)
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
    # blocking philosophy for first_seat_number. (canonical_format computed
    # at the top of this handler — the Momir life default needs it early.)

    game = create_game(
        session,
        user_id=current_user.id,
        format=canonical_format,
        seats=seats,
        first_seat_number=fsn,
        client_token=secrets.token_urlsafe(8),
        momir_physical=momir_physical,
    )
    # v3.32.0 — optional playgroup link. set_game_playgroup validates the
    # owner is a member of the target playgroup; a bad / non-member / empty
    # value simply leaves the game private (non-blocking, mirroring the
    # first_seat_number / format philosophy).
    if enable_join:
        game.join_code = game_service.generate_join_code(session)
        session.commit()

    pg_raw = playgroup_id.strip()
    if pg_raw:
        try:
            set_game_playgroup(session, game.id, current_user.id, int(pg_raw))
        except ValueError:
            pass
    # #164 — surface any commander name that matched nothing. The game is still
    # created (never fail a game over an attribution problem — the same
    # non-blocking philosophy as format / first_seat_number / user attribution);
    # the seat simply keeps its blank deck, which is honest, and the banner says
    # which name did not resolve so the user can retype or fix it later.
    if unresolved_commanders:
        from urllib.parse import quote_plus

        missing = quote_plus(", ".join(dict.fromkeys(unresolved_commanders)))
        return RedirectResponse(f"/games/{game.id}?commander_unresolved={missing}", status_code=303)
    return RedirectResponse(f"/games/{game.id}", status_code=303)


# NOTE: registered BEFORE /games/{game_id} so the literal path wins even though
# "manual-log" would never satisfy the int path converter anyway (#42).
@router.get("/games/manual-log")
def manual_log_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    from app import playgroup_service

    decks = session.query(Deck).filter(Deck.user_id == current_user.id).order_by(Deck.name).all()
    user_playgroups = playgroup_service.list_playgroups_for_user(session, current_user.id)
    return render(
        request,
        "manual_log.html",
        {
            "title": "Log a Game",
            "decks": decks,
            "formats": NEW_GAME_FORMAT_CHOICES,
            "user_playgroups": user_playgroups,
            "today": utc_now().strftime("%Y-%m-%d"),
            "current_user": current_user,
        },
    )


@router.post("/games/manual-log")
def manual_log_create(
    request: Request,
    played_date: str = Form(""),
    format: str = Form(""),
    my_deck_id: str = Form(""),
    result: str = Form(""),
    winner: str = Form(""),
    opp_names: list[str] = Form(default=[]),
    opp_decks: list[str] = Form(default=[]),
    playgroup_id: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Log an already-played external game (#42). Composes create+end via
    :func:`log_game`, which owns the security guards (deck ownership, playgroup
    access, opponent bounds) — a violation raises ValueError → 400."""
    try:
        # #130 — attach UTC: this feeds the tz-aware played_at (timestamptz).
        # The date is a user-entered day; midnight UTC preserves the prior
        # naive-UTC-stored instant.
        played_at = datetime.strptime(played_date.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format") from None

    opponents = []
    for i, name in enumerate(opp_names):
        nm = name.strip()
        if not nm:
            continue
        dk = opp_decks[i].strip() if i < len(opp_decks) else ""
        opponents.append({"name": nm[:100], "deck_name": dk[:100]})

    winner_index = None
    if result == "lost":
        w = winner.strip()
        if w == "unknown":
            winner_index = None
        elif w.isdigit():
            winner_index = int(w)
        else:
            raise HTTPException(
                status_code=400, detail="You must identify the winner when selecting 'Lost'"
            )

    deck_id = int(my_deck_id) if my_deck_id.strip().isdigit() else None
    pg_id = int(playgroup_id) if playgroup_id.strip().isdigit() else None

    game = log_game(
        session,
        user_id=current_user.id,
        result=result,
        played_at=played_at,
        opponents=opponents,
        deck_id=deck_id,
        format=normalize_game_format(format),
        playgroup_id=pg_id,
        winner_index=winner_index,
        notes=notes[:500],
    )
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


# #158 — MUST stay registered BEFORE `/games/{game_id}`. FastAPI matches in
# registration order, and `game_id: int` would reject the literal "analytics" with a
# 422 rather than falling through. Same precedent as `/games/new` and
# `/games/manual-log` above.
@router.get("/games/analytics")
def games_analytics_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Cross-game pod dynamics + pace over the games this user was part of (#158).

    `build_cross_game_analytics` returns None when no qualifying game has an event
    stream; the template renders an empty state rather than a page of zeros."""
    from app.game_analytics_service import build_cross_game_analytics

    return render(
        request,
        "games_analytics.html",
        {
            "title": "Game Analytics",
            "current_user": current_user,
            "analytics": build_cross_game_analytics(session, current_user.id),
        },
    )


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
        # #165 — the seat-claim QR, rendered SERVER-SIDE as inline SVG. Only while
        # `created` and only when the owner has enabled joining; "" otherwise, and
        # the template falls back to the printed code (which is always shown).
        "join_qr": (
            game_service.join_qr_svg(f"{_public_base_url()}/join/{game.join_code}")
            if game.status == "created" and game.join_code
            else ""
        ),
        "title": f"Game {game_id}",
        "game": game,
        "decks": decks,
        "is_owner": is_owner,
        "pickable_users": pickable_users,
        "user_playgroups": user_playgroups,
        "current_user": current_user,
        "seat_commander_images": seat_commander_images,
        # Companion mode — the table token grants ALL-seats control and must live
        # ONLY on the creator's (tablet) view. Non-owners never receive it; their
        # live-mode mutations go through the seat-scoped companion page instead.
        # (It also namespaces the localStorage tracker key — non-owners fall back
        # to the game-id-only key, which is harmless: they can't mutate a
        # `created` game, and live-mode state is server-authoritative.)
        "table_token": game.client_token if is_owner else None,
        "momir_valid_mvs": _momir_valid_mvs(session, game),
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
        # #95 — per-game analytics replayed from game_events (None for pre-v4.3 /
        # localStorage games → the template hides the section).
        ctx["analytics"] = build_game_analytics(session, game.id)
        return render(request, "game_summary.html", ctx)

    return render(request, "game_detail.html", ctx)


@router.get("/games/{game_id}/companion")
def game_companion_page(
    request: Request,
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Phone companion view for a live game (companion mode, Session 2).

    Viewer-scoped (owner / seat player / playgroup member). The page controls
    only the requesting user's OWN seat (seat-scoped mutations, no table token —
    the token NEVER reaches a phone). A viewer with no attributed seat gets
    read-only spectator mode.
    """
    game = get_viewable_game(session, game_id, current_user.id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    my_seat = next((s for s in game.seats if s.user_id == current_user.id), None)
    seat_commanders = get_seat_commander_scryfall_ids(session, game)
    # Pre-live deck self-selection: a seated player in a `created` game may pick
    # their own deck. Only fetch the picker list when it's actually offered.
    my_decks = (
        list_user_decks_for_companion(session, current_user.id)
        if (my_seat and game.status == "created")
        else []
    )
    return render(
        request,
        "game_companion.html",
        {
            "title": f"Companion · Game {game_id}",
            "game": game,
            "my_seat": my_seat,
            "seat_commanders": seat_commanders,
            "my_commander_id": seat_commanders.get(my_seat.id) if my_seat else None,
            "my_decks": my_decks,
            "current_user": current_user,
            "momir_valid_mvs": _momir_valid_mvs(session, game),
        },
    )


@router.post("/games/{game_id}/companion/deck")
def companion_set_deck(
    game_id: int,
    # #171 — ABSENT is not 0. This was `Form(0)`, so any POST that said nothing
    # about the deck was read as "clear my deck" and answered 200 — including a
    # malformed body, a truncated one, or one carrying a field this endpoint does
    # not understand. Clearing is legitimate and stays, but it must be ASKED for.
    #
    # Not reachable from the shipped UI (`pickDeck()` always sends the field, and
    # clearing is an explicit `pickDeck(0)`), but #165's phone-side join flow will
    # post new fields to this endpoint, and a request that forgot to echo `deck_id`
    # would silently wipe the player's selection while reporting success.
    deck_id: int | None = Form(None),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """A seated player picks their OWN deck (only while the game is `created`).
    No table token — personal, phones-only. Returns the re-derived seat identity
    for in-place UI update.

    ``deck_id=0`` clears the seat's deck. Omitting ``deck_id`` is a **400**: the
    service layer treats None/0 as "clear" and that is correct there, but a route
    must not turn a caller's SILENCE into that instruction.
    """
    if deck_id is None:
        raise HTTPException(
            status_code=400,
            detail="deck_id is required; send 0 to clear the seat's deck",
        )
    try:
        seat = set_own_seat_deck(session, game_id, current_user.id, deck_id or None)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except GameLockedError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse(
        {
            "ok": True,
            "seat": {
                "deck_name": seat.deck_name_at_game,
                "commander_name": seat.commander_name_at_game,
                "commander_scryfall_id": seat_commander_scryfall_id(session, seat),
            },
        }
    )


@router.get("/companion")
def companion_lobby(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """The phone's bookmark target — the current user's live/upcoming games (any
    game they hold a seat in). in_progress first, then created; most recent first,
    capped at 20. Standalone mobile page; never carries a table token."""
    _status_order = {"in_progress": 0, "created": 1}
    rows = (
        session.query(GameSeat, Game)
        .join(Game, GameSeat.game_id == Game.id)
        .filter(
            GameSeat.user_id == current_user.id,
            Game.status.in_(("in_progress", "created")),
        )
        .order_by(Game.played_at.desc())  # stable sort below keeps this within each status
        .all()
    )
    entries = [
        {
            "game_id": game.id,
            "status": game.status,
            "format": game.format or "Commander",
            "seat_count": len(game.seats),
            "played_at": game.played_at,
            "deck_name": seat.deck_name_at_game or (seat.deck.name if seat.deck else None),
            "commander_name": seat.commander_name_at_game,
            "commander_id": seat_commander_scryfall_id(session, seat),
        }
        for seat, game in rows
    ]
    entries.sort(key=lambda e: _status_order.get(e["status"], 9))  # stable → played_at desc within
    return render(
        request,
        "companion_lobby.html",
        {"title": "Live Games", "entries": entries[:20], "current_user": current_user},
    )


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
    elimination_causes: dict[int, str | None] = {}
    for seat in game.seats:
        p_val = form_data.get(f"placement_{seat.id}", "")
        l_val = form_data.get(f"final_life_{seat.id}", "")
        c_val = form_data.get(f"elimination_cause_{seat.id}", None)
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
        # A submitted cause field (even blank) is an explicit set; absent → leave.
        if c_val is not None:
            elimination_causes[seat.id] = str(c_val).strip() or None

    turn_count_raw = form_data.get("turn_count", "")
    notes = str(form_data.get("notes", ""))
    win_condition = str(form_data.get("win_condition", "")) or None
    try:
        tc = int(turn_count_raw) if str(turn_count_raw).strip() else None
    except ValueError:
        tc = None

    # #114 — a re-run on an already-finalized game is a result EDIT; audit it.
    was_finalized = game.status == "finalized"
    end_game(
        session,
        game_id,
        current_user.id,
        placements,
        final_lives,
        tc,
        notes,
        win_condition,
        elimination_causes,
    )
    if was_finalized:
        session.add(
            GameEvent(
                game_id=game_id,
                seat_id=None,
                action_type="result_edit",
                payload=json.dumps(
                    {
                        "editor_user_id": current_user.id,
                        "placements": {str(k): v for k, v in placements.items()},
                        "final_lives": {str(k): v for k, v in final_lives.items()},
                        "elimination_causes": {str(k): v for k, v in elimination_causes.items()},
                        "turn_count": tc,
                        "win_condition": win_condition,
                    }
                ),
                turn=game.turn_count or 1,
                actor_kind="owner",
                created_at=utc_now(),
            )
        )
        session.commit()

    # issue #47 — per-seat goal completion. Checkbox name is goal_{seat}_{goal};
    # presence = achieved. record_goal_results re-validates ownership (only the
    # recorder's own decks' active goals are written), so forged keys are inert.
    checked: set[tuple[int, int]] = set()
    for key in form_data:
        if not key.startswith("goal_"):
            continue
        parts = key.split("_")
        if len(parts) != 3:
            continue
        try:
            checked.add((int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    record_goal_results(session, game_id, current_user.id, checked)

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


@router.post("/games/{game_id}/first-seat")
def game_set_first_seat(
    game_id: int,
    seat_number: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Owner-only: choose who goes first, from the game page (v4.12.26).

    Moved off ``/games/new``, where the question was unanswerable — see
    :func:`game_service.set_first_seat`. Empty value clears the choice.

    A refusal is a **400, not a silent normalize.** The non-blocking posture the
    rest of game creation takes (a bad format falls back, a bad attribution is
    dropped) is wrong here: the caller asked for a specific starting player, and
    quietly starting someone else instead is the failure the whole change is
    about.
    """
    raw = seat_number.strip()
    try:
        target = int(raw) if raw else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid seat") from None
    if not game_service.set_first_seat(session, game_id, current_user.id, target):
        raise HTTPException(status_code=400, detail="Cannot set the first player for this game")
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


# --- #165: seat claiming from a phone ----------------------------------------
# ONE claim primitive, TWO front doors: a QR link carries the code in the path,
# and the manual form posts the same code. A tablet across the table, angled away,
# or a camera in bad light must never be the thing that stops someone joining.


@router.get("/join")
def join_manual_page(
    request: Request,
    code: str = "",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Manual front door — type or paste a code. Also the QR link's landing page
    when the code is passed as a query param."""
    trimmed = (code or "").strip()
    game = game_service.get_game_by_join_code(session, trimmed) if trimmed else None
    return render(
        request,
        "game_join.html",
        {
            "title": "Join a game",
            "current_user": current_user,
            "code": trimmed,
            "game": game,
            # A game that has already started is found but not joinable — say so
            # rather than pretending the code is wrong.
            "already_started": bool(game and game.status != "created"),
            "seats": game_service.claimable_seats(game) if game else [],
        },
    )


@router.get("/join/{code}")
def join_by_code(
    request: Request,
    code: str,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """QR front door. Same page as the manual route — the QR is a shortcut to the
    claim, not a separate mechanism."""
    return join_manual_page(request, code=code, session=session, current_user=current_user)


@router.post("/join/{code}/claim")
def join_claim(
    code: str,
    seat_id: int = Form(...),
    display_name: str = Form(""),
    commander_name: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        seat, unresolved = game_service.claim_seat(
            session,
            code=code,
            user_id=current_user.id,
            seat_id=seat_id,
            display_name=display_name,
            commander_entry=commander_name,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except GameLockedError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    # Land on the phone companion view — the seat is theirs now, so they can pick
    # or change their deck from the same place they will play from.
    target = f"/games/{seat.game_id}/companion"
    if unresolved:
        from urllib.parse import quote_plus

        target += f"?commander_unresolved={quote_plus(', '.join(dict.fromkeys(unresolved)))}"
    return RedirectResponse(target, status_code=303)


@router.post("/games/{game_id}/join-code")
def game_toggle_join_code(
    game_id: int,
    enable: bool = Form(True),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Owner-only: mint or revoke this game's claim code. The code IS the toggle —
    NULL means claiming is off, same posture as `Deck.share_token` (#143) and
    `Playgroup.join_code`."""
    game = get_game(session, game_id, current_user.id)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    game.join_code = game_service.generate_join_code(session) if enable else None
    session.commit()
    return RedirectResponse(f"/games/{game_id}", status_code=303)


@router.get("/games/{game_id}/lobby.json")
def game_lobby_state(
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Seat occupancy while the game is `created`, for the tablet to poll.

    #165 finding: there is NO SSE stream before `live_start` — `get_live_state`
    raises `LookupError` while `game.live_state is None` — so a claim would land in
    the database while the tablet showed a static page. Polling is the smaller
    honest fix; a pre-live stream is the upgrade if this ever feels slow.
    """
    game = get_viewable_game(session, game_id, current_user.id)
    if game is None:
        raise HTTPException(status_code=404, detail="Game not found")
    return JSONResponse(
        {
            "status": game.status,
            "seats": [
                {
                    "seat_id": s.id,
                    "seat_number": s.seat_number,
                    "player_name": s.player_name,
                    "claimed": s.user_id is not None,
                    "deck_name": s.deck_name_at_game,
                    "commander_name": s.commander_name_at_game,
                }
                for s in sorted(game.seats, key=lambda x: x.seat_number)
            ],
        }
    )
