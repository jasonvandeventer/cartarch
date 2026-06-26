"""Collection / inventory / locations / pending routes (v4 reorg extraction).

Inventory-row actions (remove, proxy, sell, trade, delete, move), the
Collection grid + export, Pending placement, Storage Locations (incl. the
quick-add card modal and bulk move/delete), and the Audit + import-undo
surface. Includes the location-grid + bulk-delete item builders; the latter
is shared read-side with the deck bulk-delete (imported from here).

Behaviour is byte-identical to the pre-extraction handlers in main.py — this
move changes wiring only, not logic.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import sort_spec
from app.audit_service import list_transaction_logs
from app.deck_service import create_deck, list_decks_basic
from app.decklist_service import name_owned_counts
from app.dependencies import (
    DRAWER_SORTER_USERNAMES,
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
    safe_redirect_url,
)
from app.inventory_service import (
    DRAWER_KEEP_PRICE_THRESHOLD,
    add_card_to_location,
    adjust_inventory_row_quantity,
    apply_collection_search_filters,
    build_collection_filter_query,
    bulk_delete_inventory_rows,
    collector_sort_key,
    confirm_all_pending,
    confirm_pending_row,
    delete_inventory_row,
    get_collection_facet_counts,
    get_inventory_row_stats,
    get_location_label,
    is_price_stale,
    list_inventory_rows,
    list_pending_rows,
    move_inventory_row_to_location,
    move_surplus_to_location,
    resolve_drawer_cull_candidates,
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
from app.models import (
    Card,
    Deck,
    ImportBatch,
    InventoryRow,
    Share,
    ShowcaseItem,
    StorageLocation,
    User,
)
from app.presentation_service import (
    build_pending_batch_groups,
    build_pending_view_model,
)
from app.pricing import card_metadata, effective_price

router = APIRouter()


# -----------------------------------------------------------------------------
# Inventory mutations
# -----------------------------------------------------------------------------


@router.post("/inventory/rows/{row_id}/remove")
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


@router.post("/inventory/rows/{row_id}/toggle-proxy")
def toggle_inventory_row_proxy(
    request: Request,
    row_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Inventory row not found")
    row.is_proxy = not bool(row.is_proxy)
    session.commit()
    return RedirectResponse(url=safe_redirect_url(request), status_code=303)


@router.post("/inventory/rows/{row_id}/sell")
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


@router.post("/inventory/rows/{row_id}/trade")
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


@router.post("/inventory/rows/{row_id}/delete")
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


# -----------------------------------------------------------------------------
# Shared Collection filter (v3.x) — ONE parse of the /collection filter surface,
# consumed by BOTH the grid page and the CSV export so the two can never drift
# on what "the filtered collection" means. The query COMPOSITION already lives
# in inventory_service.build_collection_filter_query; this is the param-parse
# half (facet-list collapse + tolerant price-string→float) that used to be
# inline in collection_page, plus the drawer/location scope derivation.
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionFilter:
    """Parsed /collection filter params (the filter surface only).

    ``colors`` is a joined letter token ("WU"); ``types``/``status``/``finishes``
    are comma-joined CSV; the ``*_raw`` price strings are echoed back to the
    template while ``facet_price_*`` are their tolerant float parse (blank /
    non-numeric → None, i.e. the price facet is skipped). ``sort``/``direction``/
    ``page``/``view`` ride along for the page; export ignores them.
    """

    search: str
    finish: str
    location_id: int
    sort: str
    direction: str
    page: int
    colors: str
    types: str
    status: str
    finishes: str
    price_min_raw: str
    price_max_raw: str
    facet_price_min: float | None
    facet_price_max: float | None
    view: str


def _parse_facet_price(value: str) -> float | None:
    """Tolerant price-string → float: blank / non-numeric → None (facet skipped)."""
    if value and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def collection_filter(
    search: str = "",
    finish: str = "",
    location_id: int = 0,
    sort: str = "newest",
    direction: str = "desc",
    page: int = 1,
    # v3.28.8 — facet-sidebar params. Each is an independent URL param,
    # AND-composed with `search` at query time (Option A interaction
    # model). All optional; empty = facet not active.
    #
    # v3.31.0 — accept these as repeated params (``list``). The sidebar
    # form submits one entry per checked box (``colors=W&colors=U``),
    # whereas the toolbar hidden inputs / pagination / export links emit
    # a single pre-joined token (``colors=WU``). Reading them as a bare
    # ``str`` kept only the FIRST repeated value, so multi-select in the
    # sidebar silently collapsed to one color/type/etc. — the visible
    # "mana pips don't filter" bug. Joining the lists below makes both
    # producers converge on the same joined string the rest of the
    # pipeline already expects.
    colors: list[str] = Query(default=[]),
    types: list[str] = Query(default=[]),
    status: list[str] = Query(default=[]),
    finishes: list[str] = Query(default=[]),
    price_min: str = "",
    price_max: str = "",
    view: str = "grid",
) -> CollectionFilter:
    """FastAPI dependency: bind + parse the /collection filter query params.

    Collapses the repeated facet params into the single joined-token form the
    downstream query + facet-set construction expect (colors joined with no
    separator, CSV facets with commas — works whether each element is an
    individual checkbox value or an already-joined toolbar/pagination token),
    and parses the price strings to floats. Used by ``collection_page`` and
    ``collection_export`` alike.
    """
    return CollectionFilter(
        search=search,
        finish=finish,
        location_id=location_id,
        sort=sort,
        direction=direction,
        page=page,
        colors="".join(c.strip() for c in colors if c.strip()),
        types=",".join(t.strip() for t in types if t.strip()),
        status=",".join(s.strip() for s in status if s.strip()),
        finishes=",".join(f.strip() for f in finishes if f.strip()),
        price_min_raw=price_min or "",
        price_max_raw=price_max or "",
        facet_price_min=_parse_facet_price(price_min),
        facet_price_max=_parse_facet_price(price_max),
        view=view,
    )


def _resolve_collection_scope(
    session: Session, user_id: int, location_id: int
) -> tuple[StorageLocation | None, str, int]:
    """Derive ``(selected_location, drawer_name, scope_location_id)`` from a raw
    ``location_id``, matching ``collection_page``: a drawer-type location becomes
    a drawer-name filter (its own query dimension) with the plain location scope
    cleared to 0; a non-drawer location keeps its id as the scope; a missing /
    unowned location scopes to 0 (all locations)."""
    selected_location = None
    if location_id:
        selected_location = get_location(session, location_id=location_id, user_id=user_id)
    drawer = ""
    if selected_location and selected_location.type == "drawer":
        drawer = selected_location.name.replace("Drawer", "").strip()
    scope_location_id = (
        location_id if (selected_location and selected_location.type != "drawer") else 0
    )
    return selected_location, drawer, scope_location_id


def _filtered_collection_query(session: Session, user_id: int, filters: CollectionFilter):
    """Resolve a ``CollectionFilter`` to the shared joined+filtered base query
    (``build_collection_filter_query``), applying the same drawer/location scope
    the grid page uses. Returns ``(base_query, selected_location, drawer)``."""
    selected_location, drawer, scope_location_id = _resolve_collection_scope(
        session, user_id, filters.location_id
    )
    base_query = build_collection_filter_query(
        session,
        user_id,
        search=filters.search,
        facet_colors=filters.colors,
        facet_types=filters.types,
        facet_status=filters.status,
        facet_finishes=filters.finishes,
        facet_price_min=filters.facet_price_min,
        facet_price_max=filters.facet_price_max,
        finish=filters.finish,
        location_id=scope_location_id,
        drawer=drawer,
    )
    return base_query, selected_location, drawer


@router.get("/collection")
def collection_page(
    request: Request,
    filters: CollectionFilter = Depends(collection_filter),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    search = filters.search
    finish = filters.finish
    location_id = filters.location_id
    sort = filters.sort
    direction = filters.direction
    page = filters.page
    colors = filters.colors
    types = filters.types
    status = filters.status
    finishes = filters.finishes
    price_min = filters.price_min_raw
    price_max = filters.price_max_raw
    view = filters.view
    facet_price_min = filters.facet_price_min
    facet_price_max = filters.facet_price_max

    per_page = 50

    _, drawer, scope_location_id = _resolve_collection_scope(session, current_user.id, location_id)

    # v3.27.19 — when count-sorting, run the name-level owned-count
    # aggregation ONCE up front. Pass the dict into list_inventory_rows
    # for the sort key AND into the template for three-level group
    # header rendering (name → printing → location). Single GROUP BY
    # query per page load — the spec's N+1 failure mode is the
    # per-InventoryRow count, not a single pre-computed pass.
    owned_counts: dict[str, int] | None = None
    if sort == "count":
        owned_counts = name_owned_counts(session, current_user.id)

    # v3.28.8 — facet price floats + view mode are parsed in the shared
    # ``collection_filter`` dependency now; view is whitelisted here.
    view_mode = "rows" if view == "rows" else "grid"

    inventory_rows, total_count = list_inventory_rows(
        session,
        user_id=current_user.id,
        search=search,
        finish=finish,
        drawer=drawer,
        location_id=scope_location_id,
        sort=sort,
        direction=direction,
        page=page,
        per_page=per_page,
        owned_counts=owned_counts,
        facet_colors=colors,
        facet_types=types,
        facet_status=status,
        facet_finishes=finishes,
        facet_price_min=facet_price_min,
        facet_price_max=facet_price_max,
    )

    # v3.39.x — card count (SUM of quantity) over the SAME filtered set
    # ``total_count`` (rows) covers, so the bulk panel can render the accurate
    # "N rows (M cards)" instead of mislabelling a row count as "cards". One
    # cheap aggregate on the already-built filter query; matches the grid's
    # matching set exactly (no facet drift). ``COALESCE(...,0)`` for an empty match.
    total_cards_matching = (
        build_collection_filter_query(
            session,
            current_user.id,
            search=search,
            facet_colors=colors,
            facet_types=types,
            facet_status=status,
            facet_finishes=finishes,
            facet_price_min=facet_price_min,
            facet_price_max=facet_price_max,
            finish=finish,
            location_id=scope_location_id,
            drawer=drawer,
        )
        .with_entities(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .scalar()
    )

    # v3.28.8 — facet counts for the sidebar. Single aggregate query;
    # reflects the active search (boolean parser) but ignores active
    # facet state for v1 simplicity. See get_collection_facet_counts
    # docstring for the rationale.
    facet_counts = get_collection_facet_counts(session, user_id=current_user.id, search=search)

    stats = get_inventory_row_stats(
        session,
        user_id=current_user.id,
        search=search,
        finish=finish,
        drawer=drawer,
        location_id=scope_location_id,
    )

    location_counts = {}
    for drawer_number, count in stats["drawer_counts"].items():
        if count > 0:
            location_counts[f"Drawer {drawer_number}"] = count

    if stats["unassigned_count"] > 0:
        location_counts["Unassigned"] = stats["unassigned_count"]

    decks = list_decks_basic(session, user_id=current_user.id)
    locations = list_locations(session, user_id=current_user.id)
    # v3.31.0 — the inventory_card "Add to Showcase" action now offers a
    # picker over the user's Showcases (multi-showcase). Empty list →
    # the macro renders the single-button form that falls back to the
    # default Showcase.
    from app import share_service

    showcases = share_service.list_showcases(session, current_user.id)
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
                "language": row.language or "en",
                "is_proxy": bool(row.is_proxy),
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
                # v3.27.19 — total owned across all printings of this
                # card name. None when sort != "count" so the template
                # can branch on its presence to decide whether to
                # render the three-level group headers.
                "owned_total": (
                    (owned_counts or {}).get((row.card.name or "").lower())
                    if owned_counts is not None
                    else None
                ),
            }
        )

    total_pages = max(1, math.ceil(total_count / per_page))
    show_onboarding = total_count == 0

    # v3.x — filter-scoped bulk-action result, passed back via query string by
    # the /collection/bulk-* routes (same query-param flash pattern as the
    # showcase/share routes). The template renders a one-shot banner; absent
    # `bulk` param → no banner.
    bulk_msg = {
        "kind": request.query_params.get("bulk"),
        "added": request.query_params.get("added"),
        "skipped": request.query_params.get("skipped"),
        "moved": request.query_params.get("moved"),
        "pending": request.query_params.get("pending"),
        "name": request.query_params.get("name"),
        "reason": request.query_params.get("reason"),
        "culled_cards": request.query_params.get("culled_cards"),
        "culled_copies": request.query_params.get("culled_copies"),
        "deleted_rows": request.query_params.get("deleted_rows"),
        "deleted_cards": request.query_params.get("deleted_cards"),
    }

    return render(
        request,
        "collection.html",
        {
            "title": "Collection",
            "bulk_msg": bulk_msg,
            "items": items,
            "search": search,
            "finish_filter": finish,
            "drawer_filter": drawer,
            "sort": sort,
            "direction": direction,
            "sort_options": sort_spec.COLLECTION_SORT_OPTIONS,
            "page": page,
            "per_page": per_page,
            "total_count": total_count,
            "total_cards_matching": total_cards_matching,
            "total_pages": total_pages,
            "total_value": stats["total_value"],
            "total_cards": stats["total_cards"],
            "pending_value": stats["pending_value"],
            "pending_cards": stats["pending_cards"],
            "unique_cards": stats["unique_cards"],
            "drawer_counts": stats["drawer_counts"],
            "unassigned_count": stats["unassigned_count"],
            "location_counts": location_counts,
            "decks": decks,
            "locations": locations,
            "showcases": showcases,
            "location_id": location_id,
            "current_user": current_user,
            "show_onboarding": show_onboarding,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            # v3.28.8 — facet state + counts + view mode for the
            # faceted-sidebar Collection redesign. Each facet's set is
            # parsed from a CSV URL param; the template renders the
            # sidebar with the corresponding options pre-checked.
            "facet_colors_set": {c for c in (colors or "").upper() if c in "WUBRGC"},
            "facet_types_set": {t.strip() for t in (types or "").split(",") if t.strip()},
            "facet_status_set": {s.strip() for s in (status or "").split(",") if s.strip()},
            "facet_finishes_set": {f.strip() for f in (finishes or "").split(",") if f.strip()},
            "facet_price_min_raw": price_min or "",
            "facet_price_max_raw": price_max or "",
            "facet_counts": facet_counts,
            "view_mode": view_mode,
        },
    )


def _csv_formula_safe(value: str) -> str:
    """Neutralize CSV formula injection (v4.0.1, security S3).

    ``csv.writer`` quotes structure but does NOT defuse a leading ``= + - @``,
    which Excel / Google Sheets execute as a formula. Prefix any such value with
    a single quote so the spreadsheet treats the cell as literal text. Applied to
    the user-entered free-text columns (location name, tags) in both CSV export
    writers. No-op for normal values (and for non-strings / empty cells).
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


@router.get("/collection/export")
def collection_export(
    filters: CollectionFilter = Depends(collection_filter),
    format: str = Query("csv"),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    # v3.x — the export now honors the active /collection filter. It consumes
    # the SAME shared filter unit the grid page does (``collection_filter`` +
    # ``_filtered_collection_query``), so a filtered export == the rows the
    # user sees, never a full dump. No filter params → the whole collection
    # (build_collection_filter_query with empty filters), preserving the old
    # default behavior.
    base_query, _selected_location, _drawer = _filtered_collection_query(
        session, current_user.id, filters
    )
    # build_collection_filter_query already joinedload's card + storage_location,
    # so the per-row card reads below are request-path-safe (no N+1, no network).
    rows = base_query.order_by(Card.name.asc()).all()

    if format == "json":
        # LLM-parseable variant — per-card gameplay metadata (persisted columns,
        # no Scryfall call) plus inventory context. Honors the same filter.
        items = []
        for row in rows:
            loc = row.storage_location
            price = effective_price(row.card, row.finish)
            items.append(
                {
                    **card_metadata(row.card),
                    "quantity": row.quantity,
                    "finish": row.finish or "normal",
                    "location": loc.name if loc else None,
                    "location_type": loc.type if loc else None,
                    "language": row.language or "en",
                    "role": row.role or None,
                    "tags": row.tags or None,
                    "is_proxy": bool(row.is_proxy),
                    "price": round(price, 2) if price else None,
                }
            )
        return JSONResponse({"cards": items})

    buf = io.StringIO()
    writer = csv.writer(buf)
    # v3.30.16 — expanded schema. The first six columns are UNCHANGED in
    # name, order, and content (downstream parsers consuming the v3.30.15
    # 6-column shape continue to work). Five new columns appended at end:
    # Location Type / Language / Role / Tags / Is Proxy. The importer
    # recognizes the new headers via HEADER_ALIASES (case + space
    # tolerant); old 6-column CSVs round-trip as before with the new
    # fields defaulted on re-import.
    #
    # v3.x — two MORE columns appended at the end: Scryfall ID (a stable join
    # key for downstream tools; the importer already reads it via
    # HEADER_ALIASES for high-precision matching) and Price (the finish-aware
    # effective_price from PERSISTED Scryfall data — NO network call on the
    # request path; "Price" is not an importer alias so re-import ignores it).
    writer.writerow(
        [
            "Name",
            "Set",
            "Collector Number",
            "Finish",
            "Quantity",
            "Location",
            "Location Type",
            "Language",
            "Role",
            "Tags",
            "Is Proxy",
            "Scryfall ID",
            "Price",
            # v3.x — read-only gameplay enrichment appended at the END (existing
            # columns byte-identical). NOT importer HEADER_ALIASES — precedent:
            # the Price column. oracle_text/legalities stay OUT of CSV
            # (newlines/commas/quotes); they live in the JSON variant.
            "Color Identity",
            "Colors",
            "Type Line",
            "Mana Cost",
            "Mana Value",
            "Rarity",
        ]
    )
    for row in rows:
        card = row.card
        loc = row.storage_location
        # effective_price reads only persisted price columns (price_usd*),
        # so this is request-path-safe. "" when no price is cached yet.
        price = effective_price(card, row.finish)
        writer.writerow(
            [
                card.name or "",
                (card.set_code or "").upper(),
                card.collector_number or "",
                row.finish or "normal",
                row.quantity,
                _csv_formula_safe(loc.name if loc else ""),
                loc.type if loc else "",
                row.language or "en",
                row.role or "",
                _csv_formula_safe(row.tags or ""),
                "true" if row.is_proxy else "false",
                card.scryfall_id or "",
                f"{price:.2f}" if price else "",
                card.color_identity or "",
                card.colors or "",
                card.type_line or "",
                card.mana_cost or "",
                "" if card.cmc is None else f"{card.cmc:g}",
                card.rarity or "",
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


@router.post("/collection/update-location")
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


@router.post("/inventory/rows/{row_id}/move")
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


# NB: a dead ``POST /collection/delete`` single-row route lived here — removed
# v3.39.x. It had no UI caller (grep-confirmed) and duplicated
# ``POST /inventory/rows/{id}/delete`` (the real "Delete Row" button). See
# collection-delete-investigation.md.


@router.post("/collection/resort")
async def collection_resort(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if current_user.username in DRAWER_SORTER_USERNAMES:
        resort_collection(session, user_id=current_user.id)
    return RedirectResponse(url="/collection", status_code=303)


# -----------------------------------------------------------------------------
# Filter-scoped bulk actions (v3.x) — "add/move all cards matching the filter"
#
# The Collection grid is paginated (50/page); these act on the WHOLE matching
# set, not the visible page. The set is resolved server-side from the SAME
# filter params the grid uses (via build_collection_filter_query), so it equals
# what the user sees. Placed (non-pending) rows only — pending matches are
# counted and surfaced, never silently dropped. Showcase add = static
# materialization; location move = the existing move primitive, deck-type
# destinations excluded, resort NOT triggered. (Recon Q-premises A–D.)
# -----------------------------------------------------------------------------


def _bulk_filter_placed_ids(
    session: Session,
    user_id: int,
    *,
    search: str,
    colors: str,
    types: str,
    status: str,
    finishes: str,
    price_min: str,
    price_max: str,
    finish: str,
    location_id: int,
) -> tuple[list[int], int]:
    """Resolve the current Collection filter to ``(placed_row_ids,
    pending_excluded_count)``.

    Mirrors ``collection_page``'s param handling so the bulk set equals the
    visible grid: price strings → floats (blank / non-numeric → None, i.e.
    facet skipped), and a drawer-type ``location_id`` becomes a drawer-name
    scope (the page never feeds a drawer location id straight through —
    drawer is its own filter dimension). Only PLACED rows are returned; the
    matching pending rows are counted so the caller can surface the drop.
    """
    facet_price_min: float | None = None
    facet_price_max: float | None = None
    if price_min and price_min.strip():
        try:
            facet_price_min = float(price_min.strip())
        except ValueError:
            facet_price_min = None
    if price_max and price_max.strip():
        try:
            facet_price_max = float(price_max.strip())
        except ValueError:
            facet_price_max = None

    drawer = ""
    scope_location_id = location_id
    if location_id:
        selected_location = get_location(session, location_id=location_id, user_id=user_id)
        if selected_location and selected_location.type == "drawer":
            drawer = selected_location.name.replace("Drawer", "").strip()
            scope_location_id = 0

    base_query = build_collection_filter_query(
        session,
        user_id,
        search=search,
        facet_colors=colors,
        facet_types=types,
        facet_status=status,
        facet_finishes=finishes,
        facet_price_min=facet_price_min,
        facet_price_max=facet_price_max,
        finish=finish,
        location_id=scope_location_id,
        drawer=drawer,
    )
    placed_ids = [
        row_id
        for (row_id,) in base_query.with_entities(InventoryRow.id)
        .filter(InventoryRow.is_pending == False)  # noqa: E712
        .all()
    ]
    pending_excluded = (
        base_query.with_entities(InventoryRow.id)
        .filter(InventoryRow.is_pending == True)  # noqa: E712
        .count()
    )
    return placed_ids, pending_excluded


def _collection_filter_redirect(filter_params: dict, message: dict) -> RedirectResponse:
    """303 back to /collection preserving the active filter (so the user lands
    on the same view) with the bulk-action result appended as query params the
    page renders into a flash banner. Empty filter values are dropped to keep
    the URL clean (``location_id=0`` == all locations)."""
    merged = {k: v for k, v in filter_params.items() if v not in ("", 0, None)}
    merged.update(message)
    return RedirectResponse(url=f"/collection?{urlencode(merged)}", status_code=303)


@router.post("/collection/bulk-add-showcase")
def collection_bulk_add_showcase(
    showcase_id: int = Form(...),
    search: str = Form(""),
    colors: str = Form(""),
    types: str = Form(""),
    status: str = Form(""),
    finishes: str = Form(""),
    price_min: str = Form(""),
    price_max: str = Form(""),
    finish: str = Form(""),
    location_id: int = Form(0),
    include_proxies: bool = Form(False),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    from app import share_service

    filter_params = {
        "search": search,
        "colors": colors,
        "types": types,
        "status": status,
        "finishes": finishes,
        "price_min": price_min,
        "price_max": price_max,
        "finish": finish,
        "location_id": location_id,
    }
    placed_ids, pending_excluded = _bulk_filter_placed_ids(
        session,
        current_user.id,
        search=search,
        colors=colors,
        types=types,
        status=status,
        finishes=finishes,
        price_min=price_min,
        price_max=price_max,
        finish=finish,
        location_id=location_id,
    )
    # Proxies are skipped by default here too — same rule as the Showcase-page
    # bulk-adds (two bulk paths must not have opposite proxy defaults). Opt in
    # with the panel's "Include proxies" checkbox (ADR proxy-valuation-2026-06-12).
    result = share_service.add_rows_to_showcase(
        session,
        current_user.id,
        showcase_id,
        row_ids=placed_ids,
        include_proxies=include_proxies,
    )
    if result is None:
        return _collection_filter_redirect(
            filter_params, {"bulk": "error", "reason": "no_showcase"}
        )
    showcase = share_service.get_showcase(session, current_user.id, showcase_id)
    return _collection_filter_redirect(
        filter_params,
        {
            "bulk": "added",
            "added": result["added"],
            "skipped": result["skipped"],
            "pending": pending_excluded,
            "name": showcase.name if showcase else "showcase",
        },
    )


@router.post("/collection/bulk-move")
def collection_bulk_move(
    target_location_id: int = Form(...),
    search: str = Form(""),
    colors: str = Form(""),
    types: str = Form(""),
    status: str = Form(""),
    finishes: str = Form(""),
    price_min: str = Form(""),
    price_max: str = Form(""),
    finish: str = Form(""),
    location_id: int = Form(0),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    filter_params = {
        "search": search,
        "colors": colors,
        "types": types,
        "status": status,
        "finishes": finishes,
        "price_min": price_min,
        "price_max": price_max,
        "finish": finish,
        "location_id": location_id,
    }
    # Destination must be owned and NOT a deck: moving straight into a deck
    # location would bypass pull_card_to_deck reconciliation / role / variant
    # accounting (recon Premise D — move_inventory_row_to_location is unguarded).
    # Decks remain reachable only via the deck import/add flows.
    target = get_location(session, location_id=target_location_id, user_id=current_user.id)
    if target is None or target.type == "deck":
        return _collection_filter_redirect(filter_params, {"bulk": "error", "reason": "bad_target"})

    placed_ids, pending_excluded = _bulk_filter_placed_ids(
        session,
        current_user.id,
        search=search,
        colors=colors,
        types=types,
        status=status,
        finishes=finishes,
        price_min=price_min,
        price_max=price_max,
        finish=finish,
        location_id=location_id,
    )
    moved = 0
    for row_id in placed_ids:
        try:
            move_inventory_row_to_location(
                session,
                row_id=row_id,
                user_id=current_user.id,
                location_id=target_location_id,
            )
            moved += 1
        except ValueError:
            pass
    # NB: resort_collection is intentionally NOT called — an explicit
    # destination must not be hijacked back into the drawers (matches the
    # explicit-destination import rule).
    return _collection_filter_redirect(
        filter_params,
        {
            "bulk": "moved",
            "moved": moved,
            "pending": pending_excluded,
            "name": target.name,
        },
    )


def _cull_resolver_kwargs(
    *,
    search: str,
    colors: str,
    types: str,
    status: str,
    finishes: str,
    price_min: str,
    price_max: str,
    finish: str,
    location_id: int,
) -> dict:
    """Parse the replayed ``/collection`` filter into ``resolve_drawer_cull_candidates``
    kwargs — same tolerant price-string→float handling as ``_bulk_filter_placed_ids``
    so the cull set matches the grid. ``location_id`` is passed through so the cull
    can be narrowed to a SINGLE drawer when the grid is scoped to one (the resolver
    honors it only when it names a drawer-type location; otherwise it spans every
    drawer). The cull is drawer-scoped + ``quantity > 1`` on top of this."""
    facet_price_min: float | None = None
    facet_price_max: float | None = None
    if price_min and price_min.strip():
        try:
            facet_price_min = float(price_min.strip())
        except ValueError:
            facet_price_min = None
    if price_max and price_max.strip():
        try:
            facet_price_max = float(price_max.strip())
        except ValueError:
            facet_price_max = None
    return {
        "location_id": location_id,
        "search": search,
        "facet_colors": colors,
        "facet_types": types,
        "facet_status": status,
        "facet_finishes": finishes,
        "facet_price_min": facet_price_min,
        "facet_price_max": facet_price_max,
        "finish": finish,
    }


@router.post("/collection/cull-preview")
def collection_cull_preview(
    request: Request,
    target_location_id: int = Form(...),
    search: str = Form(""),
    colors: str = Form(""),
    types: str = Form(""),
    status: str = Form(""),
    finishes: str = Form(""),
    price_min: str = Form(""),
    price_max: str = Form(""),
    finish: str = Form(""),
    location_id: int = Form(0),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Verify-before-commit gate for the retroactive drawer cull (v3.38.0).

    Computes the cull candidates (drawer rows, ``quantity > 1``, surplus copies
    NOT intrinsically protected) and renders a confirmation page showing exactly
    how many cards / copies would move to the chosen Bulk location — never a
    silent move. The Confirm button posts the same params to
    ``/collection/cull-to-bulk``, which recomputes the set (single source of
    truth — no drift)."""
    filter_params = {
        "search": search,
        "colors": colors,
        "types": types,
        "status": status,
        "finishes": finishes,
        "price_min": price_min,
        "price_max": price_max,
        "finish": finish,
        "location_id": location_id,
    }
    # Same non-deck guard as bulk-move: a deck destination would bypass deck
    # reconciliation. Bulk must be a real (non-deck) StorageLocation.
    target = get_location(session, location_id=target_location_id, user_id=current_user.id)
    if target is None or target.type == "deck":
        return _collection_filter_redirect(filter_params, {"bulk": "error", "reason": "bad_target"})

    candidates = resolve_drawer_cull_candidates(
        session,
        current_user.id,
        **_cull_resolver_kwargs(
            search=search,
            colors=colors,
            types=types,
            status=status,
            finishes=finishes,
            price_min=price_min,
            price_max=price_max,
            finish=finish,
            location_id=location_id,
        ),
    )
    # Group by set, then natural/numeric collector-number order (1, 2, 10, 11)
    # rather than the resolver's row-id order — collector_sort_key zero-pads the
    # numeric portion so '10' sorts after '2', not before it.
    candidates = sorted(
        candidates,
        key=lambda r: (
            (r.card.set_code or "").lower() if r.card else "",
            collector_sort_key(r.card.collector_number if r.card else None),
        ),
    )
    items = []
    total_copies = 0
    for row in candidates:
        surplus = row.quantity - 1
        total_copies += surplus
        items.append(
            {
                "name": row.card.name if row.card else "(unknown)",
                "set_code": (row.card.set_code or "").upper() if row.card else "",
                "collector_number": row.card.collector_number if row.card else "",
                "finish": row.finish,
                "quantity": row.quantity,
                "surplus": surplus,
                "price": effective_price(row.card, row.finish) if row.card else 0.0,
            }
        )

    return render(
        request,
        "cull_preview.html",
        {
            "title": "Cull drawer dupes to Bulk",
            "items": items,
            "total_cards": len(items),
            "total_copies": total_copies,
            "target": target,
            "filter_params": filter_params,
            "threshold": DRAWER_KEEP_PRICE_THRESHOLD,
            "current_user": current_user,
        },
    )


@router.post("/collection/cull-to-bulk")
def collection_cull_to_bulk(
    target_location_id: int = Form(...),
    search: str = Form(""),
    colors: str = Form(""),
    types: str = Form(""),
    status: str = Form(""),
    finishes: str = Form(""),
    price_min: str = Form(""),
    price_max: str = Form(""),
    finish: str = Form(""),
    location_id: int = Form(0),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Commit the retroactive drawer cull: each candidate keeps one findable
    copy in its drawer (the layer-2 keeper) and its surplus copies move to the
    Bulk location. ``resort_collection`` is intentionally NOT called — an
    explicit destination must not be hijacked back into the drawers (the same
    rule the bulk-move and explicit-destination-import paths follow)."""
    filter_params = {
        "search": search,
        "colors": colors,
        "types": types,
        "status": status,
        "finishes": finishes,
        "price_min": price_min,
        "price_max": price_max,
        "finish": finish,
        "location_id": location_id,
    }
    target = get_location(session, location_id=target_location_id, user_id=current_user.id)
    if target is None or target.type == "deck":
        return _collection_filter_redirect(filter_params, {"bulk": "error", "reason": "bad_target"})

    candidates = resolve_drawer_cull_candidates(
        session,
        current_user.id,
        **_cull_resolver_kwargs(
            search=search,
            colors=colors,
            types=types,
            status=status,
            finishes=finishes,
            price_min=price_min,
            price_max=price_max,
            finish=finish,
            location_id=location_id,
        ),
    )
    moved_copies = 0
    moved_cards = 0
    for row in candidates:
        try:
            moved = move_surplus_to_location(
                session,
                row_id=row.id,
                user_id=current_user.id,
                location_id=target_location_id,
                keep=1,
            )
        except ValueError:
            moved = 0
        if moved:
            moved_copies += moved
            moved_cards += 1

    return _collection_filter_redirect(
        filter_params,
        {
            "bulk": "culled",
            "culled_cards": moved_cards,
            "culled_copies": moved_copies,
            "name": target.name,
        },
    )


# -----------------------------------------------------------------------------
# Delete matching (v3.39.x, Stage 2 of the collection-delete work) — filter-scoped
# bulk delete, reusing _bulk_filter_placed_ids + bulk_delete_inventory_rows (the
# FK-safe primitive). Preview-then-confirm like the cull; typed confirmation for a
# whole-collection (unfiltered) delete. NO new deletion logic.
# -----------------------------------------------------------------------------


def _is_unfiltered(filter_params: dict) -> bool:
    """True when the Collection filter is empty — i.e. the delete targets the
    WHOLE placed collection. Drives the extra typed-confirmation requirement."""
    text_blank = all(
        not str(filter_params.get(k, "")).strip()
        for k in (
            "search",
            "colors",
            "types",
            "status",
            "finishes",
            "price_min",
            "price_max",
            "finish",
        )
    )
    loc = filter_params.get("location_id", 0)
    return text_blank and (loc in (0, "0", "", None))


def _delete_blast_radius(session: Session, user_id: int, row_ids: list[int]) -> dict:
    """Impact summary for a set of about-to-be-deleted placed rows: total card
    count (quantity sum) and how many of the rows are in decks, in showcases, and
    in *shared* showcases. Read-only; counts only — drives the preview's
    informed-consent gate before the confirm button is enabled."""
    if not row_ids:
        return {
            "card_count": 0,
            "rows_in_decks": 0,
            "rows_in_showcases": 0,
            "rows_in_shared_showcases": 0,
        }
    card_count = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .filter(InventoryRow.id.in_(row_ids))
        .scalar()
    )
    rows_in_decks = (
        session.query(InventoryRow.id)
        .join(StorageLocation, InventoryRow.storage_location_id == StorageLocation.id)
        .filter(InventoryRow.id.in_(row_ids), StorageLocation.type == "deck")
        .count()
    )
    showcased = {
        r[0]
        for r in session.query(ShowcaseItem.inventory_row_id)
        .filter(ShowcaseItem.inventory_row_id.in_(row_ids))
        .distinct()
    }
    shared = {
        r[0]
        for r in session.query(ShowcaseItem.inventory_row_id)
        .join(Share, Share.showcase_id == ShowcaseItem.showcase_id)
        .filter(ShowcaseItem.inventory_row_id.in_(row_ids))
        .distinct()
    }
    return {
        "card_count": int(card_count or 0),
        "rows_in_decks": rows_in_decks,
        "rows_in_showcases": len(showcased),
        "rows_in_shared_showcases": len(shared),
    }


@router.post("/collection/delete-preview")
def collection_delete_preview(
    request: Request,
    search: str = Form(""),
    colors: str = Form(""),
    types: str = Form(""),
    status: str = Form(""),
    finishes: str = Form(""),
    price_min: str = Form(""),
    price_max: str = Form(""),
    finish: str = Form(""),
    location_id: int = Form(0),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Verify-before-commit gate for the filter-scoped Collection delete. Shows
    the row count, card count (quantity sum) and blast radius (decks / showcases /
    shared showcases). The Confirm form posts the SAME params to
    ``/collection/delete-matching``, which recomputes the set — no drift."""
    filter_params = {
        "search": search,
        "colors": colors,
        "types": types,
        "status": status,
        "finishes": finishes,
        "price_min": price_min,
        "price_max": price_max,
        "finish": finish,
        "location_id": location_id,
    }
    placed_ids, pending_excluded = _bulk_filter_placed_ids(
        session,
        current_user.id,
        search=search,
        colors=colors,
        types=types,
        status=status,
        finishes=finishes,
        price_min=price_min,
        price_max=price_max,
        finish=finish,
        location_id=location_id,
    )
    blast = _delete_blast_radius(session, current_user.id, placed_ids)
    return render(
        request,
        "delete_preview.html",
        {
            "title": "Delete matching cards",
            "current_user": current_user,
            "filter_params": filter_params,
            "row_count": len(placed_ids),
            "card_count": blast["card_count"],
            "rows_in_decks": blast["rows_in_decks"],
            "rows_in_showcases": blast["rows_in_showcases"],
            "rows_in_shared_showcases": blast["rows_in_shared_showcases"],
            "pending_excluded": pending_excluded,
            "is_unfiltered": _is_unfiltered(filter_params),
        },
    )


@router.post("/collection/delete-matching")
def collection_delete_matching(
    search: str = Form(""),
    colors: str = Form(""),
    types: str = Form(""),
    status: str = Form(""),
    finishes: str = Form(""),
    price_min: str = Form(""),
    price_max: str = Form(""),
    finish: str = Form(""),
    location_id: int = Form(0),
    confirm_text: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Commit the filter-scoped delete: recompute the placed set (single source of
    truth) and hard-delete via ``bulk_delete_inventory_rows`` (the FK-safe primitive
    — cleans showcase/trade references). A whole-collection (unfiltered) delete
    additionally requires the typed confirmation ``DELETE``. ``resort_collection``
    is NOT called (nothing to re-file — the rows are gone).

    Recompute-on-confirm is deliberate: the delete acts on whatever matches the
    filter *now*, not on a count snapshotted by the preview — so if the matching set
    shifted between preview and confirm, the result banner reflects the ACTUAL rows
    deleted, never a stale preview number. (Same single-source-of-truth contract the
    cull's preview/confirm follows.)"""
    filter_params = {
        "search": search,
        "colors": colors,
        "types": types,
        "status": status,
        "finishes": finishes,
        "price_min": price_min,
        "price_max": price_max,
        "finish": finish,
        "location_id": location_id,
    }
    if _is_unfiltered(filter_params) and confirm_text.strip() != "DELETE":
        return _collection_filter_redirect(
            filter_params, {"bulk": "error", "reason": "confirm_required"}
        )

    placed_ids, pending_excluded = _bulk_filter_placed_ids(
        session,
        current_user.id,
        search=search,
        colors=colors,
        types=types,
        status=status,
        finishes=finishes,
        price_min=price_min,
        price_max=price_max,
        finish=finish,
        location_id=location_id,
    )
    card_count = _delete_blast_radius(session, current_user.id, placed_ids)["card_count"]
    deleted = bulk_delete_inventory_rows(session, row_ids=placed_ids, user_id=current_user.id)
    return _collection_filter_redirect(
        filter_params,
        {
            "bulk": "deleted",
            "deleted_rows": deleted,
            "deleted_cards": card_count,
            "pending": pending_excluded,
        },
    )


# -----------------------------------------------------------------------------
# Pending placement
# -----------------------------------------------------------------------------


@router.get("/pending")
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

    # v3.28.7 — batch grouping for non-drawer-sorter users. Drawer-sorter users
    # keep their drawer-grouping (`grouped_drawers`) because that mirrors their
    # physical filing workflow; non-sorter users see batches by source/date
    # (Helvault / Moxfield / Manual) instead — matches the design package's
    # narrative batch headers. Both paths share the same per-item dict so the
    # confirm/remove form contracts are identical.
    grouped_batches = []
    if not use_drawer_sorter:
        grouped_batches = build_pending_batch_groups(
            session, user_id=current_user.id, items=view_model["items"]
        )

    return render(
        request,
        "pending.html",
        {
            "title": "Pending Placement",
            **view_model,
            "grouped_batches": grouped_batches,
            "latest_batch_id": latest_batch.id if latest_batch else None,
            "current_user": current_user,
            "use_drawer_sorter": use_drawer_sorter,
            "locations": locations,
        },
    )


def _pending_stat_oob_response(session: Session, user_id: int) -> HTMLResponse:
    """Build the HTMX response for pending row mutations.

    Body contains only out-of-band swap fragments that update the
    Pending-page stat counters (pending count, drawer count, total
    copies). The row itself is deleted client-side by ``hx-swap="delete"``
    on the originating form, so there's no main content in the response.

    Keeps the user's scroll position intact when confirming or removing
    one row at a time — the v3.16.23 fix for the "Confirm scrolls me back
    to the top" complaint.
    """
    rows = list_pending_rows(session, user_id=user_id)
    view_model = build_pending_view_model(rows)
    pending_count = view_model.get("pending_count", 0)
    drawer_count = view_model.get("drawer_count", 0)
    total_copies = view_model.get("total_copies", 0)
    body = (
        f'<div id="pending-stat-count" hx-swap-oob="true">{pending_count}</div>'
        f'<div id="pending-stat-drawers" hx-swap-oob="true">{drawer_count}</div>'
        f'<div id="pending-stat-copies" hx-swap-oob="true">{total_copies}</div>'
    )
    return HTMLResponse(body)


@router.post("/pending/confirm")
async def pending_confirm(
    request: Request,
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
    if request.headers.get("HX-Request"):
        return _pending_stat_oob_response(session, current_user.id)
    return RedirectResponse(url="/pending", status_code=303)


@router.post("/pending/confirm-all")
async def pending_confirm_all(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if current_user.username in DRAWER_SORTER_USERNAMES:
        confirm_all_pending(session, user_id=current_user.id)
    return RedirectResponse(url="/pending", status_code=303)


@router.post("/pending/{row_id}/remove")
def remove_pending_row(
    request: Request,
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

    if request.headers.get("HX-Request"):
        return _pending_stat_oob_response(session, current_user.id)
    return RedirectResponse(url="/pending", status_code=303)


# -----------------------------------------------------------------------------
# Storage Locations
# -----------------------------------------------------------------------------


@router.get("/locations")
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


@router.post("/locations")
def create_location_route(
    name: str = Form(...),
    type: str = Form("other"),
    parent_id: int | None = Form(None),
    sort_order: int = Form(0),
    mode: str = Form("managed"),
    note: str = Form(""),
    capacity: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if parent_id == 0:
        parent_id = None

    # v3.28.6 — note + capacity additive fields. Capacity arrives as a string
    # because the form input is optional; empty / non-integer → None.
    note_value = note.strip() or None
    capacity_value: int | None = None
    if capacity and capacity.strip():
        try:
            parsed = int(capacity.strip())
            if parsed > 0:
                capacity_value = parsed
        except ValueError:
            capacity_value = None

    create_location(
        session,
        user_id=current_user.id,
        name=name,
        type=type,
        parent_id=parent_id,
        sort_order=sort_order,
        mode=mode,
        note=note_value,
        capacity=capacity_value,
    )
    return RedirectResponse("/locations", status_code=303)


@router.post("/locations/create-deck")
def create_deck_from_locations(
    name: str = Form(...),
    format_name: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    create_deck(session, user_id=current_user.id, name=name, format_name=format_name)
    return RedirectResponse("/locations", status_code=303)


@router.post("/decks/create-inline")
def decks_create_inline(
    name: str = Form(...),
    format_name: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """JSON variant of /decks/create for inline use in the import wizard."""
    try:
        deck = create_deck(session, user_id=current_user.id, name=name, format_name=format_name)
    except (ValueError, IntegrityError) as exc:
        session.rollback()
        return JSONResponse({"error": str(exc) or "could_not_create"}, status_code=400)
    return JSONResponse(
        {
            "id": deck.id,
            "storage_location_id": deck.storage_location_id,
            "name": deck.name,
            "format": deck.format or "",
        }
    )


@router.post("/locations/create-inline")
def locations_create_inline(
    name: str = Form(...),
    type: str = Form("other"),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """JSON variant of POST /locations for inline use in the import wizard.

    Deck-type locations are blocked here — callers should hit
    /decks/create-inline instead so a proper Deck record is created.
    """
    if type.strip().lower() == "deck":
        return JSONResponse({"error": "use /decks/create-inline for decks"}, status_code=400)
    try:
        location = create_location(session, user_id=current_user.id, name=name, type=type)
    except (ValueError, IntegrityError) as exc:
        session.rollback()
        return JSONResponse({"error": str(exc) or "could_not_create"}, status_code=400)
    return JSONResponse(
        {
            "id": location.id,
            "name": location.name,
            "type": location.type,
        }
    )


@router.post("/locations/{location_id}/delete")
def delete_location_route(
    location_id: int,
    destination_id: int | None = Form(None),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_location(
        session,
        location_id=location_id,
        user_id=current_user.id,
        destination_id=destination_id,
    )
    return RedirectResponse("/locations", status_code=303)


@router.post("/locations/{location_id}/edit")
def edit_location_route(
    location_id: int,
    name: str = Form(...),
    type: str = Form("other"),
    parent_id: int | None = Form(None),
    sort_order: int = Form(0),
    mode: str | None = Form(None),
    note: str = Form(""),
    capacity: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if parent_id == 0:
        parent_id = None

    # v3.28.6 — capacity parses from string for the same reason as the create
    # route (form value is optional). The edit popout always includes both
    # `note` and `capacity` fields, so update_note=True / update_capacity=True
    # are passed unconditionally — the service layer treats empty string /
    # zero / non-integer as "clear the stored value."
    note_value = note
    capacity_value: int | None = None
    if capacity and capacity.strip():
        try:
            parsed = int(capacity.strip())
            if parsed > 0:
                capacity_value = parsed
        except ValueError:
            capacity_value = None

    try:
        update_location(
            session,
            location_id=location_id,
            user_id=current_user.id,
            name=name,
            type=type,
            parent_id=parent_id,
            sort_order=sort_order,
            mode=mode,
            note=note_value,
            capacity=capacity_value,
            update_note=True,
            update_capacity=True,
        )
    except ValueError:
        pass
    return RedirectResponse("/locations", status_code=303)


@router.post("/locations/{location_id}/bulk-move")
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
                session,
                row_id=row_id,
                user_id=current_user.id,
                location_id=target_location_id,
            )
        except ValueError:
            pass
    return RedirectResponse(f"/locations/{location_id}", status_code=303)


def _build_bulk_delete_items(session: Session, row_ids: list[int], user_id: int) -> list[dict]:
    """Build the dict shape that ``inventory_card`` expects, owned-filtered.

    Mirrors the per-row dict construction used by ``location_detail_page``
    / deck routes so the bulk-delete confirmation page can render the
    same ``inventory_card`` macro without per-row ORM-attribute drift.
    """
    rows = (
        session.query(InventoryRow)
        .filter(InventoryRow.id.in_(row_ids), InventoryRow.user_id == user_id)
        .all()
    )
    items = []
    for row in rows:
        price = effective_price(row.card, row.finish) or 0.0
        items.append(
            {
                "id": row.id,
                "card": row.card,
                "finish": row.finish,
                "language": row.language or "en",
                "is_proxy": bool(row.is_proxy),
                "quantity": row.quantity,
                "slot": row.slot,
                "effective_price": price,
                "total_value": price * row.quantity,
                "is_pending": row.is_pending,
                "storage_location_id": row.storage_location_id,
            }
        )
    return items


@router.post("/locations/{location_id}/bulk-delete-preview")
def bulk_delete_location_preview(
    request: Request,
    location_id: int,
    row_ids: list[int] = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    location = get_location(session, location_id=location_id, user_id=current_user.id)
    if location is None:
        return RedirectResponse("/locations", status_code=303)

    items = _build_bulk_delete_items(session, row_ids, current_user.id)
    return render(
        request,
        "bulk_delete_confirm.html",
        {
            "title": f"Confirm Delete — {location.name}",
            "current_user": current_user,
            "items": items,
            "source_kind": "location",
            "source_id": location.id,
            "source_name": location.name,
            "back_url": f"/locations/{location.id}",
            "commit_url": f"/locations/{location.id}/bulk-delete-commit",
        },
    )


@router.post("/locations/{location_id}/bulk-delete-commit")
def bulk_delete_location_commit(
    location_id: int,
    row_ids: list[int] = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    bulk_delete_inventory_rows(session, row_ids=row_ids, user_id=current_user.id)
    return RedirectResponse(f"/locations/{location_id}", status_code=303)


def _build_location_items(
    session: Session,
    location_id: int,
    user_id: int,
    *,
    search: str = "",
    sort: str = "slot",
    direction: str = "asc",
) -> tuple[list[dict], float, int]:
    """Build the (items, total_value, total_quantity) for a location's card
    grid. Shared by ``location_detail_page`` and the quick-add route so the
    full page and the HTMX partial stay byte-identical."""
    loc_query = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
        .join(Card)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id == location_id,
        )
    )
    if search.strip():
        loc_query = apply_collection_search_filters(loc_query, search)

    # v3.36.11 — shared SORT control. The location grid fetches all rows (no
    # pagination), so it sorts in Python via the shared spec (sort_inventory_rows)
    # — uniform with Decks and reaching the computed Price/Color too. Default
    # "slot" preserves the prior order; unknown keys fall back to name (the
    # sorter's tiebreaker default).
    rows = sort_spec.sort_inventory_rows(loc_query.all(), sort or "slot", direction)

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
                "language": row.language or "en",
                "is_proxy": bool(row.is_proxy),
                "quantity": row.quantity,
                "slot": row.slot,
                "effective_price": price,
                "total_value": row_total,
                "is_pending": row.is_pending,
                "storage_location_id": row.storage_location_id,
            }
        )
    return items, total_value, total_quantity


@router.get("/locations/{location_id}")
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

    items, total_value, total_quantity = _build_location_items(
        session,
        location_id,
        current_user.id,
        search=search,
        sort=sort,
        direction=direction,
    )

    all_locations = list_locations(session, user_id=current_user.id)
    decks = list_decks_basic(session, user_id=current_user.id)
    # v3.31.0 — Showcase picker for the inventory_card Add-to-Showcase
    # action (multi-showcase).
    from app import share_service

    showcases = share_service.list_showcases(session, current_user.id)

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
            "sort_options": sort_spec.LOCATION_SORT_OPTIONS,
            "current_user": current_user,
            "locations": all_locations,
            "decks": decks,
            "showcases": showcases,
        },
    )


@router.post("/locations/{location_id}/add-card")
def location_add_card(
    request: Request,
    location_id: int,
    scryfall_id: str = Form(...),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    language: str = Form("en"),
    is_proxy: bool = Form(False),
    notes: str = Form(""),
    dest_location_id: int | None = Form(None),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Quick-add a single card into a StorageLocation (v3.32.x).

    Acquisition flow — places the card directly at the destination (does NOT
    run the deck "move-if-owned" reconciliation that ``/decks/{id}/add-card``
    does). The optional ``dest_location_id`` lets the modal target a different
    location than the page it was opened on; defaults to ``location_id``.
    """
    if not scryfall_id.strip():
        raise HTTPException(status_code=400, detail="No card selected")

    target_id = dest_location_id or location_id
    row = add_card_to_location(
        session,
        user_id=current_user.id,
        location_id=target_id,
        scryfall_id=scryfall_id.strip(),
        finish=finish,
        quantity=quantity,
        language=language,
        is_proxy=is_proxy,
        notes=notes,
    )
    if row is None:
        # Foreign/unknown location, or the card couldn't be resolved.
        raise HTTPException(status_code=404, detail="Could not add card")

    # The grid the modal lives on is for ``location_id``; render that list.
    if request.headers.get("HX-Request"):
        items, total_value, total_quantity = _build_location_items(
            session, location_id, current_user.id
        )
        location = get_location(session, location_id=location_id, user_id=current_user.id)
        from app import share_service

        response = render(
            request,
            "_location_card_list.html",
            {
                "location": location,
                "items": items,
                "total_value": total_value,
                "total_quantity": total_quantity,
                "current_user": current_user,
                "locations": list_locations(session, user_id=current_user.id),
                "decks": list_decks_basic(session, user_id=current_user.id),
                "showcases": share_service.list_showcases(session, current_user.id),
                "oob_stats": True,
            },
        )
        response.headers["HX-Push-Url"] = f"/locations/{location_id}"
        return response

    return RedirectResponse(url=f"/locations/{location_id}", status_code=303)


@router.get("/locations/{location_id}/export")
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
    # v3.30.16 — expanded schema matches /collection/export. Location +
    # Location Type are constants per row here (the source location is
    # fixed for this endpoint); Language / Role / Tags / Is Proxy are
    # per-row.
    writer.writerow(
        [
            "Name",
            "Set",
            "Collector Number",
            "Finish",
            "Quantity",
            "Location",
            "Location Type",
            "Language",
            "Role",
            "Tags",
            "Is Proxy",
        ]
    )
    for row in rows:
        card = row.card
        writer.writerow(
            [
                card.name or "",
                (card.set_code or "").upper(),
                card.collector_number or "",
                row.finish or "normal",
                row.quantity,
                _csv_formula_safe(location.name),
                location.type or "",
                row.language or "en",
                row.role or "",
                _csv_formula_safe(row.tags or ""),
                "true" if row.is_proxy else "false",
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
# Audit / import undo
# -----------------------------------------------------------------------------


@router.get("/audit")
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


@router.post("/imports/undo-last")
async def imports_undo_last(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    undo_last_import(session, user_id=current_user.id)
    return RedirectResponse(url="/audit", status_code=303)


@router.post("/imports/undo-batch")
async def imports_undo_batch(
    batch_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    undo_last_batch(session, batch_id=batch_id, user_id=current_user.id)
    return RedirectResponse(url="/pending", status_code=303)
