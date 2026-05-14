from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    with engine.begin() as conn:
        if not column_exists(conn, "inventory_rows", "from_drawer"):
            conn.execute(text("ALTER TABLE inventory_rows ADD COLUMN from_drawer VARCHAR(32)"))
            print("Added from_drawer column to inventory_rows")
        else:
            print("from_drawer column already exists, skipping")

        if not column_exists(conn, "inventory_rows", "from_slot"):
            conn.execute(text("ALTER TABLE inventory_rows ADD COLUMN from_slot VARCHAR(32)"))
            print("Added from_slot column to inventory_rows")
        else:
            print("from_slot column already exists, skipping")


if __name__ == "__main__":
    main()
