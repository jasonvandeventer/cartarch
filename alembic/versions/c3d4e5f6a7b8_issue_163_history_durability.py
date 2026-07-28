"""#163 — game history durability: seat FKs RESTRICT, deck retire, commander set, variants

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-28 00:00:00.000000

Deleting a deck used to null ``game_seats.deck_id`` on EVERY seat that deck ever
occupied, across every game — silently erasing its history. The same rule on
``user_id`` did it for players. Both FKs become RESTRICT here; the application-level
null-outs that ran BEFORE the DELETE (and made the FK decorative) are removed in the
same change, in ``deck_service.delete_deck`` and ``routes/admin.delete_user``.

Also lands the identity this enables:

* ``decks.retired_at`` — soft delete, since the hard one is now refused. The
  ``uq_decks_user_name`` unique constraint is rebuilt as a PARTIAL index
  (``WHERE retired_at IS NULL``) so a retired deck does not squat on its name.
* ``deck_commanders`` — the commander anchor as an order-independent SET of card
  ids, NOT a column plus a partner slot. Multi-commander is the general case
  (5 of 39 commander-carrying decks), arity is not fixed by the rules, and a
  two-column layout would relocate the existing order-instability rather than
  remove it. Backfilled from ``inventory_rows`` where ``role='commander'``, which
  is naturally multi-row. NO cross-deck uniqueness: whether two decks sharing a
  commander set are one lineage is an open owner decision.
* ``decks.contents_tracked`` — column only, read by nothing (that is #164).
* ``game_variants`` — variants COMPOSE (Planechase + Momir is legitimate), so a
  join table rather than an enum column. Backfilled from ``game_events``
  (``planechase_enable`` / ``archenemy_enable``) and from ``games.momir_physical``.

Data-preserving: no seat loses its ``deck_id`` or ``user_id``. Verified on a
prod-shaped PG18 copy — 57 seats with a deck and 99 with a user before and after,
with identical SUM(deck_id)/SUM(user_id) checksums (stronger than counts: it rules
out a swapped reference, not merely a matching total).

**Engine note.** The SQLite branches below are written for correctness but are NOT
reachable today: the Alembic chain cannot run on SQLite at all — revision
``a1d7e4f02b93`` issues ``ALTER COLUMN ... TYPE BOOLEAN``, which SQLite does not
support — and per ``CLAUDE.md`` Alembic owns the *Postgres* schema while SQLite dev
builds from ``Base.metadata.create_all``. The SQLite schema this migration mirrors
IS exercised, by the full test suite through ``create_all``, including the partial
unique index (``test_the_compound_uniqueness_is_scoped_to_live_decks``). Only the
migration path is PG-only in practice.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ── decks: retired_at + contents_tracked ────────────────────────────────
    op.add_column("decks", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "decks",
        sa.Column("contents_tracked", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # ── decks: name uniqueness becomes PARTIAL (live decks only) ────────────
    # Without this a retired deck holds its name forever and the user cannot reuse
    # a name they just "deleted" — a visible regression from an invisible change.
    if _is_pg():
        op.drop_constraint("uq_decks_user_name", "decks", type_="unique")
        op.create_index(
            "uq_decks_user_name",
            "decks",
            ["user_id", "name"],
            unique=True,
            postgresql_where=sa.text("retired_at IS NULL"),
        )
    else:
        # SQLite: batch_alter_table rebuilds the table to drop the constraint.
        with op.batch_alter_table("decks") as batch:
            batch.drop_constraint("uq_decks_user_name", type_="unique")
        op.create_index(
            "uq_decks_user_name",
            "decks",
            ["user_id", "name"],
            unique=True,
            sqlite_where=sa.text("retired_at IS NULL"),
        )

    # ── deck_commanders ─────────────────────────────────────────────────────
    op.create_table(
        "deck_commanders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "deck_id",
            sa.Integer(),
            sa.ForeignKey("decks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "card_id",
            sa.Integer(),
            sa.ForeignKey("cards.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("deck_id", "card_id", name="uq_deck_commanders_deck_card"),
    )
    op.create_index("ix_deck_commanders_deck_id", "deck_commanders", ["deck_id"])
    op.create_index("ix_deck_commanders_card_id", "deck_commanders", ["card_id"])

    # Backfill the commander SET from the deck's own inventory rows. Naturally
    # multi-row — that is how the 5 multi-commander decks are found. DISTINCT
    # guards against a deck holding two rows of the same commander card.
    # Decks with no commander row simply produce nothing, which is legal.
    op.execute(
        sa.text(
            """
            INSERT INTO deck_commanders (deck_id, card_id)
            SELECT DISTINCT d.id, ir.card_id
            FROM decks d
            JOIN storage_locations sl ON sl.id = d.storage_location_id
            JOIN inventory_rows ir
              ON ir.storage_location_id = sl.id AND ir.role = 'commander'
            """
        )
    )

    # ── game_variants ───────────────────────────────────────────────────────
    op.create_table(
        "game_variants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "game_id",
            sa.Integer(),
            sa.ForeignKey("games.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("variant", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("game_id", "variant", name="uq_game_variants_game_variant"),
    )
    op.create_index("ix_game_variants_game_id", "game_variants", ["game_id"])
    op.create_index("ix_game_variants_variant", "game_variants", ["variant"])

    # Backfill from the event stream (the only place variant was recorded) and
    # from the momir_physical boolean this supersedes.
    for action, variant in (("planechase_enable", "planechase"), ("archenemy_enable", "archenemy")):
        op.execute(
            sa.text(
                """
                INSERT INTO game_variants (game_id, variant)
                SELECT DISTINCT ge.game_id, :variant
                FROM game_events ge
                WHERE ge.action_type = :action
                """
            ).bindparams(action=action, variant=variant)
        )
    op.execute(
        sa.text(
            """
            INSERT INTO game_variants (game_id, variant)
            SELECT g.id, 'momir' FROM games g WHERE g.momir_physical = true
            """
        )
    )

    # ── the point of the whole migration: seat FKs off SET NULL ─────────────
    # Postgres only. SQLite runs with PRAGMA foreign_keys OFF project-wide, so its
    # FK clauses are inert either way and a table rebuild would buy nothing while
    # risking the data. The ORM model carries RESTRICT on both engines.
    if _is_pg():
        op.drop_constraint("game_seats_deck_id_fkey", "game_seats", type_="foreignkey")
        op.create_foreign_key(
            "game_seats_deck_id_fkey",
            "game_seats",
            "decks",
            ["deck_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.drop_constraint("game_seats_user_id_fkey", "game_seats", type_="foreignkey")
        op.create_foreign_key(
            "game_seats_user_id_fkey",
            "game_seats",
            "users",
            ["user_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    if _is_pg():
        op.drop_constraint("game_seats_user_id_fkey", "game_seats", type_="foreignkey")
        op.create_foreign_key(
            "game_seats_user_id_fkey",
            "game_seats",
            "users",
            ["user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.drop_constraint("game_seats_deck_id_fkey", "game_seats", type_="foreignkey")
        op.create_foreign_key(
            "game_seats_deck_id_fkey",
            "game_seats",
            "decks",
            ["deck_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.drop_index("ix_game_variants_variant", table_name="game_variants")
    op.drop_index("ix_game_variants_game_id", table_name="game_variants")
    op.drop_table("game_variants")

    op.drop_index("ix_deck_commanders_card_id", table_name="deck_commanders")
    op.drop_index("ix_deck_commanders_deck_id", table_name="deck_commanders")
    op.drop_table("deck_commanders")

    # Restore the plain (non-partial) unique constraint. A retired deck sharing a
    # live deck's name would block this — but retired_at is dropped below in the
    # same downgrade, and nothing can have retired a deck under the old code.
    op.drop_index("uq_decks_user_name", table_name="decks")
    if _is_pg():
        op.create_unique_constraint("uq_decks_user_name", "decks", ["user_id", "name"])
    else:
        with op.batch_alter_table("decks") as batch:
            batch.create_unique_constraint("uq_decks_user_name", ["user_id", "name"])

    op.drop_column("decks", "contents_tracked")
    op.drop_column("decks", "retired_at")
