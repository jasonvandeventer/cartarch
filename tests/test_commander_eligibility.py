"""Commander eligibility is judged on the FRONT FACE only.

`cards.type_line` stores Scryfall's ROOT type line, which for every multi-face
layout is `"Front // Back"`. The old predicate substring-matched the COMBINED
string, so any card with a legendary creature on its back qualified: Invasion of
Fiora is a Battle, The Legend of Kyoshi is a Saga, Westvale Abbey is a Land,
Nezumi Graverobber is a `flip` card. Eight owned cards across six accounts were
offered as commanders.

Outside the game a card has the characteristics of its front face (CR 712.4a), and
commander eligibility is judged there (CR 903.3a).

There was **zero** coverage of `can_be_commander` or `list_commander_candidates`
before this file — which is why a substring predicate on a field that has contained
`//` since Innistrad survived this long.

Every `type_line` and `oracle_text` below is a REAL string read from the production
`cards` table on 2026-07-27, not a synthesized one. The whole defect was that
synthesized single-faced strings never exercise the separator.
"""

from __future__ import annotations

import pytest

from app.models import Card
from app.recommendation_service import _front_face, can_be_commander

# Real oracle-text fragments (prod, 2026-07-27).
GRIST_ORACLE = (
    "As long as Grist isn't on the battlefield, it's a 1/1 Insect creature in "
    "addition to its other types.\n+1: Create a 1/1 black and green Insect "
    "creature token, then mill a card."
)
TEFERI_ORACLE = (
    "+1: Look at the top two cards of your library. Put one of them into your hand "
    "and the other on the bottom of your library.\n−10: You get an emblem with "
    '"You may activate loyalty abilities of planeswalkers any time you could cast '
    'an instant."\nTeferi, Temporal Archmage can be your commander.'
)

# (label, type_line, oracle_text, expected)
CASES = [
    # ── multi-face: back-face legend must NOT qualify ──────────────────────────
    (
        "transform battle back-face legend (Invasion of Fiora)",
        "Battle — Siege // Legendary Creature — Human Noble",
        "",
        False,
    ),
    (
        "transform saga back-face legend (The Legend of Kyoshi)",
        "Enchantment — Saga // Legendary Creature — Avatar",
        "",
        False,
    ),
    (
        "flip back-face legend (Nezumi Graverobber)",
        "Creature — Rat Rogue // Legendary Creature — Rat Wizard",
        "",
        False,
    ),
    (
        "land back-face legend (Westvale Abbey)",
        "Land // Legendary Creature — Demon",
        "",
        False,
    ),
    (
        "enchantment back-face legend (Sidequest: Hunt the Mark)",
        "Enchantment // Legendary Creature — Dragon",
        "",
        False,
    ),
    # ── multi-face: FRONT-face legend must still qualify ───────────────────────
    (
        "transform front-face legend (Grist, Voracious Larva)",
        "Legendary Creature — Insect // Legendary Planeswalker — Grist",
        "",
        True,
    ),
    (
        "adventure front-face legend (Kellan, the Fae-Blooded)",
        "Legendary Creature — Human Faerie // Sorcery — Adventure",
        "",
        True,
    ),
    (
        "prepare front-face legend (Lluwen, Exchange Student)",
        "Legendary Creature — Elf Druid // Sorcery",
        "",
        True,
    ),
    # ── the two oracle-driven branches ────────────────────────────────────────
    (
        "planeswalker carrying the phrase (Teferi, Temporal Archmage)",
        "Legendary Planeswalker — Teferi",
        TEFERI_ORACLE,
        True,
    ),
    (
        "Grist-class: creature card off the battlefield (Grist, the Hunger Tide)",
        "Legendary Planeswalker — Grist",
        GRIST_ORACLE,
        True,
    ),
    # ── single-faced controls ─────────────────────────────────────────────────
    ("Background enchantment (Master Chef)", "Legendary Enchantment — Background", "", False),
    (
        "Background that IS a creature (Faceless One)",
        "Legendary Enchantment Creature — Background",
        "",
        True,
    ),
    ("plain non-legendary creature (Abzan Falconer)", "Creature — Human Soldier", "", False),
    ("NULL type_line and oracle_text", None, None, False),
]


@pytest.mark.parametrize(
    "label,type_line,oracle,expected",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_commander_eligibility_matrix(label, type_line, oracle, expected):
    card = Card(scryfall_id=f"t-{abs(hash(label))}", name=label)
    card.type_line = type_line
    card.oracle_text = oracle
    assert can_be_commander(card) is expected, label


def test_every_card_in_the_reported_evidence_table_is_rejected():
    """The eight owned cards from the report, by their real stored type lines."""
    reported = [
        "Battle — Siege // Legendary Creature — Human Noble",
        "Battle — Siege // Legendary Creature — Serpent",
        "Enchantment // Legendary Creature — Dragon",
        "Enchantment — Saga // Legendary Creature — Avatar",  # Kuruk
        "Enchantment — Saga // Legendary Creature — Avatar",  # Kyoshi
        "Enchantment — Saga // Legendary Creature — Avatar",  # Yangchen
        "Land // Legendary Creature — Demon",
        "Creature — Rat Rogue // Legendary Creature — Rat Wizard",
    ]
    for i, tl in enumerate(reported):
        card = Card(scryfall_id=f"r-{i}", name=f"reported-{i}")
        card.type_line = tl
        card.oracle_text = ""
        assert can_be_commander(card) is False, tl


def test_front_face_helper_leaves_single_faced_lines_alone():
    assert _front_face("Legendary Creature — Human Soldier") == "legendary creature — human soldier"
    assert _front_face("Battle — Siege // Legendary Creature — Human Noble") == "battle — siege"
    assert _front_face(None) == ""
    assert _front_face("") == ""


def test_a_null_type_line_does_not_raise():
    card = Card(scryfall_id="null-1", name="unfetched")
    card.type_line = None
    card.oracle_text = None
    assert can_be_commander(card) is False


# ── Branch isolation: each clause must be the ONLY thing carrying its case ────
# A test that still passes with a clause deleted is not testing that clause.


def test_the_phrase_branch_is_the_only_thing_accepting_teferi():
    """Teferi's front face is a Planeswalker with no off-battlefield clause."""
    card = Card(scryfall_id="teferi", name="Teferi, Temporal Archmage")
    card.type_line = "Legendary Planeswalker — Teferi"
    card.oracle_text = TEFERI_ORACLE

    front = _front_face(card.type_line)
    assert not ("legendary" in front and "creature" in front), "clause 1 must not carry this case"
    assert "isn't on the battlefield" not in card.oracle_text.lower(), "clause 3 must not carry it"
    assert can_be_commander(card) is True


def test_the_grist_branch_is_the_only_thing_accepting_grist():
    """Grist's front face is a Planeswalker and it lacks the commander phrase."""
    card = Card(scryfall_id="grist", name="Grist, the Hunger Tide")
    card.type_line = "Legendary Planeswalker — Grist"
    card.oracle_text = GRIST_ORACLE

    front = _front_face(card.type_line)
    assert not ("legendary" in front and "creature" in front), "clause 1 must not carry this case"
    assert "can be your commander" not in card.oracle_text.lower(), "clause 2 must not carry it"
    assert can_be_commander(card) is True


def test_the_grist_clause_requires_legendary_on_the_FRONT_face():
    """A non-legendary card with the same off-battlefield wording stays rejected."""
    card = Card(scryfall_id="fake-grist", name="Not Legendary")
    card.type_line = "Planeswalker — Someone"
    card.oracle_text = "As long as it isn't on the battlefield, it's a 1/1 creature."
    assert can_be_commander(card) is False


def test_the_front_face_split_is_the_only_thing_rejecting_invasion_of_fiora():
    """Without the split, both clause-1 terms match the combined line."""
    combined = "Battle — Siege // Legendary Creature — Human Noble"
    assert "legendary" in combined.lower() and "creature" in combined.lower(), (
        "the combined line matches both terms — this is the whole defect"
    )
    card = Card(scryfall_id="fiora", name="Invasion of Fiora")
    card.type_line = combined
    card.oracle_text = ""
    assert can_be_commander(card) is False


def test_the_fix_does_not_read_the_layout_column():
    """`layout` is NULL on 15 rows; keying on it would reintroduce the bug there."""
    import ast
    import inspect

    from app import recommendation_service

    for fn in (recommendation_service.can_be_commander, recommendation_service._front_face):
        tree = ast.parse(inspect.getsource(fn).strip())
        node = tree.body[0]
        # Drop the docstring — it EXPLAINS why layout is not consulted, so a raw
        # source-text check matches the explanation instead of the logic.
        body = node.body[1:] if ast.get_docstring(node) else node.body
        code = "\n".join(ast.unparse(stmt) for stmt in body)
        assert "layout" not in code, f"{fn.__name__} reads cards.layout"
