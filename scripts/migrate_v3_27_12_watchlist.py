"""Watchlist schema (v3.27.12).

Adds one additive table for the user watchlist feature. A watchlist row
represents one card a user wants to track — either a specific Scryfall
printing (``card_id`` set, references ``cards.id``) or a printing-
agnostic name watch (``card_name`` set, stores the canonical Scryfall
card name copy). Exactly one of the two identity columns is populated
per row — the XOR shape. Enforced at the service layer in
``app/watchlist_service.py`` (SQLite ``CHECK`` constraints can be
added without a table rebuild but service-layer enforcement matches
the existing project convention for free-text validation, e.g.
``VALID_LOCATION_TYPES`` from v3.10.6 and ``CANONICAL_GAME_FORMATS``
from v3.27.2). Two partial-unique indexes ride alongside to prevent
duplicate watches per (user, identity) tuple.

Rationale for "both identity units" (user decision recorded at spec
time):

- ``card_id`` watches answer "I want this specific printing" — useful
  for collectors after a specific border / promo / set version.
- ``card_name`` watches answer "I want a Sol Ring" — printing-
  agnostic. Matches the most common user mental model.

Either column can be NULL but never both at once (service-layer XOR);
``card_id`` is nominally a FK to ``cards.id`` but documentary only
because the project doesn't enable ``PRAGMA foreign_keys`` (v3.27.5
established the application-layer-enforced FK pattern for this
codebase). Card-deletion is essentially never observed in production
(the ``cards`` table is shared and append-only in practice), so the
dangling-FK risk is theoretical; the user-deletion cascade in
``routes/admin.py`` does explicitly delete the user's watchlist rows
to keep that path tidy.

Idempotent: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT
EXISTS``. No backfill. The migration is safe to re-run; the registry-
level ``_is_applied`` gate in ``run_migrations.py`` provides the
outer idempotency layer as well.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    card_id INTEGER REFERENCES cards(id) ON DELETE CASCADE,
                    card_name TEXT,
                    added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    note TEXT
                )
                """
            )
        )
        # Note: the regular index on user_id is created by
        # ``Base.metadata.create_all()`` in ``init_db()`` via the
        # ``index=True`` annotation on ``WatchlistItem.user_id``. The
        # startup sequence in ``app/main.py:on_startup()`` runs
        # ``run_migrations()`` first (which creates this table + the
        # partial-unique indexes below), then ``init_db()`` (which adds
        # the regular index via create_all). Don't duplicate it here.
        #
        # Partial-unique indexes: at most one watch per (user, card_id) AND
        # at most one watch per (user, card_name). Partial predicate is what
        # makes the XOR shape work — a row with card_id IS NULL only
        # participates in the card_name uniqueness check, and vice versa.
        # SQLite supports partial indexes (>= 3.8.0; the project's SQLite
        # is far newer).
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_user_card_id "
                "ON watchlist(user_id, card_id) "
                "WHERE card_id IS NOT NULL"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_user_card_name "
                "ON watchlist(user_id, card_name) "
                "WHERE card_name IS NOT NULL"
            )
        )
        print(
            "v3.27.12 watchlist migration: table + 2 partial-unique indexes applied "
            "(regular user_id index lands via Base.metadata.create_all in init_db)"
        )


if __name__ == "__main__":
    main()
