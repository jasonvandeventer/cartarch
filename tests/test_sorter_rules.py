"""#104 sorter rule engine — validation, evaluation, and CRUD."""

from __future__ import annotations

import itertools

import pytest

from app import sorter_rule_service as srs
from app.inventory_service import resort_collection
from app.models import Card, InventoryRow, SorterRule, StorageLocation

_seq = itertools.count(1)


def _loc(db, user_id, name, type_="binder") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    db.add(loc)
    db.flush()
    return loc


def _row(db, user_id, *, name="Card", type_line="Creature — Goblin", price="0.10") -> InventoryRow:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="S",
        collector_number=str(next(_seq)),
        rarity="common",
        type_line=type_line,
        oracle_text="x",
        color_identity="",
        set_type="expansion",
        price_usd=price,
    )
    db.add(c)
    db.flush()
    r = InventoryRow(user_id=user_id, card_id=c.id, finish="normal", quantity=1, is_pending=True)
    db.add(r)
    db.flush()
    return r


# ── validation ───────────────────────────────────────────────────────────────


def test_validate_query():
    assert srs.validate_query("") is None  # empty = catch-all
    assert srs.validate_query("t:vampire") is None
    assert srs.validate_query("sol ring") is None  # bare name
    assert srs.validate_query("price:>10") is None
    assert srs.validate_query("foo:bar") is not None  # unknown key rejected


def test_has_sortable_setup_replaces_username_gate(db, user):
    from app.location_service import user_has_drawers

    # no rules, no drawers → not a sorter user (gate closed)
    assert srs.has_sortable_setup(db, user.id) is False
    assert user_has_drawers(db, user.id) is False
    # a rule alone opens the sorter (drawers not required)
    binder = _loc(db, user.id, "V")
    srs.create_sorter_rule(db, user.id, "t:vampire", binder.id)
    assert srs.has_sortable_setup(db, user.id) is True
    assert user_has_drawers(db, user.id) is False
    # a drawer alone also opens it
    _loc(db, user.id, "Drawer 1", type_="drawer")
    db.flush()
    assert user_has_drawers(db, user.id) is True


# ── evaluation ───────────────────────────────────────────────────────────────


def test_evaluate_first_match_wins_and_catch_all(db, user):
    binder = _loc(db, user.id, "Vampires")
    drawer = _loc(db, user.id, "Default", type_="drawer")
    vamp = _row(db, user.id, name="Vampire Nighthawk", type_line="Creature — Vampire")
    gob = _row(db, user.id, name="Goblin Guide", type_line="Creature — Goblin")
    # rule 1: vampires -> binder; rule 2 (catch-all): everything else -> drawer
    srs.create_sorter_rule(db, user.id, "t:vampire", binder.id)
    srs.create_sorter_rule(db, user.id, "", drawer.id)
    out = srs.evaluate_rules(db, user.id, {vamp.id, gob.id})
    assert out[vamp.id] == binder.id  # matched the specific rule
    assert out[gob.id] == drawer.id  # fell to the catch-all

    # first-match-wins: a broader earlier rule claims a card a later rule also matches
    db.query(SorterRule).delete()
    db.flush()
    a = _loc(db, user.id, "A")
    b = _loc(db, user.id, "B")
    srs.create_sorter_rule(db, user.id, "t:creature", a.id)  # pos 1, matches the vampire
    srs.create_sorter_rule(db, user.id, "t:vampire", b.id)  # pos 2, also matches
    out = srs.evaluate_rules(db, user.id, {vamp.id})
    assert out[vamp.id] == a.id  # earlier rule wins


def test_evaluate_ignores_inactive_rules(db, user):
    binder = _loc(db, user.id, "V")
    vamp = _row(db, user.id, type_line="Creature — Vampire")
    rule = srs.create_sorter_rule(db, user.id, "t:vampire", binder.id)
    srs.set_sorter_rule_active(db, user.id, rule.id, False)
    assert srs.evaluate_rules(db, user.id, {vamp.id}) == {}  # disabled → no match


# ── CRUD + reorder ───────────────────────────────────────────────────────────


def test_create_validates_query_and_target(db, user):
    loc = _loc(db, user.id, "L")
    with pytest.raises(ValueError):
        srs.create_sorter_rule(db, user.id, "foo:bar", loc.id)  # bad query
    with pytest.raises(ValueError):
        srs.create_sorter_rule(db, user.id, "t:vampire", 999999)  # not the user's location


def test_resort_rule_redirects_to_binder_unmatched_still_drawers(db, user):
    binder = _loc(db, user.id, "Vampires")  # non-drawer target
    d2 = _loc(db, user.id, "Drawer 2", type_="drawer")  # legacy fallback bucket
    vamp = _row(db, user.id, name="Vampire Nighthawk", type_line="Creature — Vampire")
    gob = _row(db, user.id, name="Goblin Guide", type_line="Creature — Goblin")
    gob.card.set_code = "dmu"  # a-d → drawer 2
    db.flush()
    srs.create_sorter_rule(db, user.id, "t:vampire", binder.id)

    resort_collection(db, user.id)
    db.refresh(vamp)
    db.refresh(gob)
    # matched → binder, no drawer/slot
    assert vamp.storage_location_id == binder.id
    assert vamp.drawer is None and vamp.slot is None
    # unmatched → legacy drawer sort
    assert gob.storage_location_id == d2.id
    assert gob.drawer == "2" and gob.slot == "1"


def test_locations_page_renders_and_create_route(client, db, user):
    loc = _loc(db, user.id, "Vampires")
    db.commit()
    # page renders with the rules section
    r = client.get("/locations")
    assert r.status_code == 200
    assert "Sorter rules" in r.text
    # create a rule via the route
    r = client.post(
        "/sorter-rules",
        data={"query": "t:vampire", "target_location_id": loc.id},
        follow_redirects=False,
    )
    assert r.status_code == 303
    rules = srs.list_sorter_rules(db, user.id)
    assert len(rules) == 1 and rules[0].query == "t:vampire"
    # a bad query redirects to the error banner, saves nothing
    r = client.post(
        "/sorter-rules",
        data={"query": "foo:bar", "target_location_id": loc.id},
        follow_redirects=False,
    )
    assert "rule_error" in r.headers["location"]
    assert len(srs.list_sorter_rules(db, user.id)) == 1


def test_reorder_and_delete(db, user):
    loc = _loc(db, user.id, "L")
    r1 = srs.create_sorter_rule(db, user.id, "t:vampire", loc.id)
    r2 = srs.create_sorter_rule(db, user.id, "t:goblin", loc.id)
    assert [r.id for r in srs.list_sorter_rules(db, user.id)] == [r1.id, r2.id]
    srs.move_sorter_rule(db, user.id, r2.id, "up")
    assert [r.id for r in srs.list_sorter_rules(db, user.id)] == [r2.id, r1.id]
    srs.delete_sorter_rule(db, user.id, r1.id)
    assert [r.id for r in srs.list_sorter_rules(db, user.id)] == [r2.id]
