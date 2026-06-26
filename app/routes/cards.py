"""Card / token / set routes (v4 reorg extraction).

Card detail + price refresh, the Tokens surface (CRUD, bulk-add, autocomplete /
lookup / search APIs), the deck-scoped token-requirement routes, and the Sets
index + per-set detail. The /tokens/{scryfall_id} catch-all is registered AFTER
the literal /tokens/* routes — FastAPI matches in registration order, so that
relative order (preserved by this contiguous extraction) must not change.

Behaviour is byte-identical to the pre-extraction handlers in main.py — this
move changes wiring only, not logic.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.deck_service import get_deck
from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
    safe_redirect_url,
)
from app.inventory_service import get_location_label, list_owned_sets
from app.jobs.price_ingest import set_price_override
from app.location_service import list_locations
from app.models import Card, CardPrice, Deck, InventoryRow, TransactionLog, User
from app.pricing import effective_price
from app.scryfall import (
    autocomplete_token_names,
    fetch_card_by_scryfall_id,
    fetch_token_by_set_number,
    refresh_card_from_scryfall,
    search_tokens_by_name,
)
from app.set_service import get_set_completion
from app.token_service import (
    add_deck_token_requirement,
    create_token,
    deck_requirement_exists_for_name,
    delete_deck_token_requirement,
    delete_token,
    list_token_subtypes,
    list_tokens,
    parse_bulk_token_lines,
    resolve_token_inventory_id_by_name,
    total_token_count,
    update_token,
)

router = APIRouter()


@router.get("/test-scryfall/{scryfall_id}")
def test_scryfall(
    scryfall_id: str,
    current_user: User = Depends(get_current_user),
):
    card = fetch_card_by_scryfall_id(scryfall_id)
    return {"card": card}


@router.get("/cards/{card_id}")
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

    # v3.27.12 — surface watchlist state to the card detail template.
    # One call returns both identity-mode watch row ids (or None) so the
    # template can render "Watch" or "Stop watching" affordances for
    # both the printing-specific and printing-agnostic paths.
    from app.watchlist_service import get_watch_ids_for_card

    watch_ids = get_watch_ids_for_card(session, current_user.id, target_card.id, target_card.name)

    # v3.28.7 — Folio Card Detail § panel data. Five panels, all backed by
    # existing schema (no migrations). P/T + flavor text scoped out per
    # the v3.28.7 owner decision (Card model has no power/toughness/flavor
    # columns; Card schema additions deferred to a follow-up patch).
    #
    # § I Printings — all printings of this card NAME, with per-printing
    #   owned counts (across the user's InventoryRows) + watch markers.
    # § II In Decks — which of the user's decks contain this card
    #   (InventoryRow.storage_location_id JOIN Deck.storage_location_id).
    # § III Prices — static USD per finish. 1d/7d/30d deltas DEFERRED.
    # § IV Legality — parsed from Card.legalities JSON.
    # § V History — TransactionLog filtered by user + card_id, top 10.

    # § I — all printings of this name. Cheap: name index covers it.
    printings_query = (
        session.query(Card)
        .filter(Card.name == target_card.name)
        .order_by(Card.set_code.asc(), Card.collector_number.asc())
        .all()
    )
    # Per-printing owned counts (single GROUP BY across the user's rows).
    owned_by_card_id: dict[int, int] = (
        dict(
            session.query(
                InventoryRow.card_id,
                func.sum(InventoryRow.quantity),
            )
            .filter(
                InventoryRow.user_id == current_user.id,
                InventoryRow.card_id.in_([p.id for p in printings_query]),
            )
            .group_by(InventoryRow.card_id)
            .all()
        )
        if printings_query
        else {}
    )
    # Per-printing watchlist membership (one batch query against WatchlistItem).
    from app.models import WatchlistItem

    watched_card_ids: set[int] = set()
    if printings_query:
        for (cid,) in (
            session.query(WatchlistItem.card_id)
            .filter(
                WatchlistItem.user_id == current_user.id,
                WatchlistItem.card_id.in_([p.id for p in printings_query]),
            )
            .all()
        ):
            if cid is not None:
                watched_card_ids.add(cid)
    printings = [
        {
            "card": p,
            "owned": int(owned_by_card_id.get(p.id, 0) or 0),
            "is_watched": p.id in watched_card_ids,
            "is_current": p.id == target_card.id,
        }
        for p in printings_query
    ]

    # § II — In Decks. JOIN InventoryRow → Deck via storage_location_id; one
    # query, GROUP BY deck so each deck shows once with the total quantity.
    in_decks_rows = (
        session.query(
            Deck.id.label("deck_id"),
            Deck.name.label("deck_name"),
            func.sum(InventoryRow.quantity).label("quantity"),
            func.max(InventoryRow.role).label("role"),
        )
        .join(InventoryRow, InventoryRow.storage_location_id == Deck.storage_location_id)
        .filter(
            Deck.user_id == current_user.id,
            InventoryRow.user_id == current_user.id,
            InventoryRow.card_id == target_card.id,
        )
        .group_by(Deck.id, Deck.name)
        .order_by(Deck.name.asc())
        .all()
    )
    in_decks = [
        {
            "deck_id": r.deck_id,
            "deck_name": r.deck_name,
            "quantity": int(r.quantity or 0),
            "role": r.role,
        }
        for r in in_decks_rows
    ]

    # § III — Prices. The displayed value is the MTGJSON-resolved price the
    # ingest denormalized onto Card.price_usd*; ``overrides`` carries the manual
    # per-finish override (if any) so the form pre-fills and the badge shows.
    prices = {
        "regular": target_card.price_usd,
        "foil": target_card.price_usd_foil,
        "etched": target_card.price_usd_etched,
    }
    overrides = {
        row.finish: row.manual_override
        for row in session.query(CardPrice).filter(CardPrice.scryfall_id == target_card.scryfall_id)
        if row.manual_override
    }

    # § IV — Legality. Card.legalities is a JSON-encoded dict from Scryfall;
    # parse-fail returns an empty dict so the template renders cleanly.
    import json as _json

    legality_map: dict[str, str] = {}
    if target_card.legalities:
        try:
            parsed = _json.loads(target_card.legalities) or {}
            # Restrict to formats the player cares about — full list is
            # noisy.
            shown = ["standard", "modern", "pioneer", "commander", "legacy", "vintage", "pauper"]
            legality_map = {fmt.capitalize(): parsed.get(fmt, "not_legal") for fmt in shown}
        except (ValueError, TypeError):
            legality_map = {}

    # § V — History. TransactionLog filtered by this card; top 10 most recent.
    history_rows = (
        session.query(TransactionLog)
        .filter(
            TransactionLog.user_id == current_user.id,
            TransactionLog.card_id == target_card.id,
        )
        .order_by(TransactionLog.created_at.desc())
        .limit(10)
        .all()
    )

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
            "watch_printing_id": watch_ids["printing_id"],
            "watch_name_id": watch_ids["name_id"],
            # v3.28.7 — § panel data
            "printings": printings,
            "in_decks": in_decks,
            "prices": prices,
            "price_overrides": overrides,
            "legality_map": legality_map,
            "history_rows": history_rows,
        },
    )


@router.post("/cards/{card_id}/price-override")
def card_price_override(
    card_id: int,
    finish: str = Form("normal"),
    value: str = Form(""),
    _csrf: None = CsrfRequired,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Set or clear the manual per-printing+finish price override. A blank value
    clears it. The override wins over every MTGJSON provider and survives
    re-ingest (see app.jobs.price_ingest.set_price_override)."""
    target_card = session.query(Card).filter(Card.id == card_id).first()
    if target_card is None:
        return RedirectResponse(url="/collection", status_code=303)
    set_price_override(session, target_card.scryfall_id, finish, value)
    return RedirectResponse(url=f"/cards/{card_id}", status_code=303)


@router.get("/tokens")
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


@router.get("/tokens/new")
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


@router.post("/tokens/create")
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


@router.post("/tokens/{token_id}/edit")
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


@router.post("/tokens/{token_id}/delete")
def tokens_delete(
    token_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_token(session, token_id, current_user.id)
    return RedirectResponse(url="/tokens", status_code=303)


@router.post("/decks/{deck_id}/tokens/add")
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


@router.post("/decks/{deck_id}/tokens/auto-add")
def decks_token_requirement_auto_add(
    deck_id: int,
    token_name: str = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """v3.30.9 — one-click "Track" from a suggested-token row.

    Consumes an auto-detected token name (sourced read-only from the
    per-deck panels cache by the deck-detail render) and inserts a
    DeckTokenRequirement via the existing ``add_deck_token_requirement``
    service function. The TokenInventory link is resolved by
    case-insensitive name match against the user's catalogue — matched
    rows get a linked requirement, unmatched names become loose
    name-only requirements (first-class case per v3.30.8). Server-side
    idempotency: ``deck_requirement_exists_for_name`` short-circuits
    duplicate submissions (stale page, double-click) to a no-op.
    Ownership-gated and CSRF-protected exactly like the manual
    ``/decks/{deck_id}/tokens/add`` route alongside it. Does NOT touch
    the panels cache, does NOT call Scryfall.
    """
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        return RedirectResponse(url="/decks", status_code=303)
    name = (token_name or "").strip()
    if not name:
        return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)
    if deck_requirement_exists_for_name(session, deck.id, name):
        return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)
    link_id = resolve_token_inventory_id_by_name(session, current_user.id, name)
    try:
        add_deck_token_requirement(
            session,
            deck_id=deck.id,
            token_name=name,
            quantity_needed=1,
            token_inventory_id=link_id,
        )
    except ValueError:
        pass
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.get("/tokens/bulk-add")
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


@router.post("/tokens/bulk-add")
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

    parsed = parse_bulk_token_lines(pairs)
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

        qty = max(1, entry["quantity"] if entry["quantity"] else default_qty)

        # DFC path — also fetch the back face
        if entry["is_dfc"]:
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
                    quantity=qty,
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
                        "front_id": f"{(front.get('set_code') or '').upper()}#{front.get('collector_number')}",
                        "back_name": back["name"],
                        "back_id": f"{(back.get('set_code') or '').upper()}#{back.get('collector_number')}",
                        "quantity": token.quantity,
                        "is_dfc": True,
                    }
                )
            except ValueError as exc:
                errors.append({"raw": entry["raw"], "error": str(exc)})
        else:
            # Single-sided path
            try:
                token = create_token(
                    session,
                    user_id=current_user.id,
                    name=front["name"] or "",
                    quantity=qty,
                    subtype=front.get("subtype"),
                    type_line=front.get("type_line"),
                    storage_location_id=storage_id,
                    image_url=front.get("image_url"),
                    is_double_sided=False,
                    set_code=front.get("set_code"),
                    collector_number=front.get("collector_number"),
                    scryfall_id=front.get("scryfall_id"),
                )
                rows_created.append(
                    {
                        "id": token.id,
                        "front_name": front["name"],
                        "front_id": f"{(front.get('set_code') or '').upper()}#{front.get('collector_number')}",
                        "back_name": None,
                        "back_id": None,
                        "quantity": token.quantity,
                        "is_dfc": False,
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


@router.get("/tokens/api/autocomplete")
def tokens_api_autocomplete(
    q: str = "",
    current_user: User = Depends(get_current_user),
):
    """Live autocomplete for the new-token form. Calls Scryfall search with
    `is:token` so non-token cards don't pollute results."""
    if len(q.strip()) < 2:
        return JSONResponse([])
    return JSONResponse(autocomplete_token_names(q, limit=10))


@router.get("/tokens/api/lookup")
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


@router.get("/tokens/api/search")
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


@router.post("/decks/{deck_id}/tokens/{req_id}/delete")
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
@router.get("/tokens/{scryfall_id}")
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


@router.post("/cards/refresh")
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


@router.get("/sets")
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


@router.get("/sets/{set_code}")
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
