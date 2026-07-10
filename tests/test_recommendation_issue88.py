"""Issue #88 — auto-tagger coverage gaps + upgrade suggestion quality.

Regression target is the DNR deck (Anti-Venom, Horrifying Healer, mono-white
Voltron / damage-lock). Fixture oracle text is the REAL text pulled from the
live deck (tests/fixtures/dnr_deck.json), so classification is exercised against
production data, not idealized strings.

Kept separate from test_recommendation_v2.py so the Molecule Man regression
there stays byte-for-byte unmodified (issue #88 constraint).
"""

from __future__ import annotations

import json
import os

from app import deck_service
from app import recommendation_service as rec
from app.models import Card, InventoryRow
from app.recommendation_service import (
    _category_priority,
    _widen_target,
    classify_role_subtype,
    evaluate_plan_coverage,
)

_SID = [10_000]


def _card(d) -> Card:
    _SID[0] += 1
    return Card(
        scryfall_id=f"i88-{_SID[0]}",
        name=d["name"],
        set_code="tst",
        collector_number=str(_SID[0]),
        type_line=d["type_line"],
        oracle_text=d["oracle_text"],
        cmc=d["cmc"],
        color_identity=d["color_identity"],
        rarity=d.get("rarity"),
        price_usd=d.get("price_usd"),
    )


def _dnr_cards() -> list[Card]:
    path = os.path.join(os.path.dirname(__file__), "fixtures", "dnr_deck.json")
    with open(path) as fh:
        return [_card(d) for d in json.load(fh)]


W = {"W"}


def _by_name(cards) -> dict[str, Card]:
    return {c.name: c for c in cards}


# --- Problem 1 + 2: tagging coverage --------------------------------------------


def test_problem1_protection_cards_tagged():
    cards = _by_name(_dnr_cards())
    for name in (
        "Darksteel Plate",
        "Mithril Coat",
        "Shielded by Faith",
        "Spirit Mantle",
        "Angel's Grace",
    ):
        broad, _subtype, _ = classify_role_subtype(cards[name], W)
        assert broad == "Protection", name


def test_problem1_grasp_of_fate_is_removal():
    cards = _by_name(_dnr_cards())
    broad, subtype, _ = classify_role_subtype(cards["Grasp of Fate"], W)
    assert broad == "Removal"


def test_problem1_laezel_counter_doubler_is_engine():
    cards = _by_name(_dnr_cards())
    broad, subtype, _ = classify_role_subtype(cards["Lae'zel, Vlaakith's Champion"], W)
    assert (broad, subtype) == ("Engine", "counter_multiplier")


def test_type_scoped_reducer_is_ramp_not_color_specific():
    """The issue's premise that Flowering of the White Tree is a cost-reducer is
    wrong — the real card is an anthem (see test below). But the requested
    pattern is sound, so prove it on a card that actually has the text: a
    type-scoped ('legendary spells') reducer is Ramp/cost_reduction and must NOT
    trip the color_specific_reducer dead-flag (that fires on WUBRG words only)."""
    legendary_reducer = Card(
        scryfall_id="i88-lr",
        name="Herald of the Host",
        set_code="tst",
        collector_number="1",
        type_line="Legendary Enchantment",
        oracle_text="Legendary spells you cast cost {1} less to cast.",
        cmc=3.0,
        color_identity="W",
    )
    broad, subtype, _ = classify_role_subtype(legendary_reducer, W)
    assert broad == "Ramp"
    # The key requirement: a type-scoped reducer is NOT the dead color-specific
    # kind (that flag fires on WUBRG color words only), so it's never scored dead.
    assert subtype != "color_specific_reducer"


def test_real_flowering_is_an_anthem_not_ramp():
    """Contradiction check (issue Step 1d): the real Flowering of the White Tree
    is a creature anthem with no cost-reduction text, so it is NOT Ramp. It
    stays no-role, which is acceptable — the acceptance bar is 'no-role <= 5'."""
    cards = _by_name(_dnr_cards())
    broad, _subtype, _ = classify_role_subtype(cards["Flowering of the White Tree"], W)
    assert broad != "Ramp"


def test_problem2_damage_redirection_is_protection():
    cards = _by_name(_dnr_cards())
    for name in (
        "Pariah",
        "Pariah's Shield",
        "Saving Grace",
        "Phyrexian Vindicator",
        "Stuffy Doll",
    ):
        broad, subtype, _ = classify_role_subtype(cards[name], W)
        assert (broad, subtype) == ("Protection", "damage_redirection"), name


def test_problem2_voltron_equipment_is_threat():
    cards = _by_name(_dnr_cards())
    for name in (
        "Blackblade Reforged",
        "Maul of the Skyclaves",
        "Fireshrieker",
        "Ethereal Armor",
    ):
        broad, subtype, _ = classify_role_subtype(cards[name], W)
        assert (broad, subtype) == ("Threat", "voltron_buff"), name


def test_voltron_gate_requires_equipment_or_aura():
    """The voltron pattern is gated on Equipment/Aura type so board anthems and
    incidental '+1/+1' text on other permanents don't false-positive as Threat."""
    anthem = Card(
        scryfall_id="i88-an",
        name="Board Anthem",
        set_code="tst",
        collector_number="1",
        type_line="Enchantment",
        oracle_text="Creatures you control get +1/+1.",
        cmc=3.0,
        color_identity="W",
    )
    broad, _subtype, _ = classify_role_subtype(anthem, W)
    assert broad != "Threat"


def test_dnr_no_detected_role_count_and_win_conditions():
    cards = _dnr_cards()
    no_role_nonland = [
        c
        for c in cards
        if classify_role_subtype(c, W)[0] is None and "land" not in (c.type_line or "").lower()
    ]
    # Down from 22 untagged non-land cards to a small residue (the anthem and an
    # equipment-copier the vocabulary intentionally doesn't cover).
    assert len(no_role_nonland) <= 5, [c.name for c in no_role_nonland]

    threat = sum(1 for c in cards if classify_role_subtype(c, W)[0] == "Threat")
    assert threat >= 3  # was "Win conditions: 0"


# --- Problem 4: protection not flagged Over when High priority ------------------

# A Voltron/damage-lock deck: protection IS the plan, so it's High priority with
# a wide target. Every other category left at generic defaults.
DNR_PROFILE_HIGH_PROTECTION = {
    "color_identity": "W",
    "high": ["protection", "voltron_buff"],
    "medium": ["early_ramp", "ramp", "draw", "removal", "wipe", "engine"],
    "low": ["symmetrical_draw"],
    "targets": {**rec._DEFAULT_PLAN_TARGETS, "protection": (6, 15), "win_conditions": (3, 6)},
}


def test_problem4_protection_not_over_when_high_priority():
    cards = _dnr_cards()
    report = evaluate_plan_coverage(cards, DNR_PROFILE_HIGH_PROTECTION)
    # High priority widens protection's ceiling (15 -> 23), so the deck's heavy
    # protection suite is not flagged as redundant "Over".
    assert report["protection"]["status"] != "over"
    assert report["protection"]["max"] == 23  # ceil(15 * 1.5)


def test_problem4_same_deck_would_be_over_at_medium_priority():
    """Control: without the High-priority widening the same deck IS flagged
    Over — proving the widening is what prevents the false positive."""
    medium_profile = {
        **DNR_PROFILE_HIGH_PROTECTION,
        "high": ["voltron_buff"],  # protection demoted to medium
        "medium": DNR_PROFILE_HIGH_PROTECTION["medium"] + ["protection"],
    }
    report = evaluate_plan_coverage(_dnr_cards(), medium_profile)
    assert report["protection"]["status"] == "over"


# --- Problem 4: target range widening mechanics (6c) -----------------------------


def test_widen_target_high_is_1_5x():
    assert _widen_target(4, 8, "high") == (4, 12)  # issue example


def test_widen_target_low_is_0_75x():
    assert _widen_target(2, 4, "low") == (2, 3)  # issue example


def test_widen_target_medium_unchanged():
    assert _widen_target(4, 8, "medium") == (4, 8)


def test_widen_target_low_never_below_min():
    assert _widen_target(5, 5, "low") == (5, 5)


def test_category_priority_resolves_aliases():
    profile = {"high": ["removal"], "low": ["protection"], "medium": []}
    assert _category_priority("removal_wipes", profile) == "high"  # alias
    assert _category_priority("protection", profile) == "low"
    assert _category_priority("ramp", profile) == "medium"  # unlisted


def test_evaluate_plan_coverage_applies_priority():
    profile = {
        "color_identity": "W",
        "high": ["protection"],
        "medium": [],
        "low": ["removal"],
        "targets": {"protection": (4, 8), "removal_wipes": (8, 12), "ramp": (10, 14)},
    }
    report = evaluate_plan_coverage([], profile)
    assert (report["protection"]["min"], report["protection"]["max"]) == (4, 12)
    assert (report["removal_wipes"]["min"], report["removal_wipes"]["max"]) == (8, 9)
    assert (report["ramp"]["min"], report["ramp"]["max"]) == (10, 14)


# --- Problem 3: upgrade suggestion quality (6b) ----------------------------------


def _white_commander() -> Card:
    return Card(
        scryfall_id="i88-cmdr",
        name="Test White Commander",
        set_code="tst",
        collector_number="1",
        type_line="Legendary Creature — Human Soldier",
        oracle_text="Vigilance.",
        cmc=3.0,
        color_identity="W",
    )


def _removal(name, price, cmc, rarity="rare") -> Card:
    _SID[0] += 1
    return Card(
        scryfall_id=f"i88-{_SID[0]}",
        name=name,
        set_code="tst",
        collector_number=str(_SID[0]),
        type_line="Instant",
        oracle_text="Destroy target creature.",
        cmc=cmc,
        color_identity="W",
        rarity=rarity,
        price_usd=str(price),
    )


def _make_deck(db, user, cards, commander, name):
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
                role="commander" if card is commander else None,
            )
        )
    db.commit()
    return deck


def _loose(db, user, card, loc):
    db.add(card)
    db.flush()
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=card.id,
            storage_location_id=loc.id,
            quantity=1,
            is_pending=False,
            is_proxy=False,
        )
    )
    db.flush()


def _box(db, user):
    from app.models import StorageLocation

    loc = StorageLocation(user_id=user.id, name="Drawer", type="other", mode="manual")
    db.add(loc)
    db.flush()
    return loc


def _removal_upgrades(analysis):
    return {s.card.name for s in analysis.upgrades_by_need.get("removal_wipes", [])}


def test_upgrade_price_floor_excludes_bulk(db, user):
    """A $5+ removal category should not surface a $0.10 bulk card as an upgrade
    (4b). Fixture gives 'Farrel's Zealot' removal text so it's a valid candidate
    excluded ONLY by the price floor; a $2 removal in the same drawer IS kept."""
    cmdr = _white_commander()
    deck_cards = [cmdr, _removal("Premium Wrath", 8.0, 2), _removal("Costly Answer", 6.0, 3)]
    deck = _make_deck(db, user, deck_cards, cmdr, "Constructed Removal")
    box = _box(db, user)
    _loose(db, user, _removal("Farrel's Zealot", 0.10, 2, rarity="common"), box)
    _loose(db, user, _removal("Solid Answer", 2.0, 2), box)
    db.commit()

    analysis = rec.analyze_deck(db, deck, user.id)
    db.commit()
    assert analysis.coverage["removal_wipes"]["status"] == "under"
    names = _removal_upgrades(analysis)
    assert "Farrel's Zealot" not in names
    assert "Solid Answer" in names


def test_upgrade_cmc_filter_excludes_off_curve(db, user):
    """A low-curve removal category (avg CMC 2.5) should not surface a 6-CMC
    removal spell (4c); a 3-CMC one, within +2, is kept. Both drawer cards are
    priced above the floor so only CMC decides."""
    cmdr = _white_commander()
    deck_cards = [cmdr, _removal("Cheap Zap", 2.0, 2), _removal("Quick Answer", 2.0, 3)]
    deck = _make_deck(db, user, deck_cards, cmdr, "Low Curve")
    box = _box(db, user)
    _loose(db, user, _removal("Ponderous Removal", 3.0, 6), box)
    _loose(db, user, _removal("On-Curve Removal", 3.0, 3), box)
    db.commit()

    analysis = rec.analyze_deck(db, deck, user.id)
    db.commit()
    names = _removal_upgrades(analysis)
    assert "Ponderous Removal" not in names
    assert "On-Curve Removal" in names
