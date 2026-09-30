"""User-owned Collection filter, sort, and layout bookmarks.

Revision ID: b82ad74e19c3
Revises: f61bc8732a90
"""

import sqlalchemy as sa

from alembic import op

revision = "b82ad74e19c3"
down_revision = "f61bc8732a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_collection_views",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("query_string", sa.Text(), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_saved_collection_view_name"),
    )


def downgrade() -> None:
    op.drop_table("saved_collection_views")
