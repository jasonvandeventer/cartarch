"""Presentation helpers for shaping inventory data for templates."""

from __future__ import annotations

from app.inventory_service import get_drawer_label, get_location_label
from app.pricing import effective_price


def build_collection_view_model(inventory_rows) -> dict:
    """Build template payload pieces for the collection page."""
    items = []
    total_value = 0.0
    total_cards = 0
    unique_cards = 0
    drawer_counts = {str(i): 0 for i in range(1, 7)}
    unassigned_count = 0

    for row in inventory_rows:
        price = effective_price(row.card, row.finish) or 0.0
        total = price * row.quantity
        items.append(
            {
                "id": row.id,
                "card": row.card,
                "finish": row.finish,
                "language": row.language or "en",
                "is_proxy": bool(row.is_proxy),
                "quantity": row.quantity,
                "drawer": row.drawer,
                "slot": row.slot,
                "is_pending": row.is_pending,
                "effective_price": price,
                "total_value": total,
                "drawer_label": get_location_label(row),
            }
        )
        total_value += total
        total_cards += row.quantity
        unique_cards += 1
        if str(row.drawer) in drawer_counts:
            drawer_counts[str(row.drawer)] += row.quantity
        else:
            unassigned_count += row.quantity

    return {
        "items": items,
        "total_value": total_value,
        "total_cards": total_cards,
        "unique_cards": unique_cards,
        "drawer_counts": drawer_counts,
        "unassigned_count": unassigned_count,
    }


def build_pending_view_model(rows) -> dict:
    """Build template payload pieces for the pending-placement page."""
    items = []
    grouped = {}
    total_copies = 0

    for row in rows:
        price = effective_price(row.card, row.finish)
        # FROM should describe where the card physically is right now. Three
        # cases, in priority order:
        #   1. row.from_drawer set (resort_collection captured an old drawer
        #      when pulling a placed row to pending — or v3.19.2's audit-log
        #      backfill recovered it). Show that drawer.
        #   2. row.from_drawer NULL but the row is imported and has no
        #      previous physical position. Show "New import" so it reads
        #      distinctly from a real-drawer source rather than collapsing
        #      onto the same label as TO.
        if row.from_drawer:
            from_label = get_drawer_label(row.from_drawer)
        else:
            from_label = "New import"
        item = {
            "id": row.id,
            "card": row.card,
            "finish": row.finish,
            "language": row.language or "en",
            "is_proxy": bool(row.is_proxy),
            "quantity": row.quantity,
            "current_location_label": from_label,
            "from_slot": row.from_slot,
            "target_location_label": get_drawer_label(row.drawer),
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
                "label": get_drawer_label(key),
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
    recent `imported` event → batch → batch.filename + imported_at, in a
    single batched query (one IN clause + one GROUP-BY) — strict no-N+1.
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

    # Most-recent imported event per row id → batch id. SQLite's MAX-over-
    # GROUP-BY returns the largest created_at; the matching batch_id comes
    # from a correlated subquery to keep this in a single statement.
    sub_rows = (
        session.query(
            TransactionLog.inventory_row_id.label("row_id"),
            func.max(TransactionLog.created_at).label("ts"),
        )
        .filter(
            TransactionLog.user_id == user_id,
            TransactionLog.inventory_row_id.in_(row_ids),
            TransactionLog.event_type == "imported",
        )
        .group_by(TransactionLog.inventory_row_id)
        .all()
    )
    # Second pass: join the (row_id, ts) tuples to TransactionLog and pull
    # batch_id. Cheap because the rows are already a small set.
    row_to_batch: dict[int, int] = {}
    if sub_rows:
        ts_pairs = [(r.row_id, r.ts) for r in sub_rows]
        tx_rows = (
            session.query(TransactionLog.inventory_row_id, TransactionLog.batch_id)
            .filter(
                TransactionLog.user_id == user_id,
                TransactionLog.event_type == "imported",
                TransactionLog.inventory_row_id.in_([p[0] for p in ts_pairs]),
            )
            .all()
        )
        # Walk and keep the latest per row (the query above already filtered).
        for row_id, batch_id in tx_rows:
            if row_id is not None and batch_id is not None:
                row_to_batch[row_id] = batch_id

    batch_ids = sorted({b for b in row_to_batch.values() if b is not None})
    batches: dict[int, ImportBatch] = {}
    if batch_ids:
        for b in session.query(ImportBatch).filter(ImportBatch.id.in_(batch_ids)).all():
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


def build_drawers_summary_view_model(grouped_rows: dict) -> dict:
    """Build summary cards for the drawers overview page."""
    drawer_summaries = []
    for drawer_name, rows in grouped_rows.items():
        total_value = sum(effective_price(row.card, row.finish) * row.quantity for row in rows)
        drawer_summaries.append(
            {"drawer": drawer_name, "row_count": len(rows), "total_value": total_value}
        )
    drawer_summaries.sort(key=lambda d: d["drawer"])
    return {"drawer_summaries": drawer_summaries}


def build_drawer_detail_view_model(drawer: str, rows) -> dict:
    """Build template payload pieces for one drawer detail page."""
    items = []
    total_copies = 0
    total_value = 0.0

    for row in rows:
        price = effective_price(row.card, row.finish) or 0.0
        total = price * row.quantity
        items.append(
            {
                "id": row.id,
                "card": row.card,
                "finish": row.finish,
                "language": row.language or "en",
                "is_proxy": bool(row.is_proxy),
                "quantity": row.quantity,
                "slot": row.slot,
                "is_pending": row.is_pending,
                "effective_price": price,
                "total_value": total,
                "drawer_label": get_drawer_label(drawer),
            }
        )
        total_copies += row.quantity
        total_value += total

    return {
        "drawer": drawer,
        "drawer_label": get_drawer_label(drawer),
        "items": items,
        "entry_count": len(items),
        "total_copies": total_copies,
        "total_value": total_value,
    }


def build_deck_detail_view_model(deck) -> dict:
    """Build template payload pieces for one deck page."""
    items = []
    deck_total_value = 0.0
    total_cards = 0

    if deck:
        for item in deck.items:
            price = effective_price(item.card, item.finish) or 0.0
            total_value = price * item.quantity
            deck_total_value += total_value
            total_cards += item.quantity
            items.append(
                {
                    "id": item.id,
                    "card": item.card,
                    "finish": item.finish,
                    "quantity": item.quantity,
                    "effective_price": price,
                    "total_value": total_value,
                }
            )

    return {
        "items": items,
        "deck_total_value": deck_total_value,
        "deck_total_cards": total_cards,
    }


def build_card_detail_view_model(card, rows) -> dict:
    """Build template payload pieces for a single-card detail page."""
    card_rows = []
    total_copies = 0
    total_value = 0.0

    for row in rows:
        price = effective_price(row.card, row.finish) or 0.0
        total = price * row.quantity
        card_rows.append(
            {
                "id": row.id,
                "finish": row.finish,
                "language": row.language or "en",
                "is_proxy": bool(row.is_proxy),
                "quantity": row.quantity,
                "drawer": row.drawer,
                "slot": row.slot,
                "is_pending": row.is_pending,
                "effective_price": price,
                "total_value": total,
                "drawer_label": get_location_label(row),
            }
        )
        total_copies += row.quantity
        total_value += total

    return {
        "card": card,
        "rows": card_rows,
        "total_copies": total_copies,
        "total_value": total_value,
    }
