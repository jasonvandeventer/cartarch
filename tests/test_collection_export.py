"""Tests for /collection/export (Phase 2 — filter-honoring + price column).

The export used to dump the ENTIRE collection regardless of the active
/collection filter, and emitted no price. It now consumes the same shared
filter unit the grid page does, so a filtered export == the rows the user sees,
and it appends a Scryfall ID join key + a finish-aware Price column sourced
purely from persisted data (no request-path network call). Covered:

  - a filtered export (location_id + price_max) yields EXACTLY the same row set
    as the same-filtered grid (``list_inventory_rows``);
  - the Price + Scryfall ID columns are present and populated;
  - no filter → the whole collection (backward-compatible default).
"""

from __future__ import annotations

import csv
import io
import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.inventory_service import list_inventory_rows
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


def _card(s, name, *, price="1.00") -> Card:
    card = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        collector_number=str(next(_seq)),
        type_line="Creature",
        color_identity="G",
        colors="G",
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


def _client(sm, user):
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    main.app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(main.app, follow_redirects=False), main.app


def _parse(response):
    return list(csv.DictReader(io.StringIO(response.text)))


def test_export_honors_filter_and_matches_grid():
    from app.dependencies import get_current_user, get_db_session

    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    src = _loc(s, u.id, "Source Binder")
    other = _loc(s, u.id, "Other Binder")
    _row(s, u.id, _card(s, "Cheap", price="0.50"), src.id)
    _row(s, u.id, _card(s, "Pricey", price="9.00"), src.id)
    _row(s, u.id, _card(s, "Elsewhere Cheap", price="0.50"), other.id)
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.get(f"/collection/export?location_id={src.id}&price_max=1")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        rows = _parse(r)
        exported_names = {row["Name"] for row in rows}

        # Same-filtered grid set.
        grid, _total = list_inventory_rows(
            s, user_id=u.id, location_id=src.id, facet_price_max=1.0, per_page=100
        )
        grid_names = {row.card.name for row in grid}

        assert exported_names == grid_names == {"Cheap"}
        assert "Pricey" not in exported_names  # over price cap
        assert "Elsewhere Cheap" not in exported_names  # other location
    finally:
        for dep in (get_db_session, get_current_user):
            app.dependency_overrides.pop(dep, None)


def test_export_has_price_and_scryfall_id():
    from app.dependencies import get_current_user, get_db_session

    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    loc = _loc(s, u.id, "Binder")
    card = _card(s, "Priced Card", price="2.50")
    _row(s, u.id, card, loc.id)
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.get("/collection/export")
        rows = _parse(r)
        assert "Price" in rows[0] and "Scryfall ID" in rows[0]
        (only,) = rows
        assert only["Price"] == "2.50"
        assert only["Scryfall ID"] == card.scryfall_id
    finally:
        for dep in (get_db_session, get_current_user):
            app.dependency_overrides.pop(dep, None)


def test_collection_page_export_link_carries_filters():
    """Phase 3: the rendered Export CSV link propagates the active filter
    querystring, so clicking it downloads a FILTERED file (not a full dump)."""
    import re

    from app.dependencies import get_current_user, get_db_session

    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    loc = _loc(s, u.id, "Binder")
    _row(s, u.id, _card(s, "Forest", price="0.10"), loc.id)
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.get(f"/collection?location_id={loc.id}&price_max=5&colors=C&search=forest")
        assert r.status_code == 200
        m = re.search(r'href="(/collection/export[^"]*)"', r.text)
        assert m, "export link not found on the collection page"
        href = m.group(1)
        assert f"location_id={loc.id}" in href
        assert "price_max=5" in href
        assert "colors=C" in href
        assert "search=forest" in href
    finally:
        for dep in (get_db_session, get_current_user):
            app.dependency_overrides.pop(dep, None)


def test_export_no_filter_is_full_dump():
    from app.dependencies import get_current_user, get_db_session

    _engine, sm = _engine_sm()
    s = sm()
    u = _user(s)
    a = _loc(s, u.id, "A")
    b = _loc(s, u.id, "B")
    _row(s, u.id, _card(s, "One"), a.id)
    _row(s, u.id, _card(s, "Two"), b.id)
    _row(s, u.id, _card(s, "Three"), a.id)
    s.commit()

    client, app = _client(sm, u)
    try:
        r = client.get("/collection/export")
        rows = _parse(r)
        assert {row["Name"] for row in rows} == {"One", "Two", "Three"}
    finally:
        for dep in (get_db_session, get_current_user):
            app.dependency_overrides.pop(dep, None)
