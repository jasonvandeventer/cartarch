from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``games.first_seat_number`` (v3.25.1 — first-player selection).

    Nullable Integer, no default. NULL means "no explicit first player was
    chosen": the game tracker falls back to its existing behavior (turn order
    seeded from the lowest clockwise seat position via ``clockwiseSeats[0]``
    in ``game_detail.html``), so every pre-v3.25.1 game and any new game
    without an explicit pick behaves exactly as before. When set, it holds the
    starting seat's ``GameSeat.seat_number`` (1..N).

    Additive single-column ALTER, no constraint changes — safe under the
    SQLite-until-v4 constraint. Idempotent: skips if the column already
    exists.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "games", "first_seat_number"):
            conn.execute(text("ALTER TABLE games ADD COLUMN first_seat_number INTEGER"))
            print("Added games.first_seat_number (NULL = no explicit first player)")
        else:
            print("games.first_seat_number already exists, skipping")


if __name__ == "__main__":
    main()
