from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    with engine.begin() as conn:
        if not column_exists(conn, "inventory_rows", "language"):
            conn.execute(text("ALTER TABLE inventory_rows ADD COLUMN language VARCHAR(8)"))
            conn.execute(text("UPDATE inventory_rows SET language = 'en' WHERE language IS NULL"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_inventory_rows_language ON inventory_rows(language)"
                )
            )
            print(
                "Added language column + index to inventory_rows; backfilled existing rows to 'en'"
            )
        else:
            print("language column already exists, skipping")


if __name__ == "__main__":
    main()
