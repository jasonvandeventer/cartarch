"""Golden characterization of the drawer sorter (#104 safety net).

Pins assign_drawer() + drawer_sort_key() BEFORE the rule engine touches the sort
path, so the existing user's physical sort can't silently change. If a later
change alters these, that's a deliberate decision to review — not a surprise.
"""

from __future__ import annotations

import itertools

from app.inventory_service import assign_drawer, drawer_sort_key
from app.models import Card, InventoryRow

_seq = itertools.count(1)


def _row(
    db,
    user_id,
    *,
    set_code="mtg",
    type_line="Creature — Goblin",
    price="0.10",
    finish="normal",
    is_proxy=False,
    language="en",
    name="Card",
    collector="1",
) -> InventoryRow:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code=set_code,
        set_name="S",
        collector_number=collector,
        rarity="common",
        type_line=type_line,
        oracle_text="x",
        color_identity="",
        set_type="expansion",
        price_usd=price,
    )
    db.add(c)
    db.flush()
    r = InventoryRow(
        user_id=user_id,
        card_id=c.id,
        finish=finish,
        quantity=1,
        is_proxy=is_proxy,
        language=language,
        is_pending=False,
    )
    db.add(r)
    db.flush()
    r.card = c
    return r


def test_assign_drawer_pins_each_branch(db, user):
    u = user.id
    # oversized wins over value ($5+ Plane still to 6 — physical size)
    assert assign_drawer(_row(db, u, type_line="Plane — Mirrodin", price="50.00")) == 6
    # value >= $5 → drawer 1 (just-under stays in the letter range)
    assert assign_drawer(_row(db, u, price="5.00", set_code="afr")) == 1
    assert assign_drawer(_row(db, u, price="4.99", set_code="afr")) == 2
    # section-6 predicates
    assert assign_drawer(_row(db, u, is_proxy=True, set_code="afr")) == 6
    assert assign_drawer(_row(db, u, type_line="Token Creature — Zombie", set_code="afr")) == 6
    assert assign_drawer(_row(db, u, language="ja", set_code="afr")) == 6
    assert (
        assign_drawer(_row(db, u, type_line="Basic Land — Island", name="Island", set_code="afr"))
        == 6
    )
    # letter-range buckets for a cheap, English, non-basic card
    assert assign_drawer(_row(db, u, set_code="dmu")) == 2  # a-d
    assert assign_drawer(_row(db, u, set_code="eld")) == 3  # e-l
    assert assign_drawer(_row(db, u, set_code="mom")) == 4  # m-r
    assert assign_drawer(_row(db, u, set_code="snc")) == 5  # s-z
    assert assign_drawer(_row(db, u, set_code="40k")) == 6  # numeric first char
    assert assign_drawer(_row(db, u, set_code="")) == 6  # empty set code


def test_drawer_sort_key_orders_by_set_then_collector(db, user):
    u = user.id
    r1 = _row(db, u, set_code="aaa", collector="001")
    r2 = _row(db, u, set_code="aaa", collector="002")
    r3 = _row(db, u, set_code="bbb", collector="001")
    assert drawer_sort_key(r1) < drawer_sort_key(r2) < drawer_sort_key(r3)
