"""Add back_set_code + back_collector_number columns to token_inventory.

For DFC token sets that Scryfall doesn't model with card_faces (TMH3 et al.,
where each face is stored as a separate single-sided record), the user enters
the back face's set + collector explicitly. Persisting them lets the edit
form re-display the lookup state and the user re-fetch back_name / back_image_url
on demand.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def main() -> None:
    with engine.begin() as conn:
        cols = _existing_columns(conn, "token_inventory")
        if "back_set_code" not in cols:
            conn.execute(text("ALTER TABLE token_inventory ADD COLUMN back_set_code VARCHAR(32)"))
        if "back_collector_number" not in cols:
            conn.execute(
                text("ALTER TABLE token_inventory ADD COLUMN back_collector_number VARCHAR(32)")
            )
    print("Added back_set_code and back_collector_number to token_inventory")


if __name__ == "__main__":
    main()
