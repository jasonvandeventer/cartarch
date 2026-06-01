"""Add ``games.ended_at`` — wall-clock end timestamp (v3.33.2).

Stamped once by ``game_service.end_game`` when a game is finalized, so the
game-summary view can show elapsed playtime (``ended_at − played_at``).

Additive single nullable column, no constraint changes — safe under the
SQLite-until-v4 "additive / no-rebuild" posture. Idempotent: skips ADD COLUMN
when the column already exists.

**NO BACKFILL — deliberate.** Past games' real durations are unrecoverable
(only ``played_at`` = creation time was ever stored; per-turn timing lived in
browser localStorage and is gone). Legacy finalized games keep ``ended_at``
NULL and the summary renders "—" for elapsed; only games finalized from
v3.33.2 onward get a real duration.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    with engine.begin() as conn:
        if not column_exists(conn, "games", "ended_at"):
            conn.execute(text("ALTER TABLE games ADD COLUMN ended_at DATETIME"))
            print("Added games.ended_at (NULL = not finalized / legacy game)")
        else:
            print("games.ended_at already exists, skipping")


if __name__ == "__main__":
    main()
