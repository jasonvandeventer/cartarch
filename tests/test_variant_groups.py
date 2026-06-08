"""Unit tests for deck variant groups (v3.33.0).

Pytest module (matches tests/test_share_service).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_variant_groups.py

A variant group is an accounting overlay: builds of the same deck that share
one physical copy of many cards. One-card-one-location is preserved — the
shared card lives in ONE deck; the group only lets deck-import reconciliation
treat a card held by a SIBLING variant deck as "covered" (no new copy needed).

Covers:
  - group CRUD + duplicate-name ValueError + ownership scoping
  - sibling_variant_deck_location_ids (siblings only; excludes self / other
    group / NULL-location / no-group)
  - delete_variant_group nulls its decks (decks survive)
  - reconciliation: sibling-held card -> covered_by_variant (not import_new);
    card nowhere -> import_new; partial coverage; and NO behaviour change when
    the deck has no variant group (the regression guard)
  - migration idempotency
  - route smoke: edit assigns a group; deck detail shows the panel;
    reconcile-preview into a variant deck shows "Covered by variant group"
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import deck_service
from app.db import Base
from app.models import Card, Deck, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _user(s, username="u1") -> User:
    u = User(username=username, password_hash="x")
    s.add(u)
    s.flush()
    return u


def _deck(s, user_id, name, group_id=None) -> Deck:
    loc = StorageLocation(user_id=user_id, name=name, type="deck", mode="managed")
    s.add(loc)
    s.flush()
    deck = Deck(user_id=user_id, name=name, storage_location_id=loc.id, variant_group_id=group_id)
    s.add(deck)
    s.flush()
    return deck


def _card(s, name="Shared Card") -> Card:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="Test",
        collector_number=str(next(_seq)),
        type_line="Artifact",
    )
    s.add(c)
    s.flush()
    return c


def _place(s, user_id, card, location_id, qty=1):
    s.add(
        InventoryRow(
            card_id=card.id,
            user_id=user_id,
            storage_location_id=location_id,
            finish="normal",
            quantity=qty,
            is_pending=False,
        )
    )
    s.flush()


def _row(card, qty=1):
    return {
        "line_number": 1,
        "scryfall_id": card.scryfall_id,
        "finish": "normal",
        "quantity": qty,
    }


def test_group_crud() -> int:
    failed = 0
    s = _fresh_session()
    u = _user(s)
    g = deck_service.create_variant_group(s, u.id, "Atraxa builds")
    if g.id and g.name == "Atraxa builds":
        print("  [OK] create_variant_group")
    else:
        print("  [FAIL] create")
        failed += 1
    try:
        deck_service.create_variant_group(s, u.id, "Atraxa builds")
        print("  [FAIL] duplicate name allowed")
        failed += 1
    except ValueError:
        print("  [OK] duplicate name rejected")
    deck_service.create_variant_group(s, u.id, "Other")
    groups = deck_service.list_variant_groups(s, u.id)
    if [g.name for g in groups] == ["Atraxa builds", "Other"]:
        print("  [OK] list_variant_groups name-ordered")
    else:
        print(f"  [FAIL] list order: {[g.name for g in groups]}")
        failed += 1
    if deck_service.rename_variant_group(s, u.id, g.id, "Atraxa") and g.name == "Atraxa":
        print("  [OK] rename_variant_group")
    else:
        print("  [FAIL] rename")
        failed += 1
    if deck_service.delete_variant_group(s, u.id, g.id) is True:
        print("  [OK] delete_variant_group")
    else:
        print("  [FAIL] delete")
        failed += 1
    assert failed == 0


def test_ownership_scoping() -> int:
    failed = 0
    s = _fresh_session()
    u1 = _user(s, "u1")
    u2 = _user(s, "u2")
    g = deck_service.create_variant_group(s, u1.id, "Mine")
    ok = (
        deck_service.rename_variant_group(s, u2.id, g.id, "Hijack") is None
        and deck_service.delete_variant_group(s, u2.id, g.id) is False
        and deck_service.get_variant_group(s, u2.id, g.id) is None
    )
    if ok:
        print("  [OK] cross-user rename/delete/get rejected")
    else:
        print("  [FAIL] ownership not enforced")
        failed += 1
    assert failed == 0


def test_sibling_lookup() -> int:
    failed = 0
    s = _fresh_session()
    u = _user(s)
    g = deck_service.create_variant_group(s, u.id, "G")
    other = deck_service.create_variant_group(s, u.id, "Other")
    a = _deck(s, u.id, "A", group_id=g.id)
    b = _deck(s, u.id, "B", group_id=g.id)
    _deck(s, u.id, "C-othergroup", group_id=other.id)
    # a deck in the same group but with NO storage location
    nul = Deck(user_id=u.id, name="NoLoc", storage_location_id=None, variant_group_id=g.id)
    s.add(nul)
    s.flush()
    standalone = _deck(s, u.id, "Standalone")

    sib = deck_service.sibling_variant_deck_location_ids(s, a)
    if sib == [b.storage_location_id]:
        print("  [OK] siblings = group peers only (excl self/other-group/NULL-loc)")
    else:
        print(f"  [FAIL] siblings: {sib} (expected [{b.storage_location_id}])")
        failed += 1
    if deck_service.sibling_variant_deck_location_ids(s, standalone) == []:
        print("  [OK] no-group deck has no siblings")
    else:
        print("  [FAIL] standalone deck returned siblings")
        failed += 1
    assert failed == 0


def test_delete_nulls_decks() -> int:
    failed = 0
    s = _fresh_session()
    u = _user(s)
    g = deck_service.create_variant_group(s, u.id, "G")
    a = _deck(s, u.id, "A", group_id=g.id)
    b = _deck(s, u.id, "B", group_id=g.id)
    deck_service.delete_variant_group(s, u.id, g.id)
    s.expire_all()
    a2 = s.get(Deck, a.id)
    b2 = s.get(Deck, b.id)
    if a2 and b2 and a2.variant_group_id is None and b2.variant_group_id is None:
        print("  [OK] delete nulls referencing decks; decks survive")
    else:
        print("  [FAIL] decks not cleared / removed by group delete")
        failed += 1
    assert failed == 0


def _reconcile(s, user_id, deck, card, qty=1):
    return deck_service.find_inventory_matches_for_deck_import(
        s, user_id, deck.id, [_row(card, qty)]
    )[0]


def test_reconcile_covered() -> int:
    failed = 0
    s = _fresh_session()
    u = _user(s)
    g = deck_service.create_variant_group(s, u.id, "G")
    a = _deck(s, u.id, "A", group_id=g.id)
    b = _deck(s, u.id, "B", group_id=g.id)
    card = _card(s)
    _place(s, u.id, card, b.storage_location_id, qty=1)  # sibling holds it

    r = _reconcile(s, u.id, a, card, qty=1)
    if (
        r["recommended_action"] == "covered_by_variant"
        and r["variant_covered_qty"] == 1
        and r["recommended_move_qty"] == 0
        and r["recommended_new_qty"] == 0
        and r["is_variant_group"] is True
    ):
        print("  [OK] sibling-held card -> covered_by_variant (no move/import)")
    else:
        print(f"  [FAIL] covered: {r['recommended_action']} cov={r['variant_covered_qty']}")
        failed += 1
    assert failed == 0


def test_reconcile_import_new() -> int:
    failed = 0
    s = _fresh_session()
    u = _user(s)
    g = deck_service.create_variant_group(s, u.id, "G")
    a = _deck(s, u.id, "A", group_id=g.id)
    _deck(s, u.id, "B", group_id=g.id)
    card = _card(s)  # owned nowhere
    r = _reconcile(s, u.id, a, card, qty=1)
    if (
        r["recommended_action"] == "import_new"
        and r["variant_covered_qty"] == 0
        and r["recommended_new_qty"] == 1
    ):
        print("  [OK] card owned nowhere -> import_new")
    else:
        print(f"  [FAIL] import_new: {r}")
        failed += 1
    assert failed == 0


def test_reconcile_partial() -> int:
    failed = 0
    s = _fresh_session()
    u = _user(s)
    g = deck_service.create_variant_group(s, u.id, "G")
    a = _deck(s, u.id, "A", group_id=g.id)
    b = _deck(s, u.id, "B", group_id=g.id)
    card = _card(s)
    _place(s, u.id, card, b.storage_location_id, qty=1)  # sibling has 1, need 2
    r = _reconcile(s, u.id, a, card, qty=2)
    if (
        r["variant_covered_qty"] == 1
        and r["recommended_action"] == "import_new"
        and r["recommended_new_qty"] == 1
    ):
        print("  [OK] partial coverage: 1 covered + import 1 new")
    else:
        print(f"  [FAIL] partial: {r}")
        failed += 1
    assert failed == 0


def test_reconcile_no_group_unchanged() -> int:
    """Regression guard: a deck with NO variant group behaves exactly as
    before — a copy in an UNRELATED deck stays informational (other_deck) and
    is never treated as covered."""
    failed = 0
    s = _fresh_session()
    u = _user(s)
    a = _deck(s, u.id, "A")  # no group
    other = _deck(s, u.id, "Unrelated")  # no group, not a sibling
    card = _card(s)
    _place(s, u.id, card, other.storage_location_id, qty=1)
    r = _reconcile(s, u.id, a, card, qty=1)
    if (
        r["variant_covered_qty"] == 0
        and r["is_variant_group"] is False
        and r["recommended_action"] == "import_new"
        and r["total_in_other_decks"] == 1
    ):
        print("  [OK] no-group deck unchanged (other-deck copy stays informational)")
    else:
        print(f"  [FAIL] no-group regression: {r}")
        failed += 1
    assert failed == 0


def test_migration_idempotent() -> int:
    """migrate twice on a throwaway DB -> exactly one variant_group_id column."""
    import os
    import tempfile

    failed = 0
    d = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = d
    import importlib

    import app.db as db

    importlib.reload(db)
    from sqlalchemy import text

    with db.engine.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)"))
        c.execute(text("CREATE TABLE IF NOT EXISTS decks (id INTEGER PRIMARY KEY, name TEXT)"))
    import scripts.migrate_v3_33_0_variant_groups as m

    importlib.reload(m)
    m.main()
    m.main()
    with db.engine.begin() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(decks)")).fetchall()]
        tbls = [
            r[0]
            for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
        ]
    if cols.count("variant_group_id") == 1 and "variant_groups" in tbls:
        print("  [OK] migration idempotent (one column, table present)")
    else:
        print(f"  [FAIL] migration: cols={cols.count('variant_group_id')} tbls={tbls}")
        failed += 1
    # Reload app.db back to the dev DATA_DIR so later tests aren't affected.
    assert failed == 0


def test_route_smoke() -> int:
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
    u = _user(s)
    a = _deck(s, u.id, "Atraxa v1")
    b = _deck(s, u.id, "Atraxa v2")
    g = deck_service.create_variant_group(s, u.id, "Atraxa builds")
    a.variant_group_id = g.id
    b.variant_group_id = g.id
    card = _card(s, name="Smothering Tithe")
    _place(s, u.id, card, b.storage_location_id, qty=1)  # held by sibling v2
    s.commit()
    a_loc = a.storage_location_id

    def _db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _db
    main.app.dependency_overrides[get_current_user] = lambda: u
    main.app.dependency_overrides[require_csrf_token] = lambda: None
    try:
        c = TestClient(main.app, follow_redirects=False)

        # Assign deck A to a (new) group via the edit form.
        r = c.post(
            f"/decks/{a.id}/edit",
            data={"name": "Atraxa v1", "csrf_token": "x", "variant_group_id": str(g.id)},
        )
        if r.status_code == 303:
            print("  [OK] deck edit assigns variant group (303)")
        else:
            print(f"  [FAIL] edit status {r.status_code}")
            failed += 1

        r = c.get(f"/decks/{a.id}")
        if r.status_code == 200 and "Variant group" in r.text and "Atraxa v2" in r.text:
            print("  [OK] deck detail shows the variant-group panel + sibling")
        else:
            print(f"  [FAIL] deck detail panel missing (status {r.status_code})")
            failed += 1

        # Reconcile-preview importing the sibling-held card into deck A.
        r = c.post(
            "/import/reconcile-preview",
            data={
                "target_location_id": str(a_loc),
                "line_number": "1",
                "name": "Smothering Tithe",
                "scryfall_id": card.scryfall_id,
                "set_code": "tst",
                "collector_number": "1",
                "finish": "normal",
                "quantity": "1",
                "location": "",
                "csrf_token": "x",
            },
        )
        if r.status_code == 200 and "Covered by variant group" in r.text:
            print("  [OK] reconcile-preview shows 'Covered by variant group'")
        else:
            print(f"  [FAIL] reconcile-preview (status {r.status_code})")
            failed += 1
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
        main.app.dependency_overrides.pop(get_current_user, None)
        main.app.dependency_overrides.pop(require_csrf_token, None)
    assert failed == 0
