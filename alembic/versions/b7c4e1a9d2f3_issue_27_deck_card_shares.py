"""issue #27 — deck_card_shares (variant-group deck sharing)

Revision ID: b7c4e1a9d2f3
Revises: 489afd0e62f9
Create Date: 2026-06-24 00:00:00.000000

Adds the ``deck_card_shares`` join table (membership ≠ location). A physical
``inventory_rows`` row stays in its source deck's storage location; this table
records that it is ALSO a member of a sibling build's decklist within the same
variant group — a reference, never a copy.

All four FKs are ON DELETE CASCADE NOT NULL (the share is meaningless without
its row / its decks / its group). ``UNIQUE(inventory_row_id, target_deck_id)``
makes a row shared to a deck at most once. ``created_at`` is a NAIVE TIMESTAMP
(``sa.DateTime``) — matching the baseline's naive-timestamp convention; do NOT
add ``timezone=True``.

POST-AUTOGENERATE NOTE (gate #4 pattern): no boolean columns here, so the
``sa.false()/sa.true()`` server_default fixup does not apply. Regenerating this
revision should reproduce it cleanly.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c4e1a9d2f3"
down_revision: str | Sequence[str] | None = "489afd0e62f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deck_card_shares",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inventory_row_id", sa.Integer(), nullable=False),
        sa.Column("source_deck_id", sa.Integer(), nullable=False),
        sa.Column("target_deck_id", sa.Integer(), nullable=False),
        sa.Column("variant_group_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["inventory_row_id"], ["inventory_rows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_deck_id"], ["decks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_group_id"], ["variant_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "inventory_row_id", "target_deck_id", name="uq_deck_card_shares_row_target"
        ),
    )
    with op.batch_alter_table("deck_card_shares", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_deck_card_shares_inventory_row_id"),
            ["inventory_row_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_deck_card_shares_source_deck_id"), ["source_deck_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_deck_card_shares_target_deck_id"), ["target_deck_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_deck_card_shares_variant_group_id"),
            ["variant_group_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("deck_card_shares", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_deck_card_shares_variant_group_id"))
        batch_op.drop_index(batch_op.f("ix_deck_card_shares_target_deck_id"))
        batch_op.drop_index(batch_op.f("ix_deck_card_shares_source_deck_id"))
        batch_op.drop_index(batch_op.f("ix_deck_card_shares_inventory_row_id"))
    op.drop_table("deck_card_shares")
