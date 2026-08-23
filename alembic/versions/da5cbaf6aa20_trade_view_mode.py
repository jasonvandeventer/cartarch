"""users.trade_view_mode — grid/list preference for a trade's card lists

Revision ID: da5cbaf6aa20
Revises: 7d348df1cc82
Create Date: 2026-08-23

The third view preference, and a third column for the same reason the second
one was separate: a trade holds a handful of cards where a showcase holds
thousands, so wanting art on one and a dense list on the other is ordinary
rather than contradictory.

``server_default="grid"`` so existing rows are valid the moment it lands, and a
raw INSERT that omits it still is.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "da5cbaf6aa20"
down_revision: str | Sequence[str] | None = "7d348df1cc82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("trade_view_mode", sa.String(length=16), nullable=False, server_default="grid"),
    )


def downgrade() -> None:
    op.drop_column("users", "trade_view_mode")
