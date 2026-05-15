from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add the four Scryfall-only printing-trait columns to cards.

    All are nullable with no default — NULL means "not yet fetched" so the
    drawer-sorter trait resolver knows to live-fetch (and the price-refresh
    loop knows to backfill). Idempotent: skips any column that already exists.
    """
    added = []
    with engine.begin() as conn:
        if not column_exists(conn, "cards", "full_art"):
            conn.execute(text("ALTER TABLE cards ADD COLUMN full_art BOOLEAN"))
            added.append("full_art")
        if not column_exists(conn, "cards", "frame_effects"):
            conn.execute(text("ALTER TABLE cards ADD COLUMN frame_effects TEXT"))
            added.append("frame_effects")
        if not column_exists(conn, "cards", "set_type"):
            conn.execute(text("ALTER TABLE cards ADD COLUMN set_type VARCHAR(64)"))
            added.append("set_type")
        if not column_exists(conn, "cards", "layout"):
            conn.execute(text("ALTER TABLE cards ADD COLUMN layout VARCHAR(64)"))
            added.append("layout")

    if added:
        print(f"Added card-trait columns: {', '.join(added)} (NULL = not yet fetched)")
    else:
        print("card-trait columns already exist, skipping")


if __name__ == "__main__":
    main()
