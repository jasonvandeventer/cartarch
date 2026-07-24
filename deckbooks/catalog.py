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
    "sam-and-frodo": {
        "deck_name": "Frodo, Adventurous Hobbit and Sam, Loyal Attendant",
        "name": "Sam & Frodo",
        "commander_names": ["Frodo, Adventurous Hobbit", "Sam, Loyal Attendant"],
        "subtitle": "Two hobbits, one Ring, and the long road to Mordor.",
        # A starting identity — edit to taste (say the word and I'll refine it).
        "identity": {
            "mission": (
                "Two hobbits carry a burden far greater than themselves. The deck should "
                "feel like the long walk to Mordor — quiet loyalty, mounting dread, and the "
                "small acts of courage that matter most when all seems lost."
            ),
            "pillars": [
                {
                    "name": "The Fellowship",
                    "description": "Loyalty and support — the cards that protect and lift the "
                    "ones beside them.",
                },
                {
                    "name": "The Long Road",
                    "description": "The journey itself: lands, landscapes, and the miles between "
                    "the Shire and the Mountain.",
                },
                {
                    "name": "The Ring's Burden",
                    "description": "Temptation, sacrifice, and the cost of carrying what no one "
                    "should have to.",
                },
            ],
            "palette": [
                "Shire green",
                "wayfarer brown",
                "Ring gold",
                "Mordor ash",
                "old parchment",
            ],
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
}


def list_book_ids() -> list[str]:
    return list(BOOKS)


def get_config(deckbook_id: str) -> dict | None:
    return BOOKS.get(deckbook_id)
