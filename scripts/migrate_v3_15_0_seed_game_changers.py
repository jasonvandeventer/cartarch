"""Seed game_changer_cards with the conservative published Game Changer list
plus the existing fast_mana / free_interaction frozensets from compute_deck_bracket.

Sourced from WotC's Bracket framework reference (April 2025) plus community
consensus. This is a starting list — admins can add/remove rows in DB without a
new migration.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine

RULES_VERSION = "1.0.0"
SOURCE = "wotc-2025-04 + community"

# WotC published Game Changer list (subset commonly cited in bracket framework
# discussion). Conservative — admins can extend in DB.
GAME_CHANGERS = sorted(
    {
        # Fast mana
        "Mana Crypt",
        "Mana Vault",
        "Mox Diamond",
        "Mox Opal",
        "Chrome Mox",
        "Jeweled Lotus",
        "Grim Monolith",
        "Lotus Petal",
        "Ancient Tomb",
        # Tutors
        "Demonic Tutor",
        "Vampiric Tutor",
        "Imperial Seal",
        "Mystical Tutor",
        "Worldly Tutor",
        "Enlightened Tutor",
        # Free interaction
        "Force of Will",
        "Force of Negation",
        "Fierce Guardianship",
        "Mana Drain",
        "Pact of Negation",
        "Deflecting Swat",
        # Generic engines / staples cited as Game Changers
        "Cyclonic Rift",
        "Smothering Tithe",
        "Rhystic Study",
        "Trouble in Pairs",
        "Dockside Extortionist",
        "Drannith Magistrate",
    }
)


def main() -> None:
    inserted = 0
    with engine.begin() as conn:
        for name in GAME_CHANGERS:
            existing = conn.execute(
                text(
                    "SELECT id FROM game_changer_cards "
                    "WHERE card_name = :n AND rules_version = :v"
                ),
                {"n": name, "v": RULES_VERSION},
            ).first()
            if existing:
                continue
            card_id_row = conn.execute(
                text("SELECT id FROM cards WHERE name = :n LIMIT 1"),
                {"n": name},
            ).first()
            card_id = card_id_row[0] if card_id_row else None
            conn.execute(
                text(
                    """
                    INSERT INTO game_changer_cards (
                        card_id, card_name, source, date_added,
                        active, rules_version
                    ) VALUES (
                        :card_id, :card_name, :source, CURRENT_DATE,
                        1, :rules_version
                    )
                    """
                ),
                {
                    "card_id": card_id,
                    "card_name": name,
                    "source": SOURCE,
                    "rules_version": RULES_VERSION,
                },
            )
            inserted += 1
    print(f"Seeded {inserted} Game Changer cards (v{RULES_VERSION})")


if __name__ == "__main__":
    main()
