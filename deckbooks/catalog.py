"""The deckbook catalog — one static config entry per deck.

Adding a deckbook is (mostly) adding an entry here: the source deck's name in the
local Cartarch DB, the display identity, and the commanders. Any per-deck seed
logic (a pre-made finalized decision, a research queue) lives in init_deck keyed
by the same id. Kept dependency-free so both init_deck and the app can import it.
"""

from __future__ import annotations

BOOKS: dict[str, dict] = {
    "osha-violation": {
        "deck_name": "OSHA Violation",
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
        "deck_name": "Second Breakfast",
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
    # --- CoruscantSunrise's other decks (seeded from prod 2026-07-24) -----------
    # Placeholder identities — theme each as we go (mission/pillars/palette).
    "containment-failure": {
        "deck_name": "Containment Failure",
        "name": "Containment Failure",
        "commander_names": ["Blech, Loafing Pest"],
        "subtitle": "The specimens have breached containment — and every drop of life makes them bigger.",
        "identity": {
            "mission": (
                "A Golgari swarm of Blech's favored vermin — Pests, Bats, Insects, Snakes, and "
                "Spiders. Flood the board wide, then turn incidental lifegain into Blech's "
                "signal: every point of life gained puts a +1/+1 counter on the whole brood at "
                "once, until a table of harmless, ignorable creepy-crawlies erupts into an "
                "oversized infestation. The deck should feel like a lab-leak in slow motion — "
                "small specimens multiplying and swelling until containment fails."
            ),
            "pillars": [
                {
                    "name": "The Brood",
                    "description": "Go wide with the five types Blech empowers — Pest, Bat, "
                    "Insect, Snake, Spider. Quantity first; a board full of small, unassuming "
                    "vermin.",
                },
                {
                    "name": "Every Drop Counts",
                    "description": "Incidental, repeatable lifegain is the trigger. Each life "
                    "gained is another mass of +1/+1 counters across the whole swarm at once.",
                },
                {
                    "name": "Containment Failure",
                    "description": "The payoff: the brood grows past control. Trample, overrun, "
                    "and go-tall-while-wide finishers turn the infestation lethal.",
                },
            ],
            "palette": [
                "biohazard green",
                "swamp-rot black",
                "carapace brown",
                "spore yellow-green",
                "specimen-jar glass",
            ],
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
    "dnr": {
        "deck_name": "DNR",
        "name": "DNR",
        "commander_names": ["Anti-Venom, Horrifying Healer"],
        "subtitle": "Do Not Resuscitate — there won't be a need. Nothing that hits you lands where it's aimed.",
        "identity": {
            "mission": (
                "A mono-white invulnerability engine. Every point of damage aimed at you is "
                "redirected — by Pariah, Pariah's Shield, and a stack of effects that emulate "
                "them — onto Anti-Venom, who prevents that damage and grows a +1/+1 counter from "
                "every point. Assemble the redundancy and you simply cannot be killed by damage; "
                "from behind that wall the game is closed at your leisure. The deck should feel "
                "like a fortress that turns every assault into fuel."
            ),
            "pillars": [
                {
                    "name": "The Redirect",
                    "description": "Pariah, Pariah's Shield, and every emulation of them — all "
                    "damage that would hit you is dealt to a creature instead. Stacked many "
                    "times over so the engine never depends on one card.",
                },
                {
                    "name": "The Unkillable Host",
                    "description": "Anti-Venom prevents the redirected damage and converts it to "
                    "+1/+1 counters. The wall doesn't just hold — it grows. Protection and "
                    "recursion keep the host on the board.",
                },
                {
                    "name": "Do Not Resuscitate",
                    "description": "Once the redirection engine is online you cannot die to "
                    "damage. Inevitability from behind the wall — close the game with the now-"
                    "enormous host, at leisure.",
                },
            ],
            "palette": [
                "clinical white",
                "surgical steel",
                "gauze linen",
                "flatline-monitor green",
                "consecrated gold",
            ],
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
    "severance-package": {
        "deck_name": "Severance Package",
        "name": "Severance Package",
        "commander_names": ["Teysa Karlov"],
        "subtitle": "An Orzhov Commander deck.",
        "identity": {
            "mission": "",
            "pillars": [],
            "palette": [],
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
    "stack-overflow": {
        "deck_name": "Stack Overflow",
        "name": "Stack Overflow",
        "commander_names": ["Melek, Izzet Paragon"],
        "subtitle": "Response to your response. Nobody's quite sure what resolves next — that's the point.",
        "identity": {
            "mission": (
                "An Izzet spellslinger built around the stack itself. Copies, redirections, "
                "and rebounds pile effects on top of one another until no one at the table — "
                "the pilot included — is quite sure what will resolve next. The deck should "
                "feel like controlled chaos: brilliant, overclocked, and one trigger away from "
                "a cascade of consequences."
            ),
            "pillars": [
                {
                    "name": "The Stack Never Settles",
                    "description": "Spells copy, fork, and trigger more spells. The stack is "
                    "always taller than it looks, and the top of it keeps changing.",
                },
                {
                    "name": "Redirect & Misdirect",
                    "description": "Change targets, bounce spells back, and make removal hit the "
                    "wrong thing. Control the question of who and what, not just how much.",
                },
                {
                    "name": "Overclocked Genius",
                    "description": "The Izzet aesthetic — arcane circuitry, storm-lit laboratories, "
                    "and mad-scientist confidence. Blue-red brilliance running hot.",
                },
            ],
            "palette": [
                "izzet electric blue",
                "molten red",
                "arc-flash white",
                "copper circuitry",
                "stormglass violet",
            ],
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
}


def list_book_ids() -> list[str]:
    return list(BOOKS)


def get_config(deckbook_id: str) -> dict | None:
    return BOOKS.get(deckbook_id)
