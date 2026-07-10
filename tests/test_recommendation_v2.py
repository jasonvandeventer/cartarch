"""Tests for the Deckbuilder v2 substrate (issue #60, P1).

Covers role-subtype classification, role-usefulness scoring, dead-role
detection (color-specific reducers in a colorless deck), the strategy-profile
seed, plan-coverage evaluation, and the Molecule Man regression fixture.

Pure functions over Card data — no DB session needed. Fixture oracle texts are
real (or lightly trimmed) for every card whose classification is asserted;
cards irrelevant to the assertions carry empty oracle text and degrade to
no-role, which is the substrate's documented behavior for unknown cards.
"""

from __future__ import annotations

from app.models import Card
from app.recommendation_service import (
    classify_role_subtype,
    evaluate_plan_coverage,
    score_role_usefulness,
    seed_strategy_profile,
)

_SID = [0]


def c(name, oracle="", tl="Artifact", cmc=3.0, color_identity=""):
    _SID[0] += 1
    return Card(
        scryfall_id=f"v2-{_SID[0]}",
        name=name,
        set_code="tst",
        collector_number=str(_SID[0]),
        type_line=tl,
        oracle_text=oracle,
        cmc=cmc,
        color_identity=color_identity,
    )


COLORLESS = set()

# --- hardcoded Molecule Man strategy profile (issue #60 section 2 seed) --------

MOLECULE_MAN_PROFILE = {
    "color_identity": "colorless",
    "high": [
        "topdeck_manipulation",
        "opponent_turn_draw",
        "sacrifice_to_draw",
        "commander_protection",
    ],
    "medium": ["early_ramp", "removal", "wipe", "protection", "engine"],
    "low": ["color_fixer", "color_specific_reducer", "tribal_support", "symmetrical_draw"],
    "targets": {
        "lands": (36, 38),
        "ramp": (10, 14),
        "topdeck_manipulation": (8, 12),
        "opponent_turn_draw": (6, 10),
        "large_payoffs": (10, 14),
        "removal_wipes": (8, 10),
        "protection": (5, 8),
        "win_conditions": (3, 6),
    },
}

# --- key card oracle texts ------------------------------------------------------

SAPPHIRE_MEDALLION = ("Sapphire Medallion", "Blue spells you cast cost {1} less to cast.", 2.0)
HAZORETS_MONUMENT = (
    "Hazoret's Monument",
    "Red creature spells you cast cost {1} less to cast. "
    "Whenever you cast a creature spell, you may discard a card. If you do, draw a card.",
    3.0,
)
OKETRAS_MONUMENT = (
    "Oketra's Monument",
    "White creature spells you cast cost {1} less to cast. "
    "Whenever you cast a creature spell, create a 1/1 white Warrior creature token with vigilance.",
    3.0,
)
MIND_STONE = ("Mind Stone", "{T}: Add {C}. {1}, {T}, Sacrifice Mind Stone: Draw a card.", 2.0)
ENDBRINGER_ORACLE = (
    "Untap Endbringer during each other player's untap step. "
    "{T}: Endbringer deals 1 damage to any target. "
    "{C}, {T}: Target creature can't attack or block this turn. "
    "{C}{C}, {T}: Draw a card."
)
CHROMATIC_LANTERN = (
    "Chromatic Lantern",
    'Lands you control have "{T}: Add one mana of any color." {T}: Add one mana of any color.',
    3.0,
)
PROPHETIC_PRISM = (
    "Prophetic Prism",
    "When Prophetic Prism enters the battlefield, draw a card. "
    "{1}, {T}: Add one mana of any color.",
    2.0,
)


def sapphire():
    return c(*SAPPHIRE_MEDALLION[:1], oracle=SAPPHIRE_MEDALLION[1], cmc=SAPPHIRE_MEDALLION[2])


def endbringer():
    return c("Endbringer", oracle=ENDBRINGER_ORACLE, tl="Creature — Eldrazi", cmc=6.0)


def mind_stone():
    return c(MIND_STONE[0], oracle=MIND_STONE[1], cmc=MIND_STONE[2])


# --- 3a: subtype classification -------------------------------------------------


def test_medallions_are_color_specific_reducers_in_colorless():
    for name, oracle, cmc in (SAPPHIRE_MEDALLION, HAZORETS_MONUMENT, OKETRAS_MONUMENT):
        broad, subtype, confidence = classify_role_subtype(
            c(name, oracle=oracle, cmc=cmc), COLORLESS
        )
        assert (broad, subtype) == ("Ramp", "color_specific_reducer"), name
        assert confidence == "high"


def test_reducer_inside_identity_is_cost_reduction_not_dead():
    broad, subtype, _ = classify_role_subtype(sapphire(), {"U"})
    assert (broad, subtype) == ("Ramp", "cost_reduction")


def test_mind_stone_subtype():
    broad, subtype, _ = classify_role_subtype(mind_stone(), COLORLESS)
    assert broad == "Ramp"
    assert subtype in ("early_ramp", "sacrifice_to_draw")


def test_endbringer_is_opponent_turn_draw():
    broad, subtype, _ = classify_role_subtype(endbringer(), COLORLESS)
    assert (broad, subtype) == ("Draw", "opponent_turn_draw")


def test_activated_group_draw_lands_are_opponent_turn_draw():
    mikokoro = c(
        "Mikokoro, Center of the Sea",
        oracle="{2}, {T}: Each player draws a card.",
        tl="Legendary Land",
        cmc=0.0,
    )
    geier = c(
        "Geier Reach Sanitarium",
        oracle="{T}: Add {C}. {2}, {T}: Each player draws a card, then discards a card.",
        tl="Legendary Land",
        cmc=0.0,
    )
    for card in (mikokoro, geier):
        broad, subtype, _ = classify_role_subtype(card, COLORLESS)
        assert (broad, subtype) == ("Draw", "opponent_turn_draw"), card.name


def test_chromatic_lantern_is_color_fixer():
    broad, subtype, _ = classify_role_subtype(
        c(*CHROMATIC_LANTERN[:1], oracle=CHROMATIC_LANTERN[1], cmc=CHROMATIC_LANTERN[2]),
        COLORLESS,
    )
    assert (broad, subtype) == ("Ramp", "color_fixer")


def test_topdeck_manipulation_without_broad_draw_role():
    ball = c("Crystal Ball", oracle="{1}, {T}: Scry 2.", cmc=3.0)
    broad, subtype, _ = classify_role_subtype(ball, COLORLESS)
    assert (broad, subtype) == ("Draw", "topdeck_manipulation")


def test_unknown_subtype_degrades_to_broad_role():
    wipe = c("Nevinyrral's Disk", oracle="{1}, {T}: Destroy all creatures.", cmc=4.0)
    broad, subtype, _ = classify_role_subtype(wipe, COLORLESS)
    assert (broad, subtype) == ("Wipe", None)


def test_unrecognized_card_has_no_role():
    blank = c("Vanilla Beater", oracle="Flying, vigilance.", tl="Creature — Bird", cmc=3.0)
    assert classify_role_subtype(blank, COLORLESS)[:2] == (None, None)


# --- 3b: usefulness scoring + dead-role detection --------------------------------


def _score(card, colors=COLORLESS, profile=MOLECULE_MAN_PROFILE):
    broad, subtype, _ = classify_role_subtype(card, colors)
    return score_role_usefulness(card, broad, subtype, colors, profile)


def test_sapphire_medallion_dead_in_colorless():
    relevance, reason = _score(sapphire())
    assert relevance == "very_low"
    assert reason.startswith("Dead")
    assert "blue" in reason


def test_mind_stone_high_in_colorless():
    relevance, _ = _score(mind_stone())
    assert relevance == "high"


def test_endbringer_high_in_molecule_man():
    relevance, _ = _score(endbringer())
    assert relevance == "high"


def test_chromatic_lantern_low_in_colorless():
    relevance, reason = _score(
        c(*CHROMATIC_LANTERN[:1], oracle=CHROMATIC_LANTERN[1], cmc=CHROMATIC_LANTERN[2])
    )
    assert relevance == "low"
    assert "color fixing" in reason


def test_unknown_subtype_never_scored_dead():
    wipe = c("Nevinyrral's Disk", oracle="{1}, {T}: Destroy all creatures.", cmc=4.0)
    relevance, _ = _score(wipe)
    assert relevance != "very_low"


def test_no_role_card_scores_medium():
    blank = c("Vanilla Beater", oracle="Flying.", tl="Creature — Bird")
    relevance, _ = _score(blank)
    assert relevance == "medium"


# --- strategy-profile seed --------------------------------------------------------


def test_seed_profile_colorless_draw_commander():
    cmdr = c(
        "Molecule Man",
        oracle="Whenever you draw your first card during each turn, "
        "put a charge counter on Molecule Man.",
        tl="Legendary Creature — Human Avatar",
        cmc=5.0,
    )
    profile = seed_strategy_profile(cmdr, COLORLESS)
    assert profile["color_identity"] == "colorless"
    assert "opponent_turn_draw" in profile["high"]
    assert "color_specific_reducer" in profile["low"]
    assert "targets" in profile
    # The seed must produce the flagship dead-role verdict on its own.
    relevance, _ = _score(sapphire(), profile=profile)
    assert relevance == "very_low"


# --- 3c/3d: Molecule Man regression fixture + plan coverage ----------------------

# Generic mana rocks from the draft — real oracle text is a bare "{T}: Add {C}."
# for all of these at this level of fidelity.
_GENERIC_ROCKS = [
    "Sol Ring",
    "Arcane Signet",
    "Fellwar Stone",
    "Coalition Relic",
    "Pristine Talisman",
    "Honor-Worn Shaku",
    "Corrupted Grafstone",
    "The Irencrag",
    "Gleaming Barrier",
    "Goldvein Pick",
    "Currency Converter",
    "Commander's Sphere",
    "Phial of Galadriel",
    "Stone of Erech",
    "Blitzball",
    "Hithlain Rope",
]

# Mainboard cards with no assertion riding on them — empty oracle, default cmc.
_FILLER_SPELLS = [
    "Trading Post",
    "Barrels of Blasting Jelly",
    "Omni-Cheese Pizza",
    "Eldrazi Confluence",
    "Yggdrasil, Rebirth Engine",
    "Ghirapur Orrery",
    "Throne of Eldraine",
    "Ravenous Amulet",
    "Mister Gutsy",
    "Academy Manufactor",
    "Scrawling Crawler",
    "Found Footage",
    "Campus Guide",
    "World Map",
    "PuPu UFO",
    "Horn of the Mark",
    "Instant Ramen",
    "Conversion Apparatus",
    "Pip-Boy 3000",
    "Spider-Bot",
    "Embermouth Sentinel",
    "Aang's Journey",
    "Energybending",
    "Mechanical Mobster",
    "Hylda's Crown of Winter",
    "Potioner's Trove",
    "Pit Automaton",
    "Patchwork Banner",
    "Skittering Surveyor",
    "Scrabbling Claws",
]

_LANDS = [
    "Fountainport",
    "Lazotep Quarry",
    "Demolition Field",
    "Bonders' Enclave",
    "The Gold Saucer",
    "Volatile Fault",
    "Ash Barrens",
    "Avengers Tower",
    "Avishkar Raceway",
    "Encroaching Wastes",
    "Evolving Wilds",
    "Fabled Passage",
    "Fomori Vault",
    "Horizon of Progress",
    "Krosan Verge",
    "Myriad Landscape",
    "Riveteers Overlook",
    "Roadside Reliquary",
    "Shire Terrace",
    "Tectonic Edge",
    "Terramorphic Expanse",
    "Vibrant Cityscape",
    "Wooded Foothills",
    "Blast Zone",
    "Boseiju, Who Shelters All",
    "High Market",
    "Swarmyard",
    "Abundant Countryside",
    "Dark Depths",
    "Heap Gate",
    "Spawning Bed",
    "Scavenger Grounds",
    "Access Tunnel",
    "Aether Hub",
    "Ancient Ziggurat",
]


def molecule_man_deck() -> list[Card]:
    """Commander + 99 from the issue #60 draft. Real oracle text on every card
    an assertion touches; the rest degrade to no-role by design."""
    deck = [
        c(
            "Molecule Man",
            oracle="Whenever you draw your first card during each turn, "
            "put a charge counter on Molecule Man.",
            tl="Legendary Creature — Human Avatar",
            cmc=5.0,
        ),
        c(*SAPPHIRE_MEDALLION[:1], oracle=SAPPHIRE_MEDALLION[1], cmc=SAPPHIRE_MEDALLION[2]),
        c(*HAZORETS_MONUMENT[:1], oracle=HAZORETS_MONUMENT[1], cmc=HAZORETS_MONUMENT[2]),
        c(*OKETRAS_MONUMENT[:1], oracle=OKETRAS_MONUMENT[1], cmc=OKETRAS_MONUMENT[2]),
        mind_stone(),
        endbringer(),
        c(*CHROMATIC_LANTERN[:1], oracle=CHROMATIC_LANTERN[1], cmc=CHROMATIC_LANTERN[2]),
        c(*PROPHETIC_PRISM[:1], oracle=PROPHETIC_PRISM[1], cmc=PROPHETIC_PRISM[2]),
        c(
            "Ugin, the Ineffable",
            oracle="Colorless spells you cast cost {2} less to cast. "
            "-3: Destroy target permanent that's one or more colors.",
            tl="Legendary Planeswalker — Ugin",
            cmc=6.0,
        ),
        c(
            "Solemn Simulacrum",
            oracle="When Solemn Simulacrum enters the battlefield, you may search your "
            "library for a basic land card, put it onto the battlefield tapped, "
            "then shuffle.",
            tl="Artifact Creature — Golem",
            cmc=4.0,
        ),
        c(
            "Hedron Archive",
            oracle="{T}: Add {C}{C}. {2}, {T}, Sacrifice Hedron Archive: Draw two cards.",
            cmc=4.0,
        ),
        c(
            "Coveted Jewel",
            oracle="When Coveted Jewel enters the battlefield, draw three cards. "
            "{T}: Add three mana of any one color.",
            cmc=6.0,
        ),
        c(
            "Universal Solvent",
            oracle="{7}, {T}, Sacrifice Universal Solvent: Destroy target permanent.",
            cmc=1.0,
        ),
        c(
            "Spine of Ish Sah",
            oracle="When Spine of Ish Sah enters the battlefield, destroy target permanent.",
            cmc=7.0,
        ),
        c(
            "Meteor Golem",
            oracle="When Meteor Golem enters the battlefield, destroy target nonland "
            "permanent an opponent controls.",
            tl="Artifact Creature — Golem",
            cmc=7.0,
        ),
        c(
            "Oblivion Stone",
            oracle="{5}, {T}, Sacrifice Oblivion Stone: Destroy each nonland permanent "
            "without a fate counter on it, then remove all fate counters from all "
            "permanents.",
            cmc=3.0,
        ),
        c(
            "Eldritch Immunity",
            oracle="Target creature you control gains indestructible until end of turn.",
            tl="Instant",
            cmc=1.0,
        ),
        c(
            "Mikokoro, Center of the Sea",
            oracle="{2}, {T}: Each player draws a card.",
            tl="Legendary Land",
            cmc=0.0,
        ),
        c(
            "Geier Reach Sanitarium",
            oracle="{T}: Add {C}. {2}, {T}: Each player draws a card, then discards a card.",
            tl="Legendary Land",
            cmc=0.0,
        ),
    ]
    deck += [c(name, oracle="{T}: Add {C}.", cmc=2.0) for name in _GENERIC_ROCKS]
    deck += [c(name) for name in _FILLER_SPELLS]
    deck += [c(name, tl="Land", cmc=0.0) for name in _LANDS]
    assert len(deck) == 100
    return deck


def _deck_scores(deck):
    return {card.name: _score(card) for card in deck}


def test_molecule_man_dead_reducers():
    scores = _deck_scores(molecule_man_deck())
    for name in ("Sapphire Medallion", "Hazoret's Monument", "Oketra's Monument"):
        relevance, reason = scores[name]
        assert relevance == "very_low", name
        assert reason.startswith("Dead"), name


def test_molecule_man_high_priority_keeps():
    scores = _deck_scores(molecule_man_deck())
    for name in ("Endbringer", "Mikokoro, Center of the Sea", "Geier Reach Sanitarium"):
        assert scores[name][0] == "high", name
    assert scores["Mind Stone"][0] == "high"


def test_molecule_man_placeholders():
    scores = _deck_scores(molecule_man_deck())
    assert scores["Prophetic Prism"][0] == "low"
    assert scores["Chromatic Lantern"][0] == "low"


def test_molecule_man_plan_coverage():
    report = evaluate_plan_coverage(molecule_man_deck(), MOLECULE_MAN_PROFILE)
    assert report["lands"]["status"] == "ok"
    assert report["ramp"]["status"] == "over"
    for cat in ("topdeck_manipulation", "opponent_turn_draw", "large_payoffs", "protection"):
        assert report[cat]["status"] == "under", cat
    # counts/min/max are reported for every targeted category
    for entry in report.values():
        assert set(entry) == {"count", "min", "max", "status"}


def test_balanced_deck_has_no_flags():
    profile = {
        "color_identity": "colorless",
        "high": [],
        "medium": [],
        "low": [],
        "targets": {"lands": (2, 3), "ramp": (1, 2), "removal_wipes": (1, 2)},
    }
    deck = [
        c("Land A", tl="Land", cmc=0.0),
        c("Land B", tl="Land", cmc=0.0),
        c("Rock", oracle="{T}: Add {C}.", cmc=2.0),
        c("Zap", oracle="Destroy target creature.", tl="Instant", cmc=2.0),
    ]
    report = evaluate_plan_coverage(deck, profile)
    assert all(entry["status"] == "ok" for entry in report.values())
