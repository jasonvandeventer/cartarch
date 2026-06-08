"""Unit tests for the single-card quick-add to a StorageLocation (v3.32.x).

Pytest module (matches tests/test_share_service).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_location_add_card.py

Covers `inventory_service.add_card_to_location` — the acquisition primitive
behind the location quick-add modal — and the `POST /locations/{id}/add-card`
route. Key behaviours pinned:
  - places the card AT the location (storage_location_id set, is_pending False)
  - merges into an existing matching placed row (no duplicate)
  - distinct language / is_proxy / finish each create a SEPARATE row (the bug
    the drawer/slot-keyed `create_or_merge_inventory_row` would have caused)
  - finish normalization ("traditional foil" -> "foil")
  - ownership rejection (foreign location -> None)
  - cache-miss card creation via a monkeypatched fetch (the patch doubles as
    the request-path network-invariant guard); unknown card -> None
  - route: modal renders on GET; HX-Request POST adds + returns the grid; the
    no-JS path 303-redirects
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import inventory_service
from app.db import Base
from app.inventory_service import add_card_to_location
from app.models import Card, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_user(session, username="u1") -> User:
    u = User(username=username, password_hash="x")
    session.add(u)
    session.flush()
    return u


def _seed_location(session, user_id, name="Binder", type_="binder") -> StorageLocation:
    loc = StorageLocation(user_id=user_id, name=name, type=type_, mode="managed")
    session.add(loc)
    session.flush()
    return loc


def _seed_card(session, name="Sol Ring") -> Card:
    """A fully-populated (non-stale) Card so get_or_create_card won't refetch."""
    card = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="Test Set",
        collector_number=str(next(_seq)),
        rarity="rare",
        image_url="http://img/x.jpg",
        type_line="Artifact",
        oracle_text="{T}: Add {C}{C}.",
        color_identity="",
        set_type="commander",
    )
    session.add(card)
    session.flush()
    return card


def _rows(session, user_id):
    return session.query(InventoryRow).filter(InventoryRow.user_id == user_id).all()


def test_create_new_row() -> int:
    failed = 0
    s = _fresh_session()
    u = _seed_user(s)
    loc = _seed_location(s, u.id)
    card = _seed_card(s)
    row = add_card_to_location(
        s,
        user_id=u.id,
        location_id=loc.id,
        scryfall_id=card.scryfall_id,
        finish="foil",
        quantity=3,
        language="ja",
        is_proxy=True,
        notes="signed",
    )
    ok = (
        row is not None
        and row.storage_location_id == loc.id
        and row.is_pending is False
        and row.finish == "foil"
        and row.quantity == 3
        and row.language == "ja"
        and bool(row.is_proxy) is True
        and row.notes == "signed"
        and len(_rows(s, u.id)) == 1
    )
    if ok:
        print("  [OK] create places a non-pending row at the location with all fields")
    else:
        print(f"  [FAIL] create_new_row: {row and vars(row)}")
        failed += 1
    assert failed == 0


def test_merge_existing() -> int:
    failed = 0
    s = _fresh_session()
    u = _seed_user(s)
    loc = _seed_location(s, u.id)
    card = _seed_card(s)
    common = dict(user_id=u.id, location_id=loc.id, scryfall_id=card.scryfall_id, finish="normal")
    add_card_to_location(s, quantity=2, **common)
    add_card_to_location(s, quantity=5, **common)
    rows = _rows(s, u.id)
    if len(rows) == 1 and rows[0].quantity == 7:
        print("  [OK] re-adding the same printing merges quantity (7), one row")
    else:
        print(f"  [FAIL] merge: {[(r.quantity) for r in rows]}")
        failed += 1
    assert failed == 0


def test_distinct_fields_separate_rows() -> int:
    failed = 0
    s = _fresh_session()
    u = _seed_user(s)
    loc = _seed_location(s, u.id)
    card = _seed_card(s)
    base = dict(user_id=u.id, location_id=loc.id, scryfall_id=card.scryfall_id)
    add_card_to_location(s, finish="normal", language="en", is_proxy=False, **base)
    add_card_to_location(s, finish="foil", language="en", is_proxy=False, **base)  # finish differs
    add_card_to_location(s, finish="normal", language="ja", is_proxy=False, **base)  # lang differs
    add_card_to_location(s, finish="normal", language="en", is_proxy=True, **base)  # proxy differs
    rows = _rows(s, u.id)
    if len(rows) == 4:
        print("  [OK] distinct finish / language / is_proxy each create a separate row")
    else:
        print(f"  [FAIL] expected 4 distinct rows, got {len(rows)}")
        failed += 1
    assert failed == 0


def test_finish_normalization() -> int:
    failed = 0
    s = _fresh_session()
    u = _seed_user(s)
    loc = _seed_location(s, u.id)
    card = _seed_card(s)
    row = add_card_to_location(
        s, user_id=u.id, location_id=loc.id, scryfall_id=card.scryfall_id, finish="traditional foil"
    )
    if row is not None and row.finish == "foil":
        print("  [OK] finish 'traditional foil' normalizes to 'foil'")
    else:
        print(f"  [FAIL] finish normalization: {row and row.finish}")
        failed += 1
    assert failed == 0


def test_ownership_rejection() -> int:
    failed = 0
    s = _fresh_session()
    owner = _seed_user(s, "owner")
    other = _seed_user(s, "other")
    loc = _seed_location(s, owner.id)
    card = _seed_card(s)
    row = add_card_to_location(
        s, user_id=other.id, location_id=loc.id, scryfall_id=card.scryfall_id
    )
    if row is None and len(_rows(s, other.id)) == 0:
        print("  [OK] adding to a location owned by another user is rejected (None, no row)")
    else:
        print("  [FAIL] ownership not enforced")
        failed += 1
    assert failed == 0


def test_cache_miss_creates_card(monkeypatch_fetch=None) -> int:
    """An unknown scryfall_id triggers exactly one get_or_create_card fetch;
    the patched fetch both supplies the payload AND proves no real network
    call happens (a live call would hit Scryfall, which tests must not)."""
    failed = 0
    s = _fresh_session()
    u = _seed_user(s)
    loc = _seed_location(s, u.id)

    calls = {"n": 0}

    def fake_fetch(scryfall_id):
        calls["n"] += 1
        return {
            "scryfall_id": scryfall_id,
            "name": "Brand New Card",
            "set_code": "new",
            "set_name": "New Set",
            "collector_number": "1",
            "rarity": "mythic",
            "image_url": "http://img/new.jpg",
            "type_line": "Creature",
            "oracle_text": "",
            "price_usd": None,
            "price_usd_foil": None,
            "price_usd_etched": None,
            "colors": "G",
            "color_identity": "G",
            "mana_cost": "{G}",
            "cmc": 1.0,
            "set_type": "expansion",
            "produced_tokens": "[]",  # stripped by card_constructor_kwargs
        }

    orig = inventory_service.fetch_card_by_scryfall_id
    inventory_service.fetch_card_by_scryfall_id = fake_fetch
    try:
        row = add_card_to_location(s, user_id=u.id, location_id=loc.id, scryfall_id="brand-new-id")
        card = s.query(Card).filter(Card.scryfall_id == "brand-new-id").first()
        if row is not None and card is not None and row.card_id == card.id and calls["n"] == 1:
            print("  [OK] cache-miss creates the Card via a single fetch and links the row")
        else:
            print(f"  [FAIL] cache-miss: row={row}, card={card}, calls={calls['n']}")
            failed += 1

        # Unknown card (fetch returns None) -> None, no row created.
        inventory_service.fetch_card_by_scryfall_id = lambda sid: None
        before = len(_rows(s, u.id))
        row2 = add_card_to_location(
            s, user_id=u.id, location_id=loc.id, scryfall_id="does-not-exist"
        )
        if row2 is None and len(_rows(s, u.id)) == before:
            print("  [OK] unresolvable card returns None and creates no row")
        else:
            print("  [FAIL] unresolvable card created a row")
            failed += 1
    finally:
        inventory_service.fetch_card_by_scryfall_id = orig
    assert failed == 0


def test_route_renders_and_adds() -> int:
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session, require_csrf_token

    failed = 0
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    s = sm()
    u = _seed_user(s)
    loc = _seed_location(s, u.id)
    card = _seed_card(s, name="Counterspell")
    s.commit()

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    main.app.dependency_overrides[get_current_user] = lambda: u
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    try:
        c = TestClient(main.app, follow_redirects=False)

        r = c.get(f"/locations/{loc.id}")
        if r.status_code == 200 and 'id="loc-add-card-form"' in r.text and "Quick add" in r.text:
            print("  [OK] GET /locations/{id} renders the quick-add modal")
        else:
            print(f"  [FAIL] modal not present (status {r.status_code})")
            failed += 1

        form = {
            "scryfall_id": card.scryfall_id,
            "finish": "normal",
            "quantity": "2",
            "language": "en",
            "csrf_token": "x",
        }
        r = c.post(f"/locations/{loc.id}/add-card", data=form, headers={"HX-Request": "true"})
        if r.status_code == 200 and "Counterspell" in r.text:
            print("  [OK] HX-Request POST adds the card and returns the grid")
        else:
            print(f"  [FAIL] HX add: status {r.status_code}")
            failed += 1

        # The row persisted at the location.
        r = c.get(f"/locations/{loc.id}")
        if "Counterspell" in r.text:
            print("  [OK] added card appears in the location grid")
        else:
            print("  [FAIL] added card not in grid after add")
            failed += 1

        # No-JS path 303-redirects.
        r = c.post(f"/locations/{loc.id}/add-card", data=form)
        if r.status_code == 303:
            print("  [OK] non-HTMX POST 303-redirects")
        else:
            print(f"  [FAIL] non-HTMX expected 303, got {r.status_code}")
            failed += 1
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
        main.app.dependency_overrides.pop(get_current_user, None)
        main.app.dependency_overrides.pop(require_csrf_token, None)
    assert failed == 0
