"""#179 — users.api_token (read-only /api/v1 bearer token)

Revision ID: c1a2b3d4e5f6
Revises: b3e7a1f42c9d
Create Date: 2026-08-03 00:00:00.000000

Additive, nullable, UNIQUE — the same token-as-toggle shape as
``decks.share_token`` (#143), ``users.wishlist_share_token`` (#146) and
``games.join_code`` (#165). Presence enables the read-only ``/api/v1`` surface
for that user, NULL disables it; revoke = set NULL, regenerate = new value.

Unlike the other three the token is NOT carried in a URL — it rides an
``Authorization: Bearer`` header and resolves *which user* is asking, since
every ``/api/v1`` route is owner-scoped. UNIQUE so a token maps to exactly one
user; NULL is exempt from UNIQUE on both Postgres and SQLite, so any number of
users may have the API switched off.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1a2b3d4e5f6"
down_revision: str | Sequence[str] | None = "b3e7a1f42c9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("api_token", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_users_api_token", "users", ["api_token"])


def downgrade() -> None:
    op.drop_constraint("uq_users_api_token", "users", type_="unique")
    op.drop_column("users", "api_token")
