"""issue #73 — audit_sessions.snapshot_detail (reconciliation change-list baseline)

Phase 3 adds a nullable JSON baseline of the location's inventory at audit start
([{row_id, qty, label}]) so the reconciliation snapshot-conflict check can
itemize WHAT changed ("Sol Ring (FDN) quantity changed from 1 to 2") instead of
only reporting that the hash differs. Nullable — audits opened before this
column exists fall back to a hash-only "inventory changed" message. Plain
ADD COLUMN — applies on SQLite AND Postgres.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b2971e2820"
down_revision: str | Sequence[str] | None = "2d3c4ea5e10c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("audit_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("snapshot_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("audit_sessions", schema=None) as batch_op:
        batch_op.drop_column("snapshot_detail")
