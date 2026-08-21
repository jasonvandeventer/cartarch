"""Trade counter-proposals: trade_revisions + trade_items.revision_id

Revision ID: 9c715ccbdc02
Revises: 815a4a99fe65
Create Date: 2026-08-21

A counter-proposal appends a REVISION rather than mutating the trade, so the
trade keeps its id, its status and its place in both inboxes, and the rejected
version is still on disk to diff against and to fall back to.

Every existing trade is backfilled onto revision 1 authored by its proposer, so
"the current items" has ONE definition from the first row onward — there is no
null-revision trade for the read path to special-case.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c715ccbdc02"
down_revision: str | Sequence[str] | None = "815a4a99fe65"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("author_name_at_revision", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trade_revisions_trade_id", "trade_revisions", ["trade_id"])
    op.create_index("ix_trade_revisions_author_user_id", "trade_revisions", ["author_user_id"])

    op.add_column("trade_items", sa.Column("revision_id", sa.Integer(), nullable=True))

    # Backfill: one revision per existing trade, authored by its proposer and
    # stamped with the trade's own created_at, then every existing item onto it.
    op.execute(
        """
        INSERT INTO trade_revisions
            (trade_id, author_user_id, author_name_at_revision, created_at)
        SELECT id, proposer_user_id, proposer_name_at_trade, created_at
        FROM trades
        """
    )
    op.execute(
        """
        UPDATE trade_items
        SET revision_id = r.id
        FROM trade_revisions r
        WHERE r.trade_id = trade_items.trade_id
        """
    )
    # An item with no revision can be neither shown nor hidden correctly, so the
    # column is NOT NULL — after the backfill, never before it.
    op.alter_column("trade_items", "revision_id", nullable=False)
    op.create_index("ix_trade_items_revision_id", "trade_items", ["revision_id"])
    op.create_foreign_key(
        "fk_trade_items_revision_id",
        "trade_items",
        "trade_revisions",
        ["revision_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # Items belonging to a counter revision are DELETED on the way down: the
    # column that says which version they belong to is going, and keeping them
    # would silently merge every version of a countered trade into one
    # oversized item list. Revision 1's items — the original proposal — stay.
    op.execute(
        """
        DELETE FROM trade_items
        WHERE revision_id IN (
            SELECT r.id FROM trade_revisions r
            WHERE r.id > (
                SELECT min(r2.id) FROM trade_revisions r2 WHERE r2.trade_id = r.trade_id
            )
        )
        """
    )
    op.drop_constraint("fk_trade_items_revision_id", "trade_items", type_="foreignkey")
    op.drop_index("ix_trade_items_revision_id", table_name="trade_items")
    op.drop_column("trade_items", "revision_id")
    op.drop_index("ix_trade_revisions_author_user_id", table_name="trade_revisions")
    op.drop_index("ix_trade_revisions_trade_id", table_name="trade_revisions")
    op.drop_table("trade_revisions")
