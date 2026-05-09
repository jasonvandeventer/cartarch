"""Token cataloging schema.

Adds two tables for the lightweight token-inventory feature:

  - token_inventory: per-user physical token rows (Pest x12, Treasure x30,
    Spirit x4 in their token box). Separate from the existing card
    InventoryRow model so resort_collection / drawer-sorter logic doesn't
    touch them. Tokens can optionally link to a Scryfall print via
    scryfall_id but manual entry is the primary path.

  - deck_token_requirements: per-deck "this deck needs N of token X" rows.
    May reference a token_inventory row via token_inventory_id (when the
    user has the exact print they want) or just by token_name when the
    requirement is loose.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS token_inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name VARCHAR(255) NOT NULL,
                    type_line VARCHAR(255),
                    subtype VARCHAR(64),
                    quantity INTEGER NOT NULL DEFAULT 1,
                    set_code VARCHAR(32),
                    collector_number VARCHAR(32),
                    scryfall_id VARCHAR(64),
                    image_url TEXT,
                    is_double_sided BOOLEAN NOT NULL DEFAULT 0,
                    back_name VARCHAR(255),
                    back_image_url TEXT,
                    storage_location_id INTEGER REFERENCES storage_locations(id) ON DELETE SET NULL,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_token_inventory_user ON token_inventory(user_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_token_inventory_name ON token_inventory(name)")
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_token_inventory_storage "
                "ON token_inventory(storage_location_id)"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS deck_token_requirements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                    token_inventory_id INTEGER REFERENCES token_inventory(id) ON DELETE SET NULL,
                    token_name VARCHAR(255) NOT NULL,
                    quantity_needed INTEGER NOT NULL DEFAULT 1,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_deck_token_req_deck "
                "ON deck_token_requirements(deck_id)"
            )
        )

    print("Created token_inventory and deck_token_requirements tables")


if __name__ == "__main__":
    main()
