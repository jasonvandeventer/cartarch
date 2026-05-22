"""Drawer read services.

Drawers are a presentation of inventory rows, not independent ownership
containers. Every query is scoped by InventoryRow.user_id.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, joinedload

from app.models import InventoryRow, StorageLocation


def list_drawer_groups(session: Session, user_id: int) -> dict[str, list[InventoryRow]]:
    """Return placed inventory rows grouped by drawer for one user.

    v3.27.7 — the join on StorageLocation + ``StorageLocation.type ==
    'drawer'`` filter is the one-line bug fix folded into the token-
    foundation release. Without it, ``list_drawer_groups`` returned ALL
    non-pending inventory rows for the user (regardless of location
    type), and the Drawers page bucketed deck-type rows under the ``-``
    group via the ``row.drawer or "-"`` grouping below. Documented as
    one of the three cross-page-disparity issues in current-status.md
    Known Problems (diagnostic captured pre-v3.27.7 via vault commit
    cff1c5e); this fix closes the over-count issue specifically. The
    other two cross-page issues (unit mismatch, pending-row policy)
    remain open as v3.27.9 prerequisites.
    """
    rows = (
        session.query(InventoryRow)
        .join(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(False),
            StorageLocation.type == "drawer",
        )
        .all()
    )

    grouped: dict[str, list[InventoryRow]] = {}
    for row in rows:
        grouped.setdefault(row.drawer or "-", []).append(row)

    return grouped


def list_rows_for_drawer(session: Session, drawer: str, user_id: int) -> list[InventoryRow]:
    """Return placed rows for one user's drawer."""
    return (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.drawer == drawer,
            InventoryRow.is_pending.is_(False),
        )
        .order_by(InventoryRow.slot.asc(), InventoryRow.id.asc())
        .all()
    )
