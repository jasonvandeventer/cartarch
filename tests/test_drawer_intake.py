"""Drawer-vs-Bulk routing — Phase 3: intake routing (v3.38.0).

Call site B: on the drawer-sorter "auto-sort to drawers" import path, cheap
non-staple surplus is diverted to a Bulk location BEFORE ``resort_collection``
runs, so the sorter only ever places keepers. The predicate sits UPSTREAM of the
sorter (which is never modified). Behavior the owner asked for: "place into Bulk
on import when value < $1 and a copy of that printing is already in the drawers."

Covered:
  - ``split_intake_quantity`` — the shared keep-one decision (protected → all
    drawers; cheap + has drawer copy → all bulk; cheap + none → keep 1).
  - ``route_intake_to_bulk`` — diverts bulk-bound copies out of the pending pool
    (so resort never sees them), keeps the right number pending, lazily creates a
    ``manual`` Bulk location, reuses an existing one.
  - ``summarize_intake_routing`` — the preview ``(drawers, bulk)`` matches the
    commit decision.
  - reconcile-preview surfaces "→ Bulk" for a drawer-sorter auto-sort, and NOT
    for an explicit destination.
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.inventory_service import (
    route_intake_to_bulk,
    split_intake_quantity,
    summarize_intake_routing,
)
from app.models import Card, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _engine_sm():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _user(s, username="test") -> User:
    u = User(username=username, password_hash="x")
    s.add(u)
    s.flush()
    return u


def _card(s, name="Cheap", *, type_line="Creature — Goblin", price="0.10") -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="Test",
        collector_number=str(next(_seq)),
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


# -- split_intake_quantity ------------------------------------------------------


def test_split_protected_all_to_drawers():
    _e, sm = _engine_sm()
    s = sm()
    basic = _card(s, "Island", type_line="Basic Land — Island", price="0.05")
    assert split_intake_quantity(
        basic, "normal", basic.id, 4, has_drawer_copy=True, deck_card_ids=set()
    ) == (4, 0)
    valuable = _card(s, "Pricey", price="3.00")
    assert split_intake_quantity(
        valuable, "normal", valuable.id, 2, has_drawer_copy=True, deck_card_ids=set()
    ) == (2, 0)
    indeck = _card(s, "Staple", price="0.10")
    assert split_intake_quantity(
        indeck, "normal", indeck.id, 3, has_drawer_copy=True, deck_card_ids={indeck.id}
    ) == (3, 0)


def test_split_cheap_with_drawer_copy_all_to_bulk():
    _e, sm = _engine_sm()
    s = sm()
    c = _card(s, "Cheap", price="0.10")
    # The owner's stated rule: < $1 AND already a drawer copy -> all to bulk.
    assert split_intake_quantity(
        c, "normal", c.id, 3, has_drawer_copy=True, deck_card_ids=set()
    ) == (0, 3)


def test_split_cheap_without_drawer_copy_keeps_one():
    _e, sm = _engine_sm()
    s = sm()
    c = _card(s, "Cheap", price="0.10")
    assert split_intake_quantity(
        c, "normal", c.id, 3, has_drawer_copy=False, deck_card_ids=set()
    ) == (1, 2)


def test_split_zero_quantity():
    _e, sm = _engine_sm()
    s = sm()
    c = _card(s, "Cheap", price="0.10")
    assert split_intake_quantity(
        c, "normal", c.id, 0, has_drawer_copy=False, deck_card_ids=set()
    ) == (0, 0)


# -- route_intake_to_bulk -------------------------------------------------------


def test_route_diverts_when_drawer_copy_exists():
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    card = _card(s, "Cheap", price="0.10")
    _row(s, u.id, card, drawer.id, qty=1)  # existing findable drawer copy
    pending = _row(s, u.id, card, None, qty=2, pending=True)  # freshly imported
    s.commit()

    drawers_n, bulk_n = route_intake_to_bulk(s, u.id, [pending.id])
    assert (drawers_n, bulk_n) == (0, 2)
    # The whole import left the pending pool (resort never sees it): the row was
    # reassigned to Bulk and placed (is_pending=False), not deleted.
    assert s.query(InventoryRow).filter(InventoryRow.is_pending.is_(True)).count() == 0
    bulk = (
        s.query(StorageLocation)
        .filter(StorageLocation.user_id == u.id, StorageLocation.name == "Bulk")
        .one()
    )
    assert bulk.type != "deck" and bulk.mode == "manual"
    in_bulk = (
        s.query(InventoryRow)
        .filter(InventoryRow.storage_location_id == bulk.id, InventoryRow.card_id == card.id)
        .one()
    )
    assert in_bulk.quantity == 2 and in_bulk.is_pending is False


def test_route_keeps_one_when_no_drawer_copy():
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s)
    _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    card = _card(s, "Cheap", price="0.10")
    pending = _row(s, u.id, card, None, qty=3, pending=True)
    s.commit()

    drawers_n, bulk_n = route_intake_to_bulk(s, u.id, [pending.id])
    assert (drawers_n, bulk_n) == (1, 2)
    # One copy stays PENDING (the keeper) for resort to place.
    keeper = s.query(InventoryRow).get(pending.id)
    assert keeper.quantity == 1 and keeper.is_pending is True
    bulk = (
        s.query(StorageLocation)
        .filter(StorageLocation.name == "Bulk", StorageLocation.user_id == u.id)
        .one()
    )
    assert (
        s.query(InventoryRow).filter(InventoryRow.storage_location_id == bulk.id).one().quantity
        == 2
    )


def test_route_leaves_protected_untouched_no_bulk_created():
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s)
    _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    basic = _card(s, "Island", type_line="Basic Land — Island", price="0.05")
    valuable = _card(s, "Pricey", price="3.00")
    p_basic = _row(s, u.id, basic, None, qty=5, pending=True)
    p_val = _row(s, u.id, valuable, None, qty=2, pending=True)
    s.commit()

    drawers_n, bulk_n = route_intake_to_bulk(s, u.id, [p_basic.id, p_val.id])
    assert (drawers_n, bulk_n) == (7, 0)
    # Both still pending for the sorter; no Bulk location was created.
    assert s.query(InventoryRow).get(p_basic.id).is_pending is True
    assert s.query(InventoryRow).get(p_val.id).is_pending is True
    assert s.query(StorageLocation).filter(StorageLocation.name == "Bulk").count() == 0


def test_route_reuses_existing_bulk_location():
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    existing_bulk = _loc(s, u.id, "Bulk", type_="box", mode="manual")
    card = _card(s, "Cheap", price="0.10")
    _row(s, u.id, card, drawer.id, qty=1)
    pending = _row(s, u.id, card, None, qty=2, pending=True)
    s.commit()

    route_intake_to_bulk(s, u.id, [pending.id])
    assert s.query(StorageLocation).filter(StorageLocation.name == "Bulk").count() == 1
    assert (
        s.query(InventoryRow)
        .filter(InventoryRow.storage_location_id == existing_bulk.id)
        .one()
        .quantity
        == 2
    )


def test_route_empty_rows_noop():
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s)
    s.commit()
    assert route_intake_to_bulk(s, u.id, []) == (0, 0)


# -- summarize_intake_routing ---------------------------------------------------


def test_summarize_matches_commit_decision():
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s)
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    has_copy = _card(s, "Has Copy", price="0.10")
    _row(s, u.id, has_copy, drawer.id, qty=1)  # drawer copy exists
    no_copy = _card(s, "No Copy", price="0.10")
    basic = _card(s, "Island", type_line="Basic Land — Island", price="0.05")
    s.commit()

    matches = [
        {
            "card_id": has_copy.id,
            "finish": "normal",
            "recommended_action": "import_new",
            "recommended_new_qty": 3,
        },
        {
            "card_id": no_copy.id,
            "finish": "normal",
            "recommended_action": "import_new",
            "recommended_new_qty": 3,
        },
        {
            "card_id": basic.id,
            "finish": "normal",
            "recommended_action": "import_new",
            "recommended_new_qty": 4,
        },
        {
            "card_id": no_copy.id,
            "finish": "normal",
            "recommended_action": "skip_already_owned",
            "recommended_new_qty": 0,
        },
    ]
    # has_copy: 0/3 ; no_copy: 1/2 ; basic: 4/0 ; skip: ignored.
    assert summarize_intake_routing(s, u.id, matches) == (5, 5)


# -- reconcile-preview integration ----------------------------------------------


def _client(sm, user):
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    def _db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _db
    main.app.dependency_overrides[get_current_user] = lambda: user
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    return TestClient(main.app, follow_redirects=False), main.app


def _preview_form(card, qty, target_location_id):
    return {
        "target_location_id": str(target_location_id),
        "line_number": "1",
        "name": card.name,
        "scryfall_id": card.scryfall_id,
        "set_code": card.set_code,
        "collector_number": card.collector_number,
        "finish": "normal",
        "quantity": str(qty),
        "location": "",
        "csrf_token": "x",
    }


def test_reconcile_preview_shows_bulk_verdict_for_autosort():
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s, "test")  # a drawer-sorter username
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    card = _card(s, "Cheap", price="0.10")
    _row(s, u.id, card, drawer.id, qty=1)  # existing drawer copy -> incoming -> bulk
    s.commit()

    client, app = _client(sm, u)
    try:
        # Auto-sort (target 0): verdict line present.
        r = client.post("/import/reconcile-preview", data=_preview_form(card, 2, 0))
        assert r.status_code == 200
        assert "→ Bulk" in r.text
    finally:
        for dep in list(app.dependency_overrides):
            app.dependency_overrides.pop(dep, None)


def test_reconcile_preview_no_verdict_for_explicit_destination():
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s, "test")
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    box = _loc(s, u.id, "Some Box", type_="box")
    card = _card(s, "Cheap", price="0.10")
    _row(s, u.id, card, drawer.id, qty=1)
    s.commit()

    client, app = _client(sm, u)
    try:
        # Explicit destination (box): no auto-sort, so no routing verdict.
        r = client.post("/import/reconcile-preview", data=_preview_form(card, 2, box.id))
        assert r.status_code == 200
        assert "→ Bulk" not in r.text
    finally:
        for dep in list(app.dependency_overrides):
            app.dependency_overrides.pop(dep, None)


def test_non_drawer_sorter_user_has_no_verdict():
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s, "someone@else.com")  # NOT a drawer-sorter
    card = _card(s, "Cheap", price="0.10")
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.post("/import/reconcile-preview", data=_preview_form(card, 2, 0))
        assert r.status_code == 200
        assert "→ Bulk" not in r.text
    finally:
        for dep in list(app.dependency_overrides):
            app.dependency_overrides.pop(dep, None)


# -- commit-path wiring (end-to-end through the seam) ---------------------------


def test_commit_autosort_diverts_cheap_dupe_to_bulk():
    """The real seam: /import/commit on the auto-sort path calls
    route_intake_to_bulk BEFORE resort_collection, so a cheap dupe whose
    printing is already in the drawers lands in Bulk, never the drawers."""
    _e, sm = _engine_sm()
    s = sm()
    u = _user(s, "test")  # drawer-sorter
    drawer = _loc(s, u.id, "Drawer 2", type_="drawer", mode="managed")
    card = _card(s, "Cheap", price="0.10")  # non-stale -> persist resolves offline
    _row(s, u.id, card, drawer.id, qty=1)  # existing findable drawer copy
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.post(
            "/import/commit",
            data={
                "filename": "test.csv",
                "line_number": "1",
                "name": card.name,
                "scryfall_id": card.scryfall_id,
                "set_code": card.set_code,
                "collector_number": card.collector_number,
                "finish": "normal",
                "quantity": "2",
                "location": "",
                "language": "en",
                "target_location_id": "0",
                "csrf_token": "x",
            },
        )
        assert r.status_code in (200, 303)

        s2 = sm()
        bulk = (
            s2.query(StorageLocation)
            .filter(StorageLocation.user_id == u.id, StorageLocation.name == "Bulk")
            .one()
        )
        assert bulk.mode == "manual"  # never re-absorbed by the sorter
        in_bulk = (
            s2.query(InventoryRow)
            .filter(InventoryRow.storage_location_id == bulk.id, InventoryRow.card_id == card.id)
            .one()
        )
        # The 2 imported surplus copies are placed in Bulk (never the drawers).
        assert in_bulk.quantity == 2 and in_bulk.is_pending is False
        # Exactly one keeper remains outside Bulk — wherever the sorter parked it
        # (it relocates by set code; Drawer 2 was arbitrary). Total = keeper + 2.
        total_qty = sum(
            row.quantity for row in s2.query(InventoryRow).filter(InventoryRow.card_id == card.id)
        )
        assert total_qty == 3
        assert total_qty - in_bulk.quantity == 1
        s2.close()
    finally:
        for dep in list(app.dependency_overrides):
            app.dependency_overrides.pop(dep, None)
