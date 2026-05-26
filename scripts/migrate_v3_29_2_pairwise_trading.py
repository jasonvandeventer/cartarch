"""Pairwise trading schema (v3.29.2).

Third and final release of the v3.29.x social-features minor. Adds two
additive tables that implement the recording-only pairwise-trading model
settled in the v3.29.2 spec's decision register.

- ``trades`` — one pairwise card-trade record between two playgroup
  co-members. One non-terminal status (``proposed``) and four terminal
  (``accepted``, ``declined``, ``cancelled``, ``abandoned``). Recording-
  only — the app never moves InventoryRow when a trade becomes
  ``accepted``; the row records the agreement and the parties resolve
  the physical exchange manually via existing Move / Adjust-quantity
  affordances (decisions B1 + B2). ``proposer_user_id`` /
  ``recipient_user_id`` / ``playgroup_id`` are all nullable at the DB
  level so the admin-cascade and playgroup-delete cleanup can SET-NULL
  on terminal trades (preserving history via the *_name_at_trade
  snapshots) — at app level they're required at proposal time.

- ``trade_items`` — one line item per side. ``side`` is one of
  ``offered`` / ``requested`` (service-layer canonical enum
  ``CANONICAL_TRADE_ITEM_SIDES`` in ``app/trade_service.py``; no DB
  CHECK, matching the SQLite-until-v4 posture). The hybrid identity
  reference per decision A4: live FKs (``inventory_row_id``,
  ``card_id``, optional ``showcase_item_id``) for navigation during
  negotiation, plus five ``*_at_trade`` snapshot columns written on
  every terminal transition for the durable historical record.
  ``showcase_item_id`` is the C1 nullable link to the v3.29.1
  ShowcaseItem the requested item came from; app-layer enforces it
  for ``side='requested'`` (decision C2) but the column itself is
  nullable so ``side='offered'`` rows can leave it NULL.

**Idempotent** — every ``CREATE TABLE`` / ``CREATE INDEX`` uses
``IF NOT EXISTS``; the registry's ``_is_applied`` gate in
``run_migrations.py`` provides the outer idempotency layer as well.

**No backfill** — there are no pre-existing trades to migrate. Trade
is a fresh feature surface for v3.29.2.

**No CHECK constraints, no existing-table alteration** — additive
tables only, per the SQLite-until-v4 posture. The string-valued
``status`` and ``side`` columns are gated at the service layer
(``CANONICAL_TRADE_STATUSES`` / ``CANONICAL_TRADE_ITEM_SIDES`` in
``app/trade_service.py``).

**No watchlist change** — want-list / wishlist integration is
deferred (decision C3).
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        # ── trades ──────────────────────────────────────────────
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposer_user_id INTEGER REFERENCES users(id),
                    recipient_user_id INTEGER REFERENCES users(id),
                    playgroup_id INTEGER REFERENCES playgroups(id),
                    status VARCHAR(32) NOT NULL DEFAULT 'proposed',
                    proposer_note TEXT,
                    recipient_note TEXT,
                    proposer_name_at_trade TEXT,
                    recipient_name_at_trade TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trades_proposer_user_id ON trades(proposer_user_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trades_recipient_user_id "
                "ON trades(recipient_user_id)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_trades_playgroup_id ON trades(playgroup_id)")
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(status)"))

        # ── trade_items ─────────────────────────────────────────
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS trade_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER NOT NULL REFERENCES trades(id),
                    side VARCHAR(16) NOT NULL,
                    inventory_row_id INTEGER REFERENCES inventory_rows(id),
                    card_id INTEGER REFERENCES cards(id),
                    showcase_item_id INTEGER REFERENCES showcase_items(id),
                    finish VARCHAR(32),
                    quantity INTEGER NOT NULL DEFAULT 1,
                    card_name_at_trade TEXT,
                    card_set_code_at_trade VARCHAR(32),
                    card_collector_number_at_trade VARCHAR(32),
                    finish_at_trade VARCHAR(32),
                    quantity_at_trade INTEGER
                )
                """
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_trade_items_trade_id ON trade_items(trade_id)")
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_trade_items_side ON trade_items(side)"))
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trade_items_inventory_row_id "
                "ON trade_items(inventory_row_id)"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_trade_items_card_id ON trade_items(card_id)")
        )
        # Composite for the per-trade-side render query (each side of a
        # trade is fetched independently for rendering).
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_trade_items_trade_id_side "
                "ON trade_items(trade_id, side)"
            )
        )

        print(
            "v3.29.2 pairwise trading migration: 2 tables + 7 indexes applied "
            "(trades + trade_items; composite (trade_id, side) for per-side render)"
        )


if __name__ == "__main__":
    main()
