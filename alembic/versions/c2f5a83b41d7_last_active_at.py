"""last-active timestamp per user — users.last_active_at

Revision ID: c2f5a83b41d7
Revises: b7c4e1a9d2f3
Create Date: 2026-06-25 00:00:00.000000

Adds ``users.last_active_at`` (nullable plain ``DateTime``) — the last time a
user made an *authenticated request*, stamped from the auth dependency and
throttled to one write per 5 minutes per user. Distinct from the existing
``last_signed_in_at`` (set on POST /login); both are kept. No backfill — NULL
for existing rows until their next authenticated request, consistent with how
``last_signed_in_at`` was introduced (and explicitly NOT copied from it: the
two columns answer different questions).

POST-AUTOGENERATE NOTE: plain additive ``ADD COLUMN``, nullable, no default or
server_default — nothing to hand-fix, no boolean / ``now()`` discipline applies.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2f5a83b41d7"
down_revision: str | Sequence[str] | None = "b7c4e1a9d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_active_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("last_active_at")
