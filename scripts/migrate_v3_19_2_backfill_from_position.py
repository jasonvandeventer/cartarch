"""Backfill ``inventory_rows.from_drawer`` / ``from_slot`` for pending rows
whose old physical position was lost by ``resort_collection`` runs prior to
v3.19.1.

v3.19.1 added the ``from_drawer`` / ``from_slot`` columns and updated
``resort_collection`` to capture the previous position when a placed row gets
pulled to pending. Rows that were pulled to pending BEFORE v3.19.1 deployed
already had their ``drawer`` / ``storage_location_id`` overwritten with the
NEW target, so the pending page can't tell where the card physically came
from — both FROM and TO collapse onto the same drawer.

Recovery: ``resort_collection`` already wrote a TransactionLog entry with
``event_type='resort'`` and ``source_location="drawer=N slot=M"`` for every
cross-drawer move. Parse the most recent such entry per affected row and
populate the new columns retroactively.

Idempotent: rows that already have ``from_drawer`` set are skipped, so
re-running the migration is safe.
"""

from __future__ import annotations

import re

from sqlalchemy import text

from app.db import engine

_SOURCE_RE = re.compile(r"drawer=(\S+)\s+slot=(\S+)")


def main() -> None:
    with engine.begin() as conn:
        # Find pending rows missing the breadcrumb fields.
        pending_rows = conn.execute(
            text("SELECT id FROM inventory_rows " "WHERE is_pending = 1 AND from_drawer IS NULL")
        ).fetchall()

        backfilled = 0
        skipped_no_log = 0

        for (row_id,) in pending_rows:
            # Most-recent resort log for this row carries the old drawer/slot
            # in source_location.
            log = conn.execute(
                text(
                    "SELECT source_location FROM transaction_logs "
                    "WHERE inventory_row_id = :row_id "
                    "  AND event_type = 'resort' "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"row_id": row_id},
            ).fetchone()

            if not log or not log[0]:
                skipped_no_log += 1
                continue

            match = _SOURCE_RE.match(log[0])
            if not match:
                skipped_no_log += 1
                continue

            old_drawer = match.group(1)
            old_slot_raw = match.group(2)
            old_slot = None if old_slot_raw in ("-", "None", "") else old_slot_raw

            conn.execute(
                text(
                    "UPDATE inventory_rows "
                    "SET from_drawer = :drawer, from_slot = :slot "
                    "WHERE id = :row_id"
                ),
                {"drawer": old_drawer, "slot": old_slot, "row_id": row_id},
            )
            backfilled += 1

        print(
            f"Backfill complete: {backfilled} pending rows updated from audit log, "
            f"{skipped_no_log} skipped (no matching resort log entry)."
        )


if __name__ == "__main__":
    main()
