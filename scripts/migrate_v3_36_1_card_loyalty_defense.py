from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``loyalty`` and ``defense`` (both TEXT NULL) to BOTH the
    request-path ``cards`` table (the ``Card`` ORM model) and the
    daemon-populated ``scryfall_cards`` bulk-cache seam (v3.36.1 — Step 3
    of the goldfish planeswalker-loyalty cascade).

    Two faithful nullable string attributes captured from Scryfall:

    * ``loyalty`` — printed starting loyalty on planeswalker cards/faces
      (a STRING; can be non-numeric, e.g. ``"X"``; absent on non-PW;
      carried on the PW face for transform/MDFC planeswalkers).
    * ``defense`` — the Battle analogue of loyalty (a STRING on Battle
      cards / the front face of Siege/DFC battles; absent elsewhere).

    Stored raw, exactly as the other string attrs are — NO parsing or
    int-coercion here (that is Step 4's job). They populate via the
    extended ``_normalize_card_payload`` on every write path; the daemon
    upsert SQL is built dynamically from ``_CACHE_COLUMNS`` so the new
    columns land in writes automatically once the seam includes them.

    **Why BOTH tables (unlike v3.30.11 ``produced_tokens``):**
    ``produced_tokens`` is a ``scryfall_cards``-only cache field stripped
    by ``card_constructor_kwargs`` before ``Card(**payload)``. ``loyalty``
    / ``defense`` are real ``Card`` ORM columns — the goldfish route reads
    them off ``Card`` and the cache-miss ``Card(**...)`` paths construct
    them — so they belong on ``cards`` too. Mirrors the v3.23.8
    ``card_traits`` precedent (full_art/frame_effects/set_type/layout),
    which likewise live on both tables.

    **No consumer reads these fields in v3.36.1.** They are dormant
    payload data; Step 4 auto-initialises planeswalker loyalty counters
    (and, for Battles, defense) in the goldfish UI off this cached data.
    The gap between this release and Step 4 lets the ``_bulk_data_loop``
    daemon backfill the columns across ``scryfall_cards`` first — existing
    rows read NULL until the next full Scryfall export cycle (~daily), and
    Step 4 treats NULL / non-numeric values gracefully (no auto-init; the
    manual counter still works).

    Idempotent: each ``ALTER TABLE ... ADD COLUMN`` is ``pragma_table_info``
    guarded so re-running is a no-op (matches v3.30.11 / v3.28.6 / v3.28.7).
    The registry in ``scripts/run_migrations.py`` adds a registry-level
    guard on top. Forward-only — no down migration. Additive, nullable, no
    rebuild, no CHECK (SQLite-until-v4).
    """
    with engine.begin() as conn:
        for table in ("cards", "scryfall_cards"):
            for column in ("loyalty", "defense"):
                if not column_exists(conn, table, column):
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} TEXT"))
                    print(f"Added {table}.{column} (TEXT NULL)")
                else:
                    print(f"{table}.{column} already exists, skipping")


if __name__ == "__main__":
    main()
