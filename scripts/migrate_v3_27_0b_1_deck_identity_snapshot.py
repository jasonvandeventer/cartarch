from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``game_seats.deck_name_at_game`` + ``commander_name_at_game``
    (v3.27.0b-1 — deck identity snapshot at game time).

    Both columns: nullable TEXT, no default. Closes the analytics-
    correctness gap where ``GameSeat.deck_id`` is a live FK; editing or
    deleting a deck retroactively changes the deck composition of every
    historical game that referenced it. Snapshot columns are captured
    at game creation (see ``_capture_deck_identity`` in
    ``app/game_service.py``) so analytics that ask "what worked in this
    game?" see the deck as it actually was when played.

    Backfill populates snapshots for every existing seat from the
    deck's CURRENT identity. Historical games whose decks have already
    been edited will carry today's (stale) identity — accepted
    limitation; the snapshot is correct from this migration forward.
    Seats with NULL ``deck_id`` OR a dangling FK (deck since deleted)
    get NULL snapshots, not an error.

    Backfill idempotency: only touches rows where ``deck_name_at_game IS
    NULL AND deck_id IS NOT NULL``, so re-running the migration is a
    no-op for already-backfilled rows.

    Additive single-column ALTERs, no constraint changes — safe under
    the SQLite-until-v4 constraint. Idempotent: skips ADD COLUMN if
    either column already exists.

    Commander identification mirrors ``get_seat_commander_image_urls``
    in ``app/game_service.py``: ``InventoryRow.role == 'commander'``
    filtered by ``deck.user_id`` (not the game owner — seats can
    reference other users' decks), ordered by ``InventoryRow.id``
    (creation order), capped at 2 (Partner / Choose-a-Background /
    Friends Forever ceiling). Two commanders join with " + " — casual
    MTG parlance; " // " is reserved for split-card faces and would be
    semantically wrong for two separate Partner cards.
    """
    with engine.begin() as conn:
        for col in ("deck_name_at_game", "commander_name_at_game"):
            if not column_exists(conn, "game_seats", col):
                conn.execute(text(f"ALTER TABLE game_seats ADD COLUMN {col} TEXT"))
                print(f"Added game_seats.{col}")
            else:
                print(f"game_seats.{col} already exists, skipping ADD COLUMN")

        # Backfill in raw SQL — single batched query per seat is fine on
        # the current dataset (production has one digit of games). The
        # idempotency filter (``deck_name_at_game IS NULL``) means
        # re-running the migration after a partial backfill picks up
        # only the still-NULL rows.
        seats_to_backfill = conn.execute(
            text(
                "SELECT id, deck_id FROM game_seats "
                "WHERE deck_name_at_game IS NULL AND deck_id IS NOT NULL"
            )
        ).fetchall()

        if not seats_to_backfill:
            print("No seats need deck-identity backfill, skipping")
            return

        backfilled = 0
        dangling = 0
        for seat_id, deck_id in seats_to_backfill:
            deck = conn.execute(
                text("SELECT name, user_id, storage_location_id " "FROM decks WHERE id = :id"),
                {"id": deck_id},
            ).fetchone()
            if deck is None:
                # Dangling FK — deck since deleted. Leave snapshots NULL.
                dangling += 1
                continue
            deck_name, deck_user_id, deck_loc_id = deck

            commander_name: str | None = None
            if deck_loc_id is not None:
                commander_rows = conn.execute(
                    text(
                        "SELECT c.name FROM inventory_rows ir "
                        "JOIN cards c ON ir.card_id = c.id "
                        "WHERE ir.user_id = :uid "
                        "AND ir.storage_location_id = :loc "
                        "AND ir.role = 'commander' "
                        "ORDER BY ir.id LIMIT 2"
                    ),
                    {"uid": deck_user_id, "loc": deck_loc_id},
                ).fetchall()
                names = [r[0] for r in commander_rows if r[0]]
                if names:
                    commander_name = " + ".join(names)

            conn.execute(
                text(
                    "UPDATE game_seats SET "
                    "deck_name_at_game = :dn, commander_name_at_game = :cn "
                    "WHERE id = :id"
                ),
                {"dn": deck_name, "cn": commander_name, "id": seat_id},
            )
            backfilled += 1

        print(
            f"Backfilled {backfilled} seat(s) with deck identity"
            + (f" ({dangling} skipped — dangling deck_id FK)" if dangling else "")
        )


if __name__ == "__main__":
    main()
