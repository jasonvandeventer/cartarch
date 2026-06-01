"""Game tracking service — create, retrieve, end, and summarise game sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Card,
    Deck,
    Game,
    GameSeat,
    InventoryRow,
    PlaygroupMember,
    User,
)

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
) -> Game:
    """Create a game and its seats. seats is a list of {player_name, deck_id, starting_life}.

    ``first_seat_number`` (the starting seat's ``seat_number``, 1..N) is
    optional; ``None`` leaves turn order to the game tracker's existing
    clockwise-seat default (preserves pre-v3.25.1 behavior).

    ``client_token`` (v3.27.0) is the collision-proof localStorage key
    namespace generated by the route handler at creation time. NULL is
    only valid for legacy games predating v3.27.0 — new games should
    always receive a token.

    Per-seat deck identity (deck_name_at_game, commander_name_at_game) is
    snapshotted at creation via :func:`_capture_deck_identity` (v3.27.0b-1)
    so subsequent deck edits / deletes don't retroactively rewrite history.
    """
    now = datetime.utcnow()
    game = Game(
        user_id=user_id,
        played_at=now,
        format=format or None,
        first_seat_number=first_seat_number,
        client_token=client_token,
        created_at=now,
    )
    session.add(game)
    session.flush()

    for i, seat in enumerate(seats, start=1):
        deck_id = seat.get("deck_id") or None
        deck_name, commander_name = _capture_deck_identity(session, deck_id)
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
    """Games visible to ``user_id`` — owned + played-in + playgroup (v3.32.0).

    Widened from owner-only to the hybrid visibility set
    (:func:`_viewable_games_predicate`). Each returned game carries a transient
    ``is_owned_by_viewer`` attribute (not a column) so the list template can
    badge "played in" games and hide the owner-only Delete control on games the
    viewer doesn't own.
    """
    games = (
        session.query(Game)
        .options(joinedload(Game.seats).joinedload(GameSeat.deck))
        .filter(_viewable_games_predicate(user_id))
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


def end_game(
    session: Session,
    game_id: int,
    user_id: int,
    placements: dict[int, int],
    final_lives: dict[int, int | None],
    turn_count: int | None,
    notes: str,
) -> bool:
    """Record final placements, life totals, and turn count for a game.

    placements: {seat_id: placement_int}  (1 = winner)
    final_lives: {seat_id: life_total}
    """
    game = session.query(Game).filter(Game.id == game_id, Game.user_id == user_id).first()
    if not game:
        return False

    for seat in game.seats:
        if seat.id in placements:
            seat.placement = placements[seat.id]
        if seat.id in final_lives:
            seat.final_life = final_lives[seat.id]

    game.turn_count = turn_count or None
    game.notes = notes.strip() or None
    # v3.27.3 — mark the game as finalized. Replaces the "any seat has
    # placement → is_ended" derivation that templates used to compute;
    # template-side now reads game.status == "finalized" directly.
    game.status = "finalized"
    session.commit()
    return True


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
