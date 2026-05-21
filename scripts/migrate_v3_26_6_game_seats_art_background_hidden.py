from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``game_seats.art_background_hidden`` (v3.26.6 — per-seat commander
    art panel toggle).

    Boolean column stored as INTEGER 0/1 (SQLite's idiomatic boolean shape).
    ``DEFAULT 0`` preserves the v3.26.1 auto-on art-background behavior for
    every existing seat — toggle is opt-OUT, not opt-in.

    Additive single-column ALTER with a NOT NULL DEFAULT, no constraint
    changes — safe under the SQLite-until-v4 constraint. Idempotent: skips
    if the column already exists.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "game_seats", "art_background_hidden"):
            conn.execute(
                text(
                    "ALTER TABLE game_seats "
                    "ADD COLUMN art_background_hidden INTEGER NOT NULL DEFAULT 0"
                )
            )
            print(
                "Added game_seats.art_background_hidden "
                "(default 0 — preserves v3.26.1 auto-on commander art behavior)"
            )
        else:
            print("game_seats.art_background_hidden already exists, skipping ADD COLUMN")


if __name__ == "__main__":
    main()
