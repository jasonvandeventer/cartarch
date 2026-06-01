"""Add ``games.playgroup_id`` — optional playgroup link (v3.32.0).

Shared game visibility: a game becomes viewable by every member of a
linked playgroup (in addition to its owner and any seat-attributed
players). The link is optional — NULL means "owner + seat-attributed
players only", which is what every legacy game keeps.

Additive single-column ALTER, no constraint changes — safe under the
SQLite-until-v4 "additive / no-rebuild" posture. Idempotent: skips
ADD COLUMN when the column already exists.

**NO BACKFILL — deliberate.** Legacy games have no truthful playgroup
association; guessing one (e.g. the owner's first playgroup) would
manufacture a misleading visibility grant under a trustworthy-looking
column — the same anti-pattern the v3.27.5 seat-attribution migration
refused. Legacy games keep ``playgroup_id`` NULL; the owner opts a game
into a playgroup explicitly from the game detail page.

**FK enforcement note**: ``REFERENCES playgroups(id) ON DELETE SET NULL``
is declared for documentation + v4 Postgres forward-compat. SQLite
ignores it (``PRAGMA foreign_keys`` is OFF project-wide — see
``app/db.py``); ``playgroup_service.delete_playgroup`` nulls referencing
games explicitly. A dangling id is access-safe regardless — once a
playgroup's member rows are gone, the membership visibility check
returns nobody.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    with engine.begin() as conn:
        if not column_exists(conn, "games", "playgroup_id"):
            conn.execute(
                text(
                    "ALTER TABLE games ADD COLUMN playgroup_id INTEGER "
                    "REFERENCES playgroups(id) ON DELETE SET NULL"
                )
            )
            print("Added games.playgroup_id (NULL = owner + seat-attributed players only)")
        else:
            print("games.playgroup_id already exists, skipping")

        # Helper index for the membership-visibility lookup ("games linked to
        # playgroups I belong to"). Idempotent via IF NOT EXISTS.
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_games_playgroup_id ON games(playgroup_id)")
        )


if __name__ == "__main__":
    main()
