"""Unit tests for the tag-system theme matchers.

Standalone runner (no pytest dependency — see scripts/manual_test_*.py for
the established pattern). Invoke via:

    DATA_DIR=dev-data DEV_MODE=true python -m tests.test_deck_service

Each test function returns (passed, failed) tuples. The main runner
aggregates and exits non-zero on any failure.

Oracle texts used in the fixtures were copied from Scryfall via the local
helper; the comment above each fixture cites the card name only, the oracle
is the authoritative source-of-truth.
"""

from __future__ import annotations

import sys

from app.deck_service import card_matches_theme, extract_commander_themes


class MockCard:
    """Minimal Card stand-in for theme tests — only oracle_text, type_line,
    cmc are read by extract_commander_themes / card_matches_theme.
    """

    def __init__(self, oracle: str, type_line: str = "Creature", cmc: float = 3):
        self.oracle_text = oracle
        self.type_line = type_line
        self.cmc = cmc


class MockRow:
    def __init__(self, oracle: str, type_line: str = "Legendary Creature"):
        self.card = MockCard(oracle, type_line)
        self.role = "commander"


# ---------------------------------------------------------------------------
# Commander oracle fixtures (from Scryfall)
# ---------------------------------------------------------------------------

GORMA_ORACLE = (
    "Lifelink\n"
    "Whenever another creature you control dies, put a +1/+1 counter on Gorma.\n"
    "Nontoken creatures you control enter with an additional +1/+1 counter on "
    "them for each creature that died under your control this turn."
)

AUNTIE_OOL_ORACLE = (
    "Ward—Blight 2. (To blight 2, a player puts two -1/-1 counters on a "
    "creature they control.)\n"
    "Whenever one or more -1/-1 counters are put on a creature, draw a card "
    "if you control that creature. If you don't control it, its controller "
    "loses 1 life."
)

# Negative-control commander for the narrow Gorma-style lifegain rule.
# Teysa Karlov grants lifelink to tokens she creates — bare "lifelink" never
# appears unprefixed in her oracle — so the v3.23.5 rule must NOT extract a
# lifegain theme from her. (She legitimately has a death-trigger theme; that
# stays. This control guards only the lifegain narrowing.)
TEYSA_KARLOV_ORACLE = (
    "If a creature dying causes a triggered ability of a permanent you "
    "control to trigger, that ability triggers an additional time.\n"
    "Creature tokens you control have vigilance and lifelink."
)


def _gorma_themes() -> dict:
    return extract_commander_themes([MockRow(GORMA_ORACLE)])


def _auntie_ool_themes() -> dict:
    return extract_commander_themes([MockRow(AUNTIE_OOL_ORACLE)])


def _teysa_karlov_themes() -> dict:
    return extract_commander_themes([MockRow(TEYSA_KARLOV_ORACLE)])


# ---------------------------------------------------------------------------
# Test 1: commander theme extraction
# ---------------------------------------------------------------------------


def test_gorma_themes() -> tuple[int, int]:
    """Gorma's themes should include +1/+1 counters, death_triggers."""
    passed = failed = 0
    themes = _gorma_themes()
    mechs = themes["mechanics"]
    for required in ("plus_one_plus_one_counters", "death_triggers", "counters"):
        if required in mechs:
            print(f"  [OK] Gorma themes include '{required}'")
            passed += 1
        else:
            print(f"  [FAIL] Gorma themes missing '{required}' — got {sorted(mechs)}")
            failed += 1
    return passed, failed


def test_auntie_ool_themes() -> tuple[int, int]:
    """Auntie Ool's themes should include -1/-1 counters, blight."""
    passed = failed = 0
    themes = _auntie_ool_themes()
    mechs = themes["mechanics"]
    for required in ("minus_one_minus_one_counters", "blight"):
        if required in mechs:
            print(f"  [OK] Auntie Ool themes include '{required}'")
            passed += 1
        else:
            print(f"  [FAIL] Auntie Ool themes missing '{required}' — got {sorted(mechs)}")
            failed += 1
    return passed, failed


# ---------------------------------------------------------------------------
# Test 2: Gorma precon — synergy match expected True
# ---------------------------------------------------------------------------

GORMA_FIXTURES = [
    (
        "Blossoming Bogbeast",
        (
            "Whenever this creature attacks, you gain 2 life. Then creatures "
            "you control gain trample and get +X/+X until end of turn, where "
            "X is the amount of life you gained this turn."
        ),
        "Creature — Beast",
    ),
    (
        "Creakwood Liege",
        (
            "Other black creatures you control get +1/+1.\n"
            "Other green creatures you control get +1/+1.\n"
            "At the beginning of your upkeep, you may create a 1/1 black and "
            "green Worm creature token."
        ),
        "Creature — Horror",
    ),
    (
        "Defiling Daemogoth",
        (
            "Menace\n"
            "Whenever a creature you control deals combat damage to a player, "
            "you gain 1 life.\n"
            "At the beginning of your end step, each opponent loses X life, "
            "where X is the amount of life you gained this turn."
        ),
        "Creature — Demon",
    ),
    (
        "Immoral Bargain",
        (
            "As an additional cost to cast this spell, sacrifice X creatures.\n"
            "Destroy X target nonland permanents."
        ),
        "Sorcery",
    ),
    (
        "Jadar, Ghoulcaller of Nephalia",
        (
            "At the beginning of your end step, if you control no creatures "
            "with decayed, create a 2/2 black Zombie creature token with "
            "decayed. (It can't block. When it attacks, sacrifice it at end "
            "of combat.)"
        ),
        "Legendary Creature — Human Wizard",
    ),
    (
        "Ophiomancer",
        (
            "At the beginning of each upkeep, if you control no Snakes, "
            "create a 1/1 black Snake creature token with deathtouch."
        ),
        "Creature — Human Shaman",
    ),
    (
        # Adventure card — Scryfall stores both faces concatenated in
        # oracle_text. Test against the same shape the dev DB has.
        "Stensian Sanguinist // Exsanguinate",
        (
            "Whenever you attack, target creature gains deathtouch until end "
            "of turn. Whenever that creature deals combat damage to a player "
            "this combat, this creature becomes prepared. (While it's "
            "prepared, you may cast a copy of its spell. Doing so unprepares it.)"
            "\n\n"
            "Each opponent loses X life. You gain life equal to the life lost "
            "this way."
        ),
        "Creature — Vampire Cleric // Sorcery",
    ),
    (
        "Tendershoot Dryad",
        (
            "Ascend (If you control ten or more permanents, you get the city's "
            "blessing for the rest of the game.)\n"
            "At the beginning of each upkeep, create a 1/1 green Saproling "
            "creature token.\n"
            "Saprolings you control get +2/+2 as long as you have the city's "
            "blessing."
        ),
        "Creature — Dryad",
    ),
    (
        "Trudge Garden",
        (
            "Whenever you gain life, you may pay {2}. If you do, create a "
            "4/4 green Fungus Beast creature token with trample."
        ),
        "Enchantment",
    ),
    (
        "Veinwitch Coven",
        (
            "Menace\n"
            "Whenever you gain life, you may pay {B}. If you do, return "
            "target creature card from your graveyard to your hand."
        ),
        "Creature — Vampire Warlock",
    ),
]


def test_gorma_fixtures() -> tuple[int, int]:
    """All listed Gorma precon cards should match the Gorma synergy themes."""
    passed = failed = 0
    themes = _gorma_themes()
    for name, oracle, type_line in GORMA_FIXTURES:
        card = MockCard(oracle, type_line)
        matched = card_matches_theme(card, themes)
        if matched:
            print(f"  [OK] Gorma fixture '{name}' matches")
            passed += 1
        else:
            print(f"  [FAIL] Gorma fixture '{name}' did NOT match")
            failed += 1
    return passed, failed


# ---------------------------------------------------------------------------
# Test 3: Auntie Ool's deck — synergy match expected True
# ---------------------------------------------------------------------------

AUNTIE_OOL_FIXTURES = [
    (
        "Evolution Sage",
        (
            "Landfall — Whenever a land you control enters, proliferate. "
            "(Choose any number of permanents and/or players, then give each "
            "another counter of each kind already there.)"
        ),
        "Creature — Elf Druid",
    ),
    (
        "Ferrafor, Young Yew",
        (
            "When Ferrafor enters, create a number of 1/1 green Saproling "
            "creature tokens equal to the number of counters among creatures "
            "target player controls.\n"
            "{T}: Double the number of each kind of counter on target creature."
        ),
        "Legendary Creature — Treefolk Druid",
    ),
    (
        "Lasting Tarfire",
        (
            "At the beginning of each end step, if you put a counter on a "
            "creature this turn, this enchantment deals 2 damage to each opponent."
        ),
        "Enchantment",
    ),
    (
        "Puca's Covenant",
        (
            "Whenever a creature you control with a counter on it dies, you "
            "may return another target permanent card with mana value less "
            "than or equal to the number of counters on that creature from "
            "your graveyard to your hand. Do this only once each turn."
        ),
        "Enchantment",
    ),
    (
        "Eventide's Shadow",
        (
            "Remove any number of counters from among permanents on the "
            "battlefield. You draw cards and lose life equal to the number "
            "of counters removed this way."
        ),
        "Sorcery",
    ),
]


def test_auntie_ool_fixtures() -> tuple[int, int]:
    """All listed Auntie Ool cards should match the Auntie Ool synergy themes."""
    passed = failed = 0
    themes = _auntie_ool_themes()
    for name, oracle, type_line in AUNTIE_OOL_FIXTURES:
        card = MockCard(oracle, type_line)
        matched = card_matches_theme(card, themes)
        if matched:
            print(f"  [OK] Auntie Ool fixture '{name}' matches")
            passed += 1
        else:
            print(f"  [FAIL] Auntie Ool fixture '{name}' did NOT match")
            failed += 1
    return passed, failed


# ---------------------------------------------------------------------------
# Test 4: Negative controls — must NOT match
# ---------------------------------------------------------------------------

NEGATIVE_FIXTURES = [
    # (deck_themes_fn, card_name, oracle, type_line)
    (
        _gorma_themes,
        "Sol Ring (in Gorma)",
        "{T}: Add {C}{C}.",
        "Artifact",
    ),
    (
        _auntie_ool_themes,
        "Chain Reaction (in Auntie Ool)",
        (
            "Chain Reaction deals X damage to each creature, where X is the "
            "number of creatures on the battlefield."
        ),
        "Sorcery",
    ),
    (
        _auntie_ool_themes,
        "Tree of Perdition (in Auntie Ool)",
        ("Defender\n{T}: Exchange target opponent's life total with this creature's toughness."),
        "Creature — Plant",
    ),
    (
        _auntie_ool_themes,
        "Grave Titan (in Auntie Ool — no death_triggers/sacrifice theme)",
        (
            "Deathtouch\n"
            "Whenever this creature enters or attacks, create two 2/2 black "
            "Zombie creature tokens."
        ),
        "Creature — Giant",
    ),
]


def test_negative_fixtures() -> tuple[int, int]:
    """Negative-control fixtures should NOT match their respective decks."""
    passed = failed = 0
    for themes_fn, name, oracle, type_line in NEGATIVE_FIXTURES:
        themes = themes_fn()
        card = MockCard(oracle, type_line)
        matched = card_matches_theme(card, themes)
        if not matched:
            print(f"  [OK] Negative '{name}' correctly NOT matched")
            passed += 1
        else:
            print(f"  [FAIL] Negative '{name}' UNEXPECTEDLY matched")
            failed += 1
    return passed, failed


# ---------------------------------------------------------------------------
# Test 5: Teysa Karlov negative control — narrow lifegain rule must NOT fire
# ---------------------------------------------------------------------------


def test_teysa_lifegain_narrowing() -> tuple[int, int]:
    """The v3.23.5 narrow Gorma-style lifegain rule must NOT extract a
    lifegain theme from Teysa Karlov. Teysa has a death trigger AND grants
    lifelink to tokens, but never has bare lifelink on herself — exactly the
    edge case the bare-lifelink-vs-grant guard was built to handle.

    Teysa's legitimate themes (death_triggers, tokens) must still be present.
    """
    passed = failed = 0
    themes = _teysa_karlov_themes()
    mechs = themes["mechanics"]

    if "lifegain" not in mechs:
        print("  [OK] Teysa Karlov correctly does NOT extract 'lifegain'")
        passed += 1
    else:
        print(
            f"  [FAIL] Teysa Karlov UNEXPECTEDLY extracted 'lifegain' — got mechs {sorted(mechs)}"
        )
        failed += 1

    # Legitimate Teysa themes must still be extracted — guards against
    # over-eager rule narrowing that would regress her actual death-trigger /
    # token archetype.
    for required in ("death_triggers", "tokens"):
        if required in mechs:
            print(f"  [OK] Teysa Karlov themes include '{required}'")
            passed += 1
        else:
            print(f"  [FAIL] Teysa Karlov themes missing '{required}' — got {sorted(mechs)}")
            failed += 1

    return passed, failed


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all() -> int:
    total_p = total_f = 0
    suites = [
        ("Test 1: Commander theme extraction (Gorma)", test_gorma_themes),
        ("Test 1: Commander theme extraction (Auntie Ool)", test_auntie_ool_themes),
        ("Test 2: Gorma precon fixtures (should MATCH)", test_gorma_fixtures),
        ("Test 3: Auntie Ool fixtures (should MATCH)", test_auntie_ool_fixtures),
        ("Test 4: Negative controls (should NOT match)", test_negative_fixtures),
        (
            "Test 5: Teysa Karlov negative control (lifegain narrowing)",
            test_teysa_lifegain_narrowing,
        ),
    ]
    for title, fn in suites:
        print(f"\n=== {title} ===")
        p, f = fn()
        total_p += p
        total_f += f
    print(f"\n{'=' * 60}")
    print(f"TOTAL: {total_p} passed, {total_f} failed")
    return total_f


if __name__ == "__main__":
    sys.exit(run_all())
