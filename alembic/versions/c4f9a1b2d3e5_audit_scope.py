"""scoped audits — audit_sessions.scope + audit_log.scope

Optional set-code filter for a Physical Audit (issue #73 follow-up). ``scope`` is
a JSON string ``{"set_codes": ["LTR", "MH3"]}`` on the session, copied onto the
log at completion; NULL = a full-location audit (today's behavior). Plain ADD
COLUMN, nullable — applies on SQLite AND Postgres and is backward-compatible with
audits started before the column existed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f9a1b2d3e5"
down_revision: str | Sequence[str] | None = "a7d43e38ef6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("audit_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scope", sa.Text(), nullable=True))
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scope", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_column("scope")
    with op.batch_alter_table("audit_sessions", schema=None) as batch_op:
        batch_op.drop_column("scope")
