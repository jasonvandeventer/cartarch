from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.dependencies import CsrfRequired, get_current_user, get_db_session, render
from app.drawer_service import list_drawer_groups, list_rows_for_drawer
from app.inventory_service import get_drawer_label
from app.location_service import numbered_drawers, user_has_drawers
from app.models import User
from app.pricing import effective_price
from app.sorter_rule_service import (
    TWELVE_DRAWER_LABELS,
    configure_twelve_drawers,
    list_sorter_rules,
)

router = APIRouter(prefix="/drawers")


@router.get("")
def drawers_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    if not user_has_drawers(session, current_user.id):
        raise HTTPException(status_code=403, detail="You have no drawer locations")
    locations = numbered_drawers(session, current_user.id)
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
            {
                "drawer": drawer_name,
                "label": get_drawer_label(drawer_name, locations.get(drawer_name)),
                "card_count": card_count,
                "total_value": total_value,
            }
        )

    drawer_summaries.sort(
        key=lambda d: (int(d["drawer"]) if d["drawer"].isdigit() else 999, d["drawer"])
    )

    return render(
        request,
        "drawers.html",
        {
            "title": "Drawers",
            "drawer_summaries": drawer_summaries,
            "can_setup_twelve": not list_sorter_rules(session, current_user.id),
            "twelve_labels": TWELVE_DRAWER_LABELS,
            "current_user": current_user,
        },
    )


@router.post("/setup-twelve")
def setup_twelve_drawers(
    request: Request,
    acknowledge: bool = Form(False),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if not acknowledge:
        raise HTTPException(
            status_code=400,
            detail="Acknowledge the new filing rules before setting up the catalog.",
        )
    try:
        configure_twelve_drawers(session, current_user.id)
        session.commit()
    except ValueError:
        session.rollback()
        raise
    return RedirectResponse("/drawers", status_code=303)


@router.get("/{drawer}")
def drawer_detail_page(
    request: Request,
    drawer: str,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    if not user_has_drawers(session, current_user.id):
        raise HTTPException(status_code=403, detail="You have no drawer locations")
    location = numbered_drawers(session, current_user.id).get(drawer)
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
                "drawer_label": get_drawer_label(drawer, location),
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
            "location_id": location.id if location else None,
            "drawer_label": get_drawer_label(drawer, location),
            "items": items,
            "entry_count": len(items),
            "total_copies": total_copies,
            "total_value": total_value,
            "current_user": current_user,
        },
    )
