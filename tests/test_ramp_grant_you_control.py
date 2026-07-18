"""#139 — the ramp tagger recognizes a mana ability granted to permanents YOU
CONTROL (Cryptolith Rite, Bootleggers' Stash), which the blanket quoted-ability
strip in `matches_ramp_non_land` used to hide. The scope word "you control" keeps
the fix from re-opening the false positives the strip exists for: token grants,
removal auras, opponent-controls auras, and equip grants stay suppressed.

Also #139 — the land-tutor pattern accepts "searches their library" (Collective
Voyage's group ramp) alongside "search your library".

Oracle texts are the real Scryfall text (pulled from prod 2026-07-18).
"""

from __future__ import annotations

from app.deck_service import _RAMP_LAND_RE, matches_ramp_non_land

# (oracle, is_ramp) — the discriminating cases.
GRANT_CASES = [
    # --- grant to permanents you control -> RAMP (the fix) ---
    ('Creatures you control have "{T}: Add one mana of any color."', True),  # Cryptolith Rite
    ('Lands you control have "{T}: Create a Treasure token."', True),  # Bootleggers' Stash
    (
        'Vigilance\nCreatures you control have "{T}: Add one mana of any color."\n'
        "When Enduring Vitality dies, ...",
        True,
    ),  # Enduring Vitality
    (
        "You may cast creature spells from the top of your library. "
        'Creatures you control have "{T}: Add one mana of any color."',
        True,
    ),  # Elven Chorus
    # --- NOT "you control" -> still suppressed (no regression) ---
    (
        'Enchant creature\nEnchanted creature has "{T}: Add one mana of any color."',
        False,
    ),  # Utopia Vow
    (
        'Enchanted permanent is a colorless land with "{T}: Add {C}" and loses all ...',
        False,
    ),  # Imprisoned
    (
        'Enchanted permanent is a Treasure artifact with "{T}, Sacrifice this artifact: '
        'Add one mana of any color," and it loses all other abilities.',
        False,
    ),  # Minimus Containment
    (
        "Whenever another nontoken creature you control dies, create a 1/1 token. "
        'It has "Sacrifice this token: Add {C}."',
        False,
    ),  # Sifter of Skulls — token grant
    (
        "Enchant creature an opponent controls\nEnchanted creature loses all abilities "
        'and is a Citizen with ... and "{T}: Add {C}" named Humble Merchant.',
        False,
    ),  # Honest Work — mana to opponent
    (
        'Enchant land\nEnchanted land has "{T}: Add one mana of any color."',
        False,
    ),  # Abundant Growth (residual)
    # granted ability that is NOT mana -> not ramp
    ('Creatures you control have "{T}: Draw a card."', False),
]


def test_grant_you_control_ramp_detection():
    for oracle, expected in GRANT_CASES:
        assert matches_ramp_non_land(oracle) is expected, oracle[:60]


def test_plain_ramp_still_detected():
    # the fix is additive — direct ramp is unchanged
    assert matches_ramp_non_land("{T}: Add {C}{C}.") is True
    assert matches_ramp_non_land("Add three mana of any one color.") is True
    # self cost-reduction still not ramp
    assert matches_ramp_non_land("This spell costs {7} less to cast if it targets you.") is False


def test_land_tutor_accepts_their_library():
    assert _RAMP_LAND_RE.search("Each player searches their library for up to X basic land cards")
    # existing "search your library" tutors unchanged
    assert _RAMP_LAND_RE.search("Search your library for up to two basic land cards, reveal them")
    assert _RAMP_LAND_RE.search(
        "Search your library for a Forest card and put it onto the battlefield"
    )
    # a non-land library search is still not a ramp-land match
    assert not _RAMP_LAND_RE.search(
        "Search your library for an instant card and put it into your hand"
    )
