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

import json

from app import deck_service
from app import recommendation_service as rec
from app.models import Card, InventoryRow
from app.recommendation_service import (
    CandidateCard,
    DeckBuildIntent,
    classify_role_subtype,
    evaluate_plan_coverage,
    score_candidate,
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


# --- P2: score_candidate integration ---------------------------------------------


def _molecule_man_commander():
    return c(
        "Molecule Man",
        oracle="Whenever you draw your first card during each turn, "
        "put a charge counter on Molecule Man.",
        tl="Legendary Creature — Human Avatar",
        cmc=5.0,
    )


def _cand(card):
    return CandidateCard(
        card=card,
        owned_quantity=1,
        available_quantity=1,
        best_inventory_row_id=None,
        already_in_deck_names=[],
        tags=deck_service.suggest_card_roles(card),
        theme_matches=[],
    )


def _static_score(card, profile=None):
    themes = rec.extract_themes(_molecule_man_commander())
    intent = DeckBuildIntent(commander_card_id=0)
    cand = _cand(card)
    score = score_candidate(cand, themes, intent, profile=profile)
    return score, cand


def test_score_candidate_penalizes_dead_reducer():
    dead_score, dead_cand = _static_score(sapphire(), MOLECULE_MAN_PROFILE)
    live_score, live_cand = _static_score(mind_stone(), MOLECULE_MAN_PROFILE)
    assert dead_cand.role_relevance == "very_low"
    assert live_cand.role_relevance == "high"
    assert dead_score < live_score
    # the dead penalty sinks it below any live card, and the reason surfaces
    assert dead_score < 0
    assert any(r.startswith("Dead") for r in dead_cand.reasons)


def test_score_candidate_boosts_priority_draw():
    end_score, end_cand = _static_score(endbringer(), MOLECULE_MAN_PROFILE)
    generic = c("Tome of Fables", oracle="{3}, {T}: Draw a card.", cmc=6.0)
    gen_score, gen_cand = _static_score(generic, MOLECULE_MAN_PROFILE)
    assert end_cand.role_relevance == "high"
    assert gen_cand.role_relevance == "medium"
    assert end_score > gen_score


def test_score_candidate_medium_is_baseline():
    disk = c("Nevinyrral's Disk", oracle="{1}, {T}: Destroy all creatures.", cmc=4.0)
    without, _ = _static_score(disk)
    with_profile, cand = _static_score(disk, MOLECULE_MAN_PROFILE)
    assert cand.role_relevance == "medium"
    assert with_profile == without


def test_score_candidate_reducer_in_identity_not_penalized():
    blue_cmdr = c(
        "Azure Sage",
        oracle="Whenever you draw a card, scry 1.",
        tl="Legendary Creature — Merfolk Wizard",
        cmc=3.0,
        color_identity="U",
    )
    profile = seed_strategy_profile(blue_cmdr, {"U"})
    without, _ = _static_score(sapphire())
    with_profile, cand = _static_score(sapphire(), profile)
    assert cand.role_subtype == ("Ramp", "cost_reduction")
    assert cand.role_relevance == "medium"
    assert with_profile == without


# --- P2: Molecule Man generation regression --------------------------------------


def _own_deck(db, user):
    """Persist the Molecule Man fixture as the user's collection; returns the
    commander Card."""
    deck = molecule_man_deck()
    for card in deck:
        db.add(card)
    db.flush()
    for card in deck:
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=card.id,
                quantity=1,
                is_pending=False,
                is_proxy=False,
            )
        )
    db.flush()
    return deck[0]


def test_molecule_man_generation_excludes_dead_reducers(db, user):
    cmdr = _own_deck(db, user)
    result = rec.generate_recommendation(db, user.id, DeckBuildIntent(commander_card_id=cmdr.id))
    picked = {cand.card.name for cand in result.mainboard + result.lands}
    for name in ("Sapphire Medallion", "Hazoret's Monument", "Oketra's Monument"):
        assert name not in picked, name
    # functional ramp and the priority draw pieces make the list instead
    for name in ("Endbringer", "Mind Stone", "Sol Ring"):
        assert name in picked, name
    # dead cards surface as explainable cuts, not silent drops
    cuts = {cand.card.name: cand for cand in result.cuts}
    assert "Sapphire Medallion" in cuts
    assert cuts["Sapphire Medallion"].role_relevance == "very_low"


# --- P3: deck analyzer -------------------------------------------------------------


def _make_deck_with_cards(db, user, cards, commander_card, name="Molecule Man EDH"):
    deck = deck_service.create_deck(db, user.id, name, format_name="commander")
    for card in cards:
        db.add(card)
    db.flush()
    for card in cards:
        db.add(
            InventoryRow(
                user_id=user.id,
                card_id=card.id,
                storage_location_id=deck.storage_location_id,
                quantity=1,
                is_pending=False,
                is_proxy=False,
                role="commander" if card is commander_card else None,
            )
        )
    db.commit()
    return deck


def _analyze_molecule_man(db, user):
    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    analysis = rec.analyze_deck(db, deck, user.id)
    db.commit()
    return deck, analysis


def test_analyze_deck_molecule_man(db, user):
    _, analysis = _analyze_molecule_man(db, user)
    statuses = {ac.card.name: ac.status for ac in analysis.cards}
    for name in ("Sapphire Medallion", "Hazoret's Monument", "Oketra's Monument"):
        assert statuses[name] == "cut", name
    for name in ("Endbringer", "Mind Stone", "Mikokoro, Center of the Sea"):
        assert statuses[name] == "keep", name
    # the commander isn't a gradable slot
    assert "Molecule Man" not in statuses
    under = {cat for cat, e in analysis.coverage.items() if e["status"] == "under"}
    assert {"large_payoffs", "protection", "win_conditions"} <= under
    assert analysis.coverage["ramp"]["status"] == "over"
    assert analysis.summary["cuts"] >= 3
    assert analysis.summary["total_cards"] == 100
    assert "not plan-complete" in analysis.summary["verdict"]


def test_analyze_deck_reducer_in_identity_is_keep(db, user):
    cmdr = c(
        "Azure Sage",
        oracle="Whenever you draw a card, scry 1.",
        tl="Legendary Creature — Merfolk Wizard",
        cmc=3.0,
        color_identity="U",
    )
    deck = _make_deck_with_cards(db, user, [cmdr, sapphire()], cmdr, name="Blue Deck")
    analysis = rec.analyze_deck(db, deck, user.id)
    (medallion,) = [ac for ac in analysis.cards if ac.card.name == "Sapphire Medallion"]
    assert medallion.status == "keep"
    assert medallion.subtype == "cost_reduction"


# --- P3: profile persistence -------------------------------------------------------


def test_get_or_seed_profile_persists_once(db, user):
    from app.models import DeckStrategyProfile

    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    first = rec.get_or_seed_profile(db, deck, cards[0])
    db.commit()
    rows = db.query(DeckStrategyProfile).filter_by(deck_id=deck.id).all()
    assert len(rows) == 1
    assert rows[0].is_custom is False
    second = rec.get_or_seed_profile(db, deck, cards[0])
    assert second == json.loads(rows[0].profile_data)
    assert first["high"] == second["high"]
    assert len(db.query(DeckStrategyProfile).filter_by(deck_id=deck.id).all()) == 1


def test_save_profile_sets_custom_and_reset_reseeds(db, user):
    from app.models import DeckStrategyProfile

    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    profile = rec.get_or_seed_profile(db, deck, cards[0])
    profile["high"] = ["opponent_turn_draw"]
    row = rec.save_profile(db, deck.id, profile)
    db.commit()
    assert row.is_custom is True
    assert json.loads(row.profile_data)["high"] == ["opponent_turn_draw"]

    rec.reset_profile(db, deck.id)
    db.commit()
    db.expire_all()  # the bulk delete bypasses the identity map (same session re-seeds below)
    assert db.query(DeckStrategyProfile).filter_by(deck_id=deck.id).first() is None
    rec.get_or_seed_profile(db, deck, cards[0])
    db.commit()
    fresh = db.query(DeckStrategyProfile).filter_by(deck_id=deck.id).first()
    assert fresh is not None
    assert fresh.is_custom is False


def test_delete_deck_removes_profile(db, user):
    import app.legacy_tables  # noqa: F401 — registers deck_bracket_* tables on Base.metadata
    from app.db import Base
    from app.models import DeckStrategyProfile

    Base.metadata.create_all(db.get_bind())  # raw deck_bracket_* tables delete_deck touches
    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    rec.get_or_seed_profile(db, deck, cards[0])
    db.commit()
    deck_id = deck.id
    assert deck_service.delete_deck(db, deck_id, user.id)
    assert db.query(DeckStrategyProfile).filter_by(deck_id=deck_id).first() is None


# --- P3: analyzer routes -----------------------------------------------------------


def test_analysis_route_owner_ok(db, user, client):
    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    resp = client.get(f"/decks/{deck.id}/analysis")
    assert resp.status_code == 200
    assert "Sapphire Medallion" in resp.text
    assert "not plan-complete" in resp.text


def test_analysis_route_non_owner_404(db, user, client):
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    other_deck = deck_service.create_deck(db, other.id, "Not Yours")
    resp = client.get(f"/decks/{other_deck.id}/analysis")
    assert resp.status_code == 404


def test_analysis_profile_save_route(db, user, client):
    from app.models import DeckStrategyProfile

    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    resp = client.post(
        f"/decks/{deck.id}/analysis/profile",
        data={
            "high": "topdeck manipulation, opponent_turn_draw",
            "medium": "ramp, removal",
            "low": "color_fixer",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/decks/{deck.id}/analysis"
    db.expire_all()
    row = db.query(DeckStrategyProfile).filter_by(deck_id=deck.id).first()
    assert row is not None
    assert row.is_custom is True
    saved = json.loads(row.profile_data)
    assert saved["high"] == ["topdeck_manipulation", "opponent_turn_draw"]
    assert saved["low"] == ["color_fixer"]


def test_analysis_profile_reset_route(db, user, client):
    from app.models import DeckStrategyProfile

    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    rec.get_or_seed_profile(db, deck, cards[0])
    db.commit()
    resp = client.post(f"/decks/{deck.id}/analysis/profile/reset", follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.query(DeckStrategyProfile).filter_by(deck_id=deck.id).first() is None


# --- P4: upgrade-by-need suggestions -------------------------------------------------


def _protection_card(name):
    return c(
        name,
        oracle="Target creature you control gains indestructible until end of turn.",
        tl="Artifact",
        cmc=2.0,
    )


def _loose(db, user, card, location=None):
    """Own a card outside any deck (optionally in a named non-deck location)."""
    db.add(card)
    db.flush()
    row = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        storage_location_id=location.id if location else None,
        quantity=1,
        is_pending=False,
        is_proxy=False,
    )
    db.add(row)
    db.flush()
    return row


def _box(db, user, name="Bulk Box"):
    from app.models import StorageLocation

    loc = StorageLocation(user_id=user.id, name=name, type="other", mode="manual")
    db.add(loc)
    db.flush()
    return loc


def test_suggest_upgrades_fills_under_need(db, user):
    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    box = _box(db, user)
    _loose(db, user, _protection_card("Guardian Idol"), box)
    off_color = c(
        "Dive Down",
        oracle="Target creature you control gains hexproof until end of turn.",
        tl="Instant",
        cmc=1.0,
        color_identity="U",
    )
    _loose(db, user, off_color, box)
    # a second loose copy of a card already IN the deck must not be suggested
    _loose(db, user, _protection_card("Eldritch Immunity"), box)
    db.commit()

    analysis = rec.analyze_deck(db, deck, user.id)
    db.commit()
    assert analysis.coverage["protection"]["status"] == "under"
    protection = analysis.upgrades_by_need["protection"]
    names = [s.card.name for s in protection]
    assert "Guardian Idol" in names
    assert "Dive Down" not in names  # off color identity
    assert "Eldritch Immunity" not in names  # already in the deck
    (idol,) = [s for s in protection if s.card.name == "Guardian Idol"]
    assert idol.fills_need == "protection"
    assert idol.relevance in ("medium", "high")
    assert idol.broad_role == "Protection"
    assert idol.in_other_deck is False
    assert idol.location == "Bulk Box"
    assert idol.quantity_available == 1


def test_suggest_upgrades_flags_cards_in_other_decks(db, user):
    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    committed = _protection_card("Loyal Bodyguard")
    other_deck = _make_deck_with_cards(db, user, [committed], None, name="Severance Package")
    db.commit()

    analysis = rec.analyze_deck(db, deck, user.id)
    db.commit()
    (bodyguard,) = [
        s for s in analysis.upgrades_by_need["protection"] if s.card.name == "Loyal Bodyguard"
    ]
    assert bodyguard.in_other_deck is True
    assert bodyguard.location == "Severance Package"
    assert other_deck.id != deck.id


def test_suggest_upgrades_caps_per_category(db, user):
    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    box = _box(db, user)
    for i in range(10):
        _loose(db, user, _protection_card(f"Ward Sphere {i}"), box)
    db.commit()

    analysis = rec.analyze_deck(db, deck, user.id)
    db.commit()
    assert len(analysis.upgrades_by_need["protection"]) == 5


def test_suggest_upgrades_empty_need_is_reported(db, user):
    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    db.commit()

    analysis = rec.analyze_deck(db, deck, user.id)
    db.commit()
    # win_conditions is under target and nothing owned fills it — empty, no error
    assert analysis.coverage["win_conditions"]["status"] == "under"
    assert analysis.upgrades_by_need["win_conditions"] == []


def test_analysis_route_renders_upgrades(db, user, client):
    cards = molecule_man_deck()
    deck = _make_deck_with_cards(db, user, cards, cards[0])
    _loose(db, user, _protection_card("Guardian Idol"), _box(db, user))
    db.commit()
    resp = client.get(f"/decks/{deck.id}/analysis")
    assert resp.status_code == 200
    assert "Upgrade suggestions" in resp.text
    assert "Guardian Idol" in resp.text
    assert "No owned cards match this need" in resp.text
