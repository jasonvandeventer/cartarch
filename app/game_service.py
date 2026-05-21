"""Game tracking service — create, retrieve, end, and summarise game sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import Card, Game, GameSeat, InventoryRow


def create_game(
    session: Session,
    user_id: int,
    format: str,
    seats: list[dict[str, Any]],
    first_seat_number: int | None = None,
) -> Game:
    """Create a game and its seats. seats is a list of {player_name, deck_id, starting_life}.

    ``first_seat_number`` (the starting seat's ``seat_number``, 1..N) is
    optional; ``None`` leaves turn order to the game tracker's existing
    clockwise-seat default (preserves pre-v3.25.1 behavior).
    """
    now = datetime.utcnow()
    game = Game(
        user_id=user_id,
        played_at=now,
        format=format or None,
        first_seat_number=first_seat_number,
        created_at=now,
    )
    session.add(game)
    session.flush()

    for i, seat in enumerate(seats, start=1):
        session.add(
            GameSeat(
                game_id=game.id,
                seat_number=i,
                player_name=(seat.get("player_name") or f"Player {i}").strip(),
                deck_id=seat.get("deck_id") or None,
                starting_life=int(seat.get("starting_life") or 40),
                grid_position=seat.get("grid_position") or None,
            )
        )

    session.commit()
    return game


def get_game(session: Session, game_id: int, user_id: int) -> Game | None:
    return (
        session.query(Game)
        .options(joinedload(Game.seats).joinedload(GameSeat.deck))
        .filter(Game.id == game_id, Game.user_id == user_id)
        .first()
    )


def list_games(session: Session, user_id: int) -> list[Game]:
    return (
        session.query(Game)
        .options(joinedload(Game.seats).joinedload(GameSeat.deck))
        .filter(Game.user_id == user_id)
        .order_by(Game.played_at.desc())
        .all()
    )


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
