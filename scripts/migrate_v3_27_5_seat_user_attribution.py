from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``game_seats.user_id`` + ``game_seats.user_name_at_game``
    (v3.27.5 — fifth and final sub-patch of the v3.27.0 sequel).

    Closes the seat→user attribution gap. ``game_new.html`` has long
    submitted ``user_ids`` per seat, but ``game_create`` silently
    dropped the field — seat→user attribution was never actually
    recorded. Two columns by design, mirroring v3.27.1's deck-identity
    snapshot pattern (live FK + analytics-stable snapshot):

    - ``user_id`` — nullable FK to ``users.id``. The navigational link
      ("is this seat a current user account?"). Nulled when that user
      is later deleted.
    - ``user_name_at_game`` — nullable TEXT snapshot captured at game
      creation. The permanent attribution that SURVIVES account
      deletion — the v3.27.1 deck snapshot precedent applied to user
      attribution. Read by analytics; renders unattributed names
      historically even after the account is gone.

    Rejected alternatives: blocking user deletion when historical
    seats reference the user (violates the public-readiness account-
    deletion requirement); FK-only (would silently anonymize historical
    seats on every account deletion — the exact corruption v3.27.1 was
    built to prevent).

    **NO BACKFILL — deliberate**, consistent with the v3.27.4 admin
    last-signed-in patch. The existing production games' submitted
    ``user_ids`` were silently dropped at creation; there is NO
    truthful record of who was attributed to legacy seats.
    Backfilling by guessing (e.g. attributing to the game owner) would
    manufacture a misleading signal under a trustworthy-looking
    column. Legacy seats keep both columns NULL, honestly representing
    "never recorded." Pre-v3.27.5 game display still uses
    ``GameSeat.player_name`` (free-text, always captured) for seat
    attribution, which remains the source of truth for guest seats too
    (account-backed seats post-v3.27.5 carry both).

    Additive single-column ALTERs, no constraint changes — safe under
    the SQLite-until-v4 constraint. Idempotent: skips ADD COLUMN if
    either column already exists.

    **FK enforcement note**: ``REFERENCES users(id) ON DELETE SET NULL``
    is declared on the new column for documentation and forward-
    compatibility with v4 Postgres. SQLite ignores it at the engine
    layer because ``PRAGMA foreign_keys`` is OFF (the project's default
    — see ``app/db.py``); the outcome is enforced by an explicit
    ``UPDATE game_seats SET user_id = NULL`` step in the admin
    user-deletion cascade in ``app/routes/admin.py``. The OUTCOME — a
    deleted user's seats get ``user_id`` nulled with
    ``user_name_at_game`` untouched — is identical whichever mechanism
    fires.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "game_seats", "user_id"):
            conn.execute(
                text(
                    "ALTER TABLE game_seats ADD COLUMN user_id INTEGER "
                    "REFERENCES users(id) ON DELETE SET NULL"
                )
            )
            print("Added game_seats.user_id (NULL = unattributed / guest / legacy)")
        else:
            print("game_seats.user_id already exists, skipping")

        if not column_exists(conn, "game_seats", "user_name_at_game"):
            conn.execute(text("ALTER TABLE game_seats ADD COLUMN user_name_at_game TEXT"))
            print("Added game_seats.user_name_at_game (NULL until captured at creation)")
        else:
            print("game_seats.user_name_at_game already exists, skipping")


if __name__ == "__main__":
    main()
