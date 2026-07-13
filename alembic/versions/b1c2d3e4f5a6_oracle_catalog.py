"""oracle_catalog — Momir Sim #109 full-oracle creature source

One row per oracle_id (per card NAME). Replaces the collection-bounded ``cards``
table as the Momir creature pool. Populated by app.jobs.oracle_ingest. Plain
CREATE TABLE — applies on SQLite AND Postgres. See models.OracleCatalog.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = "c4f9a1b2d3e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oracle_catalog",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("oracle_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mana_cost", sa.String(length=128), nullable=True),
        sa.Column("cmc", sa.Float(), nullable=True),
        sa.Column("type_line", sa.Text(), nullable=True),
        sa.Column("oracle_text", sa.Text(), nullable=True),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("power", sa.String(length=16), nullable=True),
        sa.Column("toughness", sa.String(length=16), nullable=True),
        sa.Column("colors", sa.String(length=64), nullable=True),
        sa.Column("color_identity", sa.String(length=64), nullable=True),
        sa.Column("layout", sa.String(length=64), nullable=True),
        sa.Column("scryfall_id", sa.String(length=64), nullable=True),
        sa.Column("is_momir_legal", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("oracle_catalog", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_oracle_catalog_oracle_id"), ["oracle_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_oracle_catalog_name"), ["name"], unique=False)
        batch_op.create_index(batch_op.f("ix_oracle_catalog_cmc"), ["cmc"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_oracle_catalog_is_momir_legal"), ["is_momir_legal"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("oracle_catalog", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oracle_catalog_is_momir_legal"))
        batch_op.drop_index(batch_op.f("ix_oracle_catalog_cmc"))
        batch_op.drop_index(batch_op.f("ix_oracle_catalog_name"))
        batch_op.drop_index(batch_op.f("ix_oracle_catalog_oracle_id"))
    op.drop_table("oracle_catalog")
