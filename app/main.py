"""FastAPI route entrypoint for Mana Archive.

Routes are grouped by feature flow rather than alphabetically. User-owned
operations receive `current_user.id` at the route boundary and pass it into the
service layer.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from app.audit_service import list_transaction_logs
from app.auth import hash_password
from app.db import DATA_DIR, SessionLocal, init_db
from app.deck_service import (
    CARD_ROLE_TAGS,
    compute_consistency,
    compute_dead_cards,
    compute_deck_analytics,
    compute_deck_bracket,
    compute_deck_combos,
    compute_deck_health,
    compute_deck_synergy,
    compute_deck_tokens,
    create_deck,
    delete_deck,
    extract_commander_themes,
    get_card_legality,
    get_deck,
    get_row_tags,
    list_decks,
    pull_card_to_deck,
    return_card_from_deck,
    set_row_tags,
    suggest_card_roles,
    update_deck,
)
from app.dependencies import (
    DRAWER_SORTER_USERNAMES,
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
)
from app.drawer_service import list_drawer_groups, list_rows_for_drawer
from app.game_service import (
    create_game,
    delete_game,
    end_game,
    get_deck_record,
    get_game,
    list_games,
)
from app.import_service import (
    normalize_finish,
    parse_scanner_csv,
    parse_text_list,
    persist_import_rows,
)
from app.inventory_service import (
    PRICE_STALE_DAYS,
    adjust_inventory_row_quantity,
    apply_collection_search_filters,
    confirm_all_pending,
    confirm_pending_row,
    delete_inventory_row,
    get_drawer_label,
    get_inventory_row_stats,
    get_location_label,
    is_price_stale,
    list_inventory_rows,
    list_owned_sets,
    list_pending_rows,
    move_inventory_row_to_location,
    place_imported_rows,
    resort_collection,
    undo_last_batch,
    undo_last_import,
    update_inventory_location,
)
from app.location_service import (
    create_location,
    delete_location,
    get_location,
    get_location_summary,
    list_locations,
    update_location,
)
from app.models import Card, Deck, ImportBatch, InventoryRow, User
from app.presentation_service import build_pending_view_model
from app.pricing import effective_price
from app.routes import account, admin, auth
from app.scryfall import (
    autocomplete_token_names,
    bulk_refresh_prices,
    fetch_card_by_scryfall_id,
    fetch_card_by_set_and_number,
    fetch_token_by_set_number,
    refresh_card_from_scryfall,
    search_cards_by_name,
    search_tokens_by_name,
)
from app.set_service import get_set_completion
from app.token_service import (
    add_deck_token_requirement,
    create_token,
    deck_token_status,
    delete_deck_token_requirement,
    delete_token,
    list_token_subtypes,
    list_tokens,
    parse_bulk_dfc_lines,
    total_token_count,
    update_token,
)
from scripts.run_migrations import run as run_migrations

app = FastAPI(title="Mana Archive")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "dev-only-change-me"),
    same_site="lax",
    https_only=os.getenv("DEV_MODE", "false").lower() != "true",
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(account.router)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> HTMLResponse:
    return HTMLResponse(
        f"<h2>Error</h2><p>{html.escape(str(exc))}</p><a href='/collection'>Back to collection</a>",
        status_code=400,
    )


app.mount("/static", StaticFiles(directory="app/static"), name="static")


def safe_redirect_url(request: Request, default: str = "/collection") -> str:
    # Validate before using Referer as redirect target — an attacker can set it to an external URL.
    referer = request.headers.get("referer", "")
    if not referer:
        return default
    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return default
    return referer


_PRICE_REFRESH_INTERVAL_SECONDS = 600  # 10 minutes
_PRICE_REFRESH_BATCH = 75


def _run_price_refresh_batch() -> None:
    """Refresh up to 75 of the oldest-priced cards that are owned by any user."""
    session = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=PRICE_STALE_DAYS)
        stale = (
            session.query(Card)
            .join(InventoryRow, InventoryRow.card_id == Card.id)
            .filter(
                (Card.updated_at < cutoff)
                | (Card.color_identity == None)  # noqa: E711
                | (Card.legalities == None)  # noqa: E711
            )
            .order_by(Card.updated_at.asc())
            .limit(_PRICE_REFRESH_BATCH)
            .distinct()
            .all()
        )
        if not stale:
            return

        fresh_by_id = bulk_refresh_prices([c.scryfall_id for c in stale])
        now = datetime.utcnow()
        updated = 0
        for card in stale:
            fresh = fresh_by_id.get(card.scryfall_id)
            if fresh:
                card.price_usd = fresh["price_usd"]
                card.price_usd_foil = fresh["price_usd_foil"]
                card.price_usd_etched = fresh["price_usd_etched"]
                card.colors = fresh.get("colors")
                card.color_identity = fresh.get("color_identity")
                card.mana_cost = fresh.get("mana_cost")
                card.cmc = fresh.get("cmc")
                card.legalities = fresh.get("legalities")
                card.updated_at = now
                updated += 1
        session.commit()
        print(f"[price-refresh] updated {updated}/{len(stale)} cards")
    except Exception as exc:
        session.rollback()
        print(f"[price-refresh] error: {exc}")
    finally:
        session.close()


def _price_refresh_loop() -> None:
    time.sleep(60)  # let the app finish starting before first run
    while True:
        _run_price_refresh_batch()
        time.sleep(_PRICE_REFRESH_INTERVAL_SECONDS)


def _bg_resort(user_id: int) -> None:
    """Full collection resort in a background thread using its own DB session."""
    session = SessionLocal()
    try:
        resort_collection(session, user_id=user_id)
    except Exception as exc:
        session.rollback()
        print(f"[resort] error for user {user_id}: {exc}")
    finally:
        session.close()


@app.on_event("startup")
def on_startup() -> None:
    # Prevent accidental deploys with the default dev secret — sessions would be forgeable.
    if (
        os.getenv("DEV_MODE", "false").lower() != "true"
        and os.getenv("SESSION_SECRET_KEY", "dev-only-change-me") == "dev-only-change-me"
    ):
        raise RuntimeError("SESSION_SECRET_KEY must be set in production (DEV_MODE is not 'true')")
    run_migrations()
    init_db()
    threading.Thread(target=_price_refresh_loop, daemon=True, name="price-refresh").start()


@app.get("/")
def home(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return render(
        request,
        "home.html",
        {
            "title": "Mana Archive",
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
        },
    )


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    session: Session = Depends(get_db_session),
    _: None = CsrfRequired,
):
    username = username.strip().lower()
    display_name = display_name.strip()

    if "@" not in username or "." not in username.split("@")[-1]:
        return render(
            request,
            "register.html",
            {"title": "Register", "error": "Please enter a valid email address."},
        )

    if not display_name:
        display_name = username.split("@")[0]

    if session.query(User).filter(User.username == username).first():
        return render(
            request,
            "register.html",
            {"title": "Register", "error": "An account with that email already exists."},
        )

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        is_active=True,
    )
    session.add(user)
    session.commit()

    return RedirectResponse("/login", status_code=303)


@app.get("/register")
def register_page(request: Request):
    return render(request, "register.html", {"title": "Register"})


# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------


@app.get("/import")
def import_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    return render(
        request,
        "import.html",
        {
            "title": "Import",
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            "locations": list_locations(session, current_user.id),
        },
    )


@app.post("/import/preview")
async def import_preview(
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    file_bytes = await file.read()
    result = parse_scanner_csv(file_bytes)

    return render(
        request,
        "import_preview.html",
        {
            "title": "Import Preview",
            "valid_rows": result["valid_rows"],
            "invalid_rows": result["invalid_rows"],
            "format_name": result["format_name"],
            "filename": file.filename,
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            "locations": list_locations(session, current_user.id),
            "decks": list_decks(session, user_id=current_user.id),
        },
    )


@app.post("/import/list/preview")
async def import_list_preview(
    request: Request,
    card_list: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    result = parse_text_list(card_list)
    return render(
        request,
        "import_preview.html",
        {
            "title": "Import Preview",
            "valid_rows": result["valid_rows"],
            "invalid_rows": result["invalid_rows"],
            "format_name": result["format_name"],
            "filename": "pasted list",
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            "locations": list_locations(session, current_user.id),
            "decks": list_decks(session, user_id=current_user.id),
        },
    )


@app.post("/import/commit")
async def import_commit(
    request: Request,
    filename: str = Form("uploaded.csv"),
    line_number: list[str] = Form([]),
    name: list[str] = Form([]),
    scryfall_id: list[str] = Form([]),
    set_code: list[str] = Form([]),
    collector_number: list[str] = Form([]),
    finish: list[str] = Form([]),
    quantity: list[str] = Form([]),
    location: list[str] = Form([]),
    target_location_id: int = Form(0),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    rows = []
    for i in range(len(line_number)):
        rows.append(
            {
                "line_number": int(line_number[i]),
                "name": name[i] if i < len(name) else "",
                "scryfall_id": scryfall_id[i],
                "set_code": set_code[i],
                "collector_number": collector_number[i],
                "finish": normalize_finish(finish[i]),
                "quantity": int(quantity[i]),
                "location": location[i],
            }
        )

    result = persist_import_rows(session, rows, filename=filename, user_id=current_user.id)
    row_ids = result.get("imported_row_ids", [])
    placed_in = None

    if row_ids and target_location_id:
        place_imported_rows(
            session, row_ids, user_id=current_user.id, location_id=target_location_id
        )
        loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
        placed_in = loc.name if loc else None
        if loc and loc.type != "deck" and current_user.username in DRAWER_SORTER_USERNAMES:
            resort_collection(session, user_id=current_user.id)

    elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
        resort_collection(session, user_id=current_user.id)
        return RedirectResponse(url="/pending", status_code=303)

    return render(
        request,
        "import_result.html",
        {
            "title": "Import Results",
            "imported_count": result["imported_count"],
            "failed_rows": result["failed_rows"],
            "batch_id": result["batch_id"],
            "placed_in": placed_in,
            "current_user": current_user,
        },
    )


@app.post("/import/manual/preview")
async def manual_import_preview(
    request: Request,
    scryfall_id: str = Form(""),
    set_code: str = Form(""),
    collector_number: str = Form(""),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    card = None
    resolved_id = ""

    if scryfall_id.strip():
        resolved_id = scryfall_id.strip()
        card = fetch_card_by_scryfall_id(resolved_id)
    else:
        card = fetch_card_by_set_and_number(set_code, collector_number)
        if card:
            resolved_id = card["scryfall_id"]

    return render(
        request,
        "manual_preview.html",
        {
            "title": "Manual Import Preview",
            "card": card,
            "resolved_scryfall_id": resolved_id,
            "finish": normalize_finish(finish),
            "quantity": max(1, quantity),
            "set_code": set_code,
            "collector_number": collector_number,
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            "locations": list_locations(session, current_user.id),
            "decks": list_decks(session, user_id=current_user.id),
        },
    )


@app.post("/import/manual/search")
async def manual_import_search(
    request: Request,
    name: str = Form(...),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    results = search_cards_by_name(name)

    return render(
        request,
        "manual_search_results.html",
        {
            "title": "Choose Printing",
            "query": name,
            "results": results,
            "current_user": current_user,
        },
    )


@app.post("/import/manual/commit")
async def manual_import_commit(
    request: Request,
    scryfall_id: str = Form(""),
    set_code: str = Form(""),
    collector_number: str = Form(""),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    target_location_id: int = Form(0),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    result = persist_import_rows(
        session,
        [
            {
                "line_number": 1,
                "scryfall_id": scryfall_id,
                "set_code": set_code,
                "collector_number": collector_number,
                "finish": normalize_finish(finish),
                "quantity": max(1, quantity),
                "location": "",
                "name": "",
            }
        ],
        filename="manual import",
        user_id=current_user.id,
    )

    row_ids = result.get("imported_row_ids", [])
    placed_in = None

    if row_ids and target_location_id:
        place_imported_rows(
            session, row_ids, user_id=current_user.id, location_id=target_location_id
        )
        loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
        placed_in = loc.name if loc else None
        if loc and loc.type != "deck" and current_user.username in DRAWER_SORTER_USERNAMES:
            resort_collection(session, user_id=current_user.id)
    elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
        resort_collection(session, user_id=current_user.id)

    return render(
        request,
        "import_result.html",
        {
            "title": "Import Results",
            "imported_count": result["imported_count"],
            "failed_rows": result["failed_rows"],
            "batch_id": result["batch_id"],
            "placed_in": placed_in,
            "current_user": current_user,
        },
    )


# -----------------------------------------------------------------------------
# Inventory mutations
# -----------------------------------------------------------------------------


@app.post("/inventory/rows/{row_id}/remove")
def remove_inventory_row_action(
    request: Request,
    row_id: int,
    quantity: int = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    adjust_inventory_row_quantity(
        session=session,
        row_id=row_id,
        user_id=current_user.id,
        quantity=quantity,
        event_type="remove",
        note=note or None,
    )

    return RedirectResponse(url=safe_redirect_url(request), status_code=303)


@app.post("/inventory/rows/{row_id}/sell")
def sell_inventory_row_action(
    request: Request,
    row_id: int,
    quantity: int = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    adjust_inventory_row_quantity(
        session=session,
        row_id=row_id,
        user_id=current_user.id,
        quantity=quantity,
        event_type="sold",
        note=note or None,
    )

    return RedirectResponse(url=safe_redirect_url(request), status_code=303)


@app.post("/inventory/rows/{row_id}/trade")
def trade_inventory_row_action(
    request: Request,
    row_id: int,
    quantity: int = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    adjust_inventory_row_quantity(
        session=session,
        row_id=row_id,
        user_id=current_user.id,
        quantity=quantity,
        event_type="traded",
        note=note or None,
    )

    return RedirectResponse(url=safe_redirect_url(request), status_code=303)


@app.post("/inventory/rows/{row_id}/delete")
def delete_inventory_row_action(
    request: Request,
    row_id: int,
    note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_inventory_row(
        session=session,
        row_id=row_id,
        user_id=current_user.id,
    )

    return RedirectResponse(url=safe_redirect_url(request), status_code=303)


# -----------------------------------------------------------------------------
# Collection
# -----------------------------------------------------------------------------


@app.get("/collection")
def collection_page(
    request: Request,
    search: str = "",
    finish: str = "",
    location_id: int = 0,
    sort: str = "newest",
    direction: str = "desc",
    page: int = 1,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    per_page = 50

    drawer = ""

    selected_location = None

    if location_id:
        selected_location = get_location(
            session,
            location_id=location_id,
            user_id=current_user.id,
        )
    if selected_location and selected_location.type == "drawer":
        drawer = selected_location.name.replace("Drawer", "").strip()

    inventory_rows, total_count = list_inventory_rows(
        session,
        user_id=current_user.id,
        search=search,
        finish=finish,
        drawer=drawer,
        location_id=location_id if selected_location and selected_location.type != "drawer" else 0,
        sort=sort,
        direction=direction,
        page=page,
        per_page=per_page,
    )

    stats = get_inventory_row_stats(
        session,
        user_id=current_user.id,
        search=search,
        finish=finish,
        drawer=drawer,
        location_id=location_id if selected_location and selected_location.type != "drawer" else 0,
    )

    location_counts = {}
    for drawer_number, count in stats["drawer_counts"].items():
        if count > 0:
            location_counts[f"Drawer {drawer_number}"] = count

    if stats["unassigned_count"] > 0:
        location_counts["Unassigned"] = stats["unassigned_count"]

    decks = list_decks(session, user_id=current_user.id)
    locations = list_locations(session, user_id=current_user.id)
    items = []

    for row in inventory_rows:
        price = effective_price(row.card, row.finish)
        price_updated_at = getattr(row.card, "updated_at", None)
        is_stale = is_price_stale(price_updated_at)
        has_price = price is not None
        location_label = row.storage_location.name if row.storage_location else "Unassigned"

        if has_price:
            display_price = price
            total = price * row.quantity
            price_status = "stale" if is_stale else "current"
        else:
            display_price = 0.0
            total = 0.0
            price_status = "unknown"

        items.append(
            {
                "id": row.id,
                "card": row.card,
                "finish": row.finish,
                "quantity": row.quantity,
                "drawer": row.drawer,
                "slot": row.slot,
                "is_pending": row.is_pending,
                "effective_price": display_price,
                "has_price": has_price,
                "price_status": price_status,
                "price_updated_at": price_updated_at,
                "total_value": total,
                "drawer_label": get_location_label(row),
                "location_label": location_label,
                "storage_location_id": row.storage_location_id,
            }
        )

    total_pages = max(1, math.ceil(total_count / per_page))
    show_onboarding = total_count == 0

    return render(
        request,
        "collection.html",
        {
            "title": "Collection",
            "items": items,
            "search": search,
            "finish_filter": finish,
            "drawer_filter": drawer,
            "sort": sort,
            "direction": direction,
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_pages": total_pages,
            "total_value": stats["total_value"],
            "total_cards": stats["total_cards"],
            "unique_cards": stats["unique_cards"],
            "drawer_counts": stats["drawer_counts"],
            "unassigned_count": stats["unassigned_count"],
            "location_counts": location_counts,
            "decks": decks,
            "locations": locations,
            "location_id": location_id,
            "current_user": current_user,
            "show_onboarding": show_onboarding,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
        },
    )


@app.get("/collection/export")
def collection_export(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .join(Card)
        .filter(InventoryRow.user_id == current_user.id)
        .order_by(Card.name.asc())
        .all()
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Set", "Collector Number", "Finish", "Quantity", "Location"])
    for row in rows:
        card = row.card
        loc = row.storage_location
        writer.writerow(
            [
                card.name or "",
                (card.set_code or "").upper(),
                card.collector_number or "",
                row.finish or "normal",
                row.quantity,
                loc.name if loc else "",
            ]
        )

    display_name = current_user.display_name or current_user.username
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in display_name)
    filename = f"{safe_name}_collection.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/collection/update-location")
async def collection_update_location(
    row_id: int = Form(...),
    drawer: str = Form(""),
    slot: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    update_inventory_location(
        session,
        row_id=row_id,
        user_id=current_user.id,
        drawer=drawer,
        slot=slot,
    )

    return RedirectResponse(url="/collection", status_code=303)


@app.post("/inventory/rows/{row_id}/move")
async def inventory_row_move(
    request: Request,
    row_id: int,
    location_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    move_inventory_row_to_location(
        session, row_id=row_id, user_id=current_user.id, location_id=location_id
    )
    return RedirectResponse(url=safe_redirect_url(request), status_code=303)


@app.post("/collection/delete")
async def collection_delete(
    row_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_inventory_row(session, row_id=row_id, user_id=current_user.id)
    return RedirectResponse(url="/collection", status_code=303)


@app.post("/collection/resort")
async def collection_resort(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if current_user.username in DRAWER_SORTER_USERNAMES:
        resort_collection(session, user_id=current_user.id)
    return RedirectResponse(url="/collection", status_code=303)


# -----------------------------------------------------------------------------
# Pending placement
# -----------------------------------------------------------------------------


@app.get("/pending")
def pending_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    rows = list_pending_rows(session, user_id=current_user.id)

    latest_batch = (
        session.query(ImportBatch)
        .filter(ImportBatch.user_id == current_user.id)
        .order_by(ImportBatch.id.desc())
        .first()
    )

    use_drawer_sorter = current_user.username in DRAWER_SORTER_USERNAMES
    locations = [] if use_drawer_sorter else list_locations(session, current_user.id)
    view_model = build_pending_view_model(rows)

    return render(
        request,
        "pending.html",
        {
            "title": "Pending Placement",
            **view_model,
            "latest_batch_id": latest_batch.id if latest_batch else None,
            "current_user": current_user,
            "use_drawer_sorter": use_drawer_sorter,
            "locations": locations,
        },
    )


@app.post("/pending/confirm")
async def pending_confirm(
    row_id: int = Form(...),
    location_id: int = Form(0),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    confirm_pending_row(
        session,
        row_id=row_id,
        user_id=current_user.id,
        location_id=location_id or None,
    )
    return RedirectResponse(url="/pending", status_code=303)


@app.post("/pending/confirm-all")
async def pending_confirm_all(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if current_user.username in DRAWER_SORTER_USERNAMES:
        confirm_all_pending(session, user_id=current_user.id)
    return RedirectResponse(url="/pending", status_code=303)


@app.post("/pending/{row_id}/remove")
def remove_pending_row(
    row_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    row = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.id == row_id,
            InventoryRow.is_pending,
            InventoryRow.user_id == current_user.id,
        )
        .first()
    )

    if not row:
        raise HTTPException(status_code=404, detail="Pending row not found")

    session.delete(row)
    session.commit()

    return RedirectResponse(url="/pending", status_code=303)


# -----------------------------------------------------------------------------
# Storage Locations
# -----------------------------------------------------------------------------


@app.get("/locations")
def locations_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):

    location_summaries = get_location_summary(session, user_id=current_user.id)
    locations = [summary["location"] for summary in location_summaries]

    parent_locations = [loc for loc in locations if loc.type in {"root", "box", "binder", "other"}]

    return render(
        request,
        "locations.html",
        {
            "title": "Storage Locations",
            "locations": locations,
            "parent_locations": parent_locations,
            "location_types": ["binder", "box", "drawer", "other"],
            "location_summaries": location_summaries,
            "current_user": current_user,
        },
    )


@app.post("/locations")
def create_location_route(
    name: str = Form(...),
    type: str = Form("other"),
    parent_id: int | None = Form(None),
    sort_order: int = Form(0),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if parent_id == 0:
        parent_id = None

    create_location(
        session,
        user_id=current_user.id,
        name=name,
        type=type,
        parent_id=parent_id,
        sort_order=sort_order,
    )
    return RedirectResponse("/locations", status_code=303)


@app.post("/locations/create-deck")
def create_deck_from_locations(
    name: str = Form(...),
    format_name: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    create_deck(session, user_id=current_user.id, name=name, format_name=format_name)
    return RedirectResponse("/locations", status_code=303)


@app.post("/locations/{location_id}/delete")
def delete_location_route(
    location_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_location(session, location_id=location_id, user_id=current_user.id)
    return RedirectResponse("/locations", status_code=303)


@app.post("/locations/{location_id}/edit")
def edit_location_route(
    location_id: int,
    name: str = Form(...),
    type: str = Form("other"),
    parent_id: int | None = Form(None),
    sort_order: int = Form(0),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if parent_id == 0:
        parent_id = None
    try:
        update_location(
            session,
            location_id=location_id,
            user_id=current_user.id,
            name=name,
            type=type,
            parent_id=parent_id,
            sort_order=sort_order,
        )
    except ValueError:
        pass
    return RedirectResponse("/locations", status_code=303)


@app.post("/locations/{location_id}/bulk-move")
def bulk_move_location_cards(
    location_id: int,
    row_ids: list[int] = Form(...),
    target_location_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    for row_id in row_ids:
        try:
            move_inventory_row_to_location(
                session, row_id=row_id, user_id=current_user.id, location_id=target_location_id
            )
        except ValueError:
            pass
    return RedirectResponse(f"/locations/{location_id}", status_code=303)


@app.get("/locations/{location_id}")
def location_detail_page(
    request: Request,
    location_id: int,
    search: str = "",
    sort: str = "slot",
    direction: str = "asc",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    location = get_location(session, location_id=location_id, user_id=current_user.id)
    if location is None:
        raise HTTPException(status_code=404, detail="Location not found")

    if location.type == "deck":
        deck = session.query(Deck).filter(Deck.storage_location_id == location_id).first()
        if deck:
            return RedirectResponse(f"/decks/{deck.id}", status_code=302)

    loc_query = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .join(Card)
        .filter(
            InventoryRow.user_id == current_user.id,
            InventoryRow.storage_location_id == location_id,
        )
    )
    if search.strip():
        loc_query = apply_collection_search_filters(loc_query, search)

    reverse = direction == "desc"
    if sort == "name":
        loc_query = loc_query.order_by(Card.name.desc() if reverse else Card.name.asc())
    elif sort == "value":
        rows = loc_query.all()
        rows.sort(key=lambda r: effective_price(r.card, r.finish) or 0.0, reverse=reverse)
    elif sort == "cmc":
        loc_query = loc_query.order_by(Card.cmc.desc() if reverse else Card.cmc.asc())
    elif sort == "type":
        loc_query = loc_query.order_by(Card.type_line.desc() if reverse else Card.type_line.asc())
    else:
        loc_query = loc_query.order_by(InventoryRow.slot.asc())

    if sort not in ("value",):
        rows = loc_query.all()

    items = []
    total_value = 0.0
    total_quantity = 0

    for row in rows:
        price = effective_price(row.card, row.finish) or 0.0
        row_total = price * row.quantity
        total_value += row_total
        total_quantity += row.quantity

        items.append(
            {
                "id": row.id,
                "card": row.card,
                "finish": row.finish,
                "quantity": row.quantity,
                "slot": row.slot,
                "effective_price": price,
                "total_value": row_total,
                "is_pending": row.is_pending,
                "storage_location_id": row.storage_location_id,
            }
        )

    all_locations = list_locations(session, user_id=current_user.id)
    decks = list_decks(session, user_id=current_user.id)

    return render(
        request,
        "location_detail.html",
        {
            "title": location.name,
            "location": location,
            "items": items,
            "total_quantity": total_quantity,
            "total_value": total_value,
            "search": search,
            "sort": sort,
            "direction": direction,
            "current_user": current_user,
            "locations": all_locations,
            "decks": decks,
        },
    )


@app.get("/locations/{location_id}/export")
def location_export(
    location_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    location = get_location(session, location_id=location_id, user_id=current_user.id)
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")

    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .join(Card)
        .filter(
            InventoryRow.user_id == current_user.id,
            InventoryRow.storage_location_id == location_id,
        )
        .order_by(Card.name.asc())
        .all()
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Set", "Collector Number", "Finish", "Quantity", "Location"])
    for row in rows:
        card = row.card
        writer.writerow(
            [
                card.name or "",
                (card.set_code or "").upper(),
                card.collector_number or "",
                row.finish or "normal",
                row.quantity,
                location.name,
            ]
        )

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in location.name)
    filename = f"{safe_name}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# -----------------------------------------------------------------------------
# Drawers
# -----------------------------------------------------------------------------


@app.get("/drawers")
def drawers_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.username not in DRAWER_SORTER_USERNAMES:
        raise HTTPException(status_code=403, detail="Not available for your account")
    grouped = list_drawer_groups(session, user_id=current_user.id)

    drawer_summaries = []
    for drawer_name, rows in grouped.items():
        total_value = sum(
            (effective_price(row.card, row.finish) or 0.0) * row.quantity for row in rows
        )
        drawer_summaries.append(
            {"drawer": drawer_name, "row_count": len(rows), "total_value": total_value}
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


@app.get("/drawers/{drawer}")
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


# -----------------------------------------------------------------------------
# Audit / import undo
# -----------------------------------------------------------------------------


@app.get("/audit")
def audit_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.username not in DRAWER_SORTER_USERNAMES:
        raise HTTPException(status_code=403, detail="Not available for your account")
    logs = list_transaction_logs(session, user_id=current_user.id)
    batches = (
        session.query(ImportBatch)
        .filter(ImportBatch.user_id == current_user.id)
        .order_by(ImportBatch.id.desc())
        .all()
    )

    return render(
        request,
        "audit.html",
        {
            "title": "Audit Log",
            "logs": logs,
            "batches": batches,
            "current_user": current_user,
        },
    )


@app.post("/imports/undo-last")
async def imports_undo_last(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    undo_last_import(session, user_id=current_user.id)
    return RedirectResponse(url="/audit", status_code=303)


@app.post("/imports/undo-batch")
async def imports_undo_batch(
    batch_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    undo_last_batch(session, batch_id=batch_id, user_id=current_user.id)
    return RedirectResponse(url="/pending", status_code=303)


# -----------------------------------------------------------------------------
# Decks
# -----------------------------------------------------------------------------


@app.get("/decks")
def decks_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    decks = list_decks(session, user_id=current_user.id)
    show_onboarding = len(decks) == 0

    return render(
        request,
        "decks.html",
        {
            "title": "Decks",
            "decks": decks,
            "current_user": current_user,
            "show_onboarding": show_onboarding,
        },
    )


@app.post("/decks/create")
async def decks_create(
    name: str = Form(...),
    format_name: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    create_deck(
        session,
        user_id=current_user.id,
        name=name,
        format_name=format_name,
        notes=notes,
    )

    return RedirectResponse(url="/decks", status_code=303)


_VALID_HEALTH_FILTERS = {"ramp", "draw", "removal", "wipes"}

_PANELS_CACHE_VERSION = 2
_PANELS_CACHE_DIR = DATA_DIR / "panels_cache"
_panels_memory: dict[str, dict] = {}  # in-process cache; survives navigation, cleared on reload


def _panels_cache_key(rows: list) -> str:
    """Stable hash of deck contents — changes when any card or quantity changes."""
    fingerprint = sorted(
        f"{r.card.scryfall_id}:{r.quantity}:{r.role or ''}"
        for r in rows
        if r.card and r.card.scryfall_id
    )
    return hashlib.md5(
        (f"{_PANELS_CACHE_VERSION}:" + "|".join(fingerprint)).encode(),
        usedforsecurity=False,
    ).hexdigest()


def _read_panels_cache(deck_id: int, cache_key: str) -> dict | None:
    # In-process memory cache first — guaranteed to work within same server run
    entry = _panels_memory.get(cache_key)
    if entry and time.time() - entry.get("ts", 0) < 86400:
        print(f"[panels] memory hit deck={deck_id}", flush=True)
        return entry

    # Fall back to disk cache — survives server restarts
    path = _PANELS_CACHE_DIR / f"{deck_id}.json"
    try:
        data = json.loads(path.read_text())
        stored_key = data.get("key")
        age = time.time() - data.get("ts", 0)
        if stored_key == cache_key and age < 86400:
            print(f"[panels] disk hit deck={deck_id}", flush=True)
            _panels_memory[cache_key] = data  # warm memory cache from disk
            return data
        print(
            f"[panels] disk miss deck={deck_id} key_match={stored_key == cache_key} age={age:.0f}s",
            flush=True,
        )
    except FileNotFoundError:
        print(f"[panels] no disk cache yet deck={deck_id}", flush=True)
    except Exception as e:
        print(f"[panels] disk read error deck={deck_id}: {e}", flush=True)
    return None


def _write_panels_cache(deck_id: int, cache_key: str, payload: dict) -> None:
    entry = {"ts": time.time(), **payload}
    _panels_memory[cache_key] = entry
    try:
        _PANELS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _PANELS_CACHE_DIR / f"{deck_id}.json"
        path.write_text(json.dumps({"key": cache_key, **entry}))
        print(f"[panels] disk write ok deck={deck_id} path={path}", flush=True)
    except Exception as e:
        print(f"[panels] disk write failed deck={deck_id}: {e}", flush=True)


@app.get("/decks/{deck_id}")
def deck_detail_page(
    request: Request,
    deck_id: int,
    search: str = "",
    sort: str = "name",
    direction: str = "asc",
    collection_search: str = "",
    health_filter: str = "",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    items = []
    collection_results = []
    deck_total_value = 0.0
    total_cards = 0
    deck_record = (
        get_deck_record(session, deck_id) if deck else {"wins": 0, "losses": 0, "total": 0}
    )

    if deck:
        # Commander themes feed Synergy auto-detection per row.
        _commander_rows = (
            session.query(InventoryRow)
            .options(joinedload(InventoryRow.card))
            .filter(
                InventoryRow.user_id == current_user.id,
                InventoryRow.storage_location_id == deck.storage_location_id,
                InventoryRow.role == "commander",
            )
            .all()
        )
        _themes = extract_commander_themes(_commander_rows) if _commander_rows else None

        # Auto-tag untagged rows from oracle text patterns (non-destructive).
        # Runs before the main query so items see fresh tags on the same request.
        _untagged = (
            session.query(InventoryRow)
            .options(joinedload(InventoryRow.card))
            .join(Card)
            .filter(
                InventoryRow.user_id == current_user.id,
                InventoryRow.storage_location_id == deck.storage_location_id,
                InventoryRow.tags == None,  # noqa: E711
            )
            .all()
        )
        _auto_tagged = False
        for _row in _untagged:
            _suggested = suggest_card_roles(_row.card, themes=_themes)
            if _suggested:
                set_row_tags(_row, _suggested)
                _auto_tagged = True
        if _auto_tagged:
            session.commit()

        deck_query = (
            session.query(InventoryRow)
            .options(joinedload(InventoryRow.card))
            .join(Card)
            .filter(
                InventoryRow.user_id == current_user.id,
                InventoryRow.storage_location_id == deck.storage_location_id,
            )
        )
        if search.strip():
            deck_query = apply_collection_search_filters(deck_query, search)

        reverse = direction == "desc"
        if sort == "type":
            deck_query = deck_query.order_by(
                Card.type_line.desc() if reverse else Card.type_line.asc()
            )
        elif sort == "cmc":
            deck_query = deck_query.order_by(Card.cmc.desc() if reverse else Card.cmc.asc())
        elif sort == "value":
            deck_rows = deck_query.all()
        else:
            deck_query = deck_query.order_by(Card.name.desc() if reverse else Card.name.asc())

        if sort != "value":
            deck_rows = deck_query.all()

        if sort == "value":
            deck_rows.sort(key=lambda r: effective_price(r.card, r.finish) or 0.0, reverse=reverse)

        for row in deck_rows:
            price = effective_price(row.card, row.finish) or 0.0
            total_value = price * row.quantity
            deck_total_value += total_value
            total_cards += row.quantity
            items.append(
                {
                    "id": row.id,
                    "card": row.card,
                    "finish": row.finish,
                    "quantity": row.quantity,
                    "effective_price": price,
                    "total_value": total_value,
                    "role": row.role,
                    "tags": get_row_tags(row),
                    "suggested_tags": suggest_card_roles(row.card, themes=_themes),
                    "legality_status": get_card_legality(row.card, deck.format),
                }
            )

    if collection_search.strip():
        rows, _ = list_inventory_rows(
            session,
            user_id=current_user.id,
            search=collection_search,
            page=1,
            per_page=20,
        )

        for row in rows:
            price = effective_price(row.card, row.finish) or 0.0
            collection_results.append(
                {
                    "id": row.id,
                    "card": row.card,
                    "finish": row.finish,
                    "quantity": row.quantity,
                    "location_label": get_location_label(row),
                    "effective_price": price,
                }
            )

    use_drawer_sorter = current_user.username in DRAWER_SORTER_USERNAMES

    analytics = None
    health = None
    consistency = None
    if deck and deck.storage_location_id:
        all_deck_rows = (
            session.query(InventoryRow)
            .options(joinedload(InventoryRow.card))
            .join(Card)
            .filter(
                InventoryRow.user_id == current_user.id,
                InventoryRow.storage_location_id == deck.storage_location_id,
            )
            .all()
        )
        if all_deck_rows:
            analytics = compute_deck_analytics(all_deck_rows)
            health = compute_deck_health(all_deck_rows)
            consistency = compute_consistency(all_deck_rows)

    bracket_v2 = None
    if deck and deck.storage_location_id:
        from app.bracket_v2_service import estimate_bracket_v2, persist_estimate

        # Pull combos from the panels cache if warm. Cold-cache decks fall back to
        # mechanics+intent-only on first load; the lazy panels endpoint
        # repopulates the cache + re-runs V3 for next time.
        _cached_combos: dict | None = None
        _all_rows = locals().get("all_deck_rows")
        if _all_rows:
            try:
                _ck = _panels_cache_key(_all_rows)
                _cached = _read_panels_cache(deck.id, _ck)
                if _cached:
                    _cached_combos = _cached.get("combos")
            except Exception:
                _cached_combos = None

        _est = estimate_bracket_v2(session, deck, current_user.id, combos=_cached_combos)
        try:
            persist_estimate(session, deck.id, _est)
        except Exception as exc:  # noqa: BLE001 — persistence isn't user-facing
            print(f"[bracket_v2] persist failed deck={deck.id}: {exc}", flush=True)
        bracket_v2 = {
            "bracket": _est.final_bracket,
            "mechanics_bracket": _est.mechanics_bracket,
            "intent_bracket": _est.intent_bracket,
            "rules_version": _est.rules_version,
            "score": _est.score,
            "confidence": {
                "tagging_coverage": _est.confidence_tagging_coverage,
                "mechanics_clarity": _est.confidence_mechanics_clarity,
                "intent_alignment": _est.confidence_intent_alignment,
                "combo_detection_depth": _est.confidence_combo_detection_depth,
            },
            "findings": [
                {
                    "type": f.finding_type,
                    "severity": f.severity,
                    "message": f.message,
                    "value": f.finding_value,
                }
                for f in _est.findings
            ],
        }

    # Apply health filter before splitting into commanders/deck_cards
    if health and health_filter in _VALID_HEALTH_FILTERS:
        _health_names = set(health[health_filter]["cards"])
        items = [i for i in items if i["card"].name in _health_names]

    commanders = [i for i in items if i["role"] == "commander"]
    deck_cards = [i for i in items if i["role"] != "commander"]

    # Derive color identity from all commanders (supports partner pairs)
    _identity_letters: set[str] = set()
    for c in commanders:
        for letter in (c["card"].color_identity or "").split():
            _identity_letters.add(letter)
    color_identity = " ".join(pip for pip in ["W", "U", "B", "R", "G"] if pip in _identity_letters)

    return render(
        request,
        "deck_detail.html",
        {
            "title": deck.name if deck else "Deck",
            "deck": deck,
            "color_identity": color_identity,
            "commanders": commanders if deck else [],
            "items": deck_cards if deck else [],
            "deck_total_value": deck_total_value if deck else 0.0,
            "deck_total_cards": total_cards if deck else 0,
            "bracket_v2": bracket_v2,
            "token_requirements": (
                deck_token_status(session, deck.id, current_user.id) if deck else []
            ),
            "token_inventory_options": (list_tokens(session, current_user.id) if deck else []),
            "search": search,
            "sort": sort,
            "direction": direction,
            "collection_search": collection_search,
            "collection_results": collection_results if deck else [],
            "analytics": analytics,
            "health": health,
            "consistency": consistency,
            "deck_record": deck_record,
            "health_filter": health_filter if health_filter in _VALID_HEALTH_FILTERS else "",
            "current_user": current_user,
            "use_drawer_sorter": use_drawer_sorter,
            "locations": list_locations(session, user_id=current_user.id),
        },
    )


@app.get("/decks/{deck_id}/panels")
def deck_panels_fragment(
    deck_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404)

    all_deck_rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .join(Card)
        .filter(
            InventoryRow.user_id == current_user.id,
            InventoryRow.storage_location_id == deck.storage_location_id,
        )
        .all()
    )

    bracket = None
    synergy = None
    combos: dict = {"included": [], "almost": []}
    tokens: list = []

    if all_deck_rows:
        ck = _panels_cache_key(all_deck_rows)
        cached = _read_panels_cache(deck_id, ck)

        if cached:
            tokens = cached.get("tokens", [])
            combos = cached.get("combos", {"included": [], "almost": []})
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                tokens_future = pool.submit(compute_deck_tokens, all_deck_rows)
                combos_future = pool.submit(compute_deck_combos, all_deck_rows)
                tokens = tokens_future.result()
                combos = combos_future.result()
            _write_panels_cache(deck_id, ck, {"tokens": tokens, "combos": combos})

        bracket = compute_deck_bracket(all_deck_rows, combos)
        synergy = compute_deck_synergy(all_deck_rows, combos)
        dead_cards = compute_dead_cards(all_deck_rows, synergy)

        # V3: re-run the bracket V2 estimator with combo data and persist the
        # combo-aware result. The deck-detail bracket panel rendered on initial
        # page load is mechanics+intent only; this overwrites the persisted
        # estimate so the next page load shows the V3 result.
        try:
            from app.bracket_v2_service import estimate_bracket_v2 as _v2_est
            from app.bracket_v2_service import persist_estimate as _v2_persist

            _est = _v2_est(session, deck, current_user.id, combos=combos)
            _v2_persist(session, deck.id, _est)
        except Exception as exc:  # noqa: BLE001
            print(f"[bracket_v3] persist failed deck={deck.id}: {exc}", flush=True)
    else:
        dead_cards = None

    return render(
        request,
        "_deck_panels.html",
        {
            "deck": deck,
            "bracket": bracket,
            "synergy": synergy,
            "combos": combos,
            "tokens": tokens,
            "dead_cards": dead_cards,
        },
    )


@app.post("/decks/{deck_id}/bulk-move")
def bulk_move_deck_cards(
    deck_id: int,
    row_ids: list[int] = Form(...),
    target_location_id: str = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if target_location_id == "sorter":
        if current_user.username not in DRAWER_SORTER_USERNAMES:
            return RedirectResponse(f"/decks/{deck_id}", status_code=303)
        for row_id in row_ids:
            return_card_from_deck(session, user_id=current_user.id, deck_row_id=row_id)
        threading.Thread(target=_bg_resort, args=(current_user.id,), daemon=True).start()
        return RedirectResponse(f"/decks/{deck_id}", status_code=303)

    try:
        location_id = int(target_location_id)
    except ValueError:
        return RedirectResponse(f"/decks/{deck_id}", status_code=303)
    for row_id in row_ids:
        try:
            move_inventory_row_to_location(
                session, row_id=row_id, user_id=current_user.id, location_id=location_id
            )
        except ValueError:
            pass
    return RedirectResponse(f"/decks/{deck_id}", status_code=303)


@app.post("/decks/{deck_id}/edit")
def decks_edit(
    deck_id: int,
    name: str = Form(...),
    format_name: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        update_deck(
            session,
            deck_id=deck_id,
            user_id=current_user.id,
            name=name,
            format_name=format_name,
            notes=notes,
        )
    except ValueError:
        pass
    return RedirectResponse(url="/decks", status_code=303)


@app.post("/decks/{deck_id}/delete")
async def decks_delete(
    deck_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_deck(session, deck_id=deck_id, user_id=current_user.id)
    return RedirectResponse(url="/decks", status_code=303)


@app.get("/decks/{deck_id}/export")
def decks_export(
    deck_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    rows = (
        session.query(InventoryRow)
        .join(Card)
        .filter(
            InventoryRow.user_id == current_user.id,
            InventoryRow.storage_location_id == deck.storage_location_id,
        )
        .order_by(Card.name.asc())
        .all()
    )

    commander_lines: list[str] = []
    deck_lines: list[str] = []
    for row in rows:
        card = row.card
        set_code = (card.set_code or "???").upper()
        collector = card.collector_number or "0"
        line = f"{row.quantity} {card.name} ({set_code}) {collector}"
        if row.role == "commander":
            commander_lines.append(line)
        else:
            deck_lines.append(line)

    parts: list[str] = []
    if commander_lines:
        parts.append("Commander")
        parts.extend(commander_lines)
        parts.append("")
    parts.append("Deck")
    parts.extend(deck_lines)

    content = "\n".join(parts)
    filename = f"{deck.name.replace(' ', '_')}.txt"
    return PlainTextResponse(
        content=content,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/decks/pull")
async def decks_pull(
    inventory_row_id: int = Form(...),
    deck_id: int = Form(...),
    quantity: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    pull_card_to_deck(
        session,
        user_id=current_user.id,
        deck_id=deck_id,
        inventory_row_id=inventory_row_id,
        quantity=quantity,
    )

    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@app.post("/decks/{deck_id}/intent")
def decks_intent(
    deck_id: int,
    intent_pod: str = Form(""),
    intent_speed: str = Form(""),
    intent_combo: str = Form(""),
    intent_winning: str = Form(""),
    intent_played: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Persist the bracket intent survey answers for a deck. Empty -> NULL."""
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        return RedirectResponse(url="/decks", status_code=303)
    deck.intent_pod = intent_pod.strip() or None
    deck.intent_speed = intent_speed.strip() or None
    deck.intent_combo = intent_combo.strip() or None
    deck.intent_winning = intent_winning.strip() or None
    deck.intent_played = intent_played.strip() or None
    session.commit()
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@app.post("/decks/{deck_id}/retag")
def decks_retag(
    deck_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Re-evaluate auto-tags for every row in this deck, additively.

    Existing user-set tags are preserved; suggested tags from the current
    `suggest_card_roles` patterns are unioned in. Never removes a tag.
    """
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        return RedirectResponse(url="/decks", status_code=303)

    commander_rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .filter(
            InventoryRow.user_id == current_user.id,
            InventoryRow.storage_location_id == deck.storage_location_id,
            InventoryRow.role == "commander",
        )
        .all()
    )
    themes = extract_commander_themes(commander_rows) if commander_rows else None

    rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .filter(
            InventoryRow.user_id == current_user.id,
            InventoryRow.storage_location_id == deck.storage_location_id,
        )
        .all()
    )

    changed = False
    for row in rows:
        suggested = suggest_card_roles(row.card, themes=themes)
        existing = get_row_tags(row)
        merged = sorted(set(suggested) | set(existing))
        if set(merged) != set(existing):
            set_row_tags(row, merged)
            changed = True

    if changed:
        session.commit()

    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@app.post("/decks/return")
async def decks_return(
    deck_id: int = Form(...),
    deck_row_id: int = Form(...),
    drawer: str = Form(""),
    slot: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    return_card_from_deck(
        session,
        user_id=current_user.id,
        deck_row_id=deck_row_id,
        drawer=drawer,
        slot=slot,
    )

    if current_user.username in DRAWER_SORTER_USERNAMES:
        threading.Thread(target=_bg_resort, args=(current_user.id,), daemon=True).start()

    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@app.post("/decks/rows/{row_id}/toggle-commander")
async def toggle_commander(
    request: Request,
    row_id: int,
    deck_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == current_user.id)
        .first()
    )
    if row:
        row.role = None if row.role == "commander" else "commander"
        session.commit()
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@app.post("/decks/rows/{row_id}/tags")
async def update_row_tags(
    request: Request,
    row_id: int,
    deck_id: int = Form(...),
    tags: list[str] = Form(default=[]),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == current_user.id)
        .first()
    )
    if row:
        set_row_tags(row, [t for t in tags if t in CARD_ROLE_TAGS])
        row.updated_at = datetime.utcnow()
        session.commit()
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


# -----------------------------------------------------------------------------
# Cards / pricing
# -----------------------------------------------------------------------------


@app.get("/test-scryfall/{scryfall_id}")
def test_scryfall(
    scryfall_id: str,
    current_user: User = Depends(get_current_user),
):
    card = fetch_card_by_scryfall_id(scryfall_id)
    return {"card": card}


@app.get("/cards/{card_id}")
def card_detail_page(
    request: Request,
    card_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    target_card = session.query(Card).filter(Card.id == card_id).first()
    if target_card is None:
        return RedirectResponse(url="/collection", status_code=303)

    inventory_rows = (
        session.query(InventoryRow)
        .options(
            joinedload(InventoryRow.card),
            joinedload(InventoryRow.storage_location),
        )
        .filter(
            InventoryRow.card_id == card_id,
            InventoryRow.user_id == current_user.id,
        )
        .all()
    )

    card_rows = []
    total_copies = 0
    total_value = 0.0

    for row in inventory_rows:
        price = effective_price(target_card, row.finish) or 0.0
        total = price * row.quantity
        card_rows.append(
            {
                "id": row.id,
                "finish": row.finish,
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

    return render(
        request,
        "card_detail.html",
        {
            "title": target_card.name,
            "card": target_card,
            "rows": card_rows,
            "total_copies": total_copies,
            "total_value": total_value,
            "current_user": current_user,
        },
    )


@app.get("/tokens")
def tokens_page(
    request: Request,
    name: str = "",
    subtype: str = "",
    location: str = "",
    double_sided: str = "",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """User's token-inventory page. Filter by name/subtype/location/double-sided."""
    storage_id: int | None = None
    if location.strip():
        try:
            storage_id = int(location)
        except ValueError:
            storage_id = None
    tokens = list_tokens(
        session,
        user_id=current_user.id,
        name_filter=name,
        subtype_filter=subtype,
        storage_location_id=storage_id,
        double_sided_only=(double_sided == "1"),
    )
    return render(
        request,
        "tokens.html",
        {
            "title": "Tokens",
            "tokens": tokens,
            "name": name,
            "subtype": subtype,
            "location": location,
            "double_sided": double_sided,
            "subtypes": list_token_subtypes(session, current_user.id),
            "locations": list_locations(session, current_user.id),
            "total_count": total_token_count(session, current_user.id),
            "current_user": current_user,
        },
    )


@app.get("/tokens/new")
def tokens_new_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    return render(
        request,
        "token_new.html",
        {
            "title": "New Token",
            "locations": list_locations(session, current_user.id),
            "current_user": current_user,
        },
    )


@app.post("/tokens/create")
def tokens_create(
    name: str = Form(...),
    quantity: int = Form(1),
    subtype: str = Form(""),
    type_line: str = Form(""),
    storage_location_id: str = Form(""),
    image_url: str = Form(""),
    is_double_sided: str = Form(""),
    back_name: str = Form(""),
    back_image_url: str = Form(""),
    back_set_code: str = Form(""),
    back_collector_number: str = Form(""),
    set_code: str = Form(""),
    collector_number: str = Form(""),
    scryfall_id: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        storage_id = int(storage_location_id) if storage_location_id else None
    except ValueError:
        storage_id = None
    try:
        create_token(
            session,
            user_id=current_user.id,
            name=name,
            quantity=quantity,
            subtype=subtype,
            type_line=type_line,
            storage_location_id=storage_id,
            image_url=image_url,
            is_double_sided=(is_double_sided == "1"),
            back_name=back_name,
            back_image_url=back_image_url,
            back_set_code=back_set_code,
            back_collector_number=back_collector_number,
            set_code=set_code,
            collector_number=collector_number,
            scryfall_id=scryfall_id,
            notes=notes,
        )
    except ValueError:
        return RedirectResponse(url="/tokens/new", status_code=303)
    return RedirectResponse(url="/tokens", status_code=303)


@app.post("/tokens/{token_id}/edit")
def tokens_edit(
    token_id: int,
    name: str = Form(...),
    quantity: int = Form(1),
    subtype: str = Form(""),
    storage_location_id: str = Form(""),
    image_url: str = Form(""),
    is_double_sided: str = Form(""),
    back_name: str = Form(""),
    back_image_url: str = Form(""),
    back_set_code: str = Form(""),
    back_collector_number: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        storage_id = int(storage_location_id) if storage_location_id else None
    except ValueError:
        storage_id = None
    update_token(
        session,
        token_id=token_id,
        user_id=current_user.id,
        name=name,
        quantity=max(0, quantity),
        subtype=subtype,
        storage_location_id=storage_id,
        image_url=image_url,
        is_double_sided=(is_double_sided == "1"),
        back_name=back_name,
        back_image_url=back_image_url,
        back_set_code=back_set_code,
        back_collector_number=back_collector_number,
        notes=notes,
    )
    return RedirectResponse(url="/tokens", status_code=303)


@app.post("/tokens/{token_id}/delete")
def tokens_delete(
    token_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_token(session, token_id, current_user.id)
    return RedirectResponse(url="/tokens", status_code=303)


@app.post("/decks/{deck_id}/tokens/add")
def decks_token_requirement_add(
    deck_id: int,
    token_name: str = Form(...),
    quantity_needed: int = Form(1),
    token_inventory_id: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        return RedirectResponse(url="/decks", status_code=303)
    try:
        link_id = int(token_inventory_id) if token_inventory_id else None
    except ValueError:
        link_id = None
    try:
        add_deck_token_requirement(
            session,
            deck_id=deck.id,
            token_name=token_name,
            quantity_needed=max(1, quantity_needed),
            token_inventory_id=link_id,
            notes=notes,
        )
    except ValueError:
        pass
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@app.get("/tokens/bulk-add")
def tokens_bulk_add_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Render the bulk-DFC paste-list page (no result yet)."""
    return render(
        request,
        "token_bulk_add.html",
        {
            "title": "Bulk Add DFC Tokens",
            "locations": list_locations(session, current_user.id),
            "current_user": current_user,
            "result": None,
        },
    )


@app.post("/tokens/bulk-add")
def tokens_bulk_add_submit(
    request: Request,
    pairs: str = Form(""),
    storage_location_id: str = Form(""),
    default_qty: int = Form(1),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Parse the paste-list, fetch each face from Scryfall, create rows."""
    try:
        storage_id = int(storage_location_id) if storage_location_id else None
    except ValueError:
        storage_id = None

    parsed = parse_bulk_dfc_lines(pairs)
    rows_created: list[dict] = []
    errors: list[dict] = []

    for entry in parsed:
        if not entry["ok"]:
            errors.append({"raw": entry["raw"], "error": entry["error"]})
            continue
        front = fetch_token_by_set_number(entry["front_set"], entry["front_collector"])
        if not front:
            errors.append(
                {
                    "raw": entry["raw"],
                    "error": f"front not found: {entry['front_set']} #{entry['front_collector']}",
                }
            )
            continue
        back = fetch_token_by_set_number(entry["back_set"], entry["back_collector"])
        if not back:
            errors.append(
                {
                    "raw": entry["raw"],
                    "error": f"back not found: {entry['back_set']} #{entry['back_collector']}",
                }
            )
            continue
        try:
            token = create_token(
                session,
                user_id=current_user.id,
                name=front["name"] or "",
                quantity=max(1, entry["quantity"] if entry["quantity"] else default_qty),
                subtype=front.get("subtype"),
                type_line=front.get("type_line"),
                storage_location_id=storage_id,
                image_url=front.get("image_url"),
                is_double_sided=True,
                back_name=back.get("name"),
                back_image_url=back.get("image_url"),
                back_set_code=back.get("set_code"),
                back_collector_number=back.get("collector_number"),
                set_code=front.get("set_code"),
                collector_number=front.get("collector_number"),
                scryfall_id=front.get("scryfall_id"),
            )
            rows_created.append(
                {
                    "id": token.id,
                    "front_name": front["name"],
                    "front_id": f"{front['set_code'].upper()}#{front['collector_number']}",
                    "back_name": back["name"],
                    "back_id": f"{back['set_code'].upper()}#{back['collector_number']}",
                    "quantity": token.quantity,
                }
            )
        except ValueError as exc:
            errors.append({"raw": entry["raw"], "error": str(exc)})

    return render(
        request,
        "token_bulk_add.html",
        {
            "title": "Bulk Add DFC Tokens",
            "locations": list_locations(session, current_user.id),
            "current_user": current_user,
            "result": {
                "created": rows_created,
                "errors": errors,
                "total_lines": len(parsed),
            },
            "previous_pairs": pairs,
            "previous_default_qty": default_qty,
            "previous_storage_id": storage_id,
        },
    )


@app.get("/tokens/api/autocomplete")
def tokens_api_autocomplete(
    q: str = "",
    current_user: User = Depends(get_current_user),
):
    """Live autocomplete for the new-token form. Calls Scryfall search with
    `is:token` so non-token cards don't pollute results."""
    if len(q.strip()) < 2:
        return JSONResponse([])
    return JSONResponse(autocomplete_token_names(q, limit=10))


@app.get("/tokens/api/lookup")
def tokens_api_lookup(
    name: str = "",
    set: str = "",
    collector: str = "",
    current_user: User = Depends(get_current_user),
):
    """Precise single-token lookup for auto-fill. Requires set + collector
    number; the t-prefix is auto-tried if the bare set code isn't a token
    set. Returns 400 if set + collector are missing — the picker endpoint
    is the right path for ambiguous name-only lookups."""
    if not (set.strip() and collector.strip()):
        return JSONResponse(
            {"error": "set_and_collector_required"},
            status_code=400,
        )
    data = fetch_token_by_set_number(set, collector)
    if not data:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(data)


@app.get("/tokens/api/search")
def tokens_api_search(
    name: str = "",
    current_user: User = Depends(get_current_user),
):
    """Multi-result search for the picker UI on the new-token form.

    Returns up to ~12 matching tokens with images so the user can
    disambiguate visually (e.g., picking the right Treasure printing
    when several share the name)."""
    results = search_tokens_by_name(name, limit=12)
    return JSONResponse({"results": results})


@app.post("/decks/{deck_id}/tokens/{req_id}/delete")
def decks_token_requirement_delete(
    deck_id: int,
    req_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        return RedirectResponse(url="/decks", status_code=303)
    delete_deck_token_requirement(session, req_id, deck.id)
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


# NOTE: /tokens/{scryfall_id} below MUST stay after the /tokens routes above —
# FastAPI matches in registration order; otherwise "new"/etc would be treated
# as scryfall ids.
@app.get("/tokens/{scryfall_id}")
def token_detail_page(
    request: Request,
    scryfall_id: str,
    current_user: User = Depends(get_current_user),
):
    data = fetch_card_by_scryfall_id(scryfall_id)
    if not data:
        return RedirectResponse(url="/collection", status_code=303)
    return render(
        request,
        "token_detail.html",
        {
            "title": data["name"],
            "token": data,
            "scryfall_url": f"https://scryfall.com/card/{data['set_code']}/{data['collector_number']}",
            "current_user": current_user,
        },
    )


@app.post("/cards/refresh")
async def card_refresh(
    request: Request,
    card_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    owned_row = (
        session.query(InventoryRow)
        .filter(
            InventoryRow.card_id == card_id,
            InventoryRow.user_id == current_user.id,
        )
        .first()
    )

    if owned_row is None:
        raise HTTPException(status_code=404, detail="Card not found in current user's collection")

    if refresh_card_from_scryfall(session, card_id):
        session.commit()

    return RedirectResponse(url=safe_redirect_url(request), status_code=303)


# -----------------------------------------------------------------------------
# Sets
# -----------------------------------------------------------------------------


@app.get("/sets")
def sets_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    sets = list_owned_sets(session, user_id=current_user.id)

    return render(
        request,
        "sets.html",
        {
            "title": "Sets",
            "sets": sets,
            "current_user": current_user,
        },
    )


@app.get("/sets/{set_code}")
def set_detail_page(
    request: Request,
    set_code: str,
    view: str = "all",
    show_tokens: bool = True,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    data = get_set_completion(
        session, set_code, view=view, user_id=current_user.id, include_tokens=show_tokens
    )

    return render(
        request,
        "set_detail.html",
        {
            "title": data["set_name"],
            "data": data,
            "current_user": current_user,
        },
    )


# ---------------------------------------------------------------------------
# Game tracker routes
# ---------------------------------------------------------------------------


@app.get("/games")
def games_list_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    games = list_games(session, current_user.id)
    total_wins = sum(1 for g in games for s in g.seats if s.placement == 1)
    return render(
        request,
        "games.html",
        {
            "title": "Game History",
            "games": games,
            "total_wins": total_wins,
            "current_user": current_user,
        },
    )


@app.get("/games/new")
def game_new_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    all_users = (
        session.query(User)
        .filter(User.is_active.is_(True))
        .order_by(User.display_name, User.username)
        .all()
    )
    all_decks = session.query(Deck).order_by(Deck.name).all()
    # JSON-safe: users list and deck lookup by user_id for JS filtering
    users_json = [{"id": u.id, "name": u.display_name or u.username} for u in all_users]
    decks_by_user_json = {}
    for d in all_decks:
        decks_by_user_json.setdefault(str(d.user_id), []).append({"id": d.id, "name": d.name})
    return render(
        request,
        "game_new.html",
        {
            "title": "New Game",
            "users_json": users_json,
            "decks_by_user_json": decks_by_user_json,
            "current_user": current_user,
            "current_user_id": current_user.id,
        },
    )


@app.post("/games")
def game_create(
    request: Request,
    player_count: int = Form(...),
    format: str = Form(""),
    player_names: list[str] = Form(...),
    deck_ids: list[str] = Form(...),
    grid_positions: list[str] = Form(default=[]),
    starting_life: int = Form(40),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    seats = []
    for i in range(player_count):
        name = player_names[i].strip() if i < len(player_names) else f"Player {i + 1}"
        did_raw = deck_ids[i] if i < len(deck_ids) else ""
        try:
            deck_id = int(did_raw) if did_raw else None
        except ValueError:
            deck_id = None
        pos_raw = grid_positions[i].strip() if i < len(grid_positions) else ""
        seats.append(
            {
                "player_name": name or f"Player {i + 1}",
                "deck_id": deck_id,
                "starting_life": starting_life,
                "grid_position": pos_raw or None,
            }
        )

    game = create_game(session, user_id=current_user.id, format=format, seats=seats)
    return RedirectResponse(f"/games/{game.id}", status_code=303)


@app.get("/games/{game_id}")
def game_detail_page(
    request: Request,
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    game = get_game(session, game_id, current_user.id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    decks = session.query(Deck).filter(Deck.user_id == current_user.id).order_by(Deck.name).all()
    return render(
        request,
        "game_detail.html",
        {"title": f"Game {game_id}", "game": game, "decks": decks, "current_user": current_user},
    )


@app.post("/games/{game_id}/end")
async def game_end(
    request: Request,
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    form_data = await request.form()

    game = get_game(session, game_id, current_user.id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    placements: dict[int, int] = {}
    final_lives: dict[int, int | None] = {}
    for seat in game.seats:
        p_val = form_data.get(f"placement_{seat.id}", "")
        l_val = form_data.get(f"final_life_{seat.id}", "")
        if p_val:
            try:
                placements[seat.id] = int(p_val)
            except ValueError:
                pass
        if l_val:
            try:
                final_lives[seat.id] = int(l_val)
            except ValueError:
                pass

    turn_count_raw = form_data.get("turn_count", "")
    notes = str(form_data.get("notes", ""))
    try:
        tc = int(turn_count_raw) if str(turn_count_raw).strip() else None
    except ValueError:
        tc = None

    end_game(session, game_id, current_user.id, placements, final_lives, tc, notes)
    return RedirectResponse(f"/games/{game_id}", status_code=303)


@app.post("/games/{game_id}/delete")
def game_delete(
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_game(session, game_id, current_user.id)
    return RedirectResponse("/games", status_code=303)
