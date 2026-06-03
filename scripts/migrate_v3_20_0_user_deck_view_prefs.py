"""Add per-user deck view preferences.

`users.deck_view_mode` — 'grid' (default, existing image-grid view) or 'list'
(new Moxfield-style text rows grouped by a user-selectable axis).

`users.deck_group_by` — which axis the list view groups cards on:
'type' (default), 'cmc', 'color', 'role', or 'subtype'.

Both columns ship defaulted so existing users see the same image-grid behavior
they have today; opting into list mode is explicit.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    with engine.begin() as conn:
        if not column_exists(conn, "users", "deck_view_mode"):
            conn.execute(
                text(
                    "ALTER TABLE users "
                    "ADD COLUMN deck_view_mode VARCHAR(16) DEFAULT 'grid' NOT NULL"
                )
            )
            print("Added deck_view_mode column to users (default 'grid')")
        else:
            print("deck_view_mode column already exists, skipping")

        if not column_exists(conn, "users", "deck_group_by"):
            conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN deck_group_by VARCHAR(16) DEFAULT 'type' NOT NULL"
                )
            )
            print("Added deck_group_by column to users (default 'type')")
        else:
            print("deck_group_by column already exists, skipping")


if __name__ == "__main__":
    main()
