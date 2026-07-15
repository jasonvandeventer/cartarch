"""#121 — bracket floor: pure function over hard findings + declaration surface.

The floor is GC count / MLD / two-card combos ONLY; advisory signals (tutors,
fast mana, extra turns, combo-role inference) never raise it. Every floor
carries evidence citing exact cards.
"""

from __future__ import annotations

import itertools

from sqlalchemy import text

import app.legacy_tables  # noqa: F401 — registers the raw bracket tables on Base.metadata
from app.bracket_v2_service import (
    RULES_VERSION,
    compute_bracket_floor,
    estimate_bracket_v2,
    load_persisted_estimate,
    persist_estimate,
)
from app.models import Card, Deck, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _user(db) -> User:
    u = User(username=f"u{next(_seq)}@ex.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


def _deck(db, user_id, name="Deck") -> Deck:
    loc = StorageLocation(user_id=user_id, name=f"{name}-{next(_seq)}", type="deck", mode="managed")
    db.add(loc)
    db.flush()
    d = Deck(user_id=user_id, name=f"{name}-{next(_seq)}", storage_location_id=loc.id)
    db.add(d)
    db.flush()
    return d


def _add_card(db, user_id, deck, name, cmc=2.0) -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="S",
        collector_number=str(next(_seq)),
        rarity="rare",
        type_line="Creature",
        oracle_text="x",
        color_identity="",
        set_type="expansion",
        cmc=cmc,
    )
    db.add(c)
    db.flush()
    db.add(
        InventoryRow(
            user_id=user_id,
            card_id=c.id,
            finish="normal",
            quantity=1,
            storage_location_id=deck.storage_location_id,
            is_pending=False,
        )
    )
    db.flush()
    return c


def _mark_gc(db, name):
    db.execute(
        text(
            "INSERT INTO game_changer_cards (card_name, source, active, rules_version)"
            " VALUES (:n, 'test', :t, :v)"
        ),
        {"n": name, "t": True, "v": RULES_VERSION},
    )


def _tag(db, card_id, tag):
    db.execute(
        text("INSERT INTO card_tags (card_id, tag) VALUES (:c, :t)"),
        {"c": card_id, "t": tag},
    )


def _combo(names, bracket_tag=None, mana_value_needed=None):
    return {
        "included": [
            {
                "id": "x",
                "card_names": names,
                "bracket_tag": bracket_tag,
                "mana_value_needed": mana_value_needed,
            }
        ]
    }


# --- the floor rules ----------------------------------------------------------


def test_clean_deck_floors_at_2(db):
    u = _user(db)
    deck = _deck(db, u.id)
    _add_card(db, u.id, deck, "Vanilla Bear")
    db.commit()
    floor, findings = compute_bracket_floor(db, deck, u.id, None)
    assert floor == 2  # floor 1 does not exist computationally
    assert findings == []


def test_one_to_three_game_changers_floor_3(db):
    u = _user(db)
    deck = _deck(db, u.id)
    for n in ["Rhystic Study", "Smothering Tithe"]:
        _add_card(db, u.id, deck, n)
        _mark_gc(db, n)
    db.commit()
    floor, _ = compute_bracket_floor(db, deck, u.id, None)
    assert floor == 3


def test_four_game_changers_floor_4(db):
    u = _user(db)
    deck = _deck(db, u.id)
    for n in ["GC One", "GC Two", "GC Three", "GC Four"]:
        _add_card(db, u.id, deck, n)
        _mark_gc(db, n)
    db.commit()
    floor, _ = compute_bracket_floor(db, deck, u.id, None)
    assert floor == 4


def test_mass_land_denial_floor_4(db):
    u = _user(db)
    deck = _deck(db, u.id)
    c = _add_card(db, u.id, deck, "Armageddon")
    _tag(db, c.id, "mass_land_denial")
    db.commit()
    floor, _ = compute_bracket_floor(db, deck, u.id, None)
    assert floor == 4


def test_advisory_signals_never_raise_the_floor(db):
    u = _user(db)
    deck = _deck(db, u.id)
    for tag, name in [
        ("unconditional_tutor", "Demonic Tutor"),
        ("fast_mana", "Mana Crypt Lite"),
        ("free_interaction", "Force of Nope"),
        ("extra_turn", "Time Warp"),
        ("extra_turn", "Temporal Manipulation"),
        ("extra_turn", "Capture of Jingzhou"),
        ("stax", "Winter Orb"),
    ]:
        c = _add_card(db, u.id, deck, name)
        _tag(db, c.id, tag)
    db.commit()
    floor, findings = compute_bracket_floor(db, deck, u.id, None)
    assert floor == 2
    assert findings == []


def test_two_card_combo_spellbook_tag_rules_earliness(db):
    u = _user(db)
    deck = _deck(db, u.id)
    db.commit()
    # Ruthless tag = early -> floor 4
    floor, findings = compute_bracket_floor(db, deck, u.id, _combo(["A", "B"], bracket_tag="R"))
    assert floor == 4
    assert findings[0].finding_type == "two_card_combo_detected"
    assert "A + B" in findings[0].message
    # Core tag = not early -> floor 3
    floor, findings = compute_bracket_floor(db, deck, u.id, _combo(["A", "B"], bracket_tag="C"))
    assert floor == 3
    assert findings[0].severity == "warning"


def test_two_card_combo_mana_value_needed_proxy(db):
    u = _user(db)
    deck = _deck(db, u.id)
    db.commit()
    floor, _ = compute_bracket_floor(db, deck, u.id, _combo(["A", "B"], mana_value_needed=6))
    assert floor == 4
    floor, _ = compute_bracket_floor(db, deck, u.id, _combo(["A", "B"], mana_value_needed=7))
    assert floor == 3


def test_two_card_combo_legacy_payload_uses_combined_cmc_proxy(db):
    """Payloads persisted before bracket_tag/mana_value_needed fall back to the
    combined-MV proxy from the local cards table."""
    u = _user(db)
    deck = _deck(db, u.id)
    _add_card(db, u.id, deck, "Cheap A", cmc=2.0)
    _add_card(db, u.id, deck, "Cheap B", cmc=2.0)
    _add_card(db, u.id, deck, "Big A", cmc=5.0)
    _add_card(db, u.id, deck, "Big B", cmc=5.0)
    db.commit()
    floor, _ = compute_bracket_floor(db, deck, u.id, _combo(["Cheap A", "Cheap B"]))
    assert floor == 4  # 4 combined <= 6 -> early
    floor, _ = compute_bracket_floor(db, deck, u.id, _combo(["Big A", "Big B"]))
    assert floor == 3  # 10 combined -> not early
    # unknown pieces: can't verify earliness -> not early
    floor, _ = compute_bracket_floor(db, deck, u.id, _combo(["Ghost A", "Ghost B"]))
    assert floor == 3


def test_three_card_combos_do_not_floor(db):
    u = _user(db)
    deck = _deck(db, u.id)
    db.commit()
    floor, findings = compute_bracket_floor(
        db, deck, u.id, _combo(["A", "B", "C"], bracket_tag="R")
    )
    assert floor == 2
    assert findings == []


# --- persist / load round-trip -------------------------------------------------


def _seed_bracket_rules(db):
    for b in range(1, 6):
        db.execute(
            text(
                "INSERT INTO commander_bracket_rules (bracket, name, description,"
                " max_game_changers, allows_mass_land_denial, allows_extra_turn_chains,"
                " allows_two_card_combos, allows_combo_as_primary, competitive,"
                " rules_version, effective_date)"
                " VALUES (:b, :n, '', :gc, :x, :x, :x, :x, :x, :v, CURRENT_DATE)"
            ),
            {"b": b, "n": f"Tier {b}", "gc": 999 if b == 5 else b, "x": b == 5, "v": RULES_VERSION},
        )
    db.commit()


def test_estimate_persists_floor_and_split_findings(db):
    _seed_bracket_rules(db)
    u = _user(db)
    deck = _deck(db, u.id)
    for n in ["GC One", "GC Two"]:
        _add_card(db, u.id, deck, n)
        _mark_gc(db, n)
    tut = _add_card(db, u.id, deck, "Demonic Tutor")
    _tag(db, tut.id, "unconditional_tutor")
    db.commit()

    est = estimate_bracket_v2(db, deck, u.id, combos=_combo(["A", "B"], bracket_tag="R"))
    assert est.floor_bracket == 4  # early two-card combo dominates GC floor 3
    persist_estimate(db, deck.id, est)

    got = load_persisted_estimate(db, deck.id)
    assert got["floor_bracket"] == 4
    floor_types = {f["type"] for f in got["floor_findings"]}
    assert floor_types == {"game_changer_detected", "two_card_combo_detected"}
    # GC evidence names every card (the "what do I cut" list)
    gc = next(f for f in got["floor_findings"] if f["type"] == "game_changer_detected")
    assert "GC One" in gc["message"] and "GC Two" in gc["message"]
    advisory_types = {f["type"] for f in got["advisory_findings"]}
    assert "tutor_density" in advisory_types
    assert not advisory_types & floor_types


# --- declaration route + page --------------------------------------------------


def test_declare_bracket_route(client, db, user):
    deck = _deck(db, user.id)
    db.commit()

    resp = client.post(
        f"/decks/{deck.id}/declare-bracket",
        data={"declared_bracket": "3", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Deck, deck.id).declared_bracket == 3

    # clearing back to undeclared
    resp = client.post(
        f"/decks/{deck.id}/declare-bracket",
        data={"declared_bracket": "", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(Deck, deck.id).declared_bracket is None

    # out of range rejected
    resp = client.post(
        f"/decks/{deck.id}/declare-bracket",
        data={"declared_bracket": "6", "csrf_token": "x"},
    )
    assert resp.status_code == 400


def test_bracket_page_shows_violation_with_offending_cards(client, db, user):
    _seed_bracket_rules(db)
    deck = _deck(db, user.id)
    for n in ["GC One", "GC Two", "GC Three", "GC Four"]:
        _add_card(db, user.id, deck, n)
        _mark_gc(db, n)
    deck.declared_bracket = 2
    db.commit()

    est = estimate_bracket_v2(db, deck, user.id, combos=None)
    persist_estimate(db, deck.id, est)

    resp = client.get(f"/decks/{deck.id}/bracket")
    assert resp.status_code == 200
    assert "Declaration below the floor" in resp.text
    assert "GC Four" in resp.text  # exact cards cited
    assert "VERIFIED" in resp.text
    # the blended internals no longer render
    assert "Power score" not in resp.text
    assert "Mechanical signal" not in resp.text
    assert "Signal density" not in resp.text
