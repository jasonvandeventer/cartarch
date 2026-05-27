from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``scryfall_cards.produced_tokens`` (v3.30.11 — data half of the
    two-release sequence that retires the request-path Scryfall call in
    ``fetch_deck_tokens``).

    One additive nullable column on the v3.25.0 bulk-cache table. Stores
    JSON-encoded list of token references parsed from Scryfall's
    ``all_parts`` field (filtered to ``component == "token"``), per-entry
    shape ``{name, type_line, scryfall_id}``. Populated by the
    ``_bulk_data_loop`` daemon via the extended ``_normalize_card_payload``
    (the upsert SQL is dynamically built from ``_CACHE_COLUMNS``, so the
    column lands in writes automatically once ``_CACHE_COLUMNS`` includes
    it).

    **Empty list → "[]" (NOT NULL).** The normalizer emits ``"[]"`` for a
    card with no tokens (no ``all_parts`` entries, or none with
    ``component == "token"``); NULL means "this row predates the v3.30.11
    daemon backfill". v3.30.12 consumers can rely on the distinction.

    **No consumer reads this field in v3.30.11.** ``fetch_deck_tokens`` /
    ``compute_deck_tokens`` / the deck-detail "Tokens" panel / the v3.30.10
    goldfish enrichment are all UNCHANGED. The window between v3.30.11
    shipping and v3.30.12 consumer flip is intentional — it lets the
    daemon backfill populate the column across ``scryfall_cards`` before
    anything reads it.

    Idempotent: ``pragma_table_info``-guarded so re-running this migration
    is a no-op at the in-file level (matches the v3.28.6 +
    v3.28.7 ALTER ADD COLUMN pattern). The registry in
    ``scripts/run_migrations.py`` adds a registry-level guard on top.
    Forward-only — no down migration. Old code reading ``scryfall_cards``
    ignores the new column harmlessly (additive, nullable).

    Per the v3.25.0 / v3.27.13 / v3.30.11 documentation, this is the
    ``scryfall_cards`` bulk-cache table populated by the daemon, NOT the
    smaller working ``cards`` table populated by request-path activity.
    The new column belongs on ``scryfall_cards`` because that is what
    v3.30.12 will read for token-relation data.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "scryfall_cards", "produced_tokens"):
            conn.execute(text("ALTER TABLE scryfall_cards ADD COLUMN produced_tokens TEXT"))
            print("Added scryfall_cards.produced_tokens (TEXT NULL)")
        else:
            print("scryfall_cards.produced_tokens already exists, skipping")


if __name__ == "__main__":
    main()
