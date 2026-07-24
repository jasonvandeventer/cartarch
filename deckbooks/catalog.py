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
        "cover": {
            "glow": "#3c2e1c",
            "base": "#14100a",
            "edge": "#a9832f",
            "title": "#e2b750",
            "soft": "#d8c48a",
        },
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
            "chapter_flavor": {
                "Commander": "The bard who wakes the sleeping relics.",
                "Ramp": "The artifacts that awaken the forest.",
                "Draw": "Knowledge drawn from the wake of combat.",
                "Interaction": "When the overgrown workshop must defend itself.",
                "Threats": "The relics that rise to enormous, indestructible life.",
                "Utility": "The odd curiosities that make it all hum.",
                "Mana Base": "The ground from which everything grows.",
            },
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
    "second-breakfast": {
        "deck_name": "Second Breakfast",
        "name": "Second Breakfast",
        "commander_names": ["Frodo, Adventurous Hobbit", "Sam, Loyal Attendant"],
        "subtitle": "Two hobbits, one Ring, and absolutely no skipping meals.",
        "cover": {
            "glow": "#5c3f18",
            "base": "#120d07",
            "edge": "#b98a3c",
            "title": "#f0cf7a",
            "soft": "#cdb784",
        },
        "interior": {
            "paper": "#ece0c4",
            "paper2": "#e0d1ab",
            "panel": "#dcc9a0",
            "gold": "#cf9f45",
            "goldSoft": "#e6c583",
            "bronze": "#b0803a",
            "moss": "#5f6d3c",
            "mossDeep": "#46512a",
            "ink": "#221a0e",
            "ink2": "#2c2114",
        },
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
            "chapter_flavor": {
                "Commander": "The hobbits who never miss a meal.",
                "Ramp": "Stocking the pantry before the feast.",
                "Draw": "Second helpings — always more in hand.",
                "Interaction": "A polite word for those who reach for your plate.",
                "Threats": "When the quiet larder finally overflows.",
                "Utility": "The small comforts that keep the table full.",
                "Mana Base": "The Shire underfoot.",
            },
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
        "cover": {
            "glow": "#2b4417",
            "base": "#0a0c07",
            "edge": "#6f8b3a",
            "title": "#a7d24a",
            "soft": "#c2cf7e",
        },
        "interior": {
            "paper": "#dee2c6",
            "paper2": "#d0d5ac",
            "panel": "#c8cfa0",
            "gold": "#88a23c",
            "goldSoft": "#b4cf6a",
            "bronze": "#6f7d3a",
            "moss": "#4b5a24",
            "mossDeep": "#354619",
            "ink": "#12160b",
            "ink2": "#1a1f10",
            "text": "#232a15",
            "textDim": "#56603c",
        },
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
            "chapter_flavor": {
                "Commander": "The pest that signals the swarm to grow.",
                "Ramp": "Cultivating the culture before it spreads.",
                "Draw": "Reading the specimens as they multiply.",
                "Interaction": "Culling anything that threatens the colony.",
                "Threats": "When the brood breaches containment.",
                "Utility": "The mutations that let the swarm thrive.",
                "Mana Base": "The soil the infestation roots in.",
            },
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
    "dnr": {
        "deck_name": "DNR",
        "name": "DNR",
        "commander_names": ["Anti-Venom, Horrifying Healer"],
        "subtitle": "Do Not Resuscitate — there won't be a need. Nothing that hits you lands where it's aimed.",
        "cover": {
            "glow": "#2b3742",
            "base": "#0b0e12",
            "edge": "#7f8d99",
            "title": "#e9eff3",
            "soft": "#57e0a0",
        },
        "interior": {
            "paper": "#eaedf1",
            "paper2": "#dde2e8",
            "panel": "#d5dbe2",
            "gold": "#7c8b98",
            "goldSoft": "#a9b4bd",
            "bronze": "#6c7a86",
            "moss": "#2b9068",
            "mossDeep": "#1c6a4f",
            "ink": "#10151b",
            "ink2": "#171d25",
            "text": "#1e2530",
            "textDim": "#56626f",
        },
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
            "chapter_flavor": {
                "Commander": "The host that cannot be killed.",
                "Ramp": "Prepping the theatre before the procedure.",
                "Draw": "Consulting the chart for what comes next.",
                "Interaction": "Sterilizing every incoming threat.",
                "Threats": "When the wall stops holding and starts winning.",
                "Utility": "The instruments that keep the patient alive.",
                "Mana Base": "The white ground the fortress stands on.",
            },
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
    "severance-package": {
        "deck_name": "Severance Package",
        "name": "Severance Package",
        "commander_names": ["Teysa Karlov"],
        "subtitle": "Every departure comes with benefits. We're letting them go — and cashing in.",
        "cover": {
            "glow": "#2a2620",
            "base": "#0a0908",
            "edge": "#b09553",
            "title": "#efe9db",
            "soft": "#c99a4a",
        },
        "interior": {
            "paper": "#ece7db",
            "paper2": "#ded7c5",
            "panel": "#d6cdb7",
            "gold": "#b0913f",
            "goldSoft": "#d5ba7c",
            "bronze": "#977c37",
            "moss": "#6a5a49",
            "mossDeep": "#453a30",
            "ink": "#16130f",
            "ink2": "#201b15",
        },
        "identity": {
            "mission": (
                "An Orzhov aristocrats machine. Staff up with expendable creatures and tokens, "
                "then process the layoffs — free sacrifice outlets feed a payroll of death "
                "triggers (drain, draw, tokens, counters), and Teysa Karlov signs off on each "
                "one twice. Every creature that leaves the company pays a dividend; do it enough "
                "and the table bleeds out while your books only grow. The deck should feel like "
                "ruthless, well-dressed corporate attrition — loss, monetized."
            ),
            "pillars": [
                {
                    "name": "The Workforce",
                    "description": "A wide, expendable roster — cheap creatures and token makers "
                    "that exist to be spent. Bodies are inventory, not investment.",
                },
                {
                    "name": "Processing the Layoffs",
                    "description": "Free sacrifice outlets turn creatures into value on demand, "
                    "and death-trigger payoffs (drain, draw, tokens, +1/+1 counters) collect on "
                    "every departure.",
                },
                {
                    "name": "Teysa Signs Off Twice",
                    "description": "Teysa Karlov doubles every death trigger — each severance pays "
                    "out twice. Recursion brings the workforce back to be spent again.",
                },
            ],
            "palette": [
                "orzhov ivory",
                "funerary black",
                "brushed-gold letterhead",
                "ledger grey",
                "signet-wax crimson",
            ],
            "chapter_flavor": {
                "Commander": "The executive who signs off on every departure — twice.",
                "Ramp": "Funding the payroll.",
                "Draw": "Reviewing the personnel files.",
                "Interaction": "Terminations, effective immediately.",
                "Threats": "When the layoffs bleed the whole table dry.",
                "Utility": "The paperwork that makes attrition profitable.",
                "Mana Base": "The corporate ground floor.",
            },
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
    "stack-overflow": {
        "deck_name": "Stack Overflow",
        "name": "Stack Overflow",
        "commander_names": ["Melek, Izzet Paragon"],
        "subtitle": "Response to your response. Nobody's quite sure what resolves next — that's the point.",
        "cover": {
            "glow": "#16406e",
            "base": "#070b16",
            "edge": "#3f8fd6",
            "title": "#7fd4ff",
            "soft": "#ff8a63",
        },
        "interior": {
            "paper": "#dde3ec",
            "paper2": "#ccd6e3",
            "panel": "#c3cfdd",
            "gold": "#2f6fb0",
            "goldSoft": "#6ba6de",
            "bronze": "#3f6f9c",
            "moss": "#b0492e",
            "mossDeep": "#24466f",
            "ink": "#0c1424",
            "ink2": "#131d30",
            "text": "#1a2230",
            "textDim": "#4d5a6b",
        },
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
            "chapter_flavor": {
                "Commander": "The mind that keeps the stack from ever settling.",
                "Ramp": "Fuel for the next spell, and the next.",
                "Draw": "More cards, more questions, more responses.",
                "Interaction": "Counters, redirects, and the wrong target chosen.",
                "Threats": "When the stack finally resolves your way.",
                "Utility": "Copies, forks, and beautiful uncertainty.",
                "Mana Base": "The circuitry it all runs on.",
            },
            "selection_rule": "Rarity alone never determines the definitive printing.",
        },
    },
}


def list_book_ids() -> list[str]:
    return list(BOOKS)


def get_config(deckbook_id: str) -> dict | None:
    return BOOKS.get(deckbook_id)
