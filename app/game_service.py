"""Game tracking service — create, retrieve, end, and summarise game sessions."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Card,
    Deck,
    Game,
    GameEvent,
    GameGoalResult,
    GameSeat,
    InventoryRow,
    PlaygroupMember,
    User,
)
from app.timeutil import utc_now

logger = logging.getLogger(__name__)

# Optional operator-picked win condition captured at finalize (game-event
# history, Phase 2). Service-layer enum, same non-blocking normalize pattern as
# format/status: an unknown value resolves to None rather than blocking finalize.
VALID_WIN_CONDITIONS = ("combat", "commander", "combo", "attrition", "concession", "other")


def normalize_win_condition(raw: str | None) -> str | None:
    """Normalize a submitted win_condition to the canonical set, else ``None``."""
    if not raw:
        return None
    value = raw.strip().lower()
    return value if value in VALID_WIN_CONDITIONS else None


# #163 — game VARIANTS, which compose. Planechase + Momir + random-deck is a
# legitimate combination, so this is a set per game (``game_variants`` join table),
# never a single enum column. Service-layer constrained, matching the
# VALID_LOCATION_MODES / CANONICAL_GAME_FORMATS pattern below — no DB CHECK.
#
# ``momir`` supersedes the ``games.momir_physical`` boolean, which was null on 18
# of 23 rows and true on ZERO — never once set affirmatively.
VALID_GAME_VARIANTS = frozenset({"planechase", "archenemy", "momir", "random_deck"})


def normalize_game_variant(value: str | None) -> str | None:
    """Canonical variant token, or None if unrecognised. Mirrors
    ``normalize_game_format``: a bad value normalises away rather than blocking a
    write, per the project's constrained-value rule."""
    token = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return token if token in VALID_GAME_VARIANTS else None


# v3.27.2 — Game.format canonical taxonomy. Service-layer enforcement
# (matches the existing VALID_LOCATION_TYPES / VALID_LOCATION_MODES pattern
# in app/location_service.py — no DB-level CHECK constraint, since adding
# one to an existing column would require a SQLite table rebuild reserved
# for the v4 Postgres migration).
#
# CANONICAL_GAME_FORMATS includes ``Other`` as the backfill catch-all for
# historical free-text values that don't match anything in the canonical
# set. NEW_GAME_FORMAT_CHOICES is the subset the game_new.html ``<select>``
# exposes — ``Other`` is not user-selectable; it only appears when the
# backfill migration writes it for unrecognized prior data.
CANONICAL_GAME_FORMATS = (
    "Commander",
    "Standard",
    "Modern",
    "Legacy",
    "Vintage",
    "Draft",
    "Sealed",
    "Momir",  # Momir Basic — companion generates random creatures; no decks
    "Other",
)
NEW_GAME_FORMAT_CHOICES = CANONICAL_GAME_FORMATS[:-1]  # excludes 'Other'
DEFAULT_GAME_FORMAT = "Commander"
_FORMAT_LOOKUP = {f.casefold(): f for f in CANONICAL_GAME_FORMATS}


def normalize_game_format(raw: str | None, unknown_to: str = DEFAULT_GAME_FORMAT) -> str:
    """Normalize a submitted/stored format value to the canonical taxonomy.

    Trim whitespace, case-fold, match case-insensitively against
    ``CANONICAL_GAME_FORMATS``. Empty / whitespace-only / None resolves
    to ``DEFAULT_GAME_FORMAT`` (Commander) — the v3.25.1 non-blocking
    philosophy for ``first_seat_number`` applied to format too: a bad
    value never blocks game creation.

    ``unknown_to`` controls what happens when a non-empty value doesn't
    match anything in the canonical set:

    - Default (``DEFAULT_GAME_FORMAT``) is for runtime submission via
      ``game_create``: garbage / form-tampered / future-unknown values
      silently resolve to Commander so creation never fails.
    - The migration backfill passes ``unknown_to="Other"`` instead, so
      historical free-text values that don't match the canonical set
      are preserved as a distinct signal rather than collapsed into
      the default.
    """
    if raw is None:
        return DEFAULT_GAME_FORMAT
    cleaned = raw.strip()
    if not cleaned:
        return DEFAULT_GAME_FORMAT
    return _FORMAT_LOOKUP.get(cleaned.casefold(), unknown_to)


# v3.27.3 — Game.status canonical taxonomy. Same service-layer enum pattern
# as v3.27.2 CANONICAL_GAME_FORMATS (no DB-level CHECK; adding one to the
# new column would constrain it now but every later schema change to games
# would carry the same table-rebuild caveat — defer to v4 Postgres).
#
# Replaces the brittle "any seat has placement → is_ended=True" derivation
# in game_detail.html. Distinguishes ``finalized`` (end_game was called) from
# ``abandoned`` (game created but never ended) — both have no placements in
# the old derivation, indistinguishable then. ``created`` is the default for
# newly-inserted rows; ``in_progress`` is reserved for a future tracker-
# server integration that explicitly marks a game as actively being played.
CANONICAL_GAME_STATUSES = ("created", "in_progress", "finalized", "abandoned")
DEFAULT_GAME_STATUS = "created"
_STATUS_LOOKUP = {s.casefold(): s for s in CANONICAL_GAME_STATUSES}


def normalize_game_status(raw: str | None, unknown_to: str = DEFAULT_GAME_STATUS) -> str:
    """Normalize a status value to the canonical taxonomy.

    Same shape as :func:`normalize_game_format`: trim + case-fold + lookup,
    empty/None → ``DEFAULT_GAME_STATUS`` regardless of ``unknown_to``,
    non-empty unknown obeys ``unknown_to``. There's no current user-input
    surface for status (it's set by code paths: ``create_game`` →
    ``created``, ``end_game`` → ``finalized``), but the normalizer is here
    for symmetry with the format pattern and for any future surface that
    accepts status input.
    """
    if raw is None:
        return DEFAULT_GAME_STATUS
    cleaned = raw.strip()
    if not cleaned:
        return DEFAULT_GAME_STATUS
    return _STATUS_LOOKUP.get(cleaned.casefold(), unknown_to)


def _capture_deck_identity(session: Session, deck_id: int | None) -> tuple[str | None, str | None]:
    """Snapshot deck name + commander names for a seat (v3.27.0b-1).

    Returns ``(deck_name, commander_name)`` for the given ``deck_id``.

    Commander identification mirrors :func:`get_seat_commander_image_urls`
    exactly: ``InventoryRow.role == 'commander'`` filtered by
    ``deck.user_id`` (NOT the game owner — game seats can reference other
    users' decks), ordered by ``InventoryRow.id`` (creation order in the
    deck), capped at 2 (Partner / Choose-a-Background / Friends Forever
    ceiling — the same cap the v3.26.1 art rendering enforces).

    Multi-commander pairs join with ``" + "`` — casual MTG parlance for
    two separate Partner cards. ``" // "`` is reserved for split-card
    faces (Scryfall convention) and would be semantically wrong here.

    NULL ``deck_id``, dangling FK, or a deck with no ``storage_location_id``
    all yield ``(None, None)``. A deck with no commander rows tagged
    yields ``(deck.name, None)``.
    """
    if not deck_id:
        return None, None
    deck = session.query(Deck).filter(Deck.id == deck_id).first()
    if not deck or not deck.storage_location_id:
        return None, None
    commander_rows = (
        session.query(InventoryRow)
        .join(Card)
        .filter(
            InventoryRow.user_id == deck.user_id,
            InventoryRow.storage_location_id == deck.storage_location_id,
            InventoryRow.role == "commander",
        )
        .order_by(InventoryRow.id)
        .limit(2)
        .all()
    )
    names = [r.card.name for r in commander_rows if r.card and r.card.name]
    if not names:
        # #165 — fall back to #163's commander ANCHOR (`deck_commanders`). A
        # PLACEHOLDER deck (#164) knows its commander but holds no cards, so the
        # inventory-row lookup above finds nothing and the snapshot would record
        # `(deck.name, None)` — losing the one fact such a deck exists to carry.
        #
        # Fixed at this shared function rather than at the caller so EVERY snapshot
        # site benefits: game creation, `set_own_seat_deck`, the seat claim, and the
        # #164 backfill all route through here.
        #
        # Same 2-card cap and `" + "` join as above; ordered by `card_id` for a
        # stable, order-independent rendering of a partner pair.
        from app.models import DeckCommander

        names = [
            n
            for (n,) in session.query(Card.name)
            .join(DeckCommander, DeckCommander.card_id == Card.id)
            .filter(DeckCommander.deck_id == deck.id)
            .order_by(Card.id)
            .limit(2)
            if n
        ]
    commander_name = " + ".join(names) if names else None
    return deck.name, commander_name


def _capture_user_attribution(
    session: Session, user_id: int | None
) -> tuple[int | None, str | None]:
    """Snapshot user attribution for a seat (v3.27.5).

    Returns ``(user_id, user_name_at_game)`` for a seat. Validates
    that ``user_id`` refers to a real ``User`` row; an invalid /
    absent / unknown id resolves to ``(None, None)`` so seat creation
    never fails over an attribution problem (mirrors the v3.25.1
    non-blocking philosophy for ``first_seat_number`` and the v3.27.2
    one for ``format``).

    The snapshot value uses ``user.display_name or user.username``,
    matching the project-wide template convention ("display name
    falls back to username"). Captured AT CREATION TIME so it
    survives later display-name edits and account deletion — same
    capture-at-game-start pattern as the v3.27.1 deck/commander
    snapshot.

    Cross-user permissive: this does NOT require ``user_id`` to be
    the game owner. Seats may legitimately reference another
    account, mirroring the existing all-decks dropdown precedent in
    ``game_new.html`` and the cross-user-deck pattern documented in
    ``get_seat_commander_image_urls``.

    Inactive (``User.is_active = False``) accounts are still valid
    targets — matches the deck precedent, which doesn't filter by
    is_active either. The snapshot column carries the historical
    fact regardless.
    """
    if not user_id:
        return None, None
    user = session.query(User).filter(User.id == user_id).first()
    if user is None:
        return None, None
    name = (user.display_name or user.username or "").strip() or None
    return user.id, name


def create_game(
    session: Session,
    user_id: int,
    format: str,
    seats: list[dict[str, Any]],
    first_seat_number: int | None = None,
    client_token: str | None = None,
    played_at: Any = None,
    momir_physical: bool = False,
) -> Game:
    """Create a game and its seats. seats is a list of {player_name, deck_id, starting_life}.

    ``first_seat_number`` (the starting seat's ``seat_number``, 1..N) is
    optional; ``None`` leaves turn order to the game tracker's existing
    clockwise-seat default (preserves pre-v3.25.1 behavior).

    ``client_token`` (v3.27.0) is the collision-proof localStorage key
    namespace generated by the route handler at creation time. NULL is
    only valid for legacy games predating v3.27.0 — new games should
    always receive a token.

    ``played_at`` (#42) overrides the create-time timestamp for a manually
    logged, already-played game; ``None`` keeps the live-tracking default of
    "now". This is the ONLY signature change the manual-log flow needs.

    Per-seat deck identity (deck_name_at_game, commander_name_at_game) is
    snapshotted at creation via :func:`_capture_deck_identity` (v3.27.0b-1)
    so subsequent deck edits / deletes don't retroactively rewrite history.
    A seat with a free-text ``deck_name`` and no ``deck_id`` (a manual-log
    opponent — #42) has that name stored verbatim as ``deck_name_at_game``,
    reusing the existing analytics-truth column rather than a new one.
    """
    now = utc_now()
    game = Game(
        user_id=user_id,
        played_at=played_at or now,
        format=format or None,
        first_seat_number=first_seat_number,
        client_token=client_token,
        created_at=now,
        # #113 — physical mode only applies to Momir; ignore the flag otherwise.
        momir_physical=bool(momir_physical) and (format == "Momir"),
    )
    session.add(game)
    session.flush()

    for i, seat in enumerate(seats, start=1):
        deck_id = seat.get("deck_id") or None
        deck_name, commander_name = _capture_deck_identity(session, deck_id)
        # #42 — free-text opponent deck name (no owned deck FK).
        if not deck_name and seat.get("deck_name"):
            deck_name = str(seat["deck_name"]).strip() or None
        # v3.27.5 — seat→user attribution. Returns (None, None) for unknown /
        # absent / invalid user_id, so the seat ships unattributed rather
        # than failing the whole game creation.
        seat_user_id, user_name = _capture_user_attribution(session, seat.get("user_id"))
        session.add(
            GameSeat(
                game_id=game.id,
                seat_number=i,
                player_name=(seat.get("player_name") or f"Player {i}").strip(),
                deck_id=deck_id,
                deck_name_at_game=deck_name,
                commander_name_at_game=commander_name,
                user_id=seat_user_id,
                user_name_at_game=user_name,
                starting_life=int(seat.get("starting_life") or 40),
                grid_position=seat.get("grid_position") or None,
            )
        )

    session.commit()
    return game


def log_game(
    session: Session,
    user_id: int,
    result: str,
    played_at: Any,
    opponents: list[dict[str, Any]],
    deck_id: int | None = None,
    format: str = DEFAULT_GAME_FORMAT,
    playgroup_id: int | None = None,
    winner_index: int | None = None,
    notes: str = "",
) -> Game:
    """Record an already-played external game — a Game *born finalized* (#42).

    Composes :func:`create_game` + :func:`end_game` in one call so a manual log
    is data-identical to a live-tracked, finalized game — every downstream
    analytic (deck win-rate, history, dashboard) picks it up with no change.

    Seat 1 is the logger (attributed via ``user_id`` + optional owned
    ``deck_id``); ``opponents`` (1..6 dicts of ``{"name", "deck_name"}``) become
    free-text seats 2..N, never FK'd to accounts or decks. ``result`` is one of
    ``"won" | "lost" | "draw"``. For a loss, ``winner_index`` is the 0-based
    opponent who won, or ``None`` for "unknown winner" (nobody at placement 1).

    Placement encoding (there is no distinct "draw" column in v1):
      - won: logger=1, opponents=2
      - lost, known winner: that opponent=1, everyone else=2
      - lost, unknown winner: everyone=2 (no seat at placement 1)
      - draw: everyone=1 — a tie for first, nobody lost
    # ponytail: draw counts as a win in placement-based win-rate; the upgrade
    # path is a dedicated result column, deferred to a later version.

    Raises ``ValueError`` (→ 400) on a forged deck (not owned by ``user_id``),
    an inaccessible playgroup, a bad result/winner, or an empty/oversized
    opponent set — so *nothing is created* unless every guard passes.
    """
    if result not in ("won", "lost", "draw"):
        raise ValueError("Invalid result")
    if not (1 <= len(opponents) <= 6):
        raise ValueError("A logged game needs 1 to 6 opponents")
    for opp in opponents:
        if not (opp.get("name") or "").strip():
            raise ValueError("Opponent names are required")
    if result == "lost" and winner_index is not None and not (0 <= winner_index < len(opponents)):
        raise ValueError("Invalid winner selection")
    if deck_id is not None:
        deck = session.query(Deck).filter(Deck.id == deck_id, Deck.user_id == user_id).first()
        if deck is None:
            raise ValueError("Deck must belong to you")
    if playgroup_id is not None:
        member = (
            session.query(PlaygroupMember.id)
            .filter(
                PlaygroupMember.user_id == user_id,
                PlaygroupMember.playgroup_id == playgroup_id,
            )
            .first()
        )
        if member is None:
            raise ValueError("Playgroup not accessible")

    _, my_name = _capture_user_attribution(session, user_id)
    seats: list[dict[str, Any]] = [
        {"player_name": my_name or "Me", "deck_id": deck_id, "user_id": user_id}
    ]
    for opp in opponents:
        seats.append(
            {"player_name": opp["name"].strip(), "deck_name": (opp.get("deck_name") or "")}
        )

    game = create_game(
        session,
        user_id=user_id,
        format=format,
        seats=seats,
        played_at=played_at,
    )
    if playgroup_id is not None:
        game.playgroup_id = playgroup_id

    # Seats are ordered by seat_number (1..N); index 0 is the logger.
    ordered = list(game.seats)
    placements: dict[int, int] = {}
    if result == "won":
        for i, seat in enumerate(ordered):
            placements[seat.id] = 1 if i == 0 else 2
    elif result == "draw":
        for seat in ordered:
            placements[seat.id] = 1
    else:  # lost
        winner_seat = None if winner_index is None else ordered[winner_index + 1]
        for seat in ordered:
            placements[seat.id] = 1 if seat is winner_seat else 2

    end_game(session, game.id, user_id, placements, {}, None, notes)
    # A manual log has no real duration — anchor ended_at to played_at so the
    # summary's elapsed reads "<1m" instead of days-since (end_game stamps now).
    game.ended_at = game.played_at
    session.commit()
    return game


def get_game(session: Session, game_id: int, user_id: int) -> Game | None:
    """Owner-scoped fetch — returns the game ONLY if ``user_id`` owns it.

    This is the strict guard the *mutation* routes (end / notes / delete /
    seat-assign / playgroup-set) depend on: a non-owner gets ``None`` and the
    route raises 404, exactly as before v3.32.0. Read routes that should be
    visible to participants use :func:`get_viewable_game` instead.
    """
    return (
        session.query(Game)
        .options(joinedload(Game.seats).joinedload(GameSeat.deck))
        .filter(Game.id == game_id, Game.user_id == user_id)
        .first()
    )


def _viewable_games_predicate(viewer_user_id: int):
    """SQLAlchemy predicate for "games ``viewer_user_id`` may view" (v3.32.0).

    Hybrid visibility (decision 2026-06-01): a game is viewable if the viewer
    (a) owns it, OR (b) is attributed to one of its seats
    (``GameSeat.user_id``), OR (c) the game is linked to a playgroup the
    viewer belongs to (``Game.playgroup_id``). Mutation rights are NOT widened
    by this — only read access.
    """
    seat_subq = select(GameSeat.game_id).where(GameSeat.user_id == viewer_user_id)
    pg_subq = select(PlaygroupMember.playgroup_id).where(PlaygroupMember.user_id == viewer_user_id)
    return or_(
        Game.user_id == viewer_user_id,
        Game.id.in_(seat_subq),
        and_(Game.playgroup_id.isnot(None), Game.playgroup_id.in_(pg_subq)),
    )


def _participant_games_predicate(viewer_user_id: int):
    """SQLAlchemy predicate for "games ``viewer_user_id`` was a PART OF".

    Narrower than :func:`_viewable_games_predicate`: a game qualifies only if
    the viewer (a) owns it OR (b) is attributed to one of its seats
    (``GameSeat.user_id``). It deliberately OMITS the playgroup-link clause (c)
    — a playgroup link makes a game *viewable* (openable by URL / detail page),
    but the Recent Games *list* should show only the viewer's own games, not
    every game any co-member happened to link to a shared playgroup (issue #39).
    """
    seat_subq = select(GameSeat.game_id).where(GameSeat.user_id == viewer_user_id)
    return or_(
        Game.user_id == viewer_user_id,
        Game.id.in_(seat_subq),
    )


def get_viewable_game(session: Session, game_id: int, viewer_user_id: int) -> Game | None:
    """Viewer-scoped fetch — returns the game if the viewer may *view* it.

    Hybrid visibility per :func:`_viewable_games_predicate`. Read-only callers
    (the game detail page) use this; they must still compute ``is_owner``
    (``game.user_id == viewer_user_id``) to decide whether to render the
    owner-only edit controls.
    """
    return (
        session.query(Game)
        .options(joinedload(Game.seats).joinedload(GameSeat.deck))
        .filter(Game.id == game_id, _viewable_games_predicate(viewer_user_id))
        .first()
    )


def list_games(session: Session, user_id: int) -> list[Game]:
    """Games ``user_id`` was a part of — owned + played-in (issue #39).

    Originally owner-only; v3.32.0 widened it to the full hybrid visibility set
    (owned + played-in + playgroup-linked). That over-widened the Recent Games
    *list*: a game merely linked to a shared playgroup showed up for every
    co-member even if they never played in it. Narrowed back to participant-only
    (:func:`_participant_games_predicate`) so the list shows only the viewer's
    own games; playgroup-shared games remain *viewable* by URL via
    :func:`get_viewable_game`. Each returned game still carries the transient
    ``is_owned_by_viewer`` attribute (not a column) so the list template can
    badge "played in" games and hide the owner-only Delete control.
    """
    games = (
        session.query(Game)
        .options(joinedload(Game.seats).joinedload(GameSeat.deck))
        .filter(_participant_games_predicate(user_id))
        .order_by(Game.played_at.desc())
        .all()
    )
    for game in games:
        game.is_owned_by_viewer = game.user_id == user_id
    return games


def update_seat(
    session: Session,
    game_id: int,
    seat_id: int,
    owner_user_id: int,
    player_name: str | None = None,
    target_user_id: int | None = None,
) -> bool | None:
    """Owner-only: edit a seat's display name and/or its attributed user.

    The retroactive correction surface for a recorded game (e.g. a Draft pod
    whose seats were captured as free-text ``player_name`` only, or a typo /
    placeholder like "Player 2"). Only the game OWNER may call this —
    ``Game.user_id == owner_user_id`` — matching the edit-rights model where
    participants get read-only access. Works on finalized games too (no
    ``status`` gate); seat metadata is corrigible after the result is recorded.

    ``player_name``: a non-empty trimmed value renames the seat; ``None`` or
    blank leaves the existing name untouched (the column is NOT NULL, so a
    blank submission is a no-op rather than a wipe).

    ``target_user_id``: ``None`` / ``0`` clears the attribution (``user_id`` +
    ``user_name_at_game`` → NULL, leaving the free-text name as the display); a
    real id sets the live FK and re-snapshots ``user_name_at_game`` via
    :func:`_capture_user_attribution` (cross-user-permissive, non-blocking — an
    unknown id resolves to a cleared attribution rather than erroring).

    Returns ``True`` on success, ``False`` if the game isn't owned by
    ``owner_user_id``, ``None`` if the seat isn't on that game (route maps both
    misses to 404).
    """
    game = session.query(Game).filter(Game.id == game_id, Game.user_id == owner_user_id).first()
    if not game:
        return False
    seat = next((s for s in game.seats if s.id == seat_id), None)
    if seat is None:
        return None
    if player_name is not None:
        cleaned = player_name.strip()
        if cleaned:
            seat.player_name = cleaned
    resolved_id, resolved_name = _capture_user_attribution(session, target_user_id)
    seat.user_id = resolved_id
    seat.user_name_at_game = resolved_name
    session.commit()
    return True


def set_game_playgroup(
    session: Session,
    game_id: int,
    owner_user_id: int,
    playgroup_id: int | None,
) -> bool:
    """Owner-only: link a game to a playgroup (or clear the link) (v3.32.0).

    Linking opens the game up to every member of that playgroup (read-only).
    Only the game OWNER may set this, and only to a playgroup the owner is a
    MEMBER of — you can't expose a game to a group you don't belong to.
    ``playgroup_id`` of ``None`` clears the link.

    Returns ``True`` on success, ``False`` if the game isn't owned by
    ``owner_user_id`` or the owner isn't a member of the target playgroup.
    """
    game = session.query(Game).filter(Game.id == game_id, Game.user_id == owner_user_id).first()
    if not game:
        return False
    if playgroup_id is not None:
        is_member = (
            session.query(PlaygroupMember.id)
            .filter(
                PlaygroupMember.user_id == owner_user_id,
                PlaygroupMember.playgroup_id == playgroup_id,
            )
            .first()
        )
        if is_member is None:
            return False
    game.playgroup_id = playgroup_id
    session.commit()
    return True


def set_first_seat(
    session: Session,
    game_id: int,
    owner_user_id: int,
    seat_number: int | None,
) -> bool:
    """Owner-only: record who goes first, or clear the choice (v4.12.26).

    **This is a BEFORE-START decision, not a creation-time one, and that is the
    whole point of the function existing.** ``first_seat_number`` used to be
    collected by a modal on ``/games/new``, which asked the question at the one
    moment it cannot be answered: with #165 seat claiming, the players who will
    hold seats 2..N have not joined yet, so the host was choosing between
    placeholders named ``Player 2`` and ``Player 3``.

    Nothing in the model had to move — :func:`live_game_service._first_seat_id`
    and the local tracker both read ``game.first_seat_number`` **at start time**
    and fall back to the first seat when it is NULL. Only the moment of asking
    was wrong.

    ``created`` only: once a game is live or finalized the turn order is already
    in the live blob, and rewriting the column would desync the rotation from
    what the table is looking at. ``None`` clears the choice (back to the
    clockwise default). A ``seat_number`` that is not one of this game's seats is
    REFUSED rather than normalized away — unlike format or attribution, a wrong
    starting player is silently wrong for the whole game.

    Returns ``True`` on success; ``False`` if the game isn't owned by
    ``owner_user_id``, isn't ``created``, or the seat doesn't belong to it.
    """
    game = session.query(Game).filter(Game.id == game_id, Game.user_id == owner_user_id).first()
    if not game or game.status != "created":
        return False
    if seat_number is not None:
        belongs = any(s.seat_number == seat_number for s in game.seats)
        if not belongs:
            return False
    game.first_seat_number = seat_number
    session.commit()
    return True


def end_game(
    session: Session,
    game_id: int,
    user_id: int,
    placements: dict[int, int],
    final_lives: dict[int, int | None],
    turn_count: int | None,
    notes: str,
    win_condition: str | None = None,
    elimination_causes: dict[int, str | None] | None = None,
) -> bool:
    """Record final placements, life totals, and turn count for a game.

    placements: {seat_id: placement_int}  (1 = winner; ties allowed — see #114)
    final_lives: {seat_id: life_total}
    elimination_causes: {seat_id: cause_str|None}  (#114 — per-seat cause)

    Safe to re-run on an already-finalized game (the post-finalization result
    edit path calls this again); it overwrites the fields, skips the live-state
    bookend (guarded below), and does not re-stamp ended_at.
    """
    elimination_causes = elimination_causes or {}
    game = session.query(Game).filter(Game.id == game_id, Game.user_id == user_id).first()
    if not game:
        return False

    for seat in game.seats:
        if seat.id in placements:
            seat.placement = placements[seat.id]
        if seat.id in final_lives:
            seat.final_life = final_lives[seat.id]
        if seat.id in elimination_causes:
            seat.elimination_cause = elimination_causes[seat.id] or None

    game.turn_count = turn_count or None
    game.notes = notes.strip() or None
    game.win_condition = normalize_win_condition(win_condition)
    # Companion mode: the live-state row is working memory only. On finalize the
    # final life/turn are captured on seats/game (above) exactly as before, so
    # drop the live blob. session.delete works on SQLite (FKs OFF); it's also the
    # ORM side of the Game.live_state delete-orphan cascade.
    if game.live_state is not None:
        # `finalized` bookend BEFORE the live row is deleted — the final state blob
        # is the event's payload. Shares this transaction. Non-live finalizes append
        # nothing. Bookends are table-level actions.
        blob = game.live_state.state
        try:
            final_turn = int(json.loads(blob).get("turn", 1))
        except (ValueError, TypeError):
            final_turn = game.turn_count or 1
        session.add(
            GameEvent(
                game_id=game.id,
                seat_id=None,
                action_type="finalized",
                payload=blob,
                turn=final_turn,
                actor_kind="table",
                created_at=utc_now(),
            )
        )
        session.delete(game.live_state)
    # v3.27.3 — mark the game as finalized. Replaces the "any seat has
    # placement → is_ended" derivation that templates used to compute;
    # template-side now reads game.status == "finalized" directly.
    game.status = "finalized"
    # v3.33.2 — stamp the end timestamp ONCE so the game-summary view can show
    # elapsed playtime (ended_at − played_at). Guarded so a later results edit
    # wouldn't inflate the duration by re-stamping to "now".
    if game.ended_at is None:
        game.ended_at = utc_now()
    session.commit()
    return True


def record_goal_results(
    session: Session,
    game_id: int,
    user_id: int,
    checked: set[tuple[int, int]],
) -> None:
    """Idempotent upsert of per-seat deck-goal completion at finalize (issue #47).

    ``checked`` is the set of ``(seat_id, goal_id)`` pairs the recorder ticked.
    For every seat whose deck the recorder OWNS (``deck.user_id == user_id`` —
    the multiplayer rule: only the deck owner's goals apply to that seat), every
    ACTIVE goal of that deck gets a result row, ``achieved`` = whether the pair
    is in ``checked``. Goals only become rows once they exist (non-retroactive).
    ``UNIQUE(game_seat_id, deck_goal_id)`` makes re-finalize safe — an existing
    row is updated in place. Forged form keys can't write rows: only real active
    goals of an owned seat's deck are ever iterated.
    """
    game = session.query(Game).filter(Game.id == game_id, Game.user_id == user_id).first()
    if not game:
        return
    seat_ids = [seat.id for seat in game.seats]
    # Bulk-fetch every existing result for this game's seats once, keyed by
    # (seat_id, goal_id), so the upsert loop does no per-pair SELECT (no N+1).
    existing_by_pair = (
        {
            (r.game_seat_id, r.deck_goal_id): r
            for r in session.query(GameGoalResult)
            .filter(GameGoalResult.game_seat_id.in_(seat_ids))
            .all()
        }
        if seat_ids
        else {}
    )
    for seat in game.seats:
        deck = seat.deck
        if deck is None or deck.user_id != user_id:
            continue
        for goal in deck.goals:
            if not goal.is_active:
                continue
            achieved = (seat.id, goal.id) in checked
            existing = existing_by_pair.get((seat.id, goal.id))
            if existing is not None:
                existing.achieved = achieved
            else:
                session.add(
                    GameGoalResult(
                        game_seat_id=seat.id,
                        deck_goal_id=goal.id,
                        achieved=achieved,
                    )
                )
    session.commit()


def update_game_notes(
    session: Session,
    game_id: int,
    user_id: int,
    notes: str,
) -> bool:
    """Update ``Game.notes`` independent of finalization state.

    Unlike :func:`end_game`, this touches only ``notes`` — placements,
    final_life, and turn_count are untouched, so it is safe to call on
    finalized games without clobbering recorded results.

    Empty/whitespace notes resolve to NULL, matching ``end_game``'s behavior.
    Returns True on success, False if the game is not found or not owned
    by ``user_id``.
    """
    game = session.query(Game).filter(Game.id == game_id, Game.user_id == user_id).first()
    if not game:
        return False
    game.notes = notes.strip() or None
    session.commit()
    return True


def toggle_seat_art_background(
    session: Session,
    game_id: int,
    seat_id: int,
    user_id: int,
) -> bool | None:
    """Flip ``GameSeat.art_background_hidden`` for a single seat.

    v3.26.6 per-seat opt-out for the v3.26.1 commander art panel background.
    Returns the new value (True = hidden, falls back to color gradient;
    False = art shown). Returns None if the game/seat is not found or not
    owned by ``user_id`` — route handler maps to 404.
    """
    game = session.query(Game).filter(Game.id == game_id, Game.user_id == user_id).first()
    if not game:
        return None
    seat = next((s for s in game.seats if s.id == seat_id), None)
    if seat is None:
        return None
    seat.art_background_hidden = not bool(seat.art_background_hidden)
    session.commit()
    return seat.art_background_hidden


def delete_game(session: Session, game_id: int, user_id: int) -> bool:
    game = session.query(Game).filter(Game.id == game_id, Game.user_id == user_id).first()
    if not game:
        return False
    session.delete(game)
    session.commit()
    return True


def get_deck_record(session: Session, deck_id: int) -> dict[str, int]:
    """Return win/loss/total counts for a deck across all recorded games."""
    seats = (
        session.query(GameSeat)
        .join(Game, GameSeat.game_id == Game.id)
        .filter(GameSeat.deck_id == deck_id, GameSeat.placement.isnot(None))
        .all()
    )
    wins = sum(1 for s in seats if s.placement == 1)
    total = len(seats)
    return {"wins": wins, "losses": total - wins, "total": total}


def get_seat_commander_image_urls(session: Session, game: Game) -> dict[int, list[str]]:
    """Return ``{seat_id: [commander_image_url, ...]}`` for the seats in ``game``.

    For each seat with a deck, looks up the commander rows via
    ``InventoryRow.role == 'commander'`` in the deck's storage location and
    returns the associated :attr:`Card.image_url` values, ordered by
    ``InventoryRow.id`` (creation order in the deck) and capped at two — the
    Partner / Choose-a-Background / Friends Forever ceiling that MTG rules
    permit. Seats with no deck, no commander tagged, or commanders with no
    cached image URL get an empty list.

    Filters by the deck's owner (``deck.user_id``) — not the game's owner —
    because game seats can reference decks owned by other users (see
    ``game_create`` in ``main.py``, which builds the deck dropdown from all
    decks, not just the requesting user's).

    Used by ``game_detail_page`` to thread commander art into the game-tracker
    ``seatDefs`` for the v3.26.1 panel-background visual treatment. One URL
    yields the full-card cover treatment; two URLs yield a vertical-halves
    split (top = primary, bottom = secondary).
    """
    result: dict[int, list[str]] = {}
    for seat in game.seats:
        if not seat.deck_id or not seat.deck or not seat.deck.storage_location_id:
            result[seat.id] = []
            continue
        commander_rows = (
            session.query(InventoryRow)
            .join(Card)
            .filter(
                InventoryRow.user_id == seat.deck.user_id,
                InventoryRow.storage_location_id == seat.deck.storage_location_id,
                InventoryRow.role == "commander",
            )
            .order_by(InventoryRow.id)
            .all()
        )
        urls: list[str] = []
        for row in commander_rows:
            if row.card and row.card.image_url:
                urls.append(row.card.image_url)
            if len(urls) >= 2:
                break
        result[seat.id] = urls
    return result


def deck_commander_scryfall_id(session: Session, deck: Deck | None) -> str | None:
    """The scryfall_id of a deck's PRIMARY commander (for companion art), or
    ``None``. Same resolution as :func:`get_seat_commander_image_urls` —
    ``role='commander'`` inventory rows in the deck's location, first by id.
    Degrades to ``None`` at every gap: no deck, no location, no tagged commander,
    or a commander card with no ``scryfall_id``."""
    if not deck or not deck.storage_location_id:
        return None
    row = (
        session.query(InventoryRow)
        .join(Card)
        .filter(
            InventoryRow.user_id == deck.user_id,
            InventoryRow.storage_location_id == deck.storage_location_id,
            InventoryRow.role == "commander",
        )
        .order_by(InventoryRow.id)
        .first()
    )
    if row and row.card and row.card.scryfall_id:
        return row.card.scryfall_id
    return None


def seat_commander_scryfall_id(session: Session, seat: GameSeat) -> str | None:
    """The seat's primary-commander scryfall_id (or ``None``) — see
    :func:`deck_commander_scryfall_id`."""
    return deck_commander_scryfall_id(session, seat.deck) if seat.deck_id else None


def get_seat_commander_scryfall_ids(session: Session, game: Game) -> dict[int, str | None]:
    """``{seat_id: commander_scryfall_id | None}`` for every seat in ``game``."""
    return {seat.id: seat_commander_scryfall_id(session, seat) for seat in game.seats}


class GameLockedError(Exception):
    """A seat's deck can't be changed because the game is no longer ``created``."""


def list_user_decks_for_companion(session: Session, user_id: int) -> list[dict]:
    """The user's own decks for the companion pre-live deck picker: id, name,
    commander name + art scryfall_id, sorted by name. Matches the game-creation
    picker's filter — no brew exclusion (``game_new`` lists all decks)."""
    from app.deck_service import list_pickable_decks

    decks = list_pickable_decks(session, [user_id])
    return [
        {
            "id": d.id,
            "name": d.name,
            "commander_name": _capture_deck_identity(session, d.id)[1],
            "commander_scryfall_id": deck_commander_scryfall_id(session, d),
        }
        for d in decks
    ]


def set_own_seat_deck(
    session: Session, game_id: int, user_id: int, deck_id: int | None
) -> GameSeat:
    """A seated player picks/overrides their OWN seat's deck from the companion
    view. Personal choice — phones-only, NO table-token path (the owner still
    edits seats through the game-edit surface).

    Only while the game is ``created`` (raises :class:`GameLockedError` → 409 once
    live/finalized). ``deck_id`` None/0 clears the deck (legal — creation allows a
    seat with no deck). A non-None ``deck_id`` must reference a deck OWNED by the
    caller (unknown id → ValueError/400; someone else's deck → PermissionError/403).

    Re-derives EVERY deck-denormalized seat field via the same
    :func:`_capture_deck_identity` creation uses — no forked logic."""
    game = get_viewable_game(session, game_id, user_id)
    if game is None:
        raise LookupError("Game not found")
    if game.status != "created":
        raise GameLockedError("Deck locked once the game is live")
    seat = next((s for s in game.seats if s.user_id == user_id), None)
    if seat is None:
        raise PermissionError("You don't have a seat in this game")

    deck_id = int(deck_id) if deck_id else None
    deck: Deck | None = None
    if deck_id is not None:
        deck = session.query(Deck).filter(Deck.id == deck_id).first()
        if deck is None:
            raise ValueError("Deck not found")
        if deck.user_id != user_id:
            raise PermissionError("That deck isn't yours")

    # Re-derive the seat's deck snapshot exactly as create_game does.
    deck_name, commander_name = _capture_deck_identity(session, deck_id)
    seat.deck_id = deck_id
    seat.deck = deck  # keep the relationship consistent for immediate re-reads
    seat.deck_name_at_game = deck_name
    seat.commander_name_at_game = commander_name
    session.commit()
    return seat


# --- #165: seat claiming from a phone ----------------------------------------


def generate_join_code(session: Session) -> str:
    """A unique opaque seat-claim code.

    Same primitive and retry shape as ``playgroup_service._generate_join_code``
    and ``Game.client_token``. NOT interchangeable with ``client_token``: that
    grants control of every seat and must never reach a phone; this only lets a
    logged-in member attach themselves to one unclaimed seat.
    """
    for _ in range(8):
        code = secrets.token_urlsafe(8)
        if session.query(Game.id).filter(Game.join_code == code).first() is None:
            return code
    return secrets.token_urlsafe(16)  # astronomically unlikely; widen entropy


def get_game_by_join_code(session: Session, code: str) -> Game | None:
    """Strictly by ENABLED join code. Empty/None never matches — ``join_code ==
    None`` would be NULL-rejecting anyway, but the explicit guard means a blank
    form field cannot fall through to a game whose claiming is disabled."""
    trimmed = (code or "").strip()
    if not trimmed:
        return None
    return session.query(Game).filter(Game.join_code == trimmed).first()


def claimable_seats(game: Game) -> list[GameSeat]:
    """Seats nobody has claimed — no ``user_id``. Ordered by seat number so the
    phone's list matches the table's."""
    return sorted((s for s in game.seats if s.user_id is None), key=lambda s: s.seat_number)


def joinable_games_for_user(session: Session, user_id: int) -> list[dict]:
    """Games a playgroup co-member can take a seat in WITHOUT being handed a code.

    The app already knows who is in your playgroup, so making a member scan a QR
    to join their own group's game is asking them to prove something it can look
    up. Their phone lists it instead (owner decision 2026-07-28).

    Four conditions, and each one is doing work:

    * ``status == 'created'`` — claiming is refused once live, the same boundary
      :func:`claim_seat` enforces. Listing a game you cannot join is worse than
      not listing it.
    * ``join_code IS NOT NULL`` — **the code stays THE toggle.** A host who turned
      joining off has turned it off for members too; this is a second DOOR, never
      a second switch. Deliberate: two independent ways to enable joining is a
      state nobody can reason about from the game page.
    * The user is a MEMBER of the game's playgroup. An unlinked game is invisible
      here, exactly as an unlinked game is invisible to the playgroup record.
    * The user does not already hold a seat, and one is free. ``claim_seat``
      refuses both, so offering either would be offering a dead link.

    **This is a discovery surface, NOT a new permission.** The claim it links to is
    the same ``/join/{code}`` primitive with the same guards; membership only earns
    you the link. Claiming itself stays open to anyone holding the code (owner
    decision, same date) — so a guest at the table is unaffected.
    """
    my_playgroups = select(PlaygroupMember.playgroup_id).where(PlaygroupMember.user_id == user_id)
    games = (
        session.query(Game)
        .filter(
            Game.status == "created",
            Game.join_code.isnot(None),
            Game.playgroup_id.in_(my_playgroups),
        )
        .order_by(Game.played_at.desc())
        .all()
    )
    out: list[dict] = []
    for game in games:
        if any(s.user_id == user_id for s in game.seats):
            continue
        open_seats = claimable_seats(game)
        if not open_seats:
            continue
        out.append(
            {
                "game_id": game.id,
                "join_code": game.join_code,
                "format": game.format or "Commander",
                "playgroup_name": game.playgroup.name if game.playgroup else "",
                "seat_count": len(game.seats),
                "open_seats": len(open_seats),
                "played_at": game.played_at,
            }
        )
    return out


def claim_seat(
    session: Session,
    *,
    code: str,
    user_id: int,
    seat_id: int,
    display_name: str = "",
    commander_entry: str = "",
) -> tuple[GameSeat, list[str]]:
    """A logged-in member claims ONE unclaimed seat. Returns ``(seat, unresolved)``.

    ``unresolved`` names a commander the catalog does not contain; the seat is
    still claimed (never fail a claim over a deck-naming problem — the same
    non-blocking posture game creation takes for format / first_seat / attribution)
    and the caller shows the name back.

    Guards, each deliberate:

    * ``created`` ONLY — once live, raises :class:`GameLockedError` → 409. This is
      the SAME boundary ``set_own_seat_deck`` already enforces, not a second timing
      model.
    * The seat must be **unclaimed**. Taking someone else's seat is a
      ``PermissionError`` → 403, not a silent overwrite.
    * One seat per person: a member already seated in this game cannot claim a
      second. Otherwise one phone could occupy the table.
    * **The claim ALWAYS attaches ``user_id``.** #152's playgroup record groups by
      ``GameSeat.user_id`` and never ``player_name``, so a claim that set only a
      name would regress attribution for the best-covered population.
    * The placeholder ``player_name`` ("Player 3") is OVERWRITTEN. Positional
      labels are worse than nothing — next month's "Player 3" is a different human
      (see #167 on ``Opp 1/2/3``).
    """
    from app.deck_service import resolve_commander_to_deck

    game = get_game_by_join_code(session, code)
    if game is None:
        raise LookupError("Game not found")
    if game.status != "created":
        raise GameLockedError("This game has already started")

    if any(s.user_id == user_id for s in game.seats):
        raise PermissionError("You already have a seat in this game")

    seat = next((s for s in game.seats if s.id == seat_id), None)
    if seat is None:
        raise LookupError("Seat not found")
    if seat.user_id is not None:
        raise PermissionError("That seat is already taken")

    user = session.get(User, user_id)
    seat.user_id = user_id
    resolved_name = (display_name or "").strip()
    if not resolved_name and user is not None:
        resolved_name = (user.display_name or user.username or "").strip()
    if resolved_name:
        seat.player_name = resolved_name[:128]
    seat.user_name_at_game = seat.player_name

    unresolved: list[str] = []
    if (commander_entry or "").strip():
        deck, missing = resolve_commander_to_deck(session, user_id, commander_entry, commit=False)
        unresolved = missing
        if deck is not None:
            seat.deck_id = deck.id
            deck_name, commander_name = _capture_deck_identity(session, deck.id)
            seat.deck_name_at_game = deck_name
            seat.commander_name_at_game = commander_name

    session.commit()
    return seat, unresolved


def join_qr_svg(url: str) -> str:
    """Inline SVG QR for a claim URL (#165).

    Server-side, so there is no CDN request, no vendored JS, nothing for the CSP to
    block, and it works with JS disabled. `SvgPathImage` is pure Python — Pillow is
    not involved.

    The QR is a SHORTCUT to the claim, never the mechanism: the code is always
    displayed as text beside it, so a tablet across the table, an angled screen, or
    a camera in bad light can never be what stops someone joining. Returns "" on any
    failure for the same reason — a QR that will not draw must degrade to the code,
    not to a broken page.
    """
    if not url:
        return ""
    try:
        import io

        import qrcode
        from qrcode.image.svg import SvgPathImage

        qr = qrcode.QRCode(box_size=6, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image(image_factory=SvgPathImage).save(buf)
        svg = buf.getvalue().decode("utf-8")
        # Drop the XML prolog so the fragment can be embedded directly in the page.
        return svg[svg.index("<svg") :] if "<svg" in svg else ""
    except Exception:  # noqa: BLE001 — a QR failure must never break the page
        logger.warning("join QR render failed", exc_info=True)
        return ""
