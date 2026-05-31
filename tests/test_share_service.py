"""Unit tests for multi-showcase service behaviour (v3.31.0).

Standalone runner (no pytest dependency — matches tests/test_deck_service).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true python -m tests.test_share_service

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

import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import share_service
from app.db import Base
from app.models import (
    Card,
    InventoryRow,
    Playgroup,
    PlaygroupMember,
    Share,
    ShowcaseItem,
)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _make_row(session, user_id: int, price: str | None = None, quantity: int = 1) -> InventoryRow:
    card = Card(
        scryfall_id=f"sid-{user_id}-{price}-{quantity}-{id(object())}",
        name="Test Card",
        set_code="TST",
        collector_number="1",
        price_usd=price,
    )
    session.add(card)
    session.flush()
    row = InventoryRow(card_id=card.id, user_id=user_id, quantity=quantity, finish="normal")
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
    return failed


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
    return failed


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
    return failed


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
    return failed


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
        return failed + 1
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
    return failed


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
        return failed + 1

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
    return failed


def main() -> None:
    tests = [
        ("Multiple showcases per user", test_multiple_showcases_per_user),
        ("Ownership scoping", test_ownership_scoping),
        ("Add explicit + default", test_add_explicit_and_default),
        ("Item mutation scoped by join", test_item_mutation_scoped_by_join),
        ("Total value", test_total_value),
        ("Delete cascades items + shares", test_delete_cascades_items_and_shares),
    ]
    total_failed = 0
    for title, fn in tests:
        print(f"\n=== {title} ===")
        total_failed += fn()
    print("\n" + "=" * 60)
    if total_failed:
        print(f"TOTAL: {total_failed} failed")
        sys.exit(1)
    print("TOTAL: all passed")


if __name__ == "__main__":
    main()
