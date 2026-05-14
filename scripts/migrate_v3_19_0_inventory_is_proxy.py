from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    with engine.begin() as conn:
        if not column_exists(conn, "inventory_rows", "is_proxy"):
            conn.execute(
                text("ALTER TABLE inventory_rows ADD COLUMN is_proxy BOOLEAN DEFAULT 0 NOT NULL")
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_inventory_rows_is_proxy ON inventory_rows(is_proxy)"
                )
            )
            print("Added is_proxy column + index to inventory_rows (default 0)")
        else:
            print("is_proxy column already exists, skipping")


if __name__ == "__main__":
    main()
