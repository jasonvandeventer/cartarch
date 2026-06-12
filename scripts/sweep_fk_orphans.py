"""Idempotent FK-orphan sweep — remediate references to deleted inventory rows.

Two tables reference ``inventory_rows.id``. When the inventory row is gone the
reference is dangling: (a) silently wrong today (SQLite runs with
``PRAGMA foreign_keys=OFF``), and (b) it **BLOCKS enabling the FK at the Postgres
cutover** — pgloader / `ALTER TABLE ... ADD CONSTRAINT` fails on the violating
rows. This sweep makes both FKs enable-able, with the right remediation per FK:

  - ``showcase_items.inventory_row_id`` — **NOT NULL**, ON DELETE NO ACTION. An
    orphan is a meaningless pointer (the item only exists to reference a row) and
    can't be NULLed → **DELETE the showcase_item**.
  - ``trade_items.inventory_row_id`` — **nullable**, ON DELETE NO ACTION. The
    trade_item carries the durable ``*_at_trade`` snapshot (decision A4), so the
    trade record must survive → **NULL the dangling reference** (NOT delete the
    row — deleting would lose trade history). This matches what the app's own
    delete paths already do via ``abandon_pending_trades_for_inventory_rows``.

Idempotent: a second run finds zero orphans and makes zero changes. Safe to run
on prod data during cutover prep.

Usage:
    DATA_DIR=dev-data python -m scripts.sweep_fk_orphans            # apply + report
    DATA_DIR=dev-data python -m scripts.sweep_fk_orphans --dry-run  # report only
"""

from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from app.models import InventoryRow, ShowcaseItem, TradeItem


def find_orphans(session: Session) -> dict[str, list[int]]:
    """Return the ids of orphaned rows by type (no remediation)."""
    showcase = [
        row_id
        for (row_id,) in session.query(ShowcaseItem.id)
        .outerjoin(InventoryRow, ShowcaseItem.inventory_row_id == InventoryRow.id)
        .filter(InventoryRow.id.is_(None))
        .all()
    ]
    trade = [
        row_id
        for (row_id,) in session.query(TradeItem.id)
        .outerjoin(InventoryRow, TradeItem.inventory_row_id == InventoryRow.id)
        .filter(TradeItem.inventory_row_id.isnot(None), InventoryRow.id.is_(None))
        .all()
    ]
    return {"showcase_items": showcase, "trade_items": trade}


def sweep_fk_orphans(session: Session, *, apply: bool = True) -> dict[str, int]:
    """Detect (and, when ``apply``, remediate) FK orphans. Returns counts by type.

    ``showcase_items_deleted`` — orphaned showcase_items removed.
    ``trade_items_nulled``     — orphaned trade_items whose inventory_row_id was NULLed.

    With ``apply=False`` the counts are what *would* be remediated; nothing is
    changed and nothing is committed.
    """
    orphans = find_orphans(session)
    si_ids = orphans["showcase_items"]
    ti_ids = orphans["trade_items"]

    if apply:
        if si_ids:
            session.query(ShowcaseItem).filter(ShowcaseItem.id.in_(si_ids)).delete(
                synchronize_session=False
            )
        if ti_ids:
            session.query(TradeItem).filter(TradeItem.id.in_(ti_ids)).update(
                {TradeItem.inventory_row_id: None}, synchronize_session=False
            )
        session.commit()

    return {"showcase_items_deleted": len(si_ids), "trade_items_nulled": len(ti_ids)}


def main(argv: list[str] | None = None) -> dict[str, int]:
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in argv

    from app.db import SessionLocal

    with SessionLocal() as session:
        result = sweep_fk_orphans(session, apply=not dry)

    mode = "DRY-RUN (no changes written)" if dry else "APPLIED"
    print(f"FK-orphan sweep [{mode}]")
    print(f"  orphaned showcase_items deleted: {result['showcase_items_deleted']}")
    print(f"  orphaned trade_items nulled:     {result['trade_items_nulled']}")
    return result


if __name__ == "__main__":
    main()
