"""sorter_rules — per-user configurable drawer-sorter rules (#104)

Ordered per-user rules (collection-search query -> target location). Mirrors the
deck_goals ordered-list pattern: position server_default 0, is_active server_default
true, user_id FK ondelete CASCADE (Postgres defense; SQLite FKs off). Plain CREATE
TABLE — SQLite + Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5a6b7c8d9e0"
down_revision: str | Sequence[str] | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sorter_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("query", sa.String(length=512), nullable=False),
        sa.Column("target_location_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["target_location_id"], ["storage_locations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sorter_rules", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sorter_rules_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("sorter_rules", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sorter_rules_user_id"))
    op.drop_table("sorter_rules")
