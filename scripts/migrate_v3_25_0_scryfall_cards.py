from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    """Create the local Scryfall bulk-data cache tables (v3.25.0).

    ``scryfall_cards`` mirrors the exact 21-key shape returned by
    ``app.scryfall._normalize_card_payload`` so a cached row reconstructs a
    ``BulkFetchResult`` value byte-identical to the API path. All columns are
    nullable with no DEFAULT: the daemon inserts exactly the normalized value
    and the seam reads it back verbatim, preserving the None-vs-"" semantics
    the normalizer establishes (``colors`` is NULL for a colorless card while
    ``color_identity`` is ""; ``price_usd*`` are NULL or the raw "1.23"
    string). Prices are TEXT on purpose -- REAL would not round-trip the
    string form the API path returns.

    ``scryfall_bulk_meta`` is a tiny key/value store; the daemon uses
    ``key='default_cards_updated_at'`` to skip re-downloading when Scryfall's
    bulk file is unchanged (the updated_at-guarded skip).

    Idempotent: CREATE TABLE/INDEX IF NOT EXISTS. No VACUUM anywhere -- the
    5 Gi PVC cannot absorb a 2x transient file rewrite.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS scryfall_cards (
                    scryfall_id      TEXT PRIMARY KEY,
                    name             TEXT,
                    set_code         TEXT,
                    set_name         TEXT,
                    collector_number TEXT,
                    rarity           TEXT,
                    image_url        TEXT,
                    type_line        TEXT,
                    oracle_text      TEXT,
                    price_usd        TEXT,
                    price_usd_foil   TEXT,
                    price_usd_etched TEXT,
                    colors           TEXT,
                    color_identity   TEXT,
                    mana_cost        TEXT,
                    cmc              REAL,
                    legalities       TEXT,
                    full_art         INTEGER,
                    frame_effects    TEXT,
                    set_type         TEXT,
                    layout           TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_scryfall_cards_set_collector "
                "ON scryfall_cards (set_code, collector_number)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_scryfall_cards_name ON scryfall_cards (name)")
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS scryfall_bulk_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
        )
    print("scryfall_cards + scryfall_bulk_meta ready (idempotent; no VACUUM)")


if __name__ == "__main__":
    main()
