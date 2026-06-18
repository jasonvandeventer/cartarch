"""SQLAlchemy Core ``Table`` definitions for the 7 raw-SQL-managed tables.

These tables exist in the database but are NOT mapped as ORM models in
``app/models.py`` — they were created by raw-SQL migration scripts
(``migrate_v3_15_0_bracket_v2_tables.py`` and
``migrate_v3_25_0_scryfall_cards.py``) and are read/written via raw SQL, not the
ORM. Because ``alembic revision --autogenerate`` only sees what is attached to
``Base.metadata``, these tables would be SILENTLY OMITTED from the Alembic
baseline (and worse, autogenerate would emit ``drop_table`` for them).

Binding them to ``Base.metadata`` here — as DDL-only Core Tables, with NO ORM
mappers — brings all 7 under Alembic management so the baseline creates them and
the Phase-C empty-diff gate validates their types. This is the highest-risk part
of the baseline: ``scryfall_cards`` (the cache-type-anchoring seam) and
``game_changer_cards`` (the ``WHERE active`` boolean landmine) both live here.

**Runtime-neutral:** this module is imported ONLY by ``alembic/env.py``, never by
the app. The running app's ``Base.metadata`` therefore stays exactly as before
(21 ORM tables); the 7 raw tables continue to be created by the legacy
``run_migrations()`` runner until that is retired (Phase D, not yet).

**Transcription rule (Gate #4):** every column below is transcribed
column-for-column from the PROD SNAPSHOT (``/tmp/v3.39.7-schema.sql``, i.e. what
prod actually has and what pgloader will copy), NOT from the migration scripts.
Per-column type policy, applied consciously:
  - ``game_changer_cards.active`` → native ``Boolean`` (raw SQL does ``WHERE
    active``; the v3.34.5 fix). The ``commander_bracket_rules`` flags are
    likewise ``BOOLEAN`` in the snapshot → ``Boolean`` (mirror).
  - ``scryfall_cards.full_art`` → stays ``Integer`` (the snapshot declares it
    INTEGER; raw cache SQL compares ``= 1``). Do NOT promote it to Boolean.
  - ``scryfall_cards`` prices → ``Text``; ``cmc`` → ``Float`` (REAL/double).
  - Everything else mirrors the snapshot's declared type exactly.

Integer PKs carry ``sqlite_autoincrement=True`` to match the snapshot's
``INTEGER PRIMARY KEY AUTOINCREMENT`` (a SQLite artifact; on Postgres the integer
PK becomes ``SERIAL`` regardless).
"""

from __future__ import annotations

from sqlalchemy import (
    TIMESTAMP as SA_TIMESTAMP,
)
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)

from app.db import Base

metadata = Base.metadata

# ---------------------------------------------------------------------------
# migrate_v3_15_0_bracket_v2_tables.py
# ---------------------------------------------------------------------------

card_tags = Table(
    "card_tags",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("card_id", Integer, ForeignKey("cards.id", ondelete="CASCADE"), nullable=False),
    Column("tag", String(64), nullable=False),
    Column("confidence", String(16), nullable=False, server_default=text("'medium'")),
    Column("source", String(32), nullable=False, server_default=text("'oracle_text_rule'")),
    Column("last_reviewed", SA_TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("card_id", "tag"),
    Index("ix_card_tags_card", "card_id"),
    Index("ix_card_tags_tag", "tag"),
    sqlite_autoincrement=True,
)

commander_bracket_rules = Table(
    "commander_bracket_rules",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("bracket", Integer, nullable=False),
    Column("name", String(64), nullable=False),
    Column("description", Text),
    Column("max_game_changers", Integer, nullable=False, server_default=text("0")),
    Column("allows_mass_land_denial", Boolean, nullable=False, server_default=false()),
    Column("allows_extra_turn_chains", Boolean, nullable=False, server_default=false()),
    Column("allows_two_card_combos", Boolean, nullable=False, server_default=false()),
    Column("allows_combo_as_primary", Boolean, nullable=False, server_default=false()),
    Column("competitive", Boolean, nullable=False, server_default=false()),
    Column("rules_version", String(32), nullable=False),
    Column("effective_date", Date),
    UniqueConstraint("bracket", "rules_version"),
    sqlite_autoincrement=True,
)

deck_bracket_estimates = Table(
    "deck_bracket_estimates",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("deck_id", Integer, ForeignKey("decks.id", ondelete="CASCADE"), nullable=False),
    Column("estimated_bracket", Integer, nullable=False),
    Column("mechanics_bracket", Integer, nullable=False),
    Column("intent_bracket", Integer),
    Column("final_bracket", Integer, nullable=False),
    Column("score", Float),
    Column("generated_at", SA_TIMESTAMP, server_default=text("CURRENT_TIMESTAMP")),
    Column("rules_version", String(32), nullable=False),
    # Appended later via ALTER TABLE (present in the snapshot).
    Column("confidence_tagging_coverage", Float),
    Column("confidence_mechanics_clarity", Float),
    Column("confidence_intent_alignment", Float),
    Column("confidence_combo_detection_depth", Float),
    Index("ix_bracket_estimates_deck", "deck_id"),
    sqlite_autoincrement=True,
)

deck_bracket_findings = Table(
    "deck_bracket_findings",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("deck_id", Integer, ForeignKey("decks.id", ondelete="CASCADE"), nullable=False),
    Column(
        "estimate_id",
        Integer,
        ForeignKey("deck_bracket_estimates.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("finding_type", String(64), nullable=False),
    Column("finding_value", String(255)),
    Column("severity", String(16), nullable=False, server_default=text("'info'")),
    Column("message", Text, nullable=False),
    Column("contributes_to_bracket", Integer),
    Column("weight", Float, nullable=False, server_default=text("1.0")),
    Index("ix_bracket_findings_estimate", "estimate_id"),
    sqlite_autoincrement=True,
)

game_changer_cards = Table(
    "game_changer_cards",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("card_id", Integer, ForeignKey("cards.id", ondelete="SET NULL")),
    Column("card_name", String(255), nullable=False),
    Column("source", String(128), nullable=False),
    Column("date_added", Date),
    Column("date_removed", Date),
    # Native boolean — raw SQL does ``WHERE active`` (the v3.34.5 fix). On PG this
    # is ``boolean``; the pgloader cutover must NOT cast it integer→boolean blindly.
    Column("active", Boolean, nullable=False, server_default=true()),
    Column("rules_version", String(32), nullable=False),
    UniqueConstraint("card_name", "rules_version"),
    Index("ix_game_changer_active", "active"),
    sqlite_autoincrement=True,
)

# ---------------------------------------------------------------------------
# migrate_v3_25_0_scryfall_cards.py — the Scryfall bulk cache seam (24 columns,
# fixed order; see CLAUDE.md "scryfall_cards seam is byte-identical").
# ---------------------------------------------------------------------------

scryfall_bulk_meta = Table(
    "scryfall_bulk_meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text),
)

scryfall_cards = Table(
    "scryfall_cards",
    metadata,
    Column("scryfall_id", Text, primary_key=True),
    Column("name", Text),
    Column("set_code", Text),
    Column("set_name", Text),
    Column("collector_number", Text),
    Column("rarity", Text),
    Column("image_url", Text),
    Column("type_line", Text),
    Column("oracle_text", Text),
    # Prices stay TEXT (cache-type anchoring — raw SQL reads them as strings).
    Column("price_usd", Text),
    Column("price_usd_foil", Text),
    Column("price_usd_etched", Text),
    Column("colors", Text),
    Column("color_identity", Text),
    Column("mana_cost", Text),
    # cmc REAL/double — the one numeric cache column.
    Column("cmc", Float),
    Column("legalities", Text),
    # full_art stays INTEGER 0/1 — raw cache SQL compares ``= 1`` (do NOT promote
    # to boolean; this is the deliberate counterpart to game_changer_cards.active).
    Column("full_art", Integer),
    Column("frame_effects", Text),
    Column("set_type", Text),
    Column("layout", Text),
    Column("produced_tokens", Text),
    Column("loyalty", Text),
    Column("defense", Text),
    Index("ix_scryfall_cards_set_collector", "set_code", "collector_number"),
    Index("ix_scryfall_cards_name", "name"),
)
