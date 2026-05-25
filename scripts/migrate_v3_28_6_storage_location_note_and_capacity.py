from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``storage_locations.note`` and ``storage_locations.capacity``
    (v3.28.6 — Folio Locations redesign + storage-IA de-leak).

    Two additive nullable columns surfaced by the v3.28.6 Folio
    Locations redesign. Per the v3.28.x cluster's schema-posture
    decision, additive nullable columns are permitted under the
    SQLite-until-v4 constraint.

    - ``note`` TEXT NULL — a short per-location description, rendered
      as a sub-line beneath the location name on the Locations table
      ("Commander staples · A-tier", "Trade fodder", etc.). Set via
      the create + edit forms; nullable so existing rows are
      unaffected.

    - ``capacity`` INTEGER NULL — the maximum number of cards the
      location is intended to hold. When set, the Locations table
      renders an inline capacity meter (used/capacity bar +
      percentage); when NULL, no meter. Optional by design — users
      populate over time. Empty-state by default.

    Both columns are independently nullable; a location can carry
    a note without a capacity, or a capacity without a note.

    Idempotent: each column is added only if missing.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "storage_locations", "note"):
            conn.execute(text("ALTER TABLE storage_locations ADD COLUMN note TEXT"))
            print("Added storage_locations.note (NULL)")
        else:
            print("storage_locations.note already exists, skipping")

        if not column_exists(conn, "storage_locations", "capacity"):
            conn.execute(text("ALTER TABLE storage_locations ADD COLUMN capacity INTEGER"))
            print("Added storage_locations.capacity (NULL)")
        else:
            print("storage_locations.capacity already exists, skipping")


if __name__ == "__main__":
    main()
