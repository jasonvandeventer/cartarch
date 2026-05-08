"""V1 of the Bracket Estimator V2 schema.

Creates five tables that drive the new bracket pipeline:

- commander_bracket_rules: configurable tier thresholds (rules-as-data, not hardcoded)
- game_changer_cards: WotC Game Changer list (sourced/curated)
- card_tags: per-card intrinsic tags (separate from per-row InventoryRow.tags which
  remain in place for the Synergy panel and Health metrics)
- deck_bracket_estimates: persisted bracket result per deck per rules version
- deck_bracket_findings: human-readable signals that contributed to the estimate

V2 adds deck_bracket_confidence; V3 adds combos/combo_pieces; V4 adds
deck_bracket_feedback. Those are not created here.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


def main() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS commander_bracket_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bracket INTEGER NOT NULL,
                    name VARCHAR(64) NOT NULL,
                    description TEXT,
                    max_game_changers INTEGER NOT NULL DEFAULT 0,
                    allows_mass_land_denial BOOLEAN NOT NULL DEFAULT 0,
                    allows_extra_turn_chains BOOLEAN NOT NULL DEFAULT 0,
                    allows_two_card_combos BOOLEAN NOT NULL DEFAULT 0,
                    allows_combo_as_primary BOOLEAN NOT NULL DEFAULT 0,
                    competitive BOOLEAN NOT NULL DEFAULT 0,
                    rules_version VARCHAR(32) NOT NULL,
                    effective_date DATE,
                    UNIQUE (bracket, rules_version)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS game_changer_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
                    card_name VARCHAR(255) NOT NULL,
                    source VARCHAR(128) NOT NULL,
                    date_added DATE,
                    date_removed DATE,
                    active BOOLEAN NOT NULL DEFAULT 1,
                    rules_version VARCHAR(32) NOT NULL,
                    UNIQUE (card_name, rules_version)
                )
                """
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_game_changer_active ON game_changer_cards(active)")
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS card_tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                    tag VARCHAR(64) NOT NULL,
                    confidence VARCHAR(16) NOT NULL DEFAULT 'medium',
                    source VARCHAR(32) NOT NULL DEFAULT 'oracle_text_rule',
                    last_reviewed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (card_id, tag)
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_card_tags_card ON card_tags(card_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_card_tags_tag ON card_tags(tag)"))
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS deck_bracket_estimates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                    estimated_bracket INTEGER NOT NULL,
                    mechanics_bracket INTEGER NOT NULL,
                    intent_bracket INTEGER,
                    final_bracket INTEGER NOT NULL,
                    score REAL,
                    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    rules_version VARCHAR(32) NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_bracket_estimates_deck ON deck_bracket_estimates(deck_id)"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS deck_bracket_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
                    estimate_id INTEGER NOT NULL REFERENCES deck_bracket_estimates(id) ON DELETE CASCADE,
                    finding_type VARCHAR(64) NOT NULL,
                    finding_value VARCHAR(255),
                    severity VARCHAR(16) NOT NULL DEFAULT 'info',
                    message TEXT NOT NULL,
                    contributes_to_bracket INTEGER,
                    weight REAL NOT NULL DEFAULT 1.0
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_bracket_findings_estimate ON deck_bracket_findings(estimate_id)"
            )
        )

    print(
        "Created Bracket V2 tables: commander_bracket_rules, game_changer_cards, "
        "card_tags, deck_bracket_estimates, deck_bracket_findings"
    )


if __name__ == "__main__":
    main()
