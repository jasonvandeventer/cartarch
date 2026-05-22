from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.dependencies import DRAWER_SORTER_USERNAMES, get_current_user, get_db_session, render
from app.drawer_service import list_drawer_groups, list_rows_for_drawer
from app.inventory_service import get_drawer_label
from app.models import User
from app.pricing import effective_price

router = APIRouter(prefix="/drawers")


@router.get("")
def drawers_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.username not in DRAWER_SORTER_USERNAMES:
        raise HTTPException(status_code=403, detail="Not available for your account")
    grouped = list_drawer_groups(session, user_id=current_user.id)

    # v3.27.10 prereq 1: switch the per-drawer headline from len(rows) to
    # sum(row.quantity) — card-count is the canonical unit across every
    # cross-page surface (Collection, Decks, Drawers, dashboard tiles all
    # agree). The visible Drawers-page number changes as a consequence: a
    # drawer holding 3 rows of a 4-of (qty=4 each) now reads 12 cards, not
    # 3 rows. Intentional; documented in release-history.md. We still need
    # the row list for total_value (per-row finish-aware pricing via
    # effective_price doesn't push cleanly to SQL), so the rows-fetched
    # cost is unchanged — only the headline computation moves to a sum.
    drawer_summaries = []
    for drawer_name, rows in grouped.items():
        card_count = sum(row.quantity for row in rows)
        total_value = sum(
            (effective_price(row.card, row.finish) or 0.0) * row.quantity for row in rows
        )
        drawer_summaries.append(
            {"drawer": drawer_name, "card_count": card_count, "total_value": total_value}
        )

    drawer_summaries.sort(key=lambda d: d["drawer"])

    return render(
        request,
        "drawers.html",
        {
            "title": "Drawers",
            "drawer_summaries": drawer_summaries,
            "current_user": current_user,
        },
    )


@router.get("/{drawer}")
def drawer_detail_page(
    request: Request,
    drawer: str,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.username not in DRAWER_SORTER_USERNAMES:
        raise HTTPException(status_code=403, detail="Not available for your account")
    rows = list_rows_for_drawer(session, drawer, user_id=current_user.id)

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

    return render(
        request,
        "drawer_detail.html",
        {
            "title": f"Drawer {drawer}",
            "drawer": drawer,
            "drawer_label": get_drawer_label(drawer),
            "items": items,
            "entry_count": len(items),
            "total_copies": total_copies,
            "total_value": total_value,
            "current_user": current_user,
        },
    )
