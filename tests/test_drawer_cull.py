"""Drawer-vs-Bulk routing — Phase 2: the retroactive cull (v3.38.0).

Call site A of the routing design: surplus copies of cheap, non-staple cards
sitting in the drawers are skimmed to a Bulk location, keeping ONE findable copy
of each printing. Rides the v3.36.9 bulk-move path
(``build_collection_filter_query`` scope), keeps one copy, explicit move to a
non-deck location, ``resort_collection`` NOT invoked.

Covered:
  - ``resolve_drawer_cull_candidates`` == the predicate's verdict (cheap qty>1
    drawer rows that aren't basic / in-deck / valuable); keepers + qty-1 + non-
    drawer rows excluded.
  - ``move_surplus_to_location``: keeps ``keep`` copies, moves the surplus,
    merges into an existing destination row OR creates a fresh one; no-op when
    quantity <= keep.
  - ``POST /collection/cull-to-bulk``: moves the surplus set, keeps one in the
    drawer, leaves non-candidates untouched, surfaces counts, REJECTS a deck
    destination, and does NOT call ``resort_collection``.
  - ``POST /collection/cull-preview``: renders the count without moving anything.
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.inventory_service import (
    move_surplus_to_location,
    resolve_drawer_cull_candidates,
)
from app.models import Card, InventoryRow, StorageLocation, User

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


def _card(
    s,
    name="Bulk Common",
    *,
    type_line="Creature — Goblin",
    price="0.10",
    set_code="tst",
    collector_number=None,
) -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code=set_code,
        set_name="Test",
        collector_number=str(next(_seq)) if collector_number is None else collector_number,
        rarity="common",
        type_line=type_line,
        oracle_text="x",
        image_url="http://x/img.png",
        color_identity="",
        set_type="expansion",
        price_usd=price,
    )
    s.add(c)
    s.flush()
    return c


def _loc(s, user_id, name, type_="box", mode="manual") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode=mode)
    s.add(loc)
    s.flush()
    return loc


def _row(s, user_id, card, loc_id, *, qty=1, finish="normal", pending=False) -> InventoryRow:
    row = InventoryRow(
        user_id=user_id,
        card_id=card.id,
        finish=finish,
        quantity=qty,
        is_pending=pending,
        storage_location_id=None if pending else loc_id,
    )
    s.add(row)
    s.flush()
    return row


# -- resolve_drawer_cull_candidates --------------------------------------------


def test_candidates_match_predicate_verdict():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    box = _loc(s, u.id, "Box", type_="box")
    deck = _loc(s, u.id, "Deck", type_="deck")

    cheap_dupe = _row(s, u.id, _card(s, "Cheap Dupe", price="0.10"), drawer.id, qty=3)
    basic = _row(
        s, u.id, _card(s, "Island", type_line="Basic Land — Island", price="0.05"), drawer.id, qty=4
    )
    valuable = _row(s, u.id, _card(s, "Pricey", price="2.00"), drawer.id, qty=2)
    # In a deck (also has a cheap drawer dupe) -> protected.
    staple_card = _card(s, "Command Tower", type_line="Land", price="0.15")
    _row(s, u.id, staple_card, deck.id, qty=1)
    in_deck_dupe = _row(s, u.id, staple_card, drawer.id, qty=2)
    # Singleton cheap dupe (qty 1) -> not a candidate (quantity > 1 gate).
    _row(s, u.id, _card(s, "Lone Cheap", price="0.10"), drawer.id, qty=1)
    # Cheap dupe in a BOX, not a drawer -> out of scope.
    _row(s, u.id, _card(s, "Box Dupe", price="0.10"), box.id, qty=5)
    s.commit()

    candidates = resolve_drawer_cull_candidates(s, u.id)
    ids = {r.id for r in candidates}
    assert ids == {cheap_dupe.id}, ids
    assert basic.id not in ids
    assert valuable.id not in ids
    assert in_deck_dupe.id not in ids


def test_candidates_threshold_is_parameterized():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 3", type_="drawer", mode="managed")
    fifty = _row(s, u.id, _card(s, "Fifty Cent", price="0.50"), drawer.id, qty=2)
    s.commit()

    # Default $1.00 -> 50c card is bulk-eligible.
    assert {r.id for r in resolve_drawer_cull_candidates(s, u.id)} == {fifty.id}
    # A 25c floor protects it.
    assert resolve_drawer_cull_candidates(s, u.id, price_threshold=0.25) == []


def test_candidates_empty_without_drawers():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    box = _loc(s, u.id, "Box", type_="box")
    _row(s, u.id, _card(s, "Cheap", price="0.10"), box.id, qty=4)
    s.commit()
    assert resolve_drawer_cull_candidates(s, u.id) == []


def test_candidates_scope_to_single_drawer():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    d2 = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    d3 = _loc(s, u.id, "Drawer 3", type_="drawer", mode="managed")
    in_d2 = _row(s, u.id, _card(s, "Cheap A", price="0.10"), d2.id, qty=3)
    in_d3 = _row(s, u.id, _card(s, "Cheap B", price="0.10"), d3.id, qty=3)
    box = _loc(s, u.id, "Box", type_="box")
    s.commit()

    # No scope -> every drawer.
    assert {r.id for r in resolve_drawer_cull_candidates(s, u.id)} == {in_d2.id, in_d3.id}
    # Scoped to drawer 2 only.
    assert {r.id for r in resolve_drawer_cull_candidates(s, u.id, location_id=d2.id)} == {in_d2.id}
    # A non-drawer location_id is ignored -> falls back to all drawers.
    assert {r.id for r in resolve_drawer_cull_candidates(s, u.id, location_id=box.id)} == {
        in_d2.id,
        in_d3.id,
    }


# -- move_surplus_to_location ---------------------------------------------------


def test_surplus_move_keeps_one_creates_dest_row():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    bulk = _loc(s, u.id, "Bulk", type_="box")
    card = _card(s, "Cheap", price="0.10")
    src = _row(s, u.id, card, drawer.id, qty=3)
    s.commit()

    moved = move_surplus_to_location(s, src.id, u.id, bulk.id, keep=1)
    assert moved == 2
    assert src.quantity == 1
    assert src.storage_location_id == drawer.id  # keeper stays in the drawer
    dest = (
        s.query(InventoryRow)
        .filter(InventoryRow.storage_location_id == bulk.id, InventoryRow.card_id == card.id)
        .one()
    )
    assert dest.quantity == 2
    assert dest.is_pending is False


def test_surplus_move_merges_into_existing_dest_row():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    bulk = _loc(s, u.id, "Bulk", type_="box")
    card = _card(s, "Cheap", price="0.10")
    src = _row(s, u.id, card, drawer.id, qty=4)
    existing = _row(s, u.id, card, bulk.id, qty=2)
    s.commit()

    moved = move_surplus_to_location(s, src.id, u.id, bulk.id, keep=1)
    assert moved == 3
    assert src.quantity == 1
    assert existing.quantity == 5  # 2 + 3 merged, no second bulk row
    assert (
        s.query(InventoryRow)
        .filter(InventoryRow.storage_location_id == bulk.id, InventoryRow.card_id == card.id)
        .count()
        == 1
    )


def test_surplus_move_noop_when_at_or_below_keep():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    bulk = _loc(s, u.id, "Bulk", type_="box")
    src = _row(s, u.id, _card(s, "Lone", price="0.10"), drawer.id, qty=1)
    s.commit()

    moved = move_surplus_to_location(s, src.id, u.id, bulk.id, keep=1)
    assert moved == 0
    assert src.quantity == 1
    assert s.query(InventoryRow).filter(InventoryRow.storage_location_id == bulk.id).count() == 0


# -- routes ---------------------------------------------------------------------


def _client(sm, user):
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    main.app.dependency_overrides[get_current_user] = lambda: user
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    return TestClient(main.app, follow_redirects=False), main.app


def _form(target_location_id, **over):
    form = {
        "target_location_id": target_location_id,
        "search": "",
        "colors": "",
        "types": "",
        "status": "",
        "finishes": "",
        "price_min": "",
        "price_max": "",
        "finish": "",
        "location_id": 0,
    }
    form.update(over)
    return form


def test_cull_route_moves_surplus_and_keeps_one():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    bulk = _loc(s, u.id, "Bulk", type_="box")
    cheap = _row(s, u.id, _card(s, "Cheap", price="0.10"), drawer.id, qty=3)
    valuable = _row(s, u.id, _card(s, "Pricey", price="2.00"), drawer.id, qty=2)
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.post("/collection/cull-to-bulk", data=_form(bulk.id))
        loc = r.headers.get("location", "")
        assert "bulk=culled" in loc
        assert "culled_copies=2" in loc
        assert "culled_cards=1" in loc

        s2 = sm()
        kept = s2.query(InventoryRow).get(cheap.id)
        assert kept.quantity == 1 and kept.storage_location_id == drawer.id
        in_bulk = (
            s2.query(InventoryRow)
            .filter(
                InventoryRow.storage_location_id == bulk.id,
                InventoryRow.card_id == cheap.card_id,
            )
            .one()
        )
        assert in_bulk.quantity == 2
        # Valuable dupe untouched.
        assert s2.query(InventoryRow).get(valuable.id).quantity == 2
        s2.close()
    finally:
        for dep in list(app.dependency_overrides):
            app.dependency_overrides.pop(dep, None)


def test_cull_route_rejects_deck_and_skips_resort():
    import app.routes.collections as collections_routes

    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    deck = _loc(s, u.id, "Deck", type_="deck")
    cheap = _row(s, u.id, _card(s, "Cheap", price="0.10"), drawer.id, qty=3)
    s.commit()

    called = {"resort": False}
    orig = collections_routes.resort_collection
    collections_routes.resort_collection = lambda *a, **k: called.__setitem__("resort", True)

    client, app = _client(sm, u)
    try:
        r = client.post("/collection/cull-to-bulk", data=_form(deck.id))
        loc = r.headers.get("location", "")
        assert "bulk=error" in loc and "reason=bad_target" in loc
        # Nothing moved into the deck.
        s2 = sm()
        assert s2.query(InventoryRow).get(cheap.id).quantity == 3
        assert (
            s2.query(InventoryRow).filter(InventoryRow.storage_location_id == deck.id).count() == 0
        )
        s2.close()
        assert called["resort"] is False
    finally:
        collections_routes.resort_collection = orig
        for dep in list(app.dependency_overrides):
            app.dependency_overrides.pop(dep, None)


def test_cull_route_does_not_resort_on_success():
    import app.routes.collections as collections_routes

    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    bulk = _loc(s, u.id, "Bulk", type_="box")
    _row(s, u.id, _card(s, "Cheap", price="0.10"), drawer.id, qty=3)
    s.commit()

    called = {"resort": False}
    orig = collections_routes.resort_collection
    collections_routes.resort_collection = lambda *a, **k: called.__setitem__("resort", True)

    client, app = _client(sm, u)
    try:
        client.post("/collection/cull-to-bulk", data=_form(bulk.id))
        assert called["resort"] is False
    finally:
        collections_routes.resort_collection = orig
        for dep in list(app.dependency_overrides):
            app.dependency_overrides.pop(dep, None)


def test_cull_preview_natural_collector_sort():
    """Cards sort by set then NATURAL collector number (1,2,10,11,100), grouped
    by set — not string sort (1,10,100,11,2) nor row-id order."""
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    bulk = _loc(s, u.id, "Bulk", type_="box")
    # Insert out of order, across two sets, so neither row-id nor string sort
    # would yield the expected numeric-within-set order.
    for set_code, num in [
        ("zzz", "2"),
        ("aaa", "100"),
        ("aaa", "2"),
        ("zzz", "10"),
        ("aaa", "10"),
        ("aaa", "1"),
    ]:
        _row(
            s,
            u.id,
            _card(s, f"{set_code}-{num}", price="0.10", set_code=set_code, collector_number=num),
            drawer.id,
            qty=3,
        )
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.post("/collection/cull-preview", data=_form(bulk.id))
        assert r.status_code == 200
        # Positions of each rendered "SET #N" cell, in document order.
        order = [
            r.text.index(f"{sc.upper()} #{num}</td>")
            for sc, num in [
                ("aaa", "1"),
                ("aaa", "2"),
                ("aaa", "10"),
                ("aaa", "100"),
                ("zzz", "2"),
                ("zzz", "10"),
            ]
        ]
        assert order == sorted(order), order
    finally:
        for dep in list(app.dependency_overrides):
            app.dependency_overrides.pop(dep, None)


def test_cull_preview_shows_count_without_moving():
    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    bulk = _loc(s, u.id, "Bulk", type_="box")
    cheap = _row(s, u.id, _card(s, "Cheap", price="0.10"), drawer.id, qty=3)
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.post("/collection/cull-preview", data=_form(bulk.id))
        assert r.status_code == 200
        assert "Bulk" in r.text
        # Preview MUST NOT move anything.
        s2 = sm()
        assert s2.query(InventoryRow).get(cheap.id).quantity == 3
        assert (
            s2.query(InventoryRow).filter(InventoryRow.storage_location_id == bulk.id).count() == 0
        )
        s2.close()
    finally:
        for dep in list(app.dependency_overrides):
            app.dependency_overrides.pop(dep, None)
