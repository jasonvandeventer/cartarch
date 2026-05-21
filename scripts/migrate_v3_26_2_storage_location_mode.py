from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``storage_locations.mode`` (v3.26.2 — per-location sorter modes).

    Four valid values, enforced at the service layer (matches the existing
    ``StorageLocation.type`` pattern — no DB-level CHECK):

    - ``managed`` — sorter places into and may move out of this location.
      Default. Preserves pre-v3.26.2 behavior for every existing row.
    - ``manual`` — sorter never places into and never moves out.
    - ``sink`` — source-only catch-all the sorter pulls FROM during
      rebalancing but never places INTO.
    - ``ignored`` — invisible to the sorter.

    Additive single-column ALTER with a NOT NULL DEFAULT, no constraint
    changes — safe under the SQLite-until-v4 constraint. Idempotent:
    skips if the column already exists.

    Followed by a deck-backfill block: the ALTER's DEFAULT 'managed'
    applies to every existing row, including ``type='deck'`` locations.
    But deck locations must be ``manual`` (the drawer sorter must never
    touch deck contents — matches the same intent baked into
    ``create_deck()`` in ``deck_service.py`` for newly-created decks).
    The backfill UPDATE corrects any deck rows the DEFAULT clause set to
    'managed'. Inner idempotency check on the row count — fires only
    when there is something to fix.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "storage_locations", "mode"):
            conn.execute(
                text(
                    "ALTER TABLE storage_locations "
                    "ADD COLUMN mode TEXT NOT NULL DEFAULT 'managed'"
                )
            )
            print(
                "Added storage_locations.mode "
                "(default 'managed' — preserves pre-v3.26.2 sorter behavior)"
            )
        else:
            print("storage_locations.mode already exists, skipping ADD COLUMN")

        deck_managed = conn.execute(
            text("SELECT COUNT(*) FROM storage_locations " "WHERE type='deck' AND mode='managed'")
        ).scalar()
        if deck_managed and deck_managed > 0:
            conn.execute(
                text(
                    "UPDATE storage_locations SET mode='manual' "
                    "WHERE type='deck' AND mode='managed'"
                )
            )
            print(f"Backfilled {deck_managed} deck-type location(s) to mode='manual'")
        else:
            print("All deck-type locations already at mode='manual', skipping backfill")


if __name__ == "__main__":
    main()
