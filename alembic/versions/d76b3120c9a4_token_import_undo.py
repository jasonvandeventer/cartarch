"""Record the exact token inventory row for reversible acquisitions."""

import sqlalchemy as sa

from alembic import op

revision = "d76b3120c9a4"
down_revision = "b82ad74e19c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transaction_logs", sa.Column("token_inventory_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("transaction_logs", "token_inventory_id")
