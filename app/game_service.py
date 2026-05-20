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


def get_seat_commander_image_urls(session: Session, game: Game) -> dict[int, str | None]:
    """Return ``{seat_id: commander_image_url_or_None}`` for the seats in ``game``.

    For each seat with a deck, looks up the commander row via
    ``InventoryRow.role == 'commander'`` in the deck's storage location (the
    established pattern in :func:`app.deck_service.list_decks`) and returns the
    associated :attr:`Card.image_url`. Seats with no deck, decks with no
    commander tagged, or commanders with no cached image URL get ``None``.

    Filters by the deck's owner (``deck.user_id``) — not the game's owner —
    because game seats can reference decks owned by other users (see
    ``game_create`` in ``main.py``, which builds the deck dropdown from all
    decks, not just the requesting user's).

    Used by ``game_detail_page`` to thread commander art into the game-tracker
    ``seatDefs`` for the v3.26.1 panel-background visual treatment.
    """
    result: dict[int, str | None] = {}
    for seat in game.seats:
        if not seat.deck_id or not seat.deck or not seat.deck.storage_location_id:
            result[seat.id] = None
            continue
        commander_row = (
            session.query(InventoryRow)
            .join(Card)
            .filter(
                InventoryRow.user_id == seat.deck.user_id,
                InventoryRow.storage_location_id == seat.deck.storage_location_id,
                InventoryRow.role == "commander",
            )
            .first()
        )
        if commander_row and commander_row.card and commander_row.card.image_url:
            result[seat.id] = commander_row.card.image_url
        else:
            result[seat.id] = None
    return result
