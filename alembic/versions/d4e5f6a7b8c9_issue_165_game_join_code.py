"""#165 — games.join_code (seat claiming from a phone)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-28 00:00:00.000000

Additive, nullable. The code a logged-in member enters (or reaches via a QR link)
to claim ONE unclaimed seat, while the game is still ``created``.

**Distinct from ``games.client_token`` in KIND.** The table token grants control of
every seat and must never reach a phone; this only lets a member attach themselves
to one seat. Do not conflate them.

PARTIAL unique index, mirroring the v3.29.0 ``uq_playgroups_join_code``: codes are
unique among ENABLED ones (``WHERE join_code IS NOT NULL``); NULL means claiming is
off and repeats freely across games. Without the predicate, two games could share a
code and a claim would be ambiguous.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("games", sa.Column("join_code", sa.String(length=32), nullable=True))
    op.create_index(
        "uq_games_join_code",
        "games",
        ["join_code"],
        unique=True,
        postgresql_where=sa.text("join_code IS NOT NULL"),
        sqlite_where=sa.text("join_code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_games_join_code", table_name="games")
    op.drop_column("games", "join_code")
