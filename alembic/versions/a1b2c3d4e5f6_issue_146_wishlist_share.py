"""#146 — users.wishlist_share_token + wishlist_shares (share your wishlist)

Revision ID: a1b2c3d4e5f6
Revises: f3a4b5c6d7e8
Create Date: 2026-07-18 00:00:00.000000

Two ways to share a wishlist (both read-only, names-only projection):
- ``users.wishlist_share_token`` (nullable, UNIQUE) — the public /w/{token} link,
  same token-as-toggle shape as ``decks.share_token`` (#143).
- ``wishlist_shares`` — one user's wishlist exposed to one playgroup, mirroring
  ``shares`` (Showcase→playgroup) with UNIQUE(user_id, playgroup_id).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("wishlist_share_token", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_users_wishlist_share_token", "users", ["wishlist_share_token"])

    op.create_table(
        "wishlist_shares",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("playgroup_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["playgroup_id"], ["playgroups.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "playgroup_id", name="uq_wishlist_shares_user_playgroup"),
    )
    op.create_index("ix_wishlist_shares_user_id", "wishlist_shares", ["user_id"])
    op.create_index("ix_wishlist_shares_playgroup_id", "wishlist_shares", ["playgroup_id"])


def downgrade() -> None:
    op.drop_index("ix_wishlist_shares_playgroup_id", table_name="wishlist_shares")
    op.drop_index("ix_wishlist_shares_user_id", table_name="wishlist_shares")
    op.drop_table("wishlist_shares")
    op.drop_constraint("uq_users_wishlist_share_token", "users", type_="unique")
    op.drop_column("users", "wishlist_share_token")
