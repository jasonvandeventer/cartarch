from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def main() -> None:
    """Add ``watchlist.target_price`` (v3.28.11 — Folio Watchlist refresh).

    Lets a user set a buy-target price on a watched card. When the
    card's current price (finish-aware for printing-specific watches,
    lowest-across-printings for name watches) drops to or below the
    target, the watchlist row gets a "target met" highlight.

    Per the v3.28.x cluster's schema-posture decision (#3), additive
    nullable columns are permitted under SQLite-until-v4. v3.28.11's
    is the cluster's fourth (after v3.28.6's two and v3.28.7's one) and
    its third migration overall (v3.28.6 + v3.28.7 + v3.28.11).

    Type: ``REAL`` (SQLite's native float). User-entered targets are
    simple numerics; we don't need byte-identical wire-format
    preservation here the way ``Card.price_usd*`` does (those are
    serialized from Scryfall as TEXT for the v3.25.0 bulk-cache
    round-trip). Comparisons against ``Card.price_usd*`` (TEXT) cast
    to float on the read side, mirroring the v3.28.9 finish-aware
    price-expr pattern.

    Idempotent: skips if the column already exists.
    """
    with engine.begin() as conn:
        if not column_exists(conn, "watchlist", "target_price"):
            conn.execute(text("ALTER TABLE watchlist ADD COLUMN target_price REAL"))
            print("Added watchlist.target_price (NULL)")
        else:
            print("watchlist.target_price already exists, skipping")


if __name__ == "__main__":
    main()
