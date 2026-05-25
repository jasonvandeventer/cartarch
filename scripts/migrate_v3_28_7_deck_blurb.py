from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``decks.blurb`` (v3.28.7 — Folio smaller-page polish).

    A short flavour-blurb sub-line that renders beneath each deck's name
    on the redesigned editorial-row Decks page ("Superfriends · counters
    · proliferate", "midrange / lifegain", etc.). Settable via the Edit
    popout and the New Deck form.

    Per the v3.28.x cluster's schema-posture decision (#3), additive
    nullable columns are permitted under the SQLite-until-v4 constraint.
    This is the v3.28.7 release's only schema change — one nullable
    column on the `Deck` model. The column goes on `Deck` (not
    `StorageLocation`) because the `decks-as-StorageLocations`
    relationship is a shared-table seam; deck-specific attributes
    belong on the dedicated `Deck` model alongside `format` / `notes`
    / `intent_*`.

    Additive single-column ALTER, no constraint changes — safe under
    the SQLite-until-v4 constraint. Idempotent: skips if the column
    already exists.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "decks", "blurb"):
            conn.execute(text("ALTER TABLE decks ADD COLUMN blurb TEXT"))
            print("Added decks.blurb (NULL)")
        else:
            print("decks.blurb already exists, skipping")


if __name__ == "__main__":
    main()
