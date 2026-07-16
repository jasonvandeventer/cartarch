"""Auto-tagger unit tests (Session C) — pure-logic, DB-free, fast.

Anchors expected role tags to **real Scryfall oracle text**, never name-pattern
matching (name-matching was the root of several tagger bugs). Covers the
documented failure patterns A–H from docs/tag_audit_findings.md plus the stable
intrinsic roles and the key false-positive guards.

Known-broken patterns are asserted at their CORRECT behaviour and marked
``xfail(strict=True)`` — a living punch-list: when the tagger regex is fixed the
test XPASSes and pytest fails, forcing removal of the marker. "Flip xfail → pass"
is the definition-of-done for each tagger fix.

invariant: architecture.md / docs/tag_audit_findings.md → auto-tagger role
classification is oracle-text-derived (suggest_card_roles)
"""

from __future__ import annotations

import pytest

from app.deck_service import matches_draw, matches_ramp_non_land, suggest_card_roles


class _Card:
    """Minimal stand-in: the tagger reads only name / type_line / oracle_text."""

    def __init__(self, oracle: str, type_line: str = "", name: str = "Test"):
        self.oracle_text = oracle
        self.type_line = type_line
        self.name = name


def roles(oracle: str, type_line: str = "") -> list[str]:
    return suggest_card_roles(_Card(oracle, type_line))


# ── Stable intrinsic roles (the "certain" oracle-text rules) ─────────────────


@pytest.mark.parametrize(
    "oracle,type_line,tag",
    [
        ("Destroy target creature.", "Instant", "Removal"),
        ("Destroy all creatures. They can't be regenerated.", "Sorcery", "Wipe"),
        (
            "Search your library for a card, then put that card into your hand. Then shuffle.",
            "Sorcery",
            "Tutor",
        ),
        (
            "Permanents you control gain hexproof and indestructible until end of turn.",
            "Instant",
            "Protection",
        ),
        (
            "Search your library for up to two basic land cards, reveal those cards, put one "
            "onto the battlefield tapped and the other into your hand, then shuffle.",
            "Sorcery",
            "Ramp",
        ),
        (
            "If a card or token would be put into a graveyard from anywhere, exile it instead.",
            "Enchantment",
            "Hate",
        ),
    ],
)
def test_intrinsic_role_present(oracle, type_line, tag):
    assert tag in roles(oracle, type_line)


# ── False-positive guards (the audit's "X is not Y" findings) ────────────────


def test_edict_is_removal_not_wipe():
    """A single/each-player edict answers one creature per opponent — Removal,
    not a sweeper (tag_audit Pattern F: 'Wipe' was wrongly applied to edicts)."""
    r = roles("Target player sacrifices a creature.", "Instant")
    assert "Removal" in r
    assert "Wipe" not in r


def test_trigger_draw_punisher_is_not_draw():
    """Sheoldred punishes drawing; it has no draw effect of its own. matches_draw
    must strip the trigger clause before deciding (Pattern E negative)."""
    sheoldred = (
        "Whenever you draw a card, you gain 2 life. "
        "Whenever an opponent draws a card, they lose 2 life."
    )
    assert matches_draw(sheoldred.lower()) is False
    assert "Draw" not in roles(sheoldred, "Legendary Creature — Phyrexian Praetor")


def test_quoted_token_ability_is_not_ramp():
    """Pattern H: Sifter of Skulls' mana ability lives in the TOKEN's quoted
    text; _QUOTED_ABILITY_RE strips it so Sifter itself isn't tagged Ramp."""
    sifter = (
        "Whenever another nontoken creature you control dies, create a 1/1 colorless "
        'Eldrazi Scion creature token. It has "Sacrifice this creature: Add {C}."'
    )
    assert matches_ramp_non_land(sifter.lower()) is False
    assert "Ramp" not in roles(sifter, "Creature — Eldrazi Horror")


def test_etb_pinger_is_not_threat():
    """Impact Tremors pings on ENTERS, not dies, and deals a flat 1 (not 'equal
    to power') — so neither the death-drain nor the ETB-damage Threat rule fires."""
    impact = (
        "Whenever a creature enters the battlefield under your control, "
        "Impact Tremors deals 1 damage to each opponent."
    )
    assert "Threat" not in roles(impact, "Enchantment")


# ── Pattern A — death-trigger drain → Threat (implemented) ───────────────────


def test_pattern_a_death_trigger_drain_is_threat():
    syr_konrad = (
        "Whenever another creature dies, or a creature card leaves your graveyard, "
        "Syr Konrad, the Grim deals 1 damage to each opponent. Whenever a creature an "
        "opponent controls dies, Syr Konrad, the Grim deals 1 damage to that player."
    )
    assert "Threat" in roles(syr_konrad, "Legendary Creature — Human Cleric")


# ── Pattern C — engine recognizers (implemented subset) ──────────────────────


@pytest.mark.parametrize(
    "name,oracle,type_line",
    [
        (
            "Greater Good (sac outlet)",
            "Sacrifice a creature: Draw cards equal to the sacrificed creature's power, "
            "then discard three cards.",
            "Enchantment",
        ),
        (
            "Sunbird's Invocation (free-cast)",
            "Whenever you cast a spell from your hand, reveal the top X cards of your library, "
            "where X is that spell's mana value. You may cast a spell with mana value X or less "
            "from among them without paying its mana cost.",
            "Enchantment",
        ),
        (
            "Luminous Broodmoth (non-graveyard recursion)",
            "Flying. Whenever a creature you control without flying dies, return that card to "
            "the battlefield with a flying counter on it.",
            "Creature — Insect",
        ),
    ],
)
def test_pattern_c_engine(name, oracle, type_line):
    assert "Engine" in roles(oracle, type_line)


# ── Pattern D — damage doubler → Threat (implemented) ────────────────────────


def test_pattern_d_damage_doubler_is_threat():
    gratuitous_violence = (
        "If a creature you control would deal damage to a permanent or player, "
        "it deals double that damage instead."
    )
    assert "Threat" in roles(gratuitous_violence, "Enchantment")


# ── Pattern E — "draw cards equal to" wording (implemented) ───────────────────


def test_pattern_e_draw_cards_equal_to_is_draw():
    greater_good = (
        "Sacrifice a creature: Draw cards equal to the sacrificed creature's power, "
        "then discard three cards."
    )
    assert "Draw" in roles(greater_good, "Enchantment")


def test_consequence_draw_still_detected():
    """Skullclamp's draw is the consequence of a death trigger (not the trigger
    condition) — it must still register as Draw (and Engine)."""
    skullclamp = (
        "Equipped creature gets +1/-1. Whenever equipped creature dies, draw two cards. Equip {1}"
    )
    r = roles(skullclamp, "Artifact — Equipment")
    assert "Draw" in r
    assert "Engine" in r


# ── Formerly the strict-xfail punch-list — both bugs FIXED in v3.36.10 ────────
# These two patterns shipped broken and were pinned as xfail(strict=True) in
# Session C; v3.36.10 fixed the underlying regexes (_THREAT_RE word order;
# _ENGINE_RE conditional tap-to-draw), so the xfail markers were removed and
# these are now ordinary passing assertions.


@pytest.mark.parametrize(
    "name,oracle",
    [
        (
            "Warstorm Surge",
            "Whenever a creature enters the battlefield under your control, it deals "
            "damage equal to its power to any target.",
        ),
        (
            "Terror of the Peaks",
            "Whenever another creature you control enters, Terror of the Peaks deals "
            "damage equal to that creature's power to any target.",
        ),
    ],
)
def test_pattern_d_etb_damage_equal_to_power_is_threat(name, oracle):
    assert "Threat" in roles(oracle, "Creature")


def test_pattern_c_conditional_tap_to_draw_is_engine():
    idol_of_oblivion = (
        "Whenever a token you control enters, put a charge counter on Idol of Oblivion. "
        "{T}: Draw a card. Activate this ability only if Idol of Oblivion has ten or more "
        "charge counters on it. {4}, {T}, Sacrifice Idol of Oblivion: Create a 10/10 "
        "colorless Eldrazi creature token."
    )
    assert "Engine" in roles(idol_of_oblivion, "Artifact")


# ── SELF cost-reduction is not Ramp (the "Not of This World" false positive) ──
#
# `_RAMP_NON_LAND_RE` used to carry a bare `costs? \{\d+\} less to cast`, which
# matched a card cheapening ITSELF. That is a discount on one spell, not mana
# acceleration. Because `suggest_card_roles` appends Ramp FIRST, a false Ramp
# landed at roles[0] and MASKED the card's real role downstream
# (classify_role_subtype reads roles[0]) — so a counterspell read as
# "Ramp / expensive_ramp". Oracle text below is verbatim from the live DB.

_SELF_REDUCERS = [
    pytest.param(
        "Counter target spell or ability that targets a permanent you control.\n"
        "This spell costs {7} less to cast if it targets a spell or ability that targets "
        "a creature you control with power 7 or greater.",
        "Instant",
        "Removal",
        id="not-of-this-world-counterspell",
    ),
    pytest.param(
        "This spell costs {1} less to cast for each creature on the battlefield.\n"
        "Blasphemous Act deals 13 damage to each creature.",
        "Sorcery",
        "Wipe",
        id="blasphemous-act-wipe",
    ),
    pytest.param(
        "This spell costs {1} less to cast for each instant and sorcery card in your graveyard.\n"
        "Ward {2} (Whenever this creature becomes the target of a spell or ability an opponent "
        "controls, counter it unless that player pays {2}.)",
        "Creature — Serpent",
        None,
        id="tolarian-terror-creature",
    ),
    pytest.param(
        "This spell costs {3} less to cast if you control a creature with power 4 or greater.\n"
        "Change the target of target spell or ability with a single target.",
        "Instant",
        None,
        id="bolt-bend-redirect",
    ),
]


@pytest.mark.parametrize("oracle,type_line,expected_role", _SELF_REDUCERS)
def test_self_cost_reduction_is_not_ramp(oracle, type_line, expected_role):
    """A card that discounts ITSELF is not ramp — it accelerates nothing."""
    got = roles(oracle, type_line)
    assert "Ramp" not in got, f"self-reducer wrongly tagged Ramp: {got}"
    if expected_role:
        assert expected_role in got
        assert got[0] == expected_role, (
            f"roles[0] must be the real role, not a masked one — got {got}. "
            "classify_role_subtype reads roles[0]."
        )


@pytest.mark.parametrize(
    "oracle,type_line",
    [
        # Affinity REMINDER text spells the self-reduction out in parentheses.
        # It is unquoted, so _QUOTED_ABILITY_RE never reached it.
        pytest.param(
            "Affinity for artifacts (This spell costs {1} less to cast for each artifact you "
            "control.)\nWhen Emry enters, mill four cards.",
            "Legendary Creature — Merfolk Wizard",
            id="emry-affinity-reminder",
        ),
        pytest.param(
            "Affinity for Forests (This spell costs {1} less to cast for each Forest you "
            "control.)\nLandfall — Whenever a land you control enters, create a 3/4 green "
            "Treefolk creature token with reach.",
            "Enchantment",
            id="sapling-nursery-affinity-reminder",
        ),
    ],
)
def test_affinity_reminder_text_is_not_ramp(oracle, type_line):
    assert "Ramp" not in roles(oracle, type_line)


@pytest.mark.parametrize(
    "oracle,type_line",
    [
        # Reduces OTHER spells → genuinely ramp. Must NOT regress.
        pytest.param(
            "Each spell you cast that's red or green costs {1} less to cast.",
            "Creature — Goblin Shaman",
            id="goblin-anarchomancer",
        ),
        pytest.param("Blue spells you cast cost {1} less to cast.", "Artifact", id="medallion"),
        pytest.param(
            "As this artifact enters, choose artifact, creature, enchantment, instant, or "
            "sorcery.\nSpells you cast of the chosen type cost {1} less to cast.",
            "Artifact",
            id="cloud-key",
        ),
        pytest.param(
            "As this artifact enters, choose a creature type.\n"
            "Creature spells you cast of the chosen type cost {1} less to cast.",
            "Artifact",
            id="heralds-horn",
        ),
    ],
)
def test_other_spell_cost_reduction_is_still_ramp(oracle, type_line):
    """Real cost-reduction ramp reduces OTHER spells — these must stay Ramp."""
    assert "Ramp" in roles(oracle, type_line)


@pytest.mark.parametrize(
    "oracle,type_line",
    [
        # GRANTED affinity: the reducer's subject sits in a PREVIOUS sentence, so
        # any subject-constrained pattern (`spells?[^.]{0,60}costs? \{N\} less`)
        # cannot reach it and silently drops these. Verbatim from the live DB.
        pytest.param(
            "Menace\nWhenever Don & Raph attack, the next noncreature spell you cast this "
            "turn has affinity for artifacts. (It costs {1} less to cast for each artifact "
            "you control.)",
            "Legendary Creature — Rat Ninja",
            id="don-and-raph-grants-affinity",
        ),
        pytest.param(
            "Lifelink\nEnchantment spells you cast have affinity for Auras. (They cost {1} "
            "less to cast for each Aura you control.)",
            "Legendary Creature — Fox Advisor",
            id="pearl-ear-grants-affinity",
        ),
        # Subject >60 chars from the reduction — same failure mode.
        pytest.param(
            "Spells you cast that refer to artifacts or Contraptions in their rules text "
            "cost {1} less to cast.",
            "Creature — Human Wizard",
            id="kindly-cognician-long-subject",
        ),
        # Not the word "spell" at all, but unambiguously reduces another card.
        pytest.param(
            "Your commander costs {1} less to cast for each time it's been cast from the "
            "command zone this game.",
            "Enchantment",
            id="myth-unbound-commander",
        ),
    ],
)
def test_granted_and_distant_subject_reducers_stay_ramp(oracle, type_line):
    """Regression guard for the rejected subject-constrained pattern.

    These reduce OTHER spells and are real ramp, but their subject is in a prior
    sentence or far from the reduction clause. Constraining the subject inside the
    ramp regex loses them: measured on the live DB it kept 82/85 owned and 236/255
    bulk cards, i.e. it dropped 3 owned + 20 bulk TRUE positives to block false
    ones that `_SELF_COST_REDUCTION_RE` already removes. The strip does the work.
    """
    assert "Ramp" in roles(oracle, type_line)


@pytest.mark.parametrize(
    "oracle,type_line",
    [
        pytest.param(
            "Affinity for Affinity (This card costs {1} less to cast for each ...)",
            "Artifact Creature",
            id="this-card-wording",
        ),
        pytest.param(
            "If you're on the Mirran team, this card costs {1} less to cast.",
            "Artifact Creature — Beast",
            id="unclaimed-tanadon-this-card",
        ),
    ],
)
def test_this_card_self_reduction_is_not_ramp(oracle, type_line):
    """Self-reduction phrased "this card" (not "this spell") is still not ramp."""
    assert "Ramp" not in roles(oracle, type_line)


def test_self_reduction_does_not_mask_a_real_reducer_on_the_same_card():
    """Stripping self-reduction must not blind the check to a REAL reducer."""
    oracle = (
        "This spell costs {2} less to cast if you control a Pirate.\n"
        "Artifact spells you cast cost {1} less to cast."
    )
    assert "Ramp" in roles(oracle, "Artifact")


def test_matches_ramp_non_land_self_reduction_directly():
    assert (
        matches_ramp_non_land("This spell costs {7} less to cast if it targets a spell.") is False
    )
    assert matches_ramp_non_land("Blue spells you cast cost {1} less to cast.") is True
