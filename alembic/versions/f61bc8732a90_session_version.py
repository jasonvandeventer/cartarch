"""Revoke old browser sessions when a password changes.

Revision ID: f61bc8732a90
Revises: c49ef51a823d
"""

import sqlalchemy as sa

from alembic import op

revision = "f61bc8732a90"
down_revision = "c49ef51a823d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("users", "session_version")
