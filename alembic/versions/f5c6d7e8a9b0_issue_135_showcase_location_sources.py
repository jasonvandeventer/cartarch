"""#135 — showcase_location_sources (live-mirrored showcase membership)

Revision ID: f5c6d7e8a9b0
Revises: e4b5c6d7a8f9
Create Date: 2026-08-04 00:00:00.000000

A showcase mirrors zero-or-more StorageLocations live. Curated-vs-mirrored is a
STRUCTURAL distinction (different tables) rather than a flag every mutation site
must remember to set — the locked 2026-06-14 Cluster C decision, which removes
the "a ShowcaseItem carries no provenance" root cause by construction.

Additive only. **Existing ShowcaseItems are all treated as CURATED on cutover**:
they carry no provenance, so a snapshot cannot be told from a hand-pick, and
guessing would silently convert deliberate picks into a live mirror. They render
exactly as before; a user who wants live behaviour adds the location as a source.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5c6d7e8a9b0"
down_revision: str | Sequence[str] | None = "e4b5c6d7a8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "showcase_location_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("showcase_id", sa.Integer(), nullable=False),
        sa.Column("storage_location_id", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["showcase_id"], ["showcases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["storage_location_id"], ["storage_locations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "showcase_id", "storage_location_id", name="uq_showcase_location_sources"
        ),
    )
    op.create_index(
        "ix_showcase_location_sources_showcase_id",
        "showcase_location_sources",
        ["showcase_id"],
    )
    op.create_index(
        "ix_showcase_location_sources_storage_location_id",
        "showcase_location_sources",
        ["storage_location_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_showcase_location_sources_storage_location_id",
        table_name="showcase_location_sources",
    )
    op.drop_index(
        "ix_showcase_location_sources_showcase_id", table_name="showcase_location_sources"
    )
    op.drop_table("showcase_location_sources")
