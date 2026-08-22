"""users.showcase_view_mode — grid/list preference for a Showcase

Revision ID: 7d348df1cc82
Revises: 9c715ccbdc02
Create Date: 2026-08-22

A separate column from ``deck_view_mode`` on purpose: art tiles suit a 100-card
deck and a dense list suits a 1,400-card showcase, so sharing one column would
make each surface silently change the other.

``server_default="grid"`` so existing rows are valid the moment the column
lands, and a raw INSERT that omits it still is.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7d348df1cc82"
down_revision: str | Sequence[str] | None = "9c715ccbdc02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "showcase_view_mode",
            sa.String(length=16),
            nullable=False,
            server_default="grid",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "showcase_view_mode")
