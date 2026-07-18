"""#143 — decks.share_token (public read-only deck share links)

Revision ID: f3a4b5c6d7e8
Revises: e1f2a3b4c5d6
Create Date: 2026-07-18 00:00:00.000000

Additive, nullable, UNIQUE. ``decks.share_token`` holds an unguessable
``secrets.token_urlsafe`` token; presence publishes the deck read-only at
``/d/{token}`` for anyone (no account), NULL = private. The token is the toggle
— generating publishes, clearing (revoke) invalidates the link. UNIQUE so a
token maps to exactly one deck (a NULL is exempt from UNIQUE on both Postgres
and SQLite, so every private deck coexists fine).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("decks", sa.Column("share_token", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_decks_share_token", "decks", ["share_token"])


def downgrade() -> None:
    op.drop_constraint("uq_decks_share_token", "decks", type_="unique")
    op.drop_column("decks", "share_token")
