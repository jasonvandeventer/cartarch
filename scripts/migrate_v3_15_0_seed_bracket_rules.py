"""Seed commander_bracket_rules with the 5 default tier rows.

These thresholds are configurable in DB; this migration just establishes the
default v1.0.0 ruleset. Future rules versions can be added as new rows without
removing these.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine

RULES_VERSION = "1.0.0"

TIERS = [
    {
        "bracket": 1,
        "name": "Exhibition",
        "description": "Battlecruiser games. No tutors, no fast mana, no infinite combos.",
        "max_game_changers": 0,
        "allows_mass_land_denial": False,
        "allows_extra_turn_chains": False,
        "allows_two_card_combos": False,
        "allows_combo_as_primary": False,
        "competitive": False,
    },
    {
        "bracket": 2,
        "name": "Core",
        "description": "Lightly upgraded preconstructed strength. A few tutors fine.",
        "max_game_changers": 0,
        "allows_mass_land_denial": False,
        "allows_extra_turn_chains": False,
        "allows_two_card_combos": False,
        "allows_combo_as_primary": False,
        "competitive": False,
    },
    {
        "bracket": 3,
        "name": "Upgraded",
        "description": "Tuned casual. Up to 3 Game Changers. Two-card combos allowed if not primary.",
        "max_game_changers": 3,
        "allows_mass_land_denial": False,
        "allows_extra_turn_chains": False,
        "allows_two_card_combos": True,
        "allows_combo_as_primary": False,
        "competitive": False,
    },
    {
        "bracket": 4,
        "name": "Optimized",
        "description": "High power. Mass land denial, extra-turn chains, and primary combo lines all permitted.",
        "max_game_changers": 999,
        "allows_mass_land_denial": True,
        "allows_extra_turn_chains": True,
        "allows_two_card_combos": True,
        "allows_combo_as_primary": True,
        "competitive": False,
    },
    {
        "bracket": 5,
        "name": "cEDH",
        "description": "Competitive. Cards picked to win as fast and reliably as possible.",
        "max_game_changers": 999,
        "allows_mass_land_denial": True,
        "allows_extra_turn_chains": True,
        "allows_two_card_combos": True,
        "allows_combo_as_primary": True,
        "competitive": True,
    },
]


def main() -> None:
    inserted = 0
    with engine.begin() as conn:
        for tier in TIERS:
            existing = conn.execute(
                text(
                    "SELECT id FROM commander_bracket_rules "
                    "WHERE bracket = :b AND rules_version = :v"
                ),
                {"b": tier["bracket"], "v": RULES_VERSION},
            ).first()
            if existing:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO commander_bracket_rules (
                        bracket, name, description, max_game_changers,
                        allows_mass_land_denial, allows_extra_turn_chains,
                        allows_two_card_combos, allows_combo_as_primary,
                        competitive, rules_version, effective_date
                    ) VALUES (
                        :bracket, :name, :description, :max_game_changers,
                        :allows_mass_land_denial, :allows_extra_turn_chains,
                        :allows_two_card_combos, :allows_combo_as_primary,
                        :competitive, :rules_version, CURRENT_DATE
                    )
                    """
                ),
                {**tier, "rules_version": RULES_VERSION},
            )
            inserted += 1
    print(f"Seeded {inserted} bracket rule rows (v{RULES_VERSION})")


if __name__ == "__main__":
    main()
