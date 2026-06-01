"""Add ``variant_groups`` + ``decks.variant_group_id`` — deck variant groups (v3.33.0).

A "variant group" links builds of the same deck (e.g. Atraxa v1 / v2) that
SHARE one physical copy of many cards. It is an **accounting-only overlay**:
one physical card still lives in exactly ONE deck's location (one-card-one-
location preserved — no shared pool, no row duplication, no multi-location
reads). The group only lets deck-import reconciliation treat a card held by a
sibling variant deck as "covered" (no new copy needed).

Additive: one new table + one nullable column + indexes, all ``IF NOT EXISTS``.
No table rebuild, no CHECK — safe under the SQLite-until-v4 posture. Idempotent
throughout. **NO BACKFILL** — legacy decks keep ``variant_group_id`` NULL
(standalone); the user opts decks into a group explicitly from the deck-edit
form.

**FK enforcement note**: ``REFERENCES variant_groups(id) ON DELETE SET NULL``
is declared for documentation + v4 Postgres forward-compat. SQLite ignores it
(``PRAGMA foreign_keys`` OFF project-wide); ``deck_service.delete_variant_group``
nulls referencing decks explicitly, and the admin user-deletion cascade removes
a user's groups after their decks. A dangling id is harmless — the sibling
lookup is user-scoped and simply finds no group.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS variant_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    name VARCHAR(128) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_variant_groups_user_id ON variant_groups(user_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_variant_groups_name ON variant_groups(name)")
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_variant_groups_user_name "
                "ON variant_groups(user_id, name)"
            )
        )

        if not column_exists(conn, "decks", "variant_group_id"):
            conn.execute(
                text(
                    "ALTER TABLE decks ADD COLUMN variant_group_id INTEGER "
                    "REFERENCES variant_groups(id) ON DELETE SET NULL"
                )
            )
            print("Added decks.variant_group_id (NULL = standalone deck)")
        else:
            print("decks.variant_group_id already exists, skipping")

        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_decks_variant_group_id " "ON decks(variant_group_id)"
            )
        )


if __name__ == "__main__":
    main()
