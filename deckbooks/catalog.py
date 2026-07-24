"""The deckbook catalog — one static config entry per deck.

Adding a deckbook is (mostly) adding an entry here: the source deck's name in the
local Cartarch DB, the display identity, and the commanders. Any per-deck seed
logic (a pre-made finalized decision, a research queue) lives in init_deck keyed
by the same id. Kept dependency-free so both init_deck and the app can import it.
"""

from __future__ import annotations

BOOKS: dict[str, dict] = {
    "osha-violation": {
        "deck_name": "Bello, Bard of the Brambles",
        "name": "OSHA Violation",
        "commander_names": ["Bello, Bard of the Brambles"],
        "subtitle": (
            "Animated relics. Ancient enchantments. Absolutely no regard for workplace safety."
        ),
        "identity": {
            "mission": (
                "Bello awakens forgotten relics and ancient enchantments into enormous, "
                "indestructible attackers. Every permanent should feel at home in an "
                "overgrown magical workshop where abandoned curiosities have come alive."
            ),
            "pillars": [
                {
                    "name": "Enchanted Relics",
                    "description": "Artifacts should feel storied, strange, and recently awakened.",
                },
                {
                    "name": "Living Wilderness",
                    "description": "Forests, vines, wood, moss, and elemental magic unite the deck.",
                },
                {
                    "name": "Joyful Chaos",
                    "description": "The deck should feel whimsical even while producing absurd "
                    "combat steps.",
                },
            ],
            "palette": [
                "warm bronze",
                "weathered wood",
                "moss green",
                "parchment",
                "rune-light gold",
            ],
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
    "second-breakfast": {
        "deck_name": "Frodo, Adventurous Hobbit and Sam, Loyal Attendant",
        "name": "Second Breakfast",
        "commander_names": ["Frodo, Adventurous Hobbit", "Sam, Loyal Attendant"],
        "subtitle": "Two hobbits, one Ring, and absolutely no skipping meals.",
        "identity": {
            "mission": (
                "A Food deck that keeps a full larder and a low profile. Second Breakfast "
                "amasses value one small comfort at a time — Food, tokens, incremental "
                "advantage — and never looks like the biggest threat at the table, until "
                "the feast is laid and the game is already, quietly, won."
            ),
            "pillars": [
                {
                    "name": "A Full Larder",
                    "description": "Food, treasures, and tokens that pile up unremarkably into "
                    "an unstoppable pantry of resources.",
                },
                {
                    "name": "Beneath Notice",
                    "description": "Never the scariest board. The deck thrives on being "
                    "underestimated, unthreatening, and left well alone.",
                },
                {
                    "name": "The Long Feast",
                    "description": "Value compounds over time. Patience is the win condition — "
                    "no single haymaker, just an inevitable, well-fed advantage.",
                },
            ],
            "palette": [
                "golden crust",
                "hearth ember",
                "garden green",
                "fresh butter",
                "ale-brown parchment",
            ],
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
}


def list_book_ids() -> list[str]:
    return list(BOOKS)


def get_config(deckbook_id: str) -> dict | None:
    return BOOKS.get(deckbook_id)
