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
        # FROM should describe where the card physically is right now. When
        # resort_collection pulled a placed row to pending, it stashed the
        # previous drawer/slot in from_drawer/from_slot. Prefer those when
        # present; fall back to the row's current placement for imported
        # pending rows (which never had a previous location).
        from_label = None
        if row.from_drawer:
            from_label = get_drawer_label(row.from_drawer)
        item = {
            "id": row.id,
            "card": row.card,
            "finish": row.finish,
            "language": row.language or "en",
            "is_proxy": bool(row.is_proxy),
            "quantity": row.quantity,
            "current_location_label": from_label or get_location_label(row),
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
