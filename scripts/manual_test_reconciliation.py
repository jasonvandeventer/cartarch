"""Smoke test for ``find_inventory_matches_for_deck_import`` (Session 1).

Creates and tears down its own test data — two throwaway users, fresh
locations, decks, and inventory rows — so the script is idempotent and
doesn't pollute the dev DB or depend on existing collection state.

Scenarios A-F (read-only — Session 1):

    A. owns 4 in drawer, needs 4   -> move_existing
    B. owns 2 in drawer, needs 4   -> move_existing_plus_new
    C. owns 0 of card, needs 4     -> import_new
    D. owns 2 in OTHER deck + 2 in drawer, needs 4
                                   -> move_existing_plus_new (drawer only)
    E. owns 1 in drawer + 1 in binder, needs 2
                                   -> move_existing (drawer first, then binder)
    F. user with NO drawers, owns 2 in binder, needs 4
                                   -> move_existing_plus_new (binder source)

Scenarios G-H (end-to-end commit handler — Session 2):

    G. commit with move_existing for all rows: drawer row drained to 0
       (deleted), deck row created with qty=4, no new imports
    H. commit with import_new override (user owns 4 but chose to buy new):
       drawer row UNTOUCHED, deck row created with qty=4, one unique new
       import row created

Scenarios I-K (target_deck / other_deck handling — Session 2.5):

    I. commit auto-merges into existing target-deck row: target deck has
       1 of cards[8], import 2 more → existing deck row's qty goes to 3
       (NOT a separate row with qty=2)
    J. function returns target_deck_matches when target deck has the
       card; matches list stays empty; recommended_action=import_new
    K. function returns other_deck_matches when card is in a different
       deck; both lists are surfaced separately; matches list stays
       empty; recommended_action=import_new

Scenarios L-P (collection-mode sync — Session A):

    L. user owns 4 of cards[11] in a drawer, imports 4 to auto-sort
       → "skip_already_owned", new_qty=0, owned_breakdown has 1 entry
    M. user owns 2 of cards[12] in a drawer, imports 4
       → "import_delta", new_qty=2, total_user_owned=2
    N. user owns 1 of cards[13] in the target deck (NO non-deck copies),
       imports 1 → "skip_already_owned" (deck rows count!), new_qty=0,
       owned_breakdown has 1 deck-type entry
    P. user owns 2 pending rows of cards[14], imports 4
       → "import_delta", new_qty=2, owned_breakdown has 1 pending entry

Scenarios Q-T (collection-mode commit handler — Session B):

    Q. owns 4 of cards[15] in drawer, action=skip_already_owned, qty=4
       → no new InventoryRow created, skipped_count=4, drawer untouched,
         one import_skipped TransactionLog entry
    R. owns 2 of cards[16] in drawer, target=binder, action=import_delta,
       new_qty=2, qty=4 → 1 new row qty=2 placed in binder, drawer
       untouched, skipped_count=2 (delta portion)
    S. owns 4 of cards[17] in drawer, action=import_new (override),
       new_qty=4 → 1 new pending row qty=4, drawer untouched,
       skipped_count=0
    T. owns 4 of cards[18] in drawer initially, but qty is decremented to 2
       before commit. Action=skip_already_owned with qty=4 → stale-match
       fallback to import_delta, new row qty=2 created, skipped_count=2,
       stale_match_rows has 1 entry with reason="inventory_decreased"

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
from app.inventory_service import find_inventory_matches_for_collection_import
from app.main import (
    _commit_collection_import_with_reconciliation,
    _commit_deck_import_with_reconciliation,
)
from app.models import (
    Card,
    Deck,
    ImportBatch,
    InventoryRow,
    StorageLocation,
    TransactionLog,
    User,
)


def setup(session):
    """Create two test users with the inventory state the scenarios need.

    Returns a SimpleNamespace-like object with handles to created entities.
    """
    # Re-use real Card records from the catalog. Scenarios A-F use cards
    # 0-5 (read-only). G-H use cards 6-7 (commit mutates drawer rows).
    # I uses card 8 (target deck already has it, commit auto-merges). J
    # uses card 9 (target deck has it, function output check). K uses
    # card 10 (in other deck, function output check). L uses card 11
    # (drawer, full coverage). M uses card 12 (drawer, partial coverage).
    # N uses card 13 (target deck only — deck rows count toward owned).
    # P uses card 14 (pending row only — pending counts toward owned).
    # Q-T use cards 15-18 (collection-mode commit handler dispatch tests).
    # Distinct cards per scenario keep them independent.
    cards = session.query(Card).limit(19).all()
    if len(cards) < 19:
        raise RuntimeError(
            f"dev DB has only {len(cards)} Card rows; need at least 19 for the smoke test"
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
        # G: user1 owns 4 of cards[6] in drawer (will be drained by commit)
        InventoryRow(
            user_id=user1.id,
            card_id=cards[6].id,
            finish="normal",
            quantity=4,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # H: user1 owns 4 of cards[7] in drawer (untouched by commit — user
        # overrides default to import_new)
        InventoryRow(
            user_id=user1.id,
            card_id=cards[7].id,
            finish="normal",
            quantity=4,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # I: user1 owns 1 of cards[8] in the TARGET deck (deck1). Importing
        # 2 more should merge into this existing row (qty 1 -> 3).
        InventoryRow(
            user_id=user1.id,
            card_id=cards[8].id,
            finish="normal",
            quantity=1,
            storage_location_id=target_deck_loc.id,
            is_pending=False,
        ),
        # J: user1 owns 1 of cards[9] in the TARGET deck. Used for function
        # output verification — target_deck_matches should report it.
        InventoryRow(
            user_id=user1.id,
            card_id=cards[9].id,
            finish="normal",
            quantity=1,
            storage_location_id=target_deck_loc.id,
            is_pending=False,
        ),
        # K: user1 owns 1 of cards[10] in the OTHER deck (other_deck_loc).
        # Used for function output verification — other_deck_matches should
        # report it; matches list stays empty (not movable by default).
        InventoryRow(
            user_id=user1.id,
            card_id=cards[10].id,
            finish="normal",
            quantity=1,
            storage_location_id=other_deck_loc.id,
            is_pending=False,
        ),
        # L: user1 owns 4 of cards[11] in drawer. Importing 4 should
        # skip_already_owned in collection mode.
        InventoryRow(
            user_id=user1.id,
            card_id=cards[11].id,
            finish="normal",
            quantity=4,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # M: user1 owns 2 of cards[12] in drawer. Importing 4 should
        # import_delta with new_qty=2.
        InventoryRow(
            user_id=user1.id,
            card_id=cards[12].id,
            finish="normal",
            quantity=2,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # N: user1 owns 1 of cards[13] ONLY in the target deck — no
        # non-deck copies. Importing 1 should still skip (deck rows
        # count toward total_user_owned in collection mode).
        InventoryRow(
            user_id=user1.id,
            card_id=cards[13].id,
            finish="normal",
            quantity=1,
            storage_location_id=target_deck_loc.id,
            is_pending=False,
        ),
        # P: user1 owns 2 of cards[14] as a PENDING row (no
        # storage_location_id, is_pending=True). Importing 4 should
        # import_delta with new_qty=2 because pending counts toward owned.
        InventoryRow(
            user_id=user1.id,
            card_id=cards[14].id,
            finish="normal",
            quantity=2,
            storage_location_id=None,
            is_pending=True,
        ),
        # Q: user1 owns 4 of cards[15] in drawer. Skip action will record a
        # TransactionLog entry without mutating inventory.
        InventoryRow(
            user_id=user1.id,
            card_id=cards[15].id,
            finish="normal",
            quantity=4,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # R: user1 owns 2 of cards[16] in drawer; importing 4 with delta
        # places 2 new copies in binder.
        InventoryRow(
            user_id=user1.id,
            card_id=cards[16].id,
            finish="normal",
            quantity=2,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # S: user1 owns 4 of cards[17] in drawer; import_new override
        # creates a new pending row qty=4 without touching drawer.
        InventoryRow(
            user_id=user1.id,
            card_id=cards[17].id,
            finish="normal",
            quantity=4,
            storage_location_id=drawer_loc.id,
            is_pending=False,
        ),
        # T: user1 owns 4 of cards[18] in drawer; test mutates this to 2
        # before commit to trigger stale-match fallback (skip → delta).
        InventoryRow(
            user_id=user1.id,
            card_id=cards[18].id,
            finish="normal",
            quantity=4,
            storage_location_id=drawer_loc.id,
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
    """Delete everything we created. Order respects FKs.

    Scenarios G + H create ImportBatch + TransactionLog rows via the
    commit handler's persist_import_rows path; clean those too so the
    script is fully idempotent.
    """
    if env is None:
        return
    user_ids = [env["user1"].id, env["user2"].id]
    session.query(TransactionLog).filter(TransactionLog.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    session.query(ImportBatch).filter(ImportBatch.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
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

    # ---- Scenario G: commit with move_existing — drawer drained, deck filled ----
    # User1 owns 4 of cards[6] in drawer. Importing 4 of cards[6] into deck1
    # with action=move_existing should: pull 4 from drawer (row deleted because
    # quantity hits 0), create deck row with qty=4, persist NO new imports.
    parsed_g = [
        {
            "line_number": 1,
            "name": "",
            "scryfall_id": env["cards"][6].scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": "normal",
            "quantity": 4,
            "location": "",
        }
    ]
    result_g = _commit_deck_import_with_reconciliation(
        session=session,
        user_id=env["user1"].id,
        deck=env["deck1"],
        parsed_rows=parsed_g,
        actions=["move_existing"],
        move_qtys=[4],
        new_qtys=[0],
        filename="smoke G",
    )
    # Drawer row for cards[6] should be gone (qty reached 0 -> deleted).
    drawer_row_remaining = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][6].id,
            InventoryRow.storage_location_id == env["drawer_loc_id"],
        )
        .first()
    )
    # Deck row should exist with qty 4.
    deck_row_g = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][6].id,
            InventoryRow.storage_location_id == env["deck1"].storage_location_id,
        )
        .first()
    )
    results.append(
        _check(
            "G.moved_count",
            result_g["moved_count"] == 4,
            f"got moved_count={result_g['moved_count']}",
        )
    )
    results.append(
        _check(
            "G.imported_count",
            result_g["imported_count"] == 0,
            f"got imported_count={result_g['imported_count']}",
        )
    )
    results.append(
        _check(
            "G.drawer_drained",
            drawer_row_remaining is None,
            f"drawer row still exists: id={drawer_row_remaining.id if drawer_row_remaining else None} "
            f"qty={drawer_row_remaining.quantity if drawer_row_remaining else None}",
        )
    )
    results.append(
        _check(
            "G.deck_row_qty",
            deck_row_g is not None and deck_row_g.quantity == 4,
            f"deck row: {deck_row_g!r} qty={deck_row_g.quantity if deck_row_g else None}",
        )
    )

    # ---- Scenario H: commit with import_new override — drawer untouched ----
    # User1 owns 4 of cards[7] in drawer. Importing 4 of cards[7] into deck1
    # with action=import_new should: leave drawer row alone (qty still 4),
    # create a NEW deck row with qty=4.
    parsed_h = [
        {
            "line_number": 1,
            "name": "",
            "scryfall_id": env["cards"][7].scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": "normal",
            "quantity": 4,
            "location": "",
        }
    ]
    result_h = _commit_deck_import_with_reconciliation(
        session=session,
        user_id=env["user1"].id,
        deck=env["deck1"],
        parsed_rows=parsed_h,
        actions=["import_new"],
        move_qtys=[0],
        new_qtys=[4],
        filename="smoke H",
    )
    drawer_row_h = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][7].id,
            InventoryRow.storage_location_id == env["drawer_loc_id"],
        )
        .first()
    )
    deck_row_h = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][7].id,
            InventoryRow.storage_location_id == env["deck1"].storage_location_id,
        )
        .first()
    )
    results.append(
        _check(
            "H.moved_count",
            result_h["moved_count"] == 0,
            f"got moved_count={result_h['moved_count']}",
        )
    )
    results.append(
        _check(
            "H.imported_count",
            result_h["imported_count"] == 1,
            f"got imported_count={result_h['imported_count']}",
        )
    )
    results.append(
        _check(
            "H.drawer_untouched",
            drawer_row_h is not None and drawer_row_h.quantity == 4,
            f"drawer row: {drawer_row_h!r} qty={drawer_row_h.quantity if drawer_row_h else None}",
        )
    )
    results.append(
        _check(
            "H.deck_row_qty",
            deck_row_h is not None and deck_row_h.quantity == 4,
            f"deck row: {deck_row_h!r} qty={deck_row_h.quantity if deck_row_h else None}",
        )
    )

    # ---- Scenario I: commit auto-merges into existing target-deck row ----
    # Target deck has 1 of cards[8]. Importing 2 more should NOT create a
    # second deck row — instead the existing row's qty becomes 3.
    parsed_i = [
        {
            "line_number": 1,
            "name": "",
            "scryfall_id": env["cards"][8].scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": "normal",
            "quantity": 2,
            "location": "",
        }
    ]
    result_i = _commit_deck_import_with_reconciliation(
        session=session,
        user_id=env["user1"].id,
        deck=env["deck1"],
        parsed_rows=parsed_i,
        actions=["import_new"],
        move_qtys=[0],
        new_qtys=[2],
        filename="smoke I",
    )
    # Look for ALL deck1 rows for cards[8]. Should be exactly one with qty=3.
    deck_rows_i = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][8].id,
            InventoryRow.storage_location_id == env["deck1"].storage_location_id,
        )
        .all()
    )
    results.append(
        _check(
            "I.merged_count",
            result_i["merged_count"] == 2,
            f"got merged_count={result_i['merged_count']}",
        )
    )
    results.append(
        _check(
            "I.imported_count",
            result_i["imported_count"] == 0,
            f"got imported_count={result_i['imported_count']}",
        )
    )
    results.append(
        _check(
            "I.single_deck_row",
            len(deck_rows_i) == 1,
            f"got {len(deck_rows_i)} deck rows for cards[8] (expected 1)",
        )
    )
    results.append(
        _check(
            "I.merged_qty",
            len(deck_rows_i) == 1 and deck_rows_i[0].quantity == 3,
            f"got qty={deck_rows_i[0].quantity if deck_rows_i else None} (expected 3)",
        )
    )

    # ---- Scenario J: function returns target_deck_matches ----
    out_j = find_inventory_matches_for_deck_import(
        session,
        env["user1"].id,
        env["deck1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][9].scryfall_id,
                "finish": "normal",
                "quantity": 1,
            }
        ],
    )
    row_j = out_j[0]
    results.append(
        _check(
            "J.matches_empty",
            row_j["matches"] == [] and row_j["total_available"] == 0,
            f"matches={row_j['matches']!r} total={row_j['total_available']}",
        )
    )
    results.append(
        _check(
            "J.target_deck_match_present",
            len(row_j["target_deck_matches"]) == 1
            and row_j["target_deck_matches"][0]["quantity_available"] == 1
            and row_j["target_deck_matches"][0]["location_type"] == "deck"
            and row_j["total_in_target_deck"] == 1,
            f"target_deck_matches={row_j['target_deck_matches']!r} "
            f"total_in_target={row_j['total_in_target_deck']}",
        )
    )
    results.append(
        _check(
            "J.recommends_import_new",
            row_j["recommended_action"] == "import_new"
            and row_j["recommended_new_qty"] == 1
            and row_j["recommended_move_qty"] == 0,
            f"got action={row_j['recommended_action']} "
            f"move={row_j['recommended_move_qty']} new={row_j['recommended_new_qty']}",
        )
    )

    # ---- Scenario K: function returns other_deck_matches ----
    out_k = find_inventory_matches_for_deck_import(
        session,
        env["user1"].id,
        env["deck1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][10].scryfall_id,
                "finish": "normal",
                "quantity": 1,
            }
        ],
    )
    row_k = out_k[0]
    results.append(
        _check(
            "K.matches_empty",
            row_k["matches"] == [] and row_k["total_available"] == 0,
            f"matches={row_k['matches']!r}",
        )
    )
    results.append(
        _check(
            "K.target_deck_empty",
            row_k["target_deck_matches"] == [] and row_k["total_in_target_deck"] == 0,
            f"target_deck_matches={row_k['target_deck_matches']!r}",
        )
    )
    results.append(
        _check(
            "K.other_deck_match_present",
            len(row_k["other_deck_matches"]) == 1
            and row_k["other_deck_matches"][0]["quantity_available"] == 1
            and row_k["other_deck_matches"][0]["location_name"] == "Other Deck"
            and row_k["total_in_other_decks"] == 1,
            f"other_deck_matches={row_k['other_deck_matches']!r} "
            f"total_in_other={row_k['total_in_other_decks']}",
        )
    )
    results.append(
        _check(
            "K.recommends_import_new",
            row_k["recommended_action"] == "import_new",
            f"got action={row_k['recommended_action']}",
        )
    )

    # ---- Scenario L: collection-mode skip_already_owned ----
    # User owns 4 of cards[11] in a drawer. Importing 4 → skip.
    out_l = find_inventory_matches_for_collection_import(
        session,
        env["user1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][11].scryfall_id,
                "finish": "normal",
                "quantity": 4,
            }
        ],
    )
    row_l = out_l[0]
    results.append(
        _check(
            "L.action",
            row_l["recommended_action"] == "skip_already_owned"
            and row_l["recommended_new_qty"] == 0
            and row_l["total_user_owned"] == 4,
            f"got action={row_l['recommended_action']} new={row_l['recommended_new_qty']} "
            f"owned={row_l['total_user_owned']}",
        )
    )
    results.append(
        _check(
            "L.breakdown",
            len(row_l["owned_breakdown"]) == 1
            and row_l["owned_breakdown"][0]["location_type"] == "drawer"
            and row_l["owned_breakdown"][0]["quantity"] == 4,
            f"got breakdown={row_l['owned_breakdown']!r}",
        )
    )

    # ---- Scenario M: collection-mode import_delta ----
    # User owns 2 of cards[12] in a drawer. Importing 4 → import_delta, new_qty=2.
    out_m = find_inventory_matches_for_collection_import(
        session,
        env["user1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][12].scryfall_id,
                "finish": "normal",
                "quantity": 4,
            }
        ],
    )
    row_m = out_m[0]
    results.append(
        _check(
            "M.action",
            row_m["recommended_action"] == "import_delta"
            and row_m["recommended_new_qty"] == 2
            and row_m["total_user_owned"] == 2,
            f"got action={row_m['recommended_action']} new={row_m['recommended_new_qty']} "
            f"owned={row_m['total_user_owned']}",
        )
    )

    # ---- Scenario N: deck rows count toward owned in collection mode ----
    # User owns 1 of cards[13] ONLY in target deck. No non-deck copies.
    # Importing 1 → still skip (because deck rows count).
    out_n = find_inventory_matches_for_collection_import(
        session,
        env["user1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][13].scryfall_id,
                "finish": "normal",
                "quantity": 1,
            }
        ],
    )
    row_n = out_n[0]
    results.append(
        _check(
            "N.action",
            row_n["recommended_action"] == "skip_already_owned"
            and row_n["recommended_new_qty"] == 0
            and row_n["total_user_owned"] == 1,
            f"got action={row_n['recommended_action']} new={row_n['recommended_new_qty']} "
            f"owned={row_n['total_user_owned']}",
        )
    )
    results.append(
        _check(
            "N.deck_breakdown",
            len(row_n["owned_breakdown"]) == 1
            and row_n["owned_breakdown"][0]["location_type"] == "deck",
            f"got breakdown={row_n['owned_breakdown']!r}",
        )
    )

    # ---- Scenario P: pending rows count toward owned ----
    # User has 2 of cards[14] in pending (is_pending=True, no storage_location).
    # Importing 4 → import_delta, new_qty=2.
    out_p = find_inventory_matches_for_collection_import(
        session,
        env["user1"].id,
        [
            {
                "line_number": 1,
                "scryfall_id": env["cards"][14].scryfall_id,
                "finish": "normal",
                "quantity": 4,
            }
        ],
    )
    row_p = out_p[0]
    results.append(
        _check(
            "P.action",
            row_p["recommended_action"] == "import_delta"
            and row_p["recommended_new_qty"] == 2
            and row_p["total_user_owned"] == 2,
            f"got action={row_p['recommended_action']} new={row_p['recommended_new_qty']} "
            f"owned={row_p['total_user_owned']}",
        )
    )
    results.append(
        _check(
            "P.pending_breakdown",
            len(row_p["owned_breakdown"]) == 1
            and row_p["owned_breakdown"][0]["location_type"] == "pending"
            and row_p["owned_breakdown"][0]["location_name"] == "Pending",
            f"got breakdown={row_p['owned_breakdown']!r}",
        )
    )

    # ---- Scenario Q: collection-mode commit skip_already_owned ----
    # User owns 4 of cards[15] in drawer. Action=skip_already_owned with
    # qty=4 → no new InventoryRow, skipped_count=4, drawer untouched, an
    # import_skipped TransactionLog entry exists.
    parsed_q = [
        {
            "line_number": 1,
            "name": "",
            "scryfall_id": env["cards"][15].scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": "normal",
            "quantity": 4,
            "location": "",
        }
    ]
    result_q = _commit_collection_import_with_reconciliation(
        session=session,
        user_id=env["user1"].id,
        target_location_id=0,
        parsed_rows=parsed_q,
        actions=["skip_already_owned"],
        new_qtys=[0],
        filename="smoke Q",
    )
    drawer_row_q = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][15].id,
            InventoryRow.storage_location_id == env["drawer_loc_id"],
        )
        .first()
    )
    skip_log_q = (
        session.query(TransactionLog)
        .filter(
            TransactionLog.user_id == env["user1"].id,
            TransactionLog.card_id == env["cards"][15].id,
            TransactionLog.event_type == "import_skipped",
        )
        .first()
    )
    results.append(
        _check(
            "Q.skipped_count",
            result_q["skipped_count"] == 4
            and result_q["imported_count"] == 0
            and result_q["total_quantity"] == 0,
            f"got skipped={result_q['skipped_count']} "
            f"imported={result_q['imported_count']} "
            f"total_q={result_q['total_quantity']}",
        )
    )
    results.append(
        _check(
            "Q.drawer_untouched",
            drawer_row_q is not None and drawer_row_q.quantity == 4,
            f"drawer row: qty={drawer_row_q.quantity if drawer_row_q else None}",
        )
    )
    results.append(
        _check(
            "Q.skip_log",
            skip_log_q is not None,
            f"skip log: {skip_log_q!r}",
        )
    )

    # ---- Scenario R: collection-mode commit import_delta ----
    # User owns 2 of cards[16] in drawer. Action=import_delta, new_qty=2,
    # qty=4, target=binder → 1 new row qty=2 placed in binder, drawer
    # untouched, skipped_count=2 (the 2 already owned).
    parsed_r = [
        {
            "line_number": 1,
            "name": "",
            "scryfall_id": env["cards"][16].scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": "normal",
            "quantity": 4,
            "location": "",
        }
    ]
    result_r = _commit_collection_import_with_reconciliation(
        session=session,
        user_id=env["user1"].id,
        target_location_id=env["binder_loc_id"],
        parsed_rows=parsed_r,
        actions=["import_delta"],
        new_qtys=[2],
        filename="smoke R",
    )
    drawer_row_r = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][16].id,
            InventoryRow.storage_location_id == env["drawer_loc_id"],
        )
        .first()
    )
    binder_rows_r = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][16].id,
            InventoryRow.storage_location_id == env["binder_loc_id"],
        )
        .all()
    )
    results.append(
        _check(
            "R.counts",
            result_r["imported_count"] == 1
            and result_r["total_quantity"] == 2
            and result_r["skipped_count"] == 2,
            f"got imported={result_r['imported_count']} "
            f"total_q={result_r['total_quantity']} "
            f"skipped={result_r['skipped_count']}",
        )
    )
    results.append(
        _check(
            "R.drawer_untouched",
            drawer_row_r is not None and drawer_row_r.quantity == 2,
            f"drawer row: qty={drawer_row_r.quantity if drawer_row_r else None}",
        )
    )
    results.append(
        _check(
            "R.binder_placed",
            len(binder_rows_r) == 1 and binder_rows_r[0].quantity == 2,
            f"binder rows: count={len(binder_rows_r)} "
            f"qty={binder_rows_r[0].quantity if binder_rows_r else None}",
        )
    )

    # ---- Scenario S: collection-mode commit import_new override ----
    # User owns 4 of cards[17] in drawer. Action=import_new override with
    # new_qty=4, no target → 1 new pending row qty=4, drawer untouched,
    # skipped_count=0.
    parsed_s = [
        {
            "line_number": 1,
            "name": "",
            "scryfall_id": env["cards"][17].scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": "normal",
            "quantity": 4,
            "location": "",
        }
    ]
    result_s = _commit_collection_import_with_reconciliation(
        session=session,
        user_id=env["user1"].id,
        target_location_id=0,
        parsed_rows=parsed_s,
        actions=["import_new"],
        new_qtys=[4],
        filename="smoke S",
    )
    drawer_row_s = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][17].id,
            InventoryRow.storage_location_id == env["drawer_loc_id"],
        )
        .first()
    )
    pending_rows_s = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][17].id,
            InventoryRow.is_pending.is_(True),
        )
        .all()
    )
    results.append(
        _check(
            "S.counts",
            result_s["imported_count"] == 1
            and result_s["total_quantity"] == 4
            and result_s["skipped_count"] == 0,
            f"got imported={result_s['imported_count']} "
            f"total_q={result_s['total_quantity']} "
            f"skipped={result_s['skipped_count']}",
        )
    )
    results.append(
        _check(
            "S.drawer_untouched",
            drawer_row_s is not None and drawer_row_s.quantity == 4,
            f"drawer row: qty={drawer_row_s.quantity if drawer_row_s else None}",
        )
    )
    results.append(
        _check(
            "S.pending_created",
            len(pending_rows_s) == 1 and pending_rows_s[0].quantity == 4,
            f"pending rows: count={len(pending_rows_s)} "
            f"qty={pending_rows_s[0].quantity if pending_rows_s else None}",
        )
    )

    # ---- Scenario T: stale-match fallback (inventory decreased) ----
    # User owned 4 of cards[18] when preview rendered, but qty drops to 2
    # before commit (simulates concurrent sell). Action=skip_already_owned
    # with qty=4 → fallback to import_delta with new_qty=2. Drawer is left
    # at 2, a new pending row qty=2 is created, stale_match_rows reports
    # the adjustment.
    drawer_row_t = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][18].id,
            InventoryRow.storage_location_id == env["drawer_loc_id"],
        )
        .one()
    )
    drawer_row_t.quantity = 2
    session.commit()
    parsed_t = [
        {
            "line_number": 1,
            "name": "",
            "scryfall_id": env["cards"][18].scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": "normal",
            "quantity": 4,
            "location": "",
        }
    ]
    result_t = _commit_collection_import_with_reconciliation(
        session=session,
        user_id=env["user1"].id,
        target_location_id=0,
        parsed_rows=parsed_t,
        actions=["skip_already_owned"],
        new_qtys=[0],
        filename="smoke T",
    )
    drawer_row_t_after = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][18].id,
            InventoryRow.storage_location_id == env["drawer_loc_id"],
        )
        .first()
    )
    pending_rows_t = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.user_id == env["user1"].id,
            InventoryRow.card_id == env["cards"][18].id,
            InventoryRow.is_pending.is_(True),
        )
        .all()
    )
    results.append(
        _check(
            "T.counts",
            result_t["imported_count"] == 1
            and result_t["total_quantity"] == 2
            and result_t["skipped_count"] == 2,
            f"got imported={result_t['imported_count']} "
            f"total_q={result_t['total_quantity']} "
            f"skipped={result_t['skipped_count']}",
        )
    )
    results.append(
        _check(
            "T.stale_match_rows",
            len(result_t["stale_match_rows"]) == 1
            and result_t["stale_match_rows"][0]["reason"] == "inventory_decreased"
            and result_t["stale_match_rows"][0]["expected_skip"] == 4
            and result_t["stale_match_rows"][0]["actual_new_qty"] == 2,
            f"got stale_match_rows={result_t['stale_match_rows']!r}",
        )
    )
    results.append(
        _check(
            "T.drawer_untouched",
            drawer_row_t_after is not None and drawer_row_t_after.quantity == 2,
            f"drawer row qty={drawer_row_t_after.quantity if drawer_row_t_after else None}",
        )
    )
    results.append(
        _check(
            "T.pending_created",
            len(pending_rows_t) == 1 and pending_rows_t[0].quantity == 2,
            f"pending rows: count={len(pending_rows_t)} "
            f"qty={pending_rows_t[0].quantity if pending_rows_t else None}",
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
