"""Tests for the shared /collection filter unit (Phase 1 extraction).

The Collection grid page and the CSV export must share ONE parse of the filter
surface so they can never drift on what "the filtered collection" means. This
locks the extracted pieces in ``app/routes/collections.py``:

  - ``collection_filter`` (FastAPI dependency) — collapses the repeated facet
    params into joined tokens and parses the tolerant price strings.
  - ``_resolve_collection_scope`` — a drawer-type ``location_id`` becomes a
    drawer-name filter with the plain location scope cleared; a non-drawer
    location keeps its id; a missing location scopes to 0 (all).
  - ``_filtered_collection_query`` — resolves a ``CollectionFilter`` to the same
    base query the paginated grid uses (``build_collection_filter_query`` +
    scope), so the export set == the grid set.
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.inventory_service import list_inventory_rows
from app.models import Card, InventoryRow, StorageLocation, User
from app.routes.collections import (
    _filtered_collection_query,
    _resolve_collection_scope,
    collection_filter,
)

_seq = itertools.count(1)


def _engine_sm():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _user(s, username="u1") -> User:
    u = User(username=username, password_hash="x")
    s.add(u)
    s.flush()
    return u


def _loc(s, user_id, name, type_="binder") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    s.add(loc)
    s.flush()
    return loc


def _card(s, name, *, price="1.00", color_identity="G", colors="G") -> Card:
    card = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Creature",
        color_identity=color_identity,
        colors=colors,
        price_usd=price,
    )
    s.add(card)
    s.flush()
    return card


def _row(s, user_id, card, location_id, *, pending=False, qty=1) -> InventoryRow:
    row = InventoryRow(
        card_id=card.id,
        user_id=user_id,
        quantity=qty,
        finish="normal",
        is_pending=pending,
        storage_location_id=None if pending else location_id,
    )
    s.add(row)
    s.flush()
    return row


def test_collection_filter_parses_params():
    """The dependency collapses repeated facets and parses prices tolerantly."""
    f = collection_filter(
        colors=["W", "U"],
        types=["Creature", "Instant"],
        status=["owned"],
        finishes=["foil"],
        price_min=" 1.5 ",
        price_max="not-a-number",
    )
    assert f.colors == "WU"
    assert f.types == "Creature,Instant"
    assert f.status == "owned"
    assert f.finishes == "foil"
    assert f.facet_price_min == 1.5
    assert f.facet_price_max is None  # non-numeric → facet skipped
    assert f.price_max_raw == "not-a-number"  # raw echoed for the template

    # Already-joined toolbar/pagination token survives unchanged.
    assert collection_filter(colors=["WU"], types=[], status=[], finishes=[]).colors == "WU"


def test_resolve_scope_drawer_vs_location_vs_missing():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 3", type_="drawer")
    binder = _loc(s, u.id, "My Binder", type_="binder")
    s.commit()

    # Drawer → drawer-name filter, plain scope cleared.
    sel, drw, scope = _resolve_collection_scope(s, u.id, drawer.id)
    assert drw == "3" and scope == 0 and sel.id == drawer.id

    # Non-drawer → keeps its id as the scope, no drawer.
    sel, drw, scope = _resolve_collection_scope(s, u.id, binder.id)
    assert drw == "" and scope == binder.id

    # Missing / unowned location → scope 0 (all locations).
    sel, drw, scope = _resolve_collection_scope(s, u.id, 999999)
    assert sel is None and drw == "" and scope == 0


def test_filtered_query_equals_grid_set():
    """``_filtered_collection_query`` resolves to the SAME row set the grid page
    shows via ``list_inventory_rows`` — for a location_id + price_max combo."""
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    src = _loc(s, u.id, "Source Binder")
    other = _loc(s, u.id, "Other Binder")
    cheap = _row(s, u.id, _card(s, "Cheap", price="0.50"), src.id)
    pricey = _row(s, u.id, _card(s, "Pricey", price="9.00"), src.id)
    elsewhere = _row(s, u.id, _card(s, "Elsewhere Cheap", price="0.50"), other.id)
    s.commit()

    f = collection_filter(
        location_id=src.id,
        price_max="1",
        colors=[],
        types=[],
        status=[],
        finishes=[],
    )

    # Shared export path.
    base, _sel, _drw = _filtered_collection_query(s, u.id, f)
    export_ids = {rid for (rid,) in base.with_entities(InventoryRow.id).all()}

    # Grid page path (no pagination so we compare full sets).
    rows, _total = list_inventory_rows(
        s,
        user_id=u.id,
        location_id=src.id,
        facet_price_max=1.0,
        per_page=100,
    )
    grid_ids = {r.id for r in rows}

    assert export_ids == grid_ids == {cheap.id}
    assert pricey.id not in export_ids  # over the price cap
    assert elsewhere.id not in export_ids  # different location
