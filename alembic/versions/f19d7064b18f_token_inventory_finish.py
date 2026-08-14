"""token_inventory.finish — a token has a finish, same three values as a card

Requested 2026-08-14: regular cards have a normal/foil/etched selector and
tokens did not, so a foil token could only be recorded in the notes field.

Additive and low-risk by construction:

* ``server_default="normal"`` backfills every existing row in the ALTER, which
  is what allows the column to be NOT NULL immediately. It is kept on the
  column (not dropped after backfill) so a raw INSERT that omits finish — the
  migration scripts, a psql fixup — still lands a valid value.
* Finish is NOT part of any merge key for tokens. ``token_service.create_token``
  always INSERTs and never merges, so adding the column cannot re-group,
  split or double an existing row. (Contrast ``inventory_rows``, where finish
  IS part of the merge identity.)

Revision ID: f19d7064b18f
Revises: 3d9da7716f9d
"""

import sqlalchemy as sa

from alembic import op

revision = "f19d7064b18f"
down_revision = "3d9da7716f9d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "token_inventory",
        sa.Column("finish", sa.String(length=32), nullable=False, server_default="normal"),
    )


def downgrade() -> None:
    op.drop_column("token_inventory", "finish")
