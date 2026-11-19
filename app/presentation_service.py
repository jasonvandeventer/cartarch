"""Presentation helpers for shaping inventory data for templates."""

from __future__ import annotations

from app.inventory_service import get_drawer_label, get_location_label
from app.pricing import inventory_unit_price


def pending_source_labels(session, user_id, rows):
    """Trace the copies currently pending, newest movement first."""
    from app.models import TransactionLog

    remaining = {row.id: row.quantity for row in rows if not row.from_drawer}
    if not remaining:
        return {}
    events = (
        session.query(
            TransactionLog.inventory_row_id,
            TransactionLog.event_type,
            TransactionLog.source_location,
            TransactionLog.destination_location,
            TransactionLog.quantity_delta,
        )
        .filter(
            TransactionLog.user_id == user_id,
            TransactionLog.inventory_row_id.in_(remaining),
            TransactionLog.reversed_at.is_(None),
        )
        .order_by(TransactionLog.created_at.desc(), TransactionLog.id.desc())
    )
    sources = {}
    for row_id, event, source, destination, quantity in events:
        if remaining[row_id] <= 0:
            continue
        if event in ("import", "imported") and destination == "pending" and quantity > 0:
            label = "New import"
        elif (
            event in ("return_from_deck", "switch_printing")
            and destination == "collection"
            and source
            and quantity > 0
        ):
            kind, _, name = source.partition(":")
            label = {"deck": f"Deck · {name}", "considering": f"Considering · {name}"}.get(
                kind, source
            )
        else:
            label = "Previous location unknown"
            quantity = remaining[row_id]
        sources.setdefault(row_id, set()).add(label)
        remaining[row_id] -= quantity

    labels = {}
    for row_id, names in sources.items():
        if remaining[row_id] > 0:
            names.add("Previous location unknown")
        labels[row_id] = next(iter(names)) if len(names) == 1 else "Multiple sources"
    return labels


def build_pending_view_model(rows, drawer_locations=None, source_labels=None) -> dict:
    """Build template payload pieces for the pending-placement page."""
    drawer_locations = drawer_locations or {}
    items = []
    grouped = {}
    total_copies = 0

    for row in rows:
        price = inventory_unit_price(row)
        if row.from_drawer:
            from_label = get_drawer_label(row.from_drawer, drawer_locations.get(row.from_drawer))
        else:
            from_label = (source_labels or {}).get(row.id, "Previous location unknown")
        item = {
            "id": row.id,
            "card": row.card,
            "finish": row.finish,
            "language": row.language or "en",
            "is_proxy": bool(row.is_proxy),
            "quantity": row.quantity,
            "current_location_label": from_label,
            "from_slot": row.from_slot,
            "target_location_label": get_location_label(row),
            "drawer": row.drawer,
            "slot": row.slot,
            "price": price,
        }
        items.append(item)
        total_copies += row.quantity

        if row.storage_location and row.storage_location.type == "drawer":
            drawer_number = row.storage_location.name.replace("Drawer", "").strip()
        else:
            drawer_number = "-"

        grouped.setdefault(drawer_number, []).append(item)

    grouped_drawers = []
    for key in sorted(grouped.keys(), key=lambda x: (x == "-", int(x) if x.isdigit() else 999, x)):
        grouped_drawers.append(
            {
                "drawer": key,
                "label": get_drawer_label(key, drawer_locations.get(key)),
                "count": len(grouped[key]),
                "entries": grouped[key],
            }
        )

    return {
        "items": items,
        "grouped_drawers": grouped_drawers,
        "pending_count": len(items),
        "drawer_count": len(grouped_drawers),
        "total_copies": total_copies,
    }


def build_pending_batch_groups(session, user_id: int, items: list[dict]) -> list[dict]:
    """v3.28.7 — group pending items by import batch for the editorial-row
    pending page (non-drawer-sorter path).

    `InventoryRow` has no direct FK to `ImportBatch`; the link lives on
    `TransactionLog.batch_id`. This function joins pending row ids → most-
    recent import event → batch → batch.filename + imported_at, in a
    single batched window query — strict no-N+1.
    Rows without a matching imported event fall into a "Manual" pseudo-batch
    so they still group cleanly.

    Returns a list of batch dicts ordered by batch_imported_at DESC:
      [{batch_id, source, date, note, count, entries: [item, ...]}]
    """
    from sqlalchemy import func

    from app.models import ImportBatch, TransactionLog

    if not items:
        return []

    row_ids = [it["id"] for it in items]

    # Rank within each row so timestamps, then IDs, determine the latest import.
    ranked = (
        session.query(
            TransactionLog.inventory_row_id.label("row_id"),
            TransactionLog.batch_id,
            func.row_number()
            .over(
                partition_by=TransactionLog.inventory_row_id,
                order_by=(TransactionLog.created_at.desc(), TransactionLog.id.desc()),
            )
            .label("position"),
        )
        .filter(
            TransactionLog.user_id == user_id,
            TransactionLog.inventory_row_id.in_(row_ids),
            TransactionLog.event_type.in_(("import", "imported")),
            TransactionLog.batch_id.isnot(None),
        )
        .subquery()
    )
    row_to_batch = dict(
        session.query(ranked.c.row_id, ranked.c.batch_id).filter(ranked.c.position == 1).all()
    )

    batch_ids = sorted({b for b in row_to_batch.values() if b is not None})
    batches: dict[int, ImportBatch] = {}
    if batch_ids:
        for b in (
            session.query(ImportBatch)
            .filter(ImportBatch.id.in_(batch_ids), ImportBatch.user_id == user_id)
            .all()
        ):
            batches[b.id] = b

    grouped: dict[int | str, dict] = {}
    for item in items:
        batch_id = row_to_batch.get(item["id"])
        batch = batches.get(batch_id) if batch_id else None
        key = batch_id if batch else "_manual"
        if key not in grouped:
            if batch:
                # Derive a clean "source" label from the filename (Helvault /
                # Moxfield / paste-list / CSV) when the prefix is obvious;
                # otherwise the bare filename. Editorial register matches the
                # design package's narrative batch headers.
                fname = (batch.filename or "").strip()
                lower = fname.lower()
                if "helvault" in lower:
                    source = "Helvault export"
                elif "moxfield" in lower:
                    source = "Moxfield export"
                elif fname.endswith(".csv"):
                    source = f"CSV — {fname}"
                elif fname.startswith("paste"):
                    source = "Pasted list"
                else:
                    source = fname or "Import"
                grouped[key] = {
                    "batch_id": batch_id,
                    "source": source,
                    "date": batch.imported_at,
                    "note": fname,
                    "_sort": batch.imported_at,
                    "entries": [],
                }
            else:
                grouped[key] = {
                    "batch_id": None,
                    "source": "Manual entry",
                    "date": None,
                    "note": "Added by hand",
                    "_sort": None,
                    "entries": [],
                }
        grouped[key]["entries"].append(item)

    out = []
    for _key, g in grouped.items():
        g["count"] = len(g["entries"])
        out.append(g)
    # Order: most recent batch first; "_manual" goes to the end.
    out.sort(key=lambda g: (g["_sort"] is None, -(g["_sort"].timestamp() if g["_sort"] else 0)))
    return out
