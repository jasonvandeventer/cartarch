"""Tests for the filter-scoped Collection bulk actions (v3.x).

Pytest module (matches tests/test_share_service):

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_collection_bulk.py

The feature: "add / move all cards matching the current /collection filter"
(not just the visible 50-row page). The matching set is resolved server-side
from the SAME filter params the grid uses via
``inventory_service.build_collection_filter_query`` — so it equals what the
user sees — and only PLACED (non-pending) rows are acted on, with the matching
pending rows counted and surfaced. Covered:

  - ``build_collection_filter_query`` resolves the exact set (search composes;
    pending separable) and ``list_inventory_rows`` is behaviour-preserved after
    the extraction.
  - ``POST /collection/bulk-add-showcase`` adds only the placed matches;
    pending excluded + surfaced in the redirect.
  - ``POST /collection/bulk-move`` reassigns ``storage_location_id`` for the
    placed matches, leaves pending untouched, REJECTS a deck-type destination,
    and does NOT trigger ``resort_collection``.
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.inventory_service import build_collection_filter_query, list_inventory_rows
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


def _loc(s, user_id, name, type_="binder") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    s.add(loc)
    s.flush()
    return loc


def _card(s, name, color_identity, colors) -> Card:
    card = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Creature",
        color_identity=color_identity,
        colors=colors,
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


def _seed(s):
    """A green/red collection: 2 placed green + 1 placed red in `src`, plus one
    PENDING green. Filter `id:g` (commander-legal within green) matches the
    three green cards; the placed set is the two green rows in `src`."""
    u = _user(s)
    src = _loc(s, u.id, "Source Binder")
    dst = _loc(s, u.id, "Target Binder")
    deck = _loc(s, u.id, "My Deck", type_="deck")
    g1 = _row(s, u.id, _card(s, "Green One", "G", "G"), src.id)
    g2 = _row(s, u.id, _card(s, "Green Two", "G", "G"), src.id)
    r1 = _row(s, u.id, _card(s, "Red One", "R", "R"), src.id)
    gp = _row(s, u.id, _card(s, "Green Pending", "G", "G"), src.id, pending=True)
    s.commit()
    return u, src, dst, deck, {"g1": g1, "g2": g2, "r1": r1, "gp": gp}


def test_build_query_matches_view():
    """build_collection_filter_query composes search + the placed filter into
    exactly the grid's set; pending is separable for the excluded count."""
    failed = 0
    _engine, sm = _engine_sm()
    s = sm()
    u, _src, _dst, _deck, rows = _seed(s)

    base = build_collection_filter_query(s, u.id, search="id:g")
    placed = {
        r
        for (r,) in base.with_entities(InventoryRow.id)
        .filter(InventoryRow.is_pending.is_(False))
        .all()
    }
    pending = base.with_entities(InventoryRow.id).filter(InventoryRow.is_pending.is_(True)).count()

    if placed == {rows["g1"].id, rows["g2"].id}:
        print("  [OK] filter resolves to exactly the placed green rows")
    else:
        print(f"  [FAIL] placed set wrong: {placed}")
        failed += 1
    if pending == 1:
        print("  [OK] matching pending rows counted separately (1)")
    else:
        print(f"  [FAIL] pending excluded count wrong: {pending}")
        failed += 1

    # list_inventory_rows still filters/paginates after the extraction.
    items, total = list_inventory_rows(s, user_id=u.id, search="id:g", per_page=1, page=1)
    if total == 3 and len(items) == 1:
        print("  [OK] list_inventory_rows behaviour-preserved (total=3, paginated to 1)")
    else:
        print(f"  [FAIL] list_inventory_rows drift: total={total}, page_len={len(items)}")
        failed += 1
    assert failed == 0


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


def _filter_form(**over):
    form = {
        "search": "id:g",
        "colors": "",
        "types": "",
        "status": "",
        "finishes": "",
        "price_min": "",
        "price_max": "",
        "finish": "",
        "location_id": 0,
        "csrf_token": "x",
    }
    form.update(over)
    return form


def test_bulk_add_showcase_route():
    from app import share_service
    from app.dependencies import get_current_user, get_db_session, require_csrf_token
    from app.models import ShowcaseItem

    failed = 0
    _engine, sm = _engine_sm()
    s = sm()
    u, _src, _dst, _deck, rows = _seed(s)
    sc = share_service.create_showcase(s, u.id, "Izzet", None)
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.post("/collection/bulk-add-showcase", data=_filter_form(showcase_id=sc.id))
        loc = r.headers.get("location", "")
        if r.status_code == 303 and "bulk=added" in loc and "added=2" in loc and "pending=1" in loc:
            print("  [OK] bulk-add-showcase redirects with added=2, pending=1")
        else:
            print(f"  [FAIL] redirect wrong: {r.status_code} {loc}")
            failed += 1

        check = sm()
        added_row_ids = {
            rid
            for (rid,) in check.query(ShowcaseItem.inventory_row_id)
            .filter(ShowcaseItem.showcase_id == sc.id)
            .all()
        }
        if added_row_ids == {rows["g1"].id, rows["g2"].id}:
            print("  [OK] only the placed green rows became ShowcaseItems")
        else:
            print(f"  [FAIL] showcase items wrong: {added_row_ids}")
            failed += 1
        check.close()
    finally:
        for dep in (get_db_session, get_current_user, require_csrf_token):
            app.dependency_overrides.pop(dep, None)
    assert failed == 0


def test_bulk_move_route():
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    failed = 0
    _engine, sm = _engine_sm()
    s = sm()
    u, _src, dst, _deck, rows = _seed(s)

    client, app = _client(sm, u)
    try:
        r = client.post("/collection/bulk-move", data=_filter_form(target_location_id=dst.id))
        loc = r.headers.get("location", "")
        if r.status_code == 303 and "bulk=moved" in loc and "moved=2" in loc and "pending=1" in loc:
            print("  [OK] bulk-move redirects with moved=2, pending=1")
        else:
            print(f"  [FAIL] redirect wrong: {r.status_code} {loc}")
            failed += 1

        check = sm()
        g1 = check.get(InventoryRow, rows["g1"].id)
        g2 = check.get(InventoryRow, rows["g2"].id)
        gp = check.get(InventoryRow, rows["gp"].id)
        r1 = check.get(InventoryRow, rows["r1"].id)
        if g1.storage_location_id == dst.id and g2.storage_location_id == dst.id:
            print("  [OK] placed green rows reassigned to the target location")
        else:
            print(
                f"  [FAIL] move didn't reassign: g1={g1.storage_location_id}, g2={g2.storage_location_id}"
            )
            failed += 1
        if gp.is_pending and gp.storage_location_id is None:
            print("  [OK] pending row left untouched")
        else:
            print("  [FAIL] pending row was moved")
            failed += 1
        if r1.storage_location_id != dst.id:
            print("  [OK] non-matching (red) row not moved")
        else:
            print("  [FAIL] red row moved")
            failed += 1
        check.close()
    finally:
        for dep in (get_db_session, get_current_user, require_csrf_token):
            app.dependency_overrides.pop(dep, None)
    assert failed == 0


def test_bulk_move_rejects_deck_and_skips_resort():
    from app import routes
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    failed = 0
    _engine, sm = _engine_sm()
    s = sm()
    u, _src, _dst, deck, rows = _seed(s)

    # Detect any resort_collection call from the bulk-move path.
    called = {"resort": False}
    orig = routes.collections.resort_collection
    routes.collections.resort_collection = lambda *a, **k: called.__setitem__("resort", True)

    client, app = _client(sm, u)
    try:
        r = client.post("/collection/bulk-move", data=_filter_form(target_location_id=deck.id))
        loc = r.headers.get("location", "")
        if r.status_code == 303 and "bulk=error" in loc and "reason=bad_target" in loc:
            print("  [OK] deck-type destination rejected (bulk=error, bad_target)")
        else:
            print(f"  [FAIL] deck target not rejected: {r.status_code} {loc}")
            failed += 1

        check = sm()
        g1 = check.get(InventoryRow, rows["g1"].id)
        if g1.storage_location_id != deck.id:
            print("  [OK] no rows moved into the deck location")
        else:
            print("  [FAIL] a row was moved into the deck")
            failed += 1
        check.close()

        if not called["resort"]:
            print("  [OK] resort_collection NOT invoked by the bulk-move path")
        else:
            print("  [FAIL] resort_collection was called")
            failed += 1
    finally:
        routes.collections.resort_collection = orig
        for dep in (get_db_session, get_current_user, require_csrf_token):
            app.dependency_overrides.pop(dep, None)
    assert failed == 0
