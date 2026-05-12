"""Smoke test for ``find_inventory_matches_for_deck_import`` (Session 1).

Creates and tears down its own test data — two throwaway users, fresh
locations, decks, and inventory rows — so the script is idempotent and
doesn't pollute the dev DB or depend on existing collection state.

Six scenarios, matching the Session 1 implementation plan:

    A. owns 4 in drawer, needs 4   -> move_existing
    B. owns 2 in drawer, needs 4   -> move_existing_plus_new
    C. owns 0 of card, needs 4     -> import_new
    D. owns 2 in OTHER deck + 2 in drawer, needs 4
                                   -> move_existing_plus_new (drawer only)
    E. owns 1 in drawer + 1 in binder, needs 2
                                   -> move_existing (drawer first, then binder)
    F. user with NO drawers, owns 2 in binder, needs 4
                                   -> move_existing_plus_new (binder source)

Run from project root:
    DATA_DIR=$(pwd)/dev-data SESSION_SECRET_KEY=test \\
      python -m scripts.manual_test_reconciliation

The ``-m`` invocation ensures the project root is on ``sys.path`` so
``from app...`` imports resolve.
"""

from __future__ import annotations

import sys
import time

from app.db import SessionLocal
from app.deck_service import find_inventory_matches_for_deck_import
from app.models import Card, Deck, InventoryRow, StorageLocation, User


def setup(session):
    """Create two test users with the inventory state the scenarios need.

    Returns a SimpleNamespace-like object with handles to created entities.
    """
    # Re-use real Card records from the catalog so we don't have to fabricate
    # Scryfall metadata. We need 6 distinct cards for the 6 scenarios so the
    # tuple-IN lookup picks each row independently.
    cards = session.query(Card).limit(6).all()
    if len(cards) < 6:
        raise RuntimeError(
            f"dev DB has only {len(cards)} Card rows; need at least 6 for the smoke test"
        )

    stamp = int(time.time())

    user1 = User(
        username=f"_smoke_recon_user1_{stamp}",
        password_hash="x",
        display_name="Smoke Recon 1 (with drawers)",
        is_active=True,
        is_admin=False,
    )
    user2 = User(
        username=f"_smoke_recon_user2_{stamp}",
        password_hash="x",
        display_name="Smoke Recon 2 (no drawers)",
        is_active=True,
        is_admin=False,
    )
    session.add_all([user1, user2])
    session.flush()

    # User 1: drawer, binder, OTHER deck, TARGET deck
    drawer_loc = StorageLocation(user_id=user1.id, name="Drawer 1", type="drawer")
    binder_loc = StorageLocation(user_id=user1.id, name="Binder A", type="binder")
    other_deck_loc = StorageLocation(user_id=user1.id, name="Other Deck", type="deck")
    target_deck_loc = StorageLocation(user_id=user1.id, name="Target Deck", type="deck")
    # User 2: binder + TARGET deck only (no drawer)
    binder_loc_2 = StorageLocation(user_id=user2.id, name="Binder Z", type="binder")
    target_deck_loc_2 = StorageLocation(user_id=user2.id, name="Target Deck", type="deck")

    session.add_all(
        [drawer_loc, binder_loc, other_deck_loc, target_deck_loc, binder_loc_2, target_deck_loc_2]
    )
    session.flush()

    deck1 = Deck(
        user_id=user1.id,
        storage_location_id=target_deck_loc.id,
        name="Smoke Target Deck (u1)",
        format="Commander",
    )
    deck2 = Deck(
        user_id=user2.id,
        storage_location_id=target_deck_loc_2.id,
        name="Smoke Target Deck (u2)",
        format="Commander",
    )
    session.add_all([deck1, deck2])
    session.flush()

    # Inventory rows wiring each scenario:
    inv_rows = [
        # A: user1 owns 4 of cards[0] in drawer
        InventoryRow(
            user_id=user1.id,
            card_id=cards[0].id,
            finish="normal",
            quantity=4,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # B: user1 owns 2 of cards[1] in drawer
        InventoryRow(
            user_id=user1.id,
            card_id=cards[1].id,
            finish="normal",
            quantity=2,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # C: cards[2] — intentionally no row (user1 owns 0)
        # D: user1 owns 2 of cards[3] in OTHER deck + 2 in drawer
        InventoryRow(
            user_id=user1.id,
            card_id=cards[3].id,
            finish="normal",
            quantity=2,
            storage_location_id=other_deck_loc.id,
            is_pending=False,
        ),
        InventoryRow(
            user_id=user1.id,
            card_id=cards[3].id,
            finish="normal",
            quantity=2,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # E: user1 owns 1 of cards[4] in drawer + 1 in binder
        InventoryRow(
            user_id=user1.id,
            card_id=cards[4].id,
            finish="normal",
            quantity=1,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        InventoryRow(
            user_id=user1.id,
            card_id=cards[4].id,
            finish="normal",
            quantity=1,
            storage_location_id=binder_loc.id,
            is_pending=False,
        ),
        # F: user2 owns 2 of cards[5] in binder (no drawer for this user)
        InventoryRow(
            user_id=user2.id,
            card_id=cards[5].id,
            finish="normal",
            quantity=2,
            storage_location_id=binder_loc_2.id,
            is_pending=False,
        ),
    ]
    session.add_all(inv_rows)
    session.commit()

    return {
        "user1": user1,
        "user2": user2,
        "deck1": deck1,
        "deck2": deck2,
        "cards": cards,
        "drawer_loc_id": drawer_loc.id,
        "binder_loc_id": binder_loc.id,
        "other_deck_loc_id": other_deck_loc.id,
        "binder_loc_2_id": binder_loc_2.id,
    }


def teardown(session, env):
    """Delete everything we created. Order respects FKs."""
    if env is None:
        return
    user_ids = [env["user1"].id, env["user2"].id]
    session.query(InventoryRow).filter(InventoryRow.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    session.query(Deck).filter(Deck.user_id.in_(user_ids)).delete(synchronize_session=False)
    session.query(StorageLocation).filter(StorageLocation.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    session.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    session.commit()


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def _check(label, condition, detail):
    return (label, bool(condition), detail)


def run_scenarios(session, env):
    """Run all six scenarios; return a list of (label, passed, detail) tuples."""
    results = []

    # ---- Scenario A: 4 in drawer, needs 4 -> move_existing ----
    out = find_inventory_matches_for_deck_import(
        session,
        env["user1"].id,
        env["deck1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][0].scryfall_id,
                "finish": "normal",
                "quantity": 4,
            }
        ],
    )
    row = out[0]
    matches = row["matches"]
    results.append(
        _check(
            "A.action",
            row["recommended_action"] == "move_existing"
            and row["recommended_move_qty"] == 4
            and row["recommended_new_qty"] == 0
            and row["total_available"] == 4,
            f"got action={row['recommended_action']} move={row['recommended_move_qty']} "
            f"new={row['recommended_new_qty']} total={row['total_available']}",
        )
    )
    results.append(
        _check(
            "A.match",
            len(matches) == 1
            and matches[0]["location_type"] == "drawer"
            and matches[0]["quantity_available"] == 4,
            f"got matches={matches!r}",
        )
    )

    # ---- Scenario B: 2 in drawer, needs 4 -> move_existing_plus_new ----
    out = find_inventory_matches_for_deck_import(
        session,
        env["user1"].id,
        env["deck1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][1].scryfall_id,
                "finish": "normal",
                "quantity": 4,
            }
        ],
    )
    row = out[0]
    matches = row["matches"]
    results.append(
        _check(
            "B.action",
            row["recommended_action"] == "move_existing_plus_new"
            and row["recommended_move_qty"] == 2
            and row["recommended_new_qty"] == 2
            and row["total_available"] == 2,
            f"got action={row['recommended_action']} move={row['recommended_move_qty']} "
            f"new={row['recommended_new_qty']} total={row['total_available']}",
        )
    )
    results.append(
        _check(
            "B.match",
            len(matches) == 1
            and matches[0]["location_type"] == "drawer"
            and matches[0]["quantity_available"] == 2,
            f"got matches={matches!r}",
        )
    )

    # ---- Scenario C: owns 0, needs 4 -> import_new ----
    out = find_inventory_matches_for_deck_import(
        session,
        env["user1"].id,
        env["deck1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][2].scryfall_id,
                "finish": "normal",
                "quantity": 4,
            }
        ],
    )
    row = out[0]
    results.append(
        _check(
            "C.action",
            row["recommended_action"] == "import_new"
            and row["recommended_move_qty"] == 0
            and row["recommended_new_qty"] == 4
            and row["total_available"] == 0
            and row["matches"] == [],
            f"got action={row['recommended_action']} move={row['recommended_move_qty']} "
            f"new={row['recommended_new_qty']} total={row['total_available']} "
            f"matches={row['matches']!r}",
        )
    )

    # ---- Scenario D: 2 in OTHER deck + 2 in drawer, needs 4 ----
    out = find_inventory_matches_for_deck_import(
        session,
        env["user1"].id,
        env["deck1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][3].scryfall_id,
                "finish": "normal",
                "quantity": 4,
            }
        ],
    )
    row = out[0]
    matches = row["matches"]
    # The OTHER deck row must be filtered out — only the drawer match remains.
    only_drawer = (
        len(matches) == 1
        and matches[0]["location_type"] == "drawer"
        and matches[0]["quantity_available"] == 2
    )
    results.append(
        _check(
            "D.action",
            row["recommended_action"] == "move_existing_plus_new"
            and row["recommended_move_qty"] == 2
            and row["recommended_new_qty"] == 2
            and row["total_available"] == 2,
            f"got action={row['recommended_action']} move={row['recommended_move_qty']} "
            f"new={row['recommended_new_qty']} total={row['total_available']}",
        )
    )
    results.append(
        _check(
            "D.exclude_other_deck",
            only_drawer,
            f"got matches={matches!r}",
        )
    )

    # ---- Scenario E: 1 in drawer + 1 in binder, needs 2 ----
    out = find_inventory_matches_for_deck_import(
        session,
        env["user1"].id,
        env["deck1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][4].scryfall_id,
                "finish": "normal",
                "quantity": 2,
            }
        ],
    )
    row = out[0]
    matches = row["matches"]
    ordered = (
        len(matches) == 2
        and matches[0]["location_type"] == "drawer"
        and matches[1]["location_type"] == "binder"
    )
    results.append(
        _check(
            "E.action",
            row["recommended_action"] == "move_existing"
            and row["recommended_move_qty"] == 2
            and row["recommended_new_qty"] == 0
            and row["total_available"] == 2,
            f"got action={row['recommended_action']} move={row['recommended_move_qty']} "
            f"new={row['recommended_new_qty']} total={row['total_available']}",
        )
    )
    results.append(
        _check(
            "E.match_order",
            ordered,
            f"got match types={[m['location_type'] for m in matches]}",
        )
    )

    # ---- Scenario F: user2 (no drawers), 2 in binder, needs 4 ----
    out = find_inventory_matches_for_deck_import(
        session,
        env["user2"].id,
        env["deck2"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][5].scryfall_id,
                "finish": "normal",
                "quantity": 4,
            }
        ],
    )
    row = out[0]
    matches = row["matches"]
    binder_first = (
        len(matches) == 1
        and matches[0]["location_type"] == "binder"
        and matches[0]["quantity_available"] == 2
    )
    results.append(
        _check(
            "F.action",
            row["recommended_action"] == "move_existing_plus_new"
            and row["recommended_move_qty"] == 2
            and row["recommended_new_qty"] == 2
            and row["total_available"] == 2,
            f"got action={row['recommended_action']} move={row['recommended_move_qty']} "
            f"new={row['recommended_new_qty']} total={row['total_available']}",
        )
    )
    results.append(
        _check(
            "F.no_drawer_user",
            binder_first,
            f"got matches={matches!r}",
        )
    )

    return results


def main():
    session = SessionLocal()
    env = None
    try:
        env = setup(session)
        results = run_scenarios(session, env)
        any_failed = False
        for label, passed, detail in results:
            mark = "PASS" if passed else "FAIL"
            print(f"  [{mark}] {label}: {detail}")
            if not passed:
                any_failed = True
        print()
        print("OVERALL:", "FAIL" if any_failed else "PASS")
        return 1 if any_failed else 0
    finally:
        teardown(session, env)
        session.close()


if __name__ == "__main__":
    sys.exit(main())
