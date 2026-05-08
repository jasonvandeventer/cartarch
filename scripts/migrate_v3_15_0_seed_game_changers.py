"""Seed game_changer_cards from Scryfall's `is:gamechanger` query.

Falls back to a hardcoded conservative list if Scryfall is unreachable during
the migration (e.g., on offline dev machines or during a Scryfall outage).
Admins can re-seed by clearing the schema_migrations row for
v3_15_0_seed_game_changers and restarting; the upsert is non-destructive
(existing rows are skipped, only new names are added).
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine
from app.scryfall import fetch_game_changer_names

RULES_VERSION = "1.0.0"

# Fallback list used only if Scryfall is unreachable.
_FALLBACK = sorted(
    {
        "Ancient Tomb",
        "Mana Crypt",
        "Mox Diamond",
        "Demonic Tutor",
        "Vampiric Tutor",
        "Force of Will",
        "Force of Negation",
        "Cyclonic Rift",
        "Smothering Tithe",
        "Rhystic Study",
        "Dockside Extortionist",
        "Drannith Magistrate",
    }
)


def main() -> None:
    names = fetch_game_changer_names()
    if names:
        source = "scryfall is:gamechanger"
        print(f"Fetched {len(names)} Game Changers from Scryfall")
    else:
        names = _FALLBACK
        source = "fallback (Scryfall unreachable)"
        print(f"Scryfall unreachable; using {len(names)}-card fallback list")

    inserted = 0
    with engine.begin() as conn:
        for name in sorted(set(names)):
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
                    "source": source,
                    "rules_version": RULES_VERSION,
                },
            )
            inserted += 1
    print(f"Seeded {inserted} new Game Changer rows (v{RULES_VERSION})")


if __name__ == "__main__":
    main()
