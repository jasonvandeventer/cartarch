"""Add ``decks.is_brew`` — Brew Mode deck flag (v3.37.0).

A "brew" is a deck built from cards the user may not own (for planning /
testing). When the flag is set, the add-card path flags an unowned add as a
proxy row so it never pollutes owned totals, and the deck detail surfaces an
owned/missing buy-list. See ``brew-mode-design-2026-06-10.md`` in the vault.

Additive single column, ``NOT NULL DEFAULT 0`` (every existing deck is a
normal deck) — safe under the SQLite-until-v4 "additive / no-rebuild" posture:
no table rebuild, no CHECK, no constraint change. Idempotent: skips ADD COLUMN
when the column already exists, and the registry's ``_is_applied`` gate in
``run_migrations.py`` provides the outer idempotency layer.

**Boolean handling (the v7/v8 blueprint lesson):** the column is declared
BOOLEAN and accessed ONLY through the ORM (``Deck.is_brew``). There is zero raw
SQL against it, so pgloader's default BOOLEAN→boolean mapping is correct at the
v4 cutover with no cast-file entry. SQLite stores the ``DEFAULT 0`` as integer
0/1; the ORM round-trips it as a Python bool.

**No backfill** — the constant default already sets every existing deck to a
normal (non-brew) deck.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    with engine.begin() as conn:
        if not column_exists(conn, "decks", "is_brew"):
            conn.execute(text("ALTER TABLE decks ADD COLUMN is_brew BOOLEAN NOT NULL DEFAULT 0"))
            print("Added decks.is_brew (0 = normal deck, 1 = brew)")
        else:
            print("decks.is_brew already exists, skipping")


if __name__ == "__main__":
    main()
