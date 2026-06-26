"""Tests for the collection-aware deck recommendation engine (issue #51).

Covers legality / color-identity filtering, singleton + basic-land rules,
proxy handling, deck-location penalty, need-aware + theme scoring, exact-100
assembly, and Brew creation without physical moves.
"""

from __future__ import annotations

import json

import pytest

from app import recommendation_service as rec
from app.models import Card, InventoryRow, StorageLocation
from app.recommendation_service import CandidateCard, DeckBuildIntent

_SID = [0]


def make_card(
    db,
    name,
    *,
    color_identity="G",
    type_line="Creature",
    oracle="",
    cmc=2.0,
    mana_cost="{G}",
    commander="legal",
):
    _SID[0] += 1
    legalities = json.dumps({"commander": commander}) if commander is not None else None
    card = Card(
        scryfall_id=f"sid-{_SID[0]}",
        name=name,
        set_code="tst",
        collector_number=str(_SID[0]),
        color_identity=color_identity,
        type_line=type_line,
        oracle_text=oracle,
        cmc=cmc,
        mana_cost=mana_cost,
        legalities=legalities,
    )
    db.add(card)
    db.flush()
    return card


def own(db, user, card, qty=1, location=None, is_proxy=False):
    row = InventoryRow(
        user_id=user.id,
        card_id=card.id,
        quantity=qty,
        is_pending=False,
        is_proxy=is_proxy,
        storage_location_id=location.id if location else None,
    )
    db.add(row)
    db.flush()
    return row


def deck_loc(db, user, name="Other Deck"):
    loc = StorageLocation(user_id=user.id, name=name, type="deck", mode="manual")
    db.add(loc)
    db.flush()
    return loc


def commander_card(db, **kw):
    kw.setdefault("type_line", "Legendary Creature — Insect")
    kw.setdefault("oracle", "Other Insects you control get +1/+1.")
    kw.setdefault("color_identity", "G")
    return make_card(db, "Cmdr", **kw)


def _intent(cmd, **kw):
    return DeckBuildIntent(commander_card_id=cmd.id, **kw)


# --- legality + color identity ------------------------------------------------


def test_rejects_off_color(db, user):
    cmd = commander_card(db)  # identity G
    off = make_card(db, "Lightning", color_identity="R")
    own(db, user, cmd)
    own(db, user, off)
    pool = rec.build_candidate_pool(db, user.id, cmd, _intent(cmd))
    assert "Lightning" not in {c.card.name for c in pool}


def test_allows_colorless(db, user):
    cmd = commander_card(db)
    sol = make_card(db, "Sol Ring", color_identity="", type_line="Artifact")
    own(db, user, cmd)
    own(db, user, sol)
    pool = rec.build_candidate_pool(db, user.id, cmd, _intent(cmd))
    assert "Sol Ring" in {c.card.name for c in pool}


def test_excludes_commander_illegal(db, user):
    cmd = commander_card(db)
    banned = make_card(db, "Black Lotus", commander="banned")
    notlegal = make_card(db, "Conspiracy", commander="not_legal")
    own(db, user, cmd)
    own(db, user, banned)
    own(db, user, notlegal)
    names = {c.card.name for c in rec.build_candidate_pool(db, user.id, cmd, _intent(cmd))}
    assert "Black Lotus" not in names
    assert "Conspiracy" not in names


def test_commander_unknown_identity_fails_gracefully(db, user):
    cmd = commander_card(db, color_identity=None)
    own(db, user, cmd)
    card, warnings = rec.validate_commander(db, user.id, cmd.id)
    assert card is None
    assert warnings


def test_commander_must_be_owned(db, user):
    cmd = commander_card(db)  # not owned
    card, warnings = rec.validate_commander(db, user.id, cmd.id)
    assert card is None
    assert "don't own" in warnings[0]


# --- proxies ------------------------------------------------------------------


def test_excludes_proxies_by_default(db, user):
    cmd = commander_card(db)
    proxy = make_card(db, "Proxy Bear")
    own(db, user, cmd)
    own(db, user, proxy, is_proxy=True)
    pool = rec.build_candidate_pool(db, user.id, cmd, _intent(cmd))
    assert "Proxy Bear" not in {c.card.name for c in pool}


def test_allow_proxies_true(db, user):
    cmd = commander_card(db)
    proxy = make_card(db, "Proxy Bear")
    own(db, user, cmd)
    own(db, user, proxy, is_proxy=True)
    pool = rec.build_candidate_pool(db, user.id, cmd, _intent(cmd, allow_proxies=True))
    assert "Proxy Bear" in {c.card.name for c in pool}


# --- deck-location penalty ----------------------------------------------------


def test_penalizes_card_in_another_deck(db, user):
    cmd = commander_card(db)
    loc = deck_loc(db, user, "Existing Deck")
    in_deck = make_card(db, "Committed Bear")
    own(db, user, cmd)
    own(db, user, in_deck, location=loc)
    [cand] = rec.build_candidate_pool(db, user.id, cmd, _intent(cmd))
    assert cand.already_in_deck_names == ["Existing Deck"]
    assert any("another deck" in r for r in cand.reasons)


def test_use_cards_in_other_decks_removes_penalty(db, user):
    cmd = commander_card(db)
    loc = deck_loc(db, user, "Existing Deck")
    in_deck = make_card(db, "Committed Bear")
    own(db, user, cmd)
    own(db, user, in_deck, location=loc)
    [cand] = rec.build_candidate_pool(db, user.id, cmd, _intent(cmd, use_cards_in_other_decks=True))
    assert not any("another deck" in r for r in cand.reasons)


# --- scoring ------------------------------------------------------------------


def test_need_boost_increases_role_card_score(db, user):
    cmd = commander_card(db)
    card = make_card(db, "Rampant Growth", oracle="x")
    themes = rec.extract_themes(cmd)
    cand = CandidateCard(
        card=card,
        owned_quantity=1,
        available_quantity=1,
        best_inventory_row_id=None,
        already_in_deck_names=[],
        tags=["Ramp"],
        theme_matches=[],
    )
    base = rec.score_candidate(cand, themes, _intent(cmd))
    needed = rec.score_candidate(cand, themes, _intent(cmd), needs={"Ramp": 5})
    assert needed > base


def test_theme_match_scores_higher_than_offtheme(db, user):
    cmd = commander_card(db)  # cares about Insects
    insect = make_card(db, "Giant Insect", type_line="Creature — Insect")
    beast = make_card(db, "Plain Beast", type_line="Creature — Beast")
    themes = rec.extract_themes(cmd)
    ci = CandidateCard(insect, 1, 1, None, [], [], [])
    cb = CandidateCard(beast, 1, 1, None, [], [], [])
    rec.score_candidate(ci, themes, _intent(cmd))
    rec.score_candidate(cb, themes, _intent(cmd))
    assert ci.score > cb.score
    assert any("Insect" in r for r in ci.reasons)


# --- assembly -----------------------------------------------------------------


def _seed_full_collection(db, user, cmd):
    """65 in-color creatures + a stack of Forests — enough for a 100-card deck."""
    own(db, user, cmd)
    for i in range(65):
        own(db, user, make_card(db, f"Creature {i:02d}"))
    forest = make_card(
        db, "Forest", color_identity="G", type_line="Basic Land — Forest", mana_cost=""
    )
    own(db, user, forest, qty=60)


def test_assembles_exactly_100_including_commander(db, user):
    cmd = commander_card(db)
    _seed_full_collection(db, user, cmd)
    out = rec.generate_recommendation(db, user.id, _intent(cmd))
    assert out.total_cards == 100, out.warnings
    # commander present, exactly once, role-marked at head
    assert out.mainboard[0].card.id == cmd.id
    assert out.role_counts  # computed without crashing


def test_no_duplicate_nonbasic_names(db, user):
    cmd = commander_card(db)
    own(db, user, cmd)
    # two distinct printings (card rows) of the same name
    a = make_card(db, "Reprinted Bear")
    b = make_card(db, "Reprinted Bear")
    own(db, user, a)
    own(db, user, b)
    themes = rec.extract_themes(cmd)
    pool = rec.build_candidate_pool(db, user.id, cmd, _intent(cmd))
    spells, lands, cuts = rec.assemble_deck(cmd, pool, _intent(cmd), themes)
    chosen_names = [c.card.name for c in spells]
    assert chosen_names.count("Reprinted Bear") == 1


def test_basic_land_duplicates_allowed(db, user):
    cmd = commander_card(db)
    _seed_full_collection(db, user, cmd)
    out = rec.generate_recommendation(db, user.id, _intent(cmd))
    forest = next(c for c in out.lands if c.card.name == "Forest")
    assert forest.deck_quantity > 1  # duplicates permitted for basics


def test_loose_copy_not_penalized_for_owned_duplicate(db, user):
    # owns 1 loose copy AND 1 committed to another deck → the loose copy must
    # NOT be penalized (red-team defect 4).
    cmd = commander_card(db)
    loc = deck_loc(db, user, "Existing Deck")
    bear = make_card(db, "Dual Bear")
    own(db, user, cmd)
    own(db, user, bear)  # loose
    own(db, user, bear, location=loc)  # committed
    [cand] = rec.build_candidate_pool(db, user.id, cmd, _intent(cmd))
    assert cand.available_quantity == 1
    assert not any("penalty" in r for r in cand.reasons)
    assert any("Loose copy available" in r for r in cand.reasons)


def test_need_reason_persisted_on_chosen_card(db, user):
    # a Ramp card the deck needs should carry the need-aware reason after
    # assembly (red-team defect 2 — the reason was being discarded).
    cmd = commander_card(db)
    _seed_full_collection(db, user, cmd)
    ramp = make_card(
        db, "Cultivate", type_line="Sorcery", oracle="Search your library for a basic land"
    )
    own(db, user, ramp)
    out = rec.generate_recommendation(db, user.id, _intent(cmd))
    picked = next((c for c in out.mainboard if c.card.name == "Cultivate"), None)
    assert picked is not None
    assert any(r.startswith("Helps deck need:") for r in picked.reasons)


def test_colorless_commander_uses_wastes(db, user):
    cmd = make_card(
        db,
        "Colorless Cmdr",
        color_identity="",
        type_line="Legendary Creature — Eldrazi",
        oracle="Annihilator 2",
        mana_cost="{10}",
    )
    own(db, user, cmd)
    wastes = make_card(
        db, "Wastes", color_identity="", type_line="Basic Land — Wastes", mana_cost=""
    )
    own(db, user, wastes, qty=40)
    out = rec.generate_recommendation(db, user.id, _intent(cmd))
    assert any(c.card.name == "Wastes" for c in out.lands)


def test_basics_capped_at_owned_quantity(db, user):
    # only 5 Forests owned → the deck must not assign more than 5, and must NOT
    # warn about impossible counts (red-team defect 1).
    cmd = commander_card(db)
    own(db, user, cmd)
    for i in range(65):
        own(db, user, make_card(db, f"Creature {i:02d}"))
    forest = make_card(
        db, "Forest", color_identity="G", type_line="Basic Land — Forest", mana_cost=""
    )
    own(db, user, forest, qty=5)
    out = rec.generate_recommendation(db, user.id, _intent(cmd))
    forest_cand = next((c for c in out.lands if c.card.name == "Forest"), None)
    if forest_cand:
        assert forest_cand.deck_quantity <= 5
    assert not any("copies of" in w for w in out.warnings)


# --- Brew creation ------------------------------------------------------------


def test_create_brew_does_not_move_physical_inventory(db, user):
    cmd = commander_card(db)
    _seed_full_collection(db, user, cmd)
    out = rec.generate_recommendation(db, user.id, _intent(cmd))

    before = {
        r.id: r.storage_location_id
        for r in db.query(InventoryRow).filter(InventoryRow.user_id == user.id).all()
    }
    before_proxy_count = (
        db.query(InventoryRow)
        .filter(InventoryRow.user_id == user.id, InventoryRow.is_proxy.is_(True))
        .count()
    )

    deck = rec.create_brew_from_recommendation(db, user.id, out, "My Brew")
    assert deck.is_brew is True

    # every original physical row is untouched (same location, not moved)
    after = {
        r.id: r.storage_location_id
        for r in db.query(InventoryRow)
        .filter(InventoryRow.user_id == user.id, InventoryRow.id.in_(before.keys()))
        .all()
    }
    assert after == before

    # the brew's rows are all proxy/planning rows in the deck's location
    deck_rows = (
        db.query(InventoryRow)
        .filter(InventoryRow.storage_location_id == deck.storage_location_id)
        .all()
    )
    assert deck_rows
    assert all(r.is_proxy for r in deck_rows)
    assert db.query(InventoryRow).filter(
        InventoryRow.user_id == user.id, InventoryRow.is_proxy.is_(True)
    ).count() == before_proxy_count + len(deck_rows)
    # exactly one commander row
    assert sum(1 for r in deck_rows if r.role == "commander") == 1


# --- route smoke --------------------------------------------------------------


def test_routes_smoke(client, db, user):
    cmd = commander_card(db)
    _seed_full_collection(db, user, cmd)
    db.commit()

    assert client.get("/recommendations/commander").status_code == 200
    assert client.get(f"/recommendations/commander/{cmd.id}/preview").status_code == 200
    resp = client.post(
        f"/recommendations/commander/{cmd.id}/create-brew",
        data={"deck_name": "Routed Brew"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/decks/" in resp.headers["location"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
