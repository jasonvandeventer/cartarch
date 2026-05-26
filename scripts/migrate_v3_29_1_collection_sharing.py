"""Collection sharing schema (v3.29.1).

Second release of the v3.29.x social-features minor. Adds three additive
tables that implement the curated-list collection-sharing model settled
in the v3.29.1 spec's decision register.

- ``showcases`` — a user's curated subset of their inventory prepared for
  sharing. ``UNIQUE(user_id)`` for v3.29.1 (decision A5 — one Showcase
  per user, lazily created). The constraint is the only thing a future
  multi-showcase release has to drop; the model is otherwise designed as
  a general curated list (decision E1). A Showcase is NOT a
  ``StorageLocation type="binder"`` (decision E3 — names avoided to
  prevent semantic collision with the physical-container concept). A
  Showcase is a logical curated list — cards can be in it without being
  physically moved.

- ``showcase_items`` — one curated card per Showcase, keyed on a specific
  ``InventoryRow`` (decision A3 — the Showcase references inventory, never
  forks it; ``InventoryRow`` remains the single source of truth). The
  ``quantity_offered`` column stores the sharer's intent (decision A4);
  the displayed available is computed at render time as
  ``min(quantity_offered, InventoryRow.quantity)`` — no stored quantity to
  drift when the sharer sells. ``UNIQUE(showcase_id, inventory_row_id)``
  keeps the curated set a true set (an item can't be added twice). The
  sharer-private ``notes`` column is the **one** field on this table that
  must NEVER appear in the sanitized share projection — privacy by
  construction (§8 of the spec).

- ``shares`` — one act of exposing a Showcase to one playgroup, read-only
  (decision B3 — one playgroup per share). The Showcase that this points
  at is untouched on revoke; only the Share row is deleted (decision B2 —
  hard delete, no soft-revoke). ``user_id`` is denormalized for "my
  shares" queries and the admin user-deletion cascade.
  ``UNIQUE(showcase_id, playgroup_id)`` prevents duplicate shares of the
  same Showcase to the same playgroup; the v3.29.0 ``join_by_code``
  pattern of catching IntegrityError + returning the existing row is
  mirrored at the service layer.

**Idempotent** — every ``CREATE TABLE`` / ``CREATE INDEX`` uses
``IF NOT EXISTS``; the registry's ``_is_applied`` gate in
``run_migrations.py`` provides the outer idempotency layer as well.

**No backfill** — there are no pre-existing Showcases or Shares to
migrate. Existing users start with no Showcase; one is lazily created on
first add-to-showcase action (the v3.29.0 ``join_by_code`` lazy-create
shape).

Per the project SQLite-until-v4 posture: additive tables only, no
existing-table alteration, no ``CHECK`` constraints. The XOR-style
service-layer enums (``CANONICAL_PLAYGROUP_ROLES`` etc.) do not apply
here — there are no string-valued enums in this schema.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        # ── showcases ───────────────────────────────────────────
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS showcases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    name VARCHAR(128) NOT NULL DEFAULT 'My Showcase',
                    description TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_showcases_user_id ON showcases(user_id)"))
        # One Showcase per user — decision A5. A future multi-showcase
        # release drops this constraint; everything else is already
        # general.
        conn.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_showcases_user ON showcases(user_id)")
        )

        # ── showcase_items ──────────────────────────────────────
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS showcase_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    showcase_id INTEGER NOT NULL REFERENCES showcases(id),
                    inventory_row_id INTEGER NOT NULL REFERENCES inventory_rows(id),
                    quantity_offered INTEGER NOT NULL DEFAULT 1,
                    notes TEXT,
                    added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_showcase_items_showcase_id "
                "ON showcase_items(showcase_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_showcase_items_inventory_row_id "
                "ON showcase_items(inventory_row_id)"
            )
        )
        # Unique (showcase_id, inventory_row_id) — keeps the curated set
        # a true set; service-layer ``add_showcase_item`` catches the
        # IntegrityError and treats duplicates as no-ops.
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_showcase_items_showcase_inv "
                "ON showcase_items(showcase_id, inventory_row_id)"
            )
        )

        # ── shares ──────────────────────────────────────────────
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS shares (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    showcase_id INTEGER NOT NULL REFERENCES showcases(id),
                    playgroup_id INTEGER NOT NULL REFERENCES playgroups(id),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_shares_user_id ON shares(user_id)"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_shares_showcase_id ON shares(showcase_id)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_shares_playgroup_id ON shares(playgroup_id)")
        )
        # Unique (showcase_id, playgroup_id) — prevents double-sharing
        # the same Showcase to the same playgroup. The race where two
        # ``create_share`` requests insert the same row concurrently is
        # caught by the service layer (IntegrityError → return existing).
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_shares_showcase_playgroup "
                "ON shares(showcase_id, playgroup_id)"
            )
        )

        print(
            "v3.29.1 collection sharing migration: 3 tables + 8 indexes applied "
            "(showcases + showcase_items + shares; unique user-showcase, "
            "unique pair indexes for the curated set + share uniqueness)"
        )


if __name__ == "__main__":
    main()
