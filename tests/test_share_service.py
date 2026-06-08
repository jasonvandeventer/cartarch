"""Unit tests for multi-showcase service behaviour (v3.31.0).

Pytest module (matches tests/test_deck_service).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_share_service.py

Uses an ISOLATED in-memory SQLite engine (NOT the shared dev DB) so the
test can freely create/delete rows. ``share_service`` takes the Session
as a parameter, so pointing it at a throwaway engine needs no patching of
``app.db``.

Covers the v3.31.0 multi-showcase changes specifically:
  - a user may own several Showcases (the UNIQUE(user_id) cap is gone)
  - ownership scoping on get/update/delete/add (cross-user is rejected)
  - add-to-showcase honours an explicit showcase_id and falls back to a
    lazily-created default when none is given
  - item mutation is scoped by joining the item's Showcase, not by a
    single per-user Showcase
  - get_showcase_with_items sums a finish-aware total_value
  - delete_showcase removes the Showcase, its items, and its shares
"""

from __future__ import annotations

import itertools

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import share_service
from app.db import Base
from app.models import (
    Card,
    InventoryRow,
    Playgroup,
    PlaygroupMember,
    Share,
    ShowcaseItem,
    User,
)

# Monotonic counter for unique scryfall_ids. An earlier cut used
# id(object()), but CPython frees the throwaway object immediately and
# reuses its id, so two _make_row calls with the same args collided on
# the cards.scryfall_id UNIQUE constraint (passed locally by luck, failed
# in CI). A counter is deterministically unique.
_scryfall_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _make_row(
    session,
    user_id: int,
    price: str | None = None,
    quantity: int = 1,
    is_pending: bool = False,
) -> InventoryRow:
    # InventoryRow.is_pending defaults to True (rows are pending until
    # placed); the test fixtures default to placed unless asked otherwise.
    card = Card(
        scryfall_id=f"sid-{next(_scryfall_seq)}",
        name="Test Card",
        set_code="TST",
        collector_number="1",
        price_usd=price,
    )
    session.add(card)
    session.flush()
    row = InventoryRow(
        card_id=card.id,
        user_id=user_id,
        quantity=quantity,
        finish="normal",
        is_pending=is_pending,
    )
    session.add(row)
    session.commit()
    return row


def test_multiple_showcases_per_user() -> int:
    """A user may own several Showcases; list_showcases returns them all."""
    failed = 0
    s = _fresh_session()
    a = share_service.create_showcase(s, user_id=1, name="Trade binder", description=None)
    b = share_service.create_showcase(s, user_id=1, name="Brag list", description="best of")
    showcases = share_service.list_showcases(s, user_id=1)
    if len(showcases) != 2:
        print(f"  [FAIL] expected 2 showcases, got {len(showcases)}")
        failed += 1
    else:
        print("  [OK] two showcases coexist for one user")
    # Oldest-first ordering.
    if [sc.id for sc in showcases] != [a.id, b.id]:
        print("  [FAIL] list_showcases not oldest-first")
        failed += 1
    else:
        print("  [OK] list_showcases is oldest-first")
    assert failed == 0


def test_ownership_scoping() -> int:
    """get/update/delete/add reject Showcases owned by another user."""
    failed = 0
    s = _fresh_session()
    mine = share_service.create_showcase(s, user_id=1, name="Mine", description=None)

    if share_service.get_showcase(s, user_id=2, showcase_id=mine.id) is not None:
        print("  [FAIL] get_showcase leaked another user's showcase")
        failed += 1
    else:
        print("  [OK] get_showcase rejects cross-user access")

    if (
        share_service.update_showcase(
            s, user_id=2, showcase_id=mine.id, name="Hijacked", description=None
        )
        is not None
    ):
        print("  [FAIL] update_showcase mutated another user's showcase")
        failed += 1
    else:
        print("  [OK] update_showcase rejects cross-user write")

    if share_service.delete_showcase(s, user_id=2, showcase_id=mine.id) is not False:
        print("  [FAIL] delete_showcase removed another user's showcase")
        failed += 1
    else:
        print("  [OK] delete_showcase rejects cross-user delete")

    # add_showcase_item with a showcase_id the user doesn't own → None.
    row = _make_row(s, user_id=2)
    if (
        share_service.add_showcase_item(s, user_id=2, inventory_row_id=row.id, showcase_id=mine.id)
        is not None
    ):
        print("  [FAIL] add_showcase_item added to another user's showcase")
        failed += 1
    else:
        print("  [OK] add_showcase_item rejects unowned showcase_id")
    assert failed == 0


def test_add_explicit_and_default() -> int:
    """add_showcase_item honours showcase_id; falls back to default when None."""
    failed = 0
    s = _fresh_session()
    sc1 = share_service.create_showcase(s, user_id=1, name="One", description=None)
    sc2 = share_service.create_showcase(s, user_id=1, name="Two", description=None)
    row = _make_row(s, user_id=1)

    item = share_service.add_showcase_item(
        s, user_id=1, inventory_row_id=row.id, showcase_id=sc2.id
    )
    if item is None or item.showcase_id != sc2.id:
        print("  [FAIL] explicit showcase_id not honoured")
        failed += 1
    else:
        print("  [OK] add_showcase_item lands in the chosen showcase")

    # No showcase_id → default (oldest) showcase, which is sc1.
    row2 = _make_row(s, user_id=1)
    item2 = share_service.add_showcase_item(
        s, user_id=1, inventory_row_id=row2.id, showcase_id=None
    )
    if item2 is None or item2.showcase_id != sc1.id:
        print("  [FAIL] default fallback did not use the oldest showcase")
        failed += 1
    else:
        print("  [OK] add_showcase_item falls back to the default showcase")
    assert failed == 0


def test_item_mutation_scoped_by_join() -> int:
    """quantity/remove resolve ownership by joining the item's Showcase."""
    failed = 0
    s = _fresh_session()
    sc = share_service.create_showcase(s, user_id=1, name="One", description=None)
    row = _make_row(s, user_id=1)
    item = share_service.add_showcase_item(s, user_id=1, inventory_row_id=row.id, showcase_id=sc.id)

    # Wrong user cannot touch the item.
    if (
        share_service.update_quantity_offered(
            s, user_id=2, showcase_item_id=item.id, quantity_offered=5
        )
        is not None
    ):
        print("  [FAIL] update_quantity_offered allowed cross-user mutation")
        failed += 1
    else:
        print("  [OK] update_quantity_offered scoped to owner")

    if share_service.remove_showcase_item(s, user_id=2, showcase_item_id=item.id) is not False:
        print("  [FAIL] remove_showcase_item allowed cross-user delete")
        failed += 1
    else:
        print("  [OK] remove_showcase_item scoped to owner")

    # Owner can, even though it's not their oldest/default showcase.
    if (
        share_service.update_quantity_offered(
            s, user_id=1, showcase_item_id=item.id, quantity_offered=3
        )
        is None
    ):
        print("  [FAIL] owner could not update their own item")
        failed += 1
    else:
        print("  [OK] owner updates item in a non-default showcase")
    assert failed == 0


def test_total_value() -> int:
    """get_showcase_with_items sums finish-aware value; None when not owned."""
    failed = 0
    s = _fresh_session()
    sc = share_service.create_showcase(s, user_id=1, name="One", description=None)
    row = _make_row(s, user_id=1, price="1.50", quantity=2)
    share_service.add_showcase_item(
        s, user_id=1, inventory_row_id=row.id, showcase_id=sc.id, quantity_offered=1
    )

    data = share_service.get_showcase_with_items(s, user_id=1, showcase_id=sc.id)
    if data is None:
        print("  [FAIL] owner got None for their own showcase")
        assert failed == 0 + 1
    # available = min(quantity_offered=1, quantity=2) = 1; value = 1 * 1.50
    if abs(data["total_value"] - 1.50) > 1e-6:
        print(f"  [FAIL] total_value expected 1.50, got {data['total_value']}")
        failed += 1
    else:
        print("  [OK] total_value sums finish-aware price * available")

    if share_service.get_showcase_with_items(s, user_id=2, showcase_id=sc.id) is not None:
        print("  [FAIL] get_showcase_with_items leaked another user's showcase")
        failed += 1
    else:
        print("  [OK] get_showcase_with_items rejects cross-user access")
    assert failed == 0


def test_delete_cascades_items_and_shares() -> int:
    """delete_showcase removes the Showcase, its items, and its shares."""
    failed = 0
    s = _fresh_session()
    sc = share_service.create_showcase(s, user_id=1, name="One", description=None)
    row = _make_row(s, user_id=1)
    item = share_service.add_showcase_item(s, user_id=1, inventory_row_id=row.id, showcase_id=sc.id)
    item_id = item.id

    # A playgroup the user belongs to, plus a share of this showcase.
    pg = Playgroup(name="PG", created_by=1, join_code="ABC123")
    s.add(pg)
    s.flush()
    s.add(PlaygroupMember(playgroup_id=pg.id, user_id=1, role="owner"))
    s.commit()
    share = share_service.create_share(s, user_id=1, showcase_id=sc.id, playgroup_id=pg.id)
    if share is None:
        print("  [FAIL] could not create share fixture")
        assert failed == 0 + 1

    if share_service.delete_showcase(s, user_id=1, showcase_id=sc.id) is not True:
        print("  [FAIL] delete_showcase returned False for owner")
        failed += 1
    if share_service.get_showcase(s, user_id=1, showcase_id=sc.id) is not None:
        print("  [FAIL] showcase still present after delete")
        failed += 1
    if s.query(ShowcaseItem).filter(ShowcaseItem.id == item_id).first() is not None:
        print("  [FAIL] showcase items not cascade-deleted")
        failed += 1
    if s.query(Share).filter(Share.showcase_id == sc.id).first() is not None:
        print("  [FAIL] shares of the deleted showcase remain")
        failed += 1
    if not failed:
        print("  [OK] delete_showcase cascades items and revokes shares")
    assert failed == 0


def test_bulk_add_rows() -> int:
    """add_rows_to_showcase: whole-collection + per-location, idempotent,
    pending-excluded, ownership-scoped."""
    failed = 0
    s = _fresh_session()
    sc = share_service.create_showcase(s, user_id=1, name="One", description=None)

    # Two placed rows in location 10, one placed row unassigned, one pending.
    r1 = _make_row(s, user_id=1, quantity=3)
    r1.storage_location_id = 10
    r2 = _make_row(s, user_id=1, quantity=1)
    r2.storage_location_id = 10
    _make_row(s, user_id=1, quantity=2)  # placed, no location
    pending = _make_row(s, user_id=1, quantity=1, is_pending=True)  # excluded
    s.commit()

    # Per-location: only the two rows in location 10.
    res = share_service.add_rows_to_showcase(s, user_id=1, showcase_id=sc.id, location_id=10)
    if res is None or res["added"] != 2:
        print(f"  [FAIL] per-location add expected 2, got {res}")
        failed += 1
    else:
        print("  [OK] per-location bulk add scopes to the location")

    # quantity_offered should track the row's quantity (offer what you own).
    item_r1 = (
        s.query(ShowcaseItem)
        .filter(ShowcaseItem.showcase_id == sc.id, ShowcaseItem.inventory_row_id == r1.id)
        .first()
    )
    if item_r1 is None or item_r1.quantity_offered != 3:
        print("  [FAIL] bulk add did not offer the full owned quantity")
        failed += 1
    else:
        print("  [OK] bulk add offers the full owned quantity")

    # Whole collection: adds r3 (new), skips r1+r2 (already in), excludes pending.
    res2 = share_service.add_rows_to_showcase(s, user_id=1, showcase_id=sc.id)
    if res2 is None or res2["added"] != 1 or res2["skipped"] != 2:
        print(f"  [FAIL] whole-collection add expected added=1 skipped=2, got {res2}")
        failed += 1
    else:
        print("  [OK] whole-collection add is idempotent + excludes pending")

    if (
        s.query(ShowcaseItem)
        .filter(ShowcaseItem.showcase_id == sc.id, ShowcaseItem.inventory_row_id == pending.id)
        .first()
        is not None
    ):
        print("  [FAIL] pending row leaked into the showcase")
        failed += 1
    else:
        print("  [OK] pending row excluded")

    # Cross-user ownership.
    if share_service.add_rows_to_showcase(s, user_id=2, showcase_id=sc.id) is not None:
        print("  [FAIL] bulk add allowed on another user's showcase")
        failed += 1
    else:
        print("  [OK] bulk add rejects cross-user showcase")
    assert failed == 0


def test_bulk_add_rows_by_row_ids() -> int:
    """add_rows_to_showcase(row_ids=...): the filter-scoped Collection bulk
    path. row_ids takes precedence over location_id; the user_id + is_pending
    guards still apply (a foreign or pending id can't leak in); idempotent."""
    failed = 0
    s = _fresh_session()
    sc = share_service.create_showcase(s, user_id=1, name="Filtered", description=None)

    r1 = _make_row(s, user_id=1, quantity=2)
    r2 = _make_row(s, user_id=1, quantity=1)
    placed_other = _make_row(s, user_id=1, quantity=1)  # owned but NOT in the id set
    pending = _make_row(s, user_id=1, quantity=1, is_pending=True)
    foreign = _make_row(s, user_id=2, quantity=1)  # another user's row
    s.commit()

    # Explicit id set: only r1 + r2 added, even though a foreign id and a
    # pending id are passed in (both filtered out by the guards).
    res = share_service.add_rows_to_showcase(
        s, user_id=1, showcase_id=sc.id, row_ids=[r1.id, r2.id, pending.id, foreign.id]
    )
    if res is None or res["added"] != 2:
        print(f"  [FAIL] row_ids add expected 2, got {res}")
        failed += 1
    else:
        print("  [OK] row_ids add inserts exactly the owned, placed ids")

    if (
        s.query(ShowcaseItem)
        .filter(
            ShowcaseItem.showcase_id == sc.id,
            ShowcaseItem.inventory_row_id.in_([pending.id, foreign.id, placed_other.id]),
        )
        .count()
        != 0
    ):
        print("  [FAIL] a guarded/excluded id leaked into the showcase")
        failed += 1
    else:
        print("  [OK] pending / foreign / out-of-set ids excluded")

    # row_ids takes precedence over location_id (location_id ignored when both).
    placed_other.storage_location_id = 99
    s.commit()
    res_prec = share_service.add_rows_to_showcase(
        s, user_id=1, showcase_id=sc.id, location_id=99, row_ids=[r1.id, r2.id]
    )
    if res_prec is None or res_prec["added"] != 0 or res_prec["skipped"] != 2:
        print(f"  [FAIL] row_ids precedence expected added=0 skipped=2, got {res_prec}")
        failed += 1
    else:
        print("  [OK] row_ids takes precedence over location_id + idempotent re-add")

    # Empty id set matches nothing, never raises.
    res_empty = share_service.add_rows_to_showcase(s, user_id=1, showcase_id=sc.id, row_ids=[])
    if res_empty is None or res_empty["added"] != 0 or res_empty["total"] != 0:
        print(f"  [FAIL] empty row_ids expected added=0 total=0, got {res_empty}")
        failed += 1
    else:
        print("  [OK] empty row_ids is a safe no-op")
    assert failed == 0


def test_card_search_scryfall_syntax() -> int:
    """v3.32.3 — the card search inside a Showcase / shared view accepts the
    app's boolean/Scryfall query language (same parser as the Collection bar),
    applied server-side via `get_showcase_with_items(..., search=...)` and
    `get_share_view(..., search=...)`."""
    failed = 0
    s = _fresh_session()
    user = User(username="searcher", password_hash="x")
    s.add(user)
    s.flush()
    sc = share_service.create_showcase(s, user.id, "Binder", None)

    # name, type_line, color_identity
    specs = [
        ("Llanowar Elves", "Creature — Elf Druid", "G"),
        ("Sol Ring", "Artifact", ""),  # colorless
        ("Lightning Bolt", "Instant", "R"),
        ("Birds of Paradise", "Creature — Bird", "G"),
    ]
    for name, type_line, ci in specs:
        card = Card(
            scryfall_id=f"srch-{next(_scryfall_seq)}",
            name=name,
            set_code="tst",
            collector_number=str(next(_scryfall_seq)),
            type_line=type_line,
            color_identity=ci,
        )
        s.add(card)
        s.flush()
        row = InventoryRow(
            card_id=card.id, user_id=user.id, quantity=1, finish="normal", is_pending=False
        )
        s.add(row)
        s.flush()
        s.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=row.id, quantity_offered=1))
    s.commit()

    def names(search):
        data = share_service.get_showcase_with_items(s, user.id, sc.id, search=search)
        return sorted(i["card"].name for i in data["items"])

    cases = [
        ("", ["Birds of Paradise", "Lightning Bolt", "Llanowar Elves", "Sol Ring"]),
        ("t:creature", ["Birds of Paradise", "Llanowar Elves"]),
        # id: is the commander-legal "within" filter → colorless Sol Ring is a
        # subset of {G} and matches alongside the green cards.
        ("id:g", ["Birds of Paradise", "Llanowar Elves", "Sol Ring"]),
        ("t:instant OR t:artifact", ["Lightning Bolt", "Sol Ring"]),
        ("-t:creature", ["Lightning Bolt", "Sol Ring"]),
        ("t:goblin", []),  # no matches
    ]
    for search, expected in cases:
        got = names(search)
        if got == expected:
            print(f"  [OK] showcase search {search!r} → {expected}")
        else:
            print(f"  [FAIL] showcase search {search!r}: expected {expected}, got {got}")
            failed += 1

    # The same search threads through the read-only share view, AFTER the
    # privacy projection (filter runs server-side before projection).
    pg = Playgroup(name="PG", created_by=user.id, join_code="SRCH1")
    s.add(pg)
    s.flush()
    share = Share(user_id=user.id, showcase_id=sc.id, playgroup_id=pg.id)
    s.add(share)
    s.commit()
    view = share_service.get_share_view(s, user.id, share.id, search="t:creature")
    sv_names = sorted(i["card"].name for i in view["items"])
    if sv_names == ["Birds of Paradise", "Llanowar Elves"]:
        print("  [OK] share view search 't:creature' filters the projection")
    else:
        print(f"  [FAIL] share view search: got {sv_names}")
        failed += 1
    assert failed == 0


def test_share_view_renders_through_route() -> int:
    """Regression: GET /shares/{id} must actually render.

    A prod 500 (`UndefinedError: 'total_value'`) shipped in v3.31.0 because
    the value-totals feature updated `get_share_view` (service) + the
    template but NOT the `shares_view` route's render context — service-only
    tests didn't catch it. This exercises the full route → template path.

    Uses one shared in-memory DB (StaticPool — otherwise each SQLite
    connection gets its own empty in-memory DB) + dependency overrides so
    the route reads the fixtures we commit here, not the dev DB.
    """
    from fastapi.testclient import TestClient

    from app import main
    from app.dependencies import get_current_user, get_db_session

    failed = 0
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    s = sm()
    user = User(username="viewer", password_hash="x")
    s.add(user)
    s.flush()
    sc = share_service.create_showcase(s, user.id, "SC", None)
    card = Card(scryfall_id="sv-1", name="C", set_code="T", collector_number="1", price_usd="2.00")
    s.add(card)
    s.flush()
    inv = InventoryRow(
        card_id=card.id, user_id=user.id, quantity=1, finish="normal", is_pending=False
    )
    s.add(inv)
    s.flush()
    s.add(ShowcaseItem(showcase_id=sc.id, inventory_row_id=inv.id, quantity_offered=1))
    pg = Playgroup(name="PG", created_by=user.id, join_code="JOINX")
    s.add(pg)
    s.flush()
    share = Share(user_id=user.id, showcase_id=sc.id, playgroup_id=pg.id)
    s.add(share)
    s.commit()
    share_id = share.id

    def _override_db():
        db = sm()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[get_db_session] = _override_db
    main.app.dependency_overrides[get_current_user] = lambda: user
    try:
        c = TestClient(main.app)
        r = c.get(f"/shares/{share_id}")
        if r.status_code != 200:
            print(f"  [FAIL] /shares/{share_id} -> {r.status_code} (expected 200)")
            failed += 1
        elif "total value" not in r.text:
            print("  [FAIL] share view rendered but 'total value' missing")
            failed += 1
        else:
            print("  [OK] /shares/{id} renders through the route with total value")
    finally:
        main.app.dependency_overrides.pop(get_db_session, None)
        main.app.dependency_overrides.pop(get_current_user, None)
    assert failed == 0
