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


# ── KNOWN-BROKEN punch-list (assert correct + xfail strict) ───────────────────


@pytest.mark.xfail(
    strict=True,
    reason="tag_audit Pattern D: _THREAT_RE expects 'damage to <target> ... equal to "
    "power' word order, but the real oracle reads 'damage equal to its power to any "
    "target' — so the ETB-damage rule matches neither Warstorm Surge nor Terror of "
    "the Peaks. Fix the regex word order, then this XPASSes and the marker comes off.",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="tag_audit Pattern C: the conditional tap-to-draw engine (Idol of Oblivion) "
    "was recommended but never added to _ENGINE_RE — only 5 of the 6 recommended "
    "patterns shipped. Add the pattern, then this XPASSes and the marker comes off.",
)
def test_pattern_c_conditional_tap_to_draw_is_engine():
    idol_of_oblivion = (
        "Whenever a token you control enters, put a charge counter on Idol of Oblivion. "
        "{T}: Draw a card. Activate this ability only if Idol of Oblivion has ten or more "
        "charge counters on it. {4}, {T}, Sacrifice Idol of Oblivion: Create a 10/10 "
        "colorless Eldrazi creature token."
    )
    assert "Engine" in roles(idol_of_oblivion, "Artifact")
