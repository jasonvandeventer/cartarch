"""Sort rollout tests (v3.36.11) — the Session-B surfaces.

Covers the InventoryRow Python sorter shared by Decks + Locations
(``sort_inventory_rows``) and the Collection SQL paths added to
``list_inventory_rows`` (Rarity by CASE rank, Quantity Available by
InventoryRow.quantity). The Showcase/Share dict sorter is in
tests/test_sort_spec.py + tests/test_share_service.py.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import sort_spec
from app.db import Base
from app.inventory_service import list_inventory_rows
from app.models import Card, InventoryRow

_scryfall_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


# --- sort_inventory_rows (Decks / Locations) ---------------------------------


def _row(idx, name, *, cmc=None, rarity=None, price=None, qty=1, finish="normal", slot=""):
    card = SimpleNamespace(
        name=name,
        cmc=cmc,
        colors="",
        set_code="tst",
        collector_number="1",
        rarity=rarity,
        type_line="Creature",
        price_usd=price,
        price_usd_foil=None,
        price_usd_etched=None,
    )
    return SimpleNamespace(id=idx, card=card, finish=finish, quantity=qty, slot=slot)


def test_row_sorter_name_and_unknown_fallback():
    rows = [_row(1, "Bayou"), _row(2, "Aether Vial"), _row(3, "Counterspell")]
    asc = sort_spec.sort_inventory_rows(list(rows), "name", "asc")
    assert [r.card.name for r in asc] == ["Aether Vial", "Bayou", "Counterspell"]
    # Unknown key -> deterministic name/id fallback (not query order).
    fb = sort_spec.sort_inventory_rows(list(rows), "bogus", "asc")
    assert [r.card.name for r in fb] == ["Aether Vial", "Bayou", "Counterspell"]


def test_row_sorter_cmc_nulls_last():
    rows = [_row(1, "A", cmc=3.0), _row(2, "B", cmc=None), _row(3, "C", cmc=1.0)]
    desc = sort_spec.sort_inventory_rows(list(rows), "cmc", "desc")
    assert [r.card.cmc for r in desc] == [3.0, 1.0, None]


def test_row_sorter_price_zero_null_last_finish_aware():
    rows = [
        _row(1, "A", price="5.00"),
        _row(2, "B", price=None),
        _row(3, "C", price="12.50"),
        _row(4, "D", price="0.00"),
    ]
    desc = sort_spec.sort_inventory_rows(list(rows), "value", "desc")
    assert [r.card.name for r in desc] == ["C", "A", "B", "D"]


def test_row_sorter_available_is_quantity():
    rows = [_row(1, "A", qty=2), _row(2, "B", qty=9), _row(3, "C", qty=5)]
    desc = sort_spec.sort_inventory_rows(list(rows), "available", "desc")
    assert [r.quantity for r in desc] == [9, 5, 2]


def test_row_sorter_rarity_rank():
    rows = [
        _row(1, "A", rarity="mythic"),
        _row(2, "B", rarity="common"),
        _row(3, "C", rarity="rare"),
    ]
    asc = sort_spec.sort_inventory_rows(list(rows), "rarity", "asc")
    assert [r.card.rarity for r in asc] == ["common", "rare", "mythic"]


# --- Collection list_inventory_rows SQL paths --------------------------------


def _make_card_row(session, name, *, rarity=None, qty=1):
    card = Card(
        scryfall_id=f"sid-{next(_scryfall_seq)}",
        name=name,
        set_code="TST",
        collector_number="1",
        rarity=rarity,
    )
    session.add(card)
    session.flush()
    row = InventoryRow(
        card_id=card.id,
        user_id=1,
        quantity=qty,
        finish="normal",
        is_pending=False,
    )
    session.add(row)
    session.commit()
    return row


def test_collection_rarity_sort_is_by_rank_sql():
    s = _fresh_session()
    _make_card_row(s, "Mythic Card", rarity="mythic")
    _make_card_row(s, "Common Card", rarity="common")
    _make_card_row(s, "Rare Card", rarity="rare")
    _make_card_row(s, "Tokenish", rarity=None)  # unknown -> last
    rows, total = list_inventory_rows(s, user_id=1, sort="rarity", direction="asc")
    assert total == 4
    assert [r.card.rarity for r in rows] == ["common", "rare", "mythic", None]


def test_collection_available_sort_is_quantity_sql():
    s = _fresh_session()
    _make_card_row(s, "Two", qty=2)
    _make_card_row(s, "Nine", qty=9)
    _make_card_row(s, "Five", qty=5)
    rows, _ = list_inventory_rows(s, user_id=1, sort="available", direction="desc")
    assert [r.quantity for r in rows] == [9, 5, 2]
    rows_asc, _ = list_inventory_rows(s, user_id=1, sort="available", direction="asc")
    assert [r.quantity for r in rows_asc] == [2, 5, 9]
