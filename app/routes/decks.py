"""Deck management routes (extracted from main.py during the v4 reorg).

Covers the deck index, deck detail, card add/move/printing/tag operations,
intent/retag, pull/return, export, the cards-partial + panels HTMX fragments,
and the deck-scoped panels-cache helpers (whose read side the goldfish route
imports from here).

Behaviour is byte-identical to the pre-extraction handlers in main.py — this
move changes wiring only, not logic.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app import sort_spec
from app.db import DATA_DIR
from app.deck_service import (
    CARD_ROLE_TAGS,
    DECK_GROUP_BY_OPTIONS,
    DECK_VIEW_MODES,
    add_auto_tags,
    assign_deck_variant_group,
    bump_deck_row_quantity,
    compute_consistency,
    compute_dead_cards,
    compute_deck_analytics,
    compute_deck_game_stats,
    compute_deck_health,
    compute_deck_synergy,
    compute_deck_tokens,
    create_deck,
    create_variant_group,
    delete_deck,
    extract_commander_themes,
    find_inventory_matches_for_deck_import,
    get_card_legality,
    get_deck,
    get_row_tag_details,
    get_row_tags,
    group_deck_items,
    list_decks,
    list_user_printings_for_card,
    list_variant_groups,
    pull_card_to_deck,
    return_card_from_deck,
    set_row_tags,
    suggest_card_roles,
    suggest_card_roles_with_confidence,
    switch_deck_row_printing,
    update_deck,
)
from app.dependencies import (
    DRAWER_SORTER_USERNAMES,
    CsrfRequired,
    get_current_user,
    get_db_session,
    render,
    safe_redirect_url,
)
from app.game_service import get_deck_record
from app.import_service import normalize_finish
from app.inventory_service import (
    apply_collection_search_filters,
    bulk_delete_inventory_rows,
    get_location_label,
    list_inventory_rows,
    move_inventory_row_to_location,
    resort_collection,
)
from app.location_service import list_locations
from app.models import Card, Deck, InventoryRow, User
from app.pricing import effective_price
from app.scryfall import autocomplete_cards_for_add, fetch_card_printings
from app.token_service import deck_token_status, list_tokens

router = APIRouter()


@router.get("/decks")
def decks_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    decks = list_decks(session, user_id=current_user.id)
    show_onboarding = len(decks) == 0

    # v3.28.7 — editorial-row Decks page. Attach per-deck game stats
    # (games / wins / win_rate / last_played) via the v3.28.5 batched
    # aggregate pattern. Single GROUP BY across all decks, dict lookup
    # per template iteration — no N+1.
    game_stats = compute_deck_game_stats(
        session, user_id=current_user.id, deck_ids=[d.id for d in decks]
    )
    for deck in decks:
        stats = game_stats.get(deck.id, {})
        deck.games = stats.get("games", 0)
        deck.wins = stats.get("wins", 0)
        deck.losses = stats.get("losses", 0)
        deck.win_rate = stats.get("win_rate", 0.0)
        deck.last_played = stats.get("last_played")

    # Featured deck = most active by game count (ties broken by name).
    # Editorial-row layout: featured renders with full panel; rest as
    # compact rows. None when zero decks have games (featured slot
    # collapses to the first deck by name).
    featured = None
    if decks:
        with_games = [d for d in decks if d.games > 0]
        featured = max(with_games, key=lambda d: d.games) if with_games else decks[0]

    return render(
        request,
        "decks.html",
        {
            "title": "Decks",
            "decks": decks,
            "featured": featured,
            "current_user": current_user,
            "show_onboarding": show_onboarding,
            # v3.33.0 — variant-group picker options for the deck-edit popouts.
            "variant_groups": list_variant_groups(session, current_user.id),
        },
    )


@router.post("/decks/create")
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

_PANELS_CACHE_VERSION = 3
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


def _build_review_tag_items(rows: list) -> list[dict]:
    """Build the deck-detail review-tags panel data (v3.23.3).

    Surfaces rows that carry at least one auto/medium tag. These are the
    auto-tagger's heuristic suggestions (currently always Synergy via
    `card_matches_theme` — intrinsic role tags land at auto/certain after
    v3.23.2 so they don't appear here). The user reviews each, then either
    confirms (promoting to user/high) or removes (deleting that tag from
    the row's tag list).

    Each item carries the row id and a list of `{tag, all_other_tags}`
    entries — the partial template uses these to render per-tag chip
    actions plus a row-level "Confirm row" shortcut.

    Commander rows are excluded — their tags don't drive Synergy/Health
    classification.
    """
    from app.deck_service import get_row_tag_details

    out: list[dict] = []
    for row in rows:
        if row.role == "commander":
            continue
        if not row.card:
            continue
        details = get_row_tag_details(row)
        review_tags = [
            d["tag"]
            for d in details
            if d.get("source") == "auto" and d.get("confidence") == "medium"
        ]
        if not review_tags:
            continue
        confirmed_tags = [d["tag"] for d in details if d["tag"] not in review_tags]
        out.append(
            {
                "row_id": row.id,
                "card_id": row.card.id,
                "card_name": row.card.name or "Unknown",
                "review_tags": sorted(review_tags),
                "confirmed_tags": sorted(confirmed_tags),
            }
        )
    out.sort(key=lambda item: item["card_name"].lower())
    return out


def _build_deck_card_items(
    session: Session,
    deck: Deck,
    user_id: int,
    search: str,
    sort: str,
    direction: str,
) -> tuple[list[dict], float, int]:
    """Filter + sort + materialize the deck-card item list.

    Shared by `deck_detail_page` (full page render) and `deck_cards_partial`
    (the HTMX-driven search swap on /decks/{id}). Returns (items list, total
    value, total card count). Theme extraction + suggested_tags + tag/legality
    decoration is included so the partial render produces identical card UI
    to the full-page render.

    Does NOT auto-tag untagged rows — that side effect stays in
    `deck_detail_page` so search keystrokes don't write to the DB.
    """
    items: list[dict] = []
    total_value = 0.0
    total_cards = 0

    if not deck or not deck.storage_location_id:
        return items, total_value, total_cards

    commander_rows = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id == deck.storage_location_id,
            InventoryRow.role == "commander",
        )
        .all()
    )
    themes = extract_commander_themes(commander_rows) if commander_rows else None

    deck_query = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .join(Card)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.storage_location_id == deck.storage_location_id,
        )
    )
    if search.strip():
        deck_query = apply_collection_search_filters(deck_query, search)

    # v3.36.11 — shared SORT control. The deck card list is fetched whole (no
    # pagination), so it sorts in Python via the shared spec (sort_inventory_rows)
    # — reaching the computed Price/Color uniformly with the other surfaces.
    # In list view the result is then bucketed by group_deck_items, which
    # preserves this order WITHIN each group (sort acts within groups). Default
    # "name"; unknown keys fall back to name via the sorter's tiebreaker.
    deck_rows = sort_spec.sort_inventory_rows(deck_query.all(), sort or "name", direction)

    for row in deck_rows:
        price = effective_price(row.card, row.finish) or 0.0
        row_total = price * row.quantity
        total_value += row_total
        total_cards += row.quantity
        items.append(
            {
                "id": row.id,
                "card": row.card,
                "finish": row.finish,
                "language": row.language or "en",
                "is_proxy": bool(row.is_proxy),
                "quantity": row.quantity,
                "effective_price": price,
                "total_value": row_total,
                "role": row.role,
                "tags": get_row_tags(row),
                "tag_details": get_row_tag_details(row),
                "suggested_tags": suggest_card_roles(row.card, themes=themes),
                "legality_status": get_card_legality(row.card, deck.format),
            }
        )

    return items, total_value, total_cards


def _untracked_auto_tokens_from_cache(
    deck_id: int,
    all_deck_rows: list,
    tracked_names_lower: set[str],
) -> list[dict]:
    """v3.30.9 — auto-detected tokens NOT yet tracked, read-only from cache.

    Reads the per-deck panels cache the v3.8.9 "Tokens" panel already
    populates (via the lazy ``GET /decks/{deck_id}/panels`` fragment) and
    returns the subset of its token list whose name is not already in
    ``DeckTokenRequirement`` for this deck. Cache miss returns ``[]`` —
    explicit graceful degradation; v3.30.9 MUST NEVER call
    ``fetch_deck_tokens`` on the deck-detail render path (the lazy
    fragment is the only place that's allowed to, and even there it's
    cached). Untouched contract: the cache key is computed from the same
    ``_panels_cache_key`` the fragment uses, so a deck whose contents
    have changed since the cache was written reads as a miss → empty
    suggestion list until the user revisits the deck and the fragment
    refills the cache. Suppresses any tokens whose name (case-insensitive)
    already appears in this deck's DeckTokenRequirement rows.
    """
    if not all_deck_rows:
        return []
    try:
        ck = _panels_cache_key(all_deck_rows)
        cached = _read_panels_cache(deck_id, ck)
    except Exception:
        # Defensive: cache read errors must not break the deck-detail render.
        return []
    if not cached:
        return []
    out: list[dict] = []
    for t in cached.get("tokens") or []:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        if name.lower() in tracked_names_lower:
            continue
        out.append(t)
    return out


@router.get("/decks/{deck_id}")
def deck_detail_page(
    request: Request,
    deck_id: int,
    search: str = "",
    sort: str = "name",
    direction: str = "asc",
    collection_search: str = "",
    health_filter: str = "",
    view: str = "",
    group: str = "",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    # Resolve view mode + group axis: explicit query param wins over the
    # user's persisted preference, which wins over the hardcoded defaults.
    # The query-param path is what the HTMX toggle/group-by controls use to
    # change view without an extra round-trip.
    view_mode = view if view in DECK_VIEW_MODES else (current_user.deck_view_mode or "grid")
    group_by = group if group in DECK_GROUP_BY_OPTIONS else (current_user.deck_group_by or "type")
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
            # v3.23.2: per-pattern confidence — intrinsic role tags emit as
            # auto/certain (unambiguous oracle-text rules), Synergy emits as
            # auto/medium (themes-match heuristic with false-positive risk).
            _suggested = suggest_card_roles_with_confidence(_row.card, themes=_themes)
            if _suggested:
                set_row_tags(_row, _suggested)
                _auto_tagged = True
        if _auto_tagged:
            session.commit()

        items, deck_total_value, total_cards = _build_deck_card_items(
            session, deck, current_user.id, search, sort, direction
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
                    "language": row.language or "en",
                    "is_proxy": bool(row.is_proxy),
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

    # v3.27.9: bracket_v2 estimator no longer runs on the request path. The
    # bracket display rolled up to "untrusted" pending a dedicated analytics
    # rebuild (see roadmap.md Deferred / latent items "Deck Analytics
    # Rebuild"). bracket_v2_service / its tables / its migrations are left
    # dormant for the rebuild to reuse — same pattern as the retired
    # .site-header CSS from v3.27.8. The render-context passthrough below
    # keeps the dormant {% if bracket_v2 %} panel in deck_detail.html as a
    # no-op so the template doesn't need stripping.
    bracket_v2 = None

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

    # v3.30.9 — compute once before the render dict so suggested_tokens
    # can derive from the same token_requirements list without a duplicate
    # service call.
    _token_requirements = deck_token_status(session, deck.id, current_user.id) if deck else []
    # deck_token_status returns list[dict]; access via subscript.
    _tracked_names_lower = {
        (r.get("token_name") or "").strip().lower() for r in _token_requirements
    }
    _suggested_tokens = (
        _untracked_auto_tokens_from_cache(
            deck.id,
            locals().get("all_deck_rows") or [],
            _tracked_names_lower,
        )
        if deck
        else []
    )

    # v3.33.0 — sibling decks in the same variant group (read-only panel).
    variant_siblings = (
        session.query(Deck)
        .filter(
            Deck.variant_group_id == deck.variant_group_id,
            Deck.user_id == current_user.id,
            Deck.id != deck.id,
        )
        .order_by(Deck.name.asc())
        .all()
        if deck and deck.variant_group_id
        else []
    )

    return render(
        request,
        "deck_detail.html",
        {
            "title": deck.name if deck else "Deck",
            "deck": deck,
            "variant_group": deck.variant_group if deck else None,
            "variant_siblings": variant_siblings,
            "color_identity": color_identity,
            "commanders": commanders if deck else [],
            "items": deck_cards if deck else [],
            "deck_total_value": deck_total_value if deck else 0.0,
            "deck_total_cards": total_cards if deck else 0,
            "bracket_v2": bracket_v2,
            "token_requirements": _token_requirements,
            "token_inventory_options": (list_tokens(session, current_user.id) if deck else []),
            # v3.30.9 — auto-detected tokens NOT yet tracked, read from the
            # existing per-deck panels cache (no Scryfall, no fresh compute).
            # The "Tokens Needed" panel surfaces these as one-click "+ Track"
            # suggestions; clicking inserts a DeckTokenRequirement via the
            # auto-add route. ALWAYS computed (works alongside partial /
            # full declared lists too), gated server-side on cache presence.
            "suggested_tokens": _suggested_tokens,
            "search": search,
            "sort": sort,
            "direction": direction,
            "sort_options": sort_spec.DECK_SORT_OPTIONS,
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
            "view_mode": view_mode,
            "group_by": group_by,
            "deck_card_groups": group_deck_items(deck_cards, group_by) if deck else [],
            "review_tag_items": (
                _build_review_tag_items(locals().get("all_deck_rows") or []) if deck else []
            ),
        },
    )


@router.post("/account/deck-view-pref")
async def update_deck_view_pref(
    request: Request,
    view: str = Form(""),
    group: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Persist the user's deck-view preferences (view mode + group-by axis).

    Called by the toggle / group-by controls on the deck detail page. Either
    or both fields can be sent in a single POST; missing fields leave the
    existing preference untouched. Invalid values are ignored.

    Returns 303 to the Referer so the user lands back on whichever deck
    they were viewing.
    """
    changed = False
    if view and view in DECK_VIEW_MODES and current_user.deck_view_mode != view:
        current_user.deck_view_mode = view
        changed = True
    if group and group in DECK_GROUP_BY_OPTIONS and current_user.deck_group_by != group:
        current_user.deck_group_by = group
        changed = True
    if changed:
        session.commit()
    return RedirectResponse(url=safe_redirect_url(request), status_code=303)


def _deck_cards_partial_response(
    request: Request, session: Session, current_user: User, deck_id: int
) -> HTMLResponse:
    """Render the deck-card-list partial for HTMX swap-in.

    Used by mutation routes (switch-printing, bump-qty) that need to
    re-render the deck card display after the underlying row changes.
    Uses the user's persisted view/group prefs (not URL params — those
    only matter on the dedicated cards-partial GET endpoint).

    Caller is responsible for ensuring the deck exists; this helper does
    a defensive re-check anyway so it can be invoked from anywhere.
    """
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    view_mode = current_user.deck_view_mode or "grid"
    group_by = current_user.deck_group_by or "type"
    items, _, _ = _build_deck_card_items(
        session, deck, current_user.id, search="", sort="name", direction="asc"
    )
    use_drawer_sorter = current_user.username in DRAWER_SORTER_USERNAMES
    return render(
        request,
        "_deck_card_list.html",
        {
            "deck": deck,
            "items": items,
            "deck_card_groups": group_deck_items(items, group_by),
            "view_mode": view_mode,
            "group_by": group_by,
            "commanders": [],
            "use_drawer_sorter": use_drawer_sorter,
            "locations": list_locations(session, user_id=current_user.id),
        },
    )


@router.get("/decks/{deck_id}/cards-partial")
def deck_cards_partial(
    deck_id: int,
    request: Request,
    search: str = "",
    sort: str = "name",
    direction: str = "asc",
    view: str = "",
    group: str = "",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """HTMX-driven partial: re-renders ONLY the filtered deck-card display.

    Triggered by the search form and the view/group-by controls on
    /decks/{id} via `hx-get` so the user gets in-place updates without
    losing scroll position or collapsing expanded panels. The full
    deck-detail route remains the no-JS fallback — the form keeps
    `method="get" action="/decks/{id}"` so users without HTMX get the
    original full-page reload behavior.
    """
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    view_mode = view if view in DECK_VIEW_MODES else (current_user.deck_view_mode or "grid")
    group_by = group if group in DECK_GROUP_BY_OPTIONS else (current_user.deck_group_by or "type")

    # Side-effect persistence: if the URL explicitly carries a view/group
    # value that differs from the user's saved preference, write it back.
    # Means the group-by selector in the search form auto-persists on
    # Apply, rather than the user having to find a separate "save" affordance.
    pref_changed = False
    if view in DECK_VIEW_MODES and current_user.deck_view_mode != view:
        current_user.deck_view_mode = view
        pref_changed = True
    if group in DECK_GROUP_BY_OPTIONS and current_user.deck_group_by != group:
        current_user.deck_group_by = group
        pref_changed = True
    if pref_changed:
        session.commit()

    items, _, _ = _build_deck_card_items(session, deck, current_user.id, search, sort, direction)
    use_drawer_sorter = current_user.username in DRAWER_SORTER_USERNAMES
    response = render(
        request,
        "_deck_card_list.html",
        {
            "deck": deck,
            "items": items,
            "deck_card_groups": group_deck_items(items, group_by),
            "view_mode": view_mode,
            "group_by": group_by,
            "commanders": [],  # the partial only re-renders deck cards, not commanders
            "use_drawer_sorter": use_drawer_sorter,
            "locations": list_locations(session, user_id=current_user.id),
        },
    )
    # Tell HTMX to push the full-page URL to the address bar (not the partial
    # endpoint URL) so bookmarks / shares hit the real page on a cold visit.
    # `hx-push-url="true"` on the form would otherwise push /cards-partial?...
    # which only serves a fragment.
    qs_params = {"search": search, "sort": sort, "direction": direction}
    if view in DECK_VIEW_MODES:
        qs_params["view"] = view
    if group in DECK_GROUP_BY_OPTIONS:
        qs_params["group"] = group
    qs = urlencode(qs_params)
    response.headers["HX-Push-Url"] = f"/decks/{deck_id}?{qs}"
    return response


@router.get("/decks/{deck_id}/panels")
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

    # v3.27.9: combos + bracket no longer compute on the panels endpoint.
    # compute_deck_combos issued a Spellbook /find-my-combos POST per deck
    # (request-path network invariant violation surfaced on /decks cold load;
    # see roadmap.md Deferred / latent items "Deck Analytics Rebuild").
    # Synergy + dead-cards still render: synergy reads combos as a dict but
    # tolerates an empty .included gracefully (loses the "Direct via combo
    # membership" classification path, keeps tribal / payoff / engine paths).
    # tokens / synergy / dead_cards are all local computations against the
    # bulk Scryfall cache and stay on the request path. compute_deck_combos
    # / bracket_v2_service are dormant code — left importable for the
    # analytics rebuild to reuse. (The V1 compute_deck_bracket estimator was
    # deleted in the pre-v4 cleanup sprint, 2026-06-09.)
    bracket = None
    synergy = None
    combos: dict = {"included": [], "almost": []}
    tokens: list = []

    if all_deck_rows:
        ck = _panels_cache_key(all_deck_rows)
        cached = _read_panels_cache(deck_id, ck)

        if cached:
            tokens = cached.get("tokens", [])
        else:
            tokens = compute_deck_tokens(all_deck_rows)
            _write_panels_cache(deck_id, ck, {"tokens": tokens, "combos": combos})

        synergy = compute_deck_synergy(all_deck_rows, combos)
        dead_cards = compute_dead_cards(all_deck_rows, synergy)
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


@router.post("/decks/{deck_id}/bulk-move")
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
        # Resort synchronously in the request (same as the import flow,
        # v3.11.18). A background thread races the redirect → /pending and
        # contends with concurrent removals for the SQLite write lock,
        # leaving returned rows unsorted ("Drawer - · Slot ?").
        resort_collection(session, user_id=current_user.id)
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


@router.post("/decks/{deck_id}/bulk-delete-preview")
def bulk_delete_deck_preview(
    request: Request,
    deck_id: int,
    row_ids: list[int] = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    deck = (
        session.query(Deck)
        .filter(Deck.id == deck_id, Deck.user_id == current_user.id)
        .one_or_none()
    )
    if deck is None:
        return RedirectResponse("/decks", status_code=303)

    # Lazy import to avoid a circular import at module load.
    # _build_bulk_delete_items is shared with the location bulk-delete flow.
    from app.routes.collections import _build_bulk_delete_items

    items = _build_bulk_delete_items(session, row_ids, current_user.id)
    return render(
        request,
        "bulk_delete_confirm.html",
        {
            "title": f"Confirm Delete — {deck.name}",
            "current_user": current_user,
            "items": items,
            "source_kind": "deck",
            "source_id": deck.id,
            "source_name": deck.name,
            "back_url": f"/decks/{deck.id}",
            "commit_url": f"/decks/{deck.id}/bulk-delete-commit",
        },
    )


@router.post("/decks/{deck_id}/bulk-delete-commit")
def bulk_delete_deck_commit(
    deck_id: int,
    row_ids: list[int] = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    bulk_delete_inventory_rows(session, row_ids=row_ids, user_id=current_user.id)
    return RedirectResponse(f"/decks/{deck_id}", status_code=303)


@router.post("/decks/{deck_id}/edit")
def decks_edit(
    deck_id: int,
    name: str = Form(...),
    format_name: str = Form(""),
    notes: str = Form(""),
    blurb: str = Form(""),
    variant_group_id: str = Form(""),
    new_variant_group_name: str = Form(""),
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
            blurb=blurb,
            update_blurb=True,
        )
        # v3.33.0 — variant-group assignment (separate from update_deck so its
        # signature + callers stay untouched). Create-by-name wins over the
        # picker; empty picker clears the link.
        if new_variant_group_name.strip():
            group = create_variant_group(session, current_user.id, new_variant_group_name)
            assign_deck_variant_group(session, current_user.id, deck_id, group.id)
        elif variant_group_id.strip():
            assign_deck_variant_group(session, current_user.id, deck_id, int(variant_group_id))
        else:
            assign_deck_variant_group(session, current_user.id, deck_id, None)
    except ValueError:
        pass
    return RedirectResponse(url="/decks", status_code=303)


@router.post("/decks/{deck_id}/delete")
async def decks_delete(
    deck_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_deck(session, deck_id=deck_id, user_id=current_user.id)
    return RedirectResponse(url="/decks", status_code=303)


@router.get("/decks/{deck_id}/export")
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


@router.post("/decks/pull")
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


@router.get("/decks/api/card-autocomplete")
def decks_card_autocomplete(
    q: str = "",
    current_user: User = Depends(get_current_user),
):
    """Lightweight JSON autocomplete for the deck-detail "Add card" panel.

    Returns up to 50 Scryfall printings matching ``q`` (min 2 chars). The
    payload is intentionally slim — just enough for the dropdown to render
    a thumbnail + name + set/collector line and submit the selected
    printing back via the hidden ``scryfall_id`` field on the Add form.
    50 is high enough that popular reprints (Sol Ring, basic lands) cover
    their meaningful printings; the dropdown is scrollable in the panel CSS.
    """
    return JSONResponse(autocomplete_cards_for_add(q, limit=50))


@router.post("/decks/{deck_id}/add-card")
async def decks_add_card(
    request: Request,
    deck_id: int,
    scryfall_id: str = Form(...),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Single-card add to a deck, mirroring the import-flow reconciliation.

    Reuses the same reconciliation pipeline the import flow does — calls
    ``find_inventory_matches_for_deck_import`` to figure out whether the
    user owns the card in non-deck inventory (then prefer moving over
    duplicating) or not (then import a fresh row). The function-provided
    ``recommended_action`` / ``recommended_move_qty`` / ``recommended_new_qty``
    drive ``_commit_deck_import_with_reconciliation`` directly — no UI
    reconciliation panel is shown for a single-card add because the action
    is implicit (move when possible, otherwise import).

    Responds with the HTMX partial when ``HX-Request`` is set so the deck
    card grid updates in place; otherwise 303-redirects to the deck page
    (no-JS / non-HTMX fallback).
    """
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck or not deck.storage_location_id:
        raise HTTPException(status_code=404, detail="Deck not found")

    scryfall_id = scryfall_id.strip()
    if not scryfall_id:
        raise HTTPException(status_code=400, detail="scryfall_id is required")

    quantity = max(1, min(int(quantity), 99))
    finish_normalized = normalize_finish(finish)

    parsed_rows = [
        {
            "line_number": 1,
            "name": "",
            "scryfall_id": scryfall_id,
            "set_code": "",
            "collector_number": "",
            "finish": finish_normalized,
            "quantity": quantity,
            "location": "",
        }
    ]

    matches = find_inventory_matches_for_deck_import(session, current_user.id, deck.id, parsed_rows)
    rc = matches[0]
    action = rc["recommended_action"]

    # Lazy import to avoid a circular import (main.py imports this module).
    # The reconciliation-commit helper lives with the import flow in main.py.
    from app.main import _commit_deck_import_with_reconciliation

    _commit_deck_import_with_reconciliation(
        session=session,
        user_id=current_user.id,
        deck=deck,
        parsed_rows=parsed_rows,
        actions=[action],
        move_qtys=[rc["recommended_move_qty"]],
        new_qtys=[rc["recommended_new_qty"]],
        filename="add-card",
    )

    if request.headers.get("HX-Request"):
        items, _value, _count = _build_deck_card_items(
            session, deck, current_user.id, search="", sort="name", direction="asc"
        )
        use_drawer_sorter = current_user.username in DRAWER_SORTER_USERNAMES
        response = render(
            request,
            "_deck_card_list.html",
            {
                "deck": deck,
                "items": items,
                "commanders": [],
                "use_drawer_sorter": use_drawer_sorter,
                "locations": list_locations(session, user_id=current_user.id),
            },
        )
        response.headers["HX-Push-Url"] = f"/decks/{deck_id}"
        return response

    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.get("/decks/{deck_id}/rows/{row_id}/printings-modal")
def deck_row_printings_modal(
    deck_id: int,
    row_id: int,
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """HTMX fragment: the Switch Printing modal contents.

    Triggered from the deck-detail list/grid view via `hx-get`, swapped
    into a viewport-fixed `#switch-printing-modal` host element. The
    modal lists every printing of the row's card, with the user's owned
    printings surfaced in an "In your collection" section at the top
    (source-of-truth positioning per the roadmap entry).
    """
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck or not deck.storage_location_id:
        raise HTTPException(status_code=404, detail="Deck not found")

    row = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .filter(
            InventoryRow.id == row_id,
            InventoryRow.user_id == current_user.id,
            InventoryRow.storage_location_id == deck.storage_location_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Deck row not found")

    card_name = row.card.name if row.card else ""
    printings = fetch_card_printings(card_name) if card_name else []
    owned_printings = list_user_printings_for_card(session, current_user.id, card_name)
    # Build a lookup of owned (set, collector) → list of finish entries so
    # the template can mark which finishes the user owns of each printing.
    owned_by_key: dict[tuple[str, str], dict[str, int]] = {}
    for entry in owned_printings:
        key = (entry["set_code"], entry["collector_number"])
        owned_by_key.setdefault(key, {})[entry["finish"]] = entry["quantity"]
    # Annotate every printing with owned_finishes so the template renders
    # toggle buttons with owned-count hints (e.g. "Foil (2)").
    for p in printings:
        key = (p["set_code"], p["collector_number"])
        p["owned_finishes"] = owned_by_key.get(key, {})

    return render(
        request,
        "_switch_printing_modal.html",
        {
            "deck": deck,
            "row": row,
            "card_name": card_name,
            "current_set_code": (row.card.set_code or "").lower() if row.card else "",
            "current_collector_number": (row.card.collector_number or "") if row.card else "",
            "current_finish": (row.finish or "normal").lower(),
            "printings": printings,
            "owned_printings": owned_printings,
        },
    )


@router.post("/decks/{deck_id}/rows/{row_id}/switch-printing")
async def deck_row_switch_printing(
    deck_id: int,
    row_id: int,
    request: Request,
    scryfall_id: str = Form(...),
    finish: str = Form("normal"),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Swap the printing on a deck row to a different (set, collector, finish).

    Preserves row.id / quantity / tags / role / notes — only card_id and
    finish change. After the swap, returns the re-rendered deck card list
    partial when HTMX is the caller; otherwise 303s back to the deck page.
    """
    ok = switch_deck_row_printing(
        session,
        user_id=current_user.id,
        deck_id=deck_id,
        row_id=row_id,
        new_scryfall_id=scryfall_id.strip(),
        new_finish=finish,
    )
    if not ok:
        raise HTTPException(status_code=400, detail="Could not switch printing.")

    if request.headers.get("HX-Request"):
        return _deck_cards_partial_response(request, session, current_user, deck_id)
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/{deck_id}/rows/{row_id}/bump-qty")
async def deck_row_bump_qty(
    deck_id: int,
    row_id: int,
    request: Request,
    delta: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Increment / decrement a deck row's quantity by ±1.

    Used by the basic-land +/- controls on the deck-detail page. Quantity
    of 0 deletes the row. Anything other than ±1 is rejected so the
    button can't accidentally page through quantities.
    """
    if delta not in (-1, 1):
        raise HTTPException(status_code=400, detail="delta must be ±1")

    result = bump_deck_row_quantity(
        session,
        user_id=current_user.id,
        deck_id=deck_id,
        row_id=row_id,
        delta=delta,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Deck row not found")

    if request.headers.get("HX-Request"):
        return _deck_cards_partial_response(request, session, current_user, deck_id)
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/{deck_id}/intent")
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


@router.post("/decks/{deck_id}/retag")
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
        # v3.23.2: use structured per-pattern confidence so the Retag pass
        # emits intrinsic role tags as auto/certain and Synergy as
        # auto/medium. add_auto_tags reads per-entry confidence from the
        # dict shape and preserves user-confirmed tags unchanged.
        suggested = suggest_card_roles_with_confidence(row.card, themes=themes)
        if add_auto_tags(row, suggested):
            changed = True

    if changed:
        session.commit()

    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/return")
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
        resort_collection(session, user_id=current_user.id)

    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/rows/{row_id}/toggle-commander")
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

        # v3.27.9: tokens-only cache warm-up. The pre-v3.27.9 path also
        # warmed combos for the bracket_v2 panel; bracket + combos are now
        # off the deck-facing surfaces pending the analytics rebuild (see
        # roadmap.md Deferred / latent items "Deck Analytics Rebuild"), so
        # we only warm the tokens slot the panels endpoint still consumes.
        # Failures are swallowed; the lazy panels endpoint repopulates if
        # this warm-up fails.
        try:
            deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
            if deck and deck.storage_location_id:
                all_rows = (
                    session.query(InventoryRow)
                    .options(joinedload(InventoryRow.card))
                    .join(Card)
                    .filter(
                        InventoryRow.user_id == current_user.id,
                        InventoryRow.storage_location_id == deck.storage_location_id,
                    )
                    .all()
                )
                if all_rows:
                    ck = _panels_cache_key(all_rows)
                    if not _read_panels_cache(deck_id, ck):
                        tokens = compute_deck_tokens(all_rows)
                        _write_panels_cache(
                            deck_id,
                            ck,
                            {"tokens": tokens, "combos": {"included": [], "almost": []}},
                        )
        except Exception as exc:  # noqa: BLE001 — non-critical warm-up
            print(
                f"[toggle_commander] panels cache warm-up failed deck={deck_id}: {exc}",
                flush=True,
            )

    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/rows/{row_id}/tags")
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


@router.post("/decks/{deck_id}/rows/{row_id}/review-tag")
async def review_tag_action(
    request: Request,
    deck_id: int,
    row_id: int,
    action: str = Form(...),
    tag: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Per-row review-tags actions (v3.23.3).

    Three action variants, all single-row scoped (no deck-wide bulk —
    Synergy can over-tag, the user should commit per card):

      - action="confirm" + tag=Name → promote that tag from auto/medium
        to user/high. Other tags on the row unchanged.
      - action="remove" + tag=Name → delete that tag from the row's tag
        list. Other tags unchanged.
      - action="confirm_row" → promote every auto/medium tag on the row
        to user/high in one shot.

    On HX-Request, returns the updated review-tags panel HTML (HTMX
    swaps `#review-tags-panel-content`). Otherwise 303-redirects back
    to /decks/{deck_id}.
    """
    from app.deck_service import get_row_tag_details

    deck = session.query(Deck).filter(Deck.id == deck_id, Deck.user_id == current_user.id).first()
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    row = (
        session.query(InventoryRow)
        .options(joinedload(InventoryRow.card))
        .filter(InventoryRow.id == row_id, InventoryRow.user_id == current_user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")

    details = get_row_tag_details(row)
    changed = False

    if action == "confirm" and tag in CARD_ROLE_TAGS:
        promoted: list[dict] = []
        for d in details:
            if d["tag"] == tag and d.get("source") == "auto":
                promoted.append({"tag": tag, "confidence": "high", "source": "user"})
                changed = True
            else:
                promoted.append(d)
        if changed:
            set_row_tags(row, promoted)
    elif action == "remove" and tag in CARD_ROLE_TAGS:
        kept = [d for d in details if d["tag"] != tag]
        if len(kept) != len(details):
            set_row_tags(row, kept)
            changed = True
    elif action == "confirm_row":
        # Promote every auto/medium tag on the row in one shot.
        promoted = []
        for d in details:
            if d.get("source") == "auto" and d.get("confidence") == "medium":
                promoted.append({"tag": d["tag"], "confidence": "high", "source": "user"})
                changed = True
            else:
                promoted.append(d)
        if changed:
            set_row_tags(row, promoted)

    if changed:
        row.updated_at = datetime.utcnow()
        session.commit()

    # HTMX response: re-render the panel content from fresh deck state.
    if request.headers.get("HX-Request"):
        all_rows = (
            session.query(InventoryRow)
            .options(joinedload(InventoryRow.card))
            .filter(
                InventoryRow.user_id == current_user.id,
                InventoryRow.storage_location_id == deck.storage_location_id,
            )
            .all()
        )
        items = _build_review_tag_items(all_rows)
        return render(
            request,
            "_review_tags_panel_content.html",
            {"deck": deck, "review_tag_items": items},
        )

    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)
