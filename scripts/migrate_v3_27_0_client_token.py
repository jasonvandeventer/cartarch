from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``games.client_token`` (v3.27.0 — game-tracker localStorage
    ID-reuse collision fix).

    Nullable Text, no default. NULL means "legacy game predating this
    fix": the game tracker falls back to the bare ``mana-game-${gameId}``
    localStorage key so existing in-progress trackers keep their saved
    state and are NOT wiped. New games always get a token generated at
    creation time (server-side, exactly once, via ``secrets.token_urlsafe``).

    The token is the second half of a composite localStorage key
    (``mana-game-${gameId}-${clientToken}``) — a per-game namespace that
    survives SQLite rowid reuse after a game is deleted and a new game
    is created with the same id. This is a key-only fix; the saved-state
    blob shape and ``gameFingerprint()`` are unchanged (would mid-game-
    wipe live trackers for zero benefit, same rationale documented for
    ``first_seat_number`` in v3.25.1).

    Additive single-column ALTER, no constraint changes — safe under the
    SQLite-until-v4 constraint. Idempotent: skips if the column already
    exists. NOT backfilled — legacy NULL is the correct value for games
    created pre-v3.27.0; the client fallback handles them.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "games", "client_token"):
            conn.execute(text("ALTER TABLE games ADD COLUMN client_token TEXT"))
            print("Added games.client_token (NULL = legacy game, falls back to bare key)")
        else:
            print("games.client_token already exists, skipping")


if __name__ == "__main__":
    main()
