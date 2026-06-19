"""Idempotent reference-data seeding — the post-baseline seed path (Gate #4, Phase D).

Run this AFTER ``alembic upgrade head`` on any FRESHLY-CREATED database:

    alembic upgrade head && python scripts/seed_reference_data.py

It populates the three live reference/seed tables the app needs on a from-scratch
bring-up (CI, disaster recovery, an empty rehearsal sandbox):

  1. commander_bracket_rules  — the 5 Commander bracket-rule tiers (pure reference).
  2. game_changer_cards       — the "Game Changer" list (the ``WHERE active`` table);
                                Scryfall ``is:gamechanger`` with an offline fallback.
  3. card_tags                — bracket-v2 auto-tags over every Card row.

Why a script and not an Alembic data-migration: it keeps the Alembic revision chain
pure-DDL (so the cutover ``alembic stamp head`` is unambiguous) and keeps the Scryfall
network fetch + full-table tagging OUT of ``alembic upgrade head`` (migrations stay
deterministic and network-free).

IDEMPOTENT: each underlying seed is check-then-insert / upsert on a natural key, so
re-running is safe and inserts nothing new. This is what makes it a no-op at the
Postgres cutover — pgloader copies the seed rows from prod, and a later run of this
script adds nothing.

NOT re-homed here: the 4 one-time historical backfills (scrub_legacy_tags,
backfill_from_position, promote_intrinsic_auto_certain, token_dfc_backfill). Those are
already baked into prod data and must NOT be re-applied to migrated data.

The DB targeted is whatever ``DATABASE_URL`` resolves to (the app/db.py env seam) —
SQLite when unset, Postgres when set. Seeds land wherever the app points.

NOTE on card_tags: it tags the Card rows that EXIST. On a truly empty fresh DB (no
cards yet) it correctly writes 0 tags — that is not a failure; tags accrue as cards
are imported. The non-empty guarantees a fresh DB MUST meet are game_changer_cards
and commander_bracket_rules, which populate independent of any other table.
"""

from __future__ import annotations

import os
import sys

# Run from any working directory (mirrors scripts/orphan_audit.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.migrate_v3_15_0_seed_bracket_rules import main as seed_bracket_rules  # noqa: E402
from scripts.migrate_v3_15_0_seed_card_tags import main as seed_card_tags  # noqa: E402
from scripts.migrate_v3_15_0_seed_game_changers import main as seed_game_changers  # noqa: E402


def main() -> None:
    print("Seeding reference data (idempotent)...")
    print("-" * 60)
    print("[1/3] commander_bracket_rules")
    seed_bracket_rules()
    print("[2/3] game_changer_cards")
    seed_game_changers()
    print("[3/3] card_tags")
    seed_card_tags()
    print("-" * 60)
    print("Reference-data seeding complete.")


if __name__ == "__main__":
    main()
