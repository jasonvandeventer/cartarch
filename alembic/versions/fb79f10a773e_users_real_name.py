"""users.real_name — the person's actual name, for people who play together

Requested 2026-08-14: show real names in the playgroup, and default a game
seat's name to them.

**A SEPARATE COLUMN, not a repurposing of display_name.** display_name is the
pseudonymous handle, and it is what the ANONYMOUS wishlist page (/w/{token})
renders — the documented PII guard there is "display_name ONLY, never
username". Writing real names into display_name would silently turn a public
pseudonymous surface into a first-name one for anyone holding a share link.
real_name is member-facing only; see User.player_label.

Nullable with no default and no backfill: a blank real_name simply falls back
to the handle, so every existing row keeps rendering exactly as it does today.

Revision ID: fb79f10a773e
Revises: f19d7064b18f
"""

import sqlalchemy as sa

from alembic import op

revision = "fb79f10a773e"
down_revision = "f19d7064b18f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("real_name", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "real_name")
