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
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app import deck_service, sort_spec
from app.bracket_v2_service import (
    estimate_bracket_v2,
    gc_list_version,
    load_persisted_estimate,
    persist_estimate,
)
from app.combo_refresh_service import deck_combo_status, load_deck_combos
from app.db import DATA_DIR
from app.deck_service import (
    CARD_ROLE_TAGS,
    DECK_GROUP_BY_OPTIONS,
    DECK_VIEW_MODES,
    add_auto_tags,
    add_card_to_considering,
    assign_deck_variant_group,
    build_public_deck_view,
    bump_deck_row_quantity,
    check_deck_legality,
    compute_consistency,
    compute_dead_cards,
    compute_deck_analytics,
    compute_deck_game_stats,
    compute_deck_health,
    compute_deck_synergy,
    compute_deck_tokens,
    create_deck,
    create_deck_goal,
    create_variant_group,
    deactivate_deck_goal,
    deck_goal_stats,
    delete_deck,
    delete_deck_goal,
    demote_to_considering,
    edit_deck_goal,
    extract_commander_themes,
    find_inventory_matches_for_deck_import,
    generate_deck_share_token,
    get_card_legality,
    get_deck,
    get_deck_by_share_token,
    get_inbound_shares_for_deck,
    get_outbound_shares_for_deck,
    get_row_tag_details,
    get_row_tags,
    group_deck_items,
    grouped_card_search,
    inbound_shared_rows_for_deck,
    list_considering_rows,
    list_decks,
    list_user_printings_for_card,
    list_variant_groups,
    materialize_brew,
    move_deck_goal,
    outbound_share_map,
    own_deck_card_options,
    promote_from_considering,
    pull_card_to_deck,
    remove_from_considering,
    resolve_add_printing,
    resolved_deck_rows,
    return_card_from_deck,
    revoke_deck_share_token,
    set_row_tags,
    share_card_to_deck,
    suggest_card_roles,
    suggest_card_roles_with_confidence,
    switch_deck_row_printing,
    unshare_card_from_deck,
    update_deck,
)
from app.decklist_service import build_brew_buylist
from app.dependencies import (
    CsrfRequired,
    get_current_user,
    get_db_session,
    get_optional_current_user,
    render,
    safe_redirect_url,
)
from app.game_service import deck_commander_cards, get_deck_record
from app.import_service import normalize_finish
from app.inventory_service import (
    apply_collection_search_filters,
    bulk_delete_inventory_rows,
    get_location_label,
    get_or_create_card,
    list_inventory_rows,
    move_inventory_row_to_location,
    resort_collection,
)
from app.location_service import list_locations
from app.models import Card, Deck, InventoryRow, User
from app.pricing import card_metadata, effective_price
from app.scryfall import autocomplete_cards_for_add, fetch_card_printings
from app.sorter_rule_service import has_sortable_setup
from app.timeutil import utc_now
from app.token_service import deck_token_status, list_tokens
from app.watchlist_service import add_names_to_watchlist

router = APIRouter()


@router.get("/decks")
def decks_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    decks = list_decks(session, user_id=current_user.id)
    # #164 placeholders anchor game history but hold no cards and cannot be
    # played. They are split OUT of `decks` here rather than filtered in the
    # template, so everything downstream — the header totals, the featured-deck
    # pick, the compact rows, the variant-share loops — excludes them for free
    # and none of it can drift. Owner decision 2026-07-28: keep them REACHABLE
    # (hiding them entirely would make them undeletable, and filling one in is
    # now how a placeholder graduates), just not mixed in with real decks.
    record_only = [d for d in decks if deck_service.is_record_only_deck(d)]
    decks = [d for d in decks if not deck_service.is_record_only_deck(d)]
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
        # #156 Option C — must be copied explicitly: this loop enumerates the
        # stats dict key by key, so a new service key does NOT reach the
        # template on its own (the #152 failure mode, where every service test
        # passed while the page rendered its empty state).
        deck.borrowed_games = stats.get("borrowed_games", 0)
        deck.last_played = stats.get("last_played")

    # issue #27 — variant-group share management surfaced in the deck-edit
    # popouts (decks.html, both the featured and per-row Edit forms). Gated on
    # variant_group_id so non-variant decks pay nothing. Each variant-group deck
    # gets: its sibling decks (share targets), its own placeable cards (the
    # share picker), and its existing outbound shares (the unshare list). The
    # siblings come from the already-loaded `decks` list (no extra query).
    decks_by_group: dict[int, list] = {}
    for d in decks:
        if d.variant_group_id is not None:
            decks_by_group.setdefault(d.variant_group_id, []).append(d)
    for d in decks:
        if d.variant_group_id is None:
            d.share_siblings = []
            d.share_own_cards = []
            d.outbound_shares = []
            continue
        d.share_siblings = [
            {"id": s.id, "name": s.name}
            for s in decks_by_group.get(d.variant_group_id, [])
            if s.id != d.id
        ]
        d.share_own_cards = own_deck_card_options(session, current_user.id, d)
        d.outbound_shares = get_outbound_shares_for_deck(session, d)

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
            # #152 lesson: a new context key needs THIS line, not just the service.
            "record_only_decks": record_only,
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
    is_brew: bool = Form(False),
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
        is_brew=is_brew,
    )

    return RedirectResponse(url="/decks", status_code=303)


# ── Per-deck goals (issue #46) ──────────────────────────────────
# Managed from the /decks edit popouts; read-only list on deck_detail. The
# goal-scoped routes (/decks/goals/{goal_id}/...) are registered BEFORE the
# /decks/{deck_id}/... routes so the literal "goals" segment matches first
# (FastAPI matches in registration order; a typed-int {deck_id} would 422 on
# "goals" rather than fall through). All are CSRF-guarded + owner-scoped via the
# service layer (a non-owned goal/deck is a silent no-op → redirect to /decks).


@router.post("/decks/{deck_id}/goals")
async def decks_add_goal(
    deck_id: int,
    label: str = Form(...),
    description: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    # The label inputs carry `required` (native browser guard), so an empty
    # label only reaches here from a non-browser client; surface it via the
    # one-shot ?goal_error banner rather than failing silently.
    try:
        create_deck_goal(session, current_user.id, deck_id, label, description)
    except ValueError:
        return RedirectResponse(url="/decks?goal_error=label", status_code=303)
    return RedirectResponse(url="/decks", status_code=303)


@router.post("/decks/goals/{goal_id}/edit")
async def decks_edit_goal(
    goal_id: int,
    label: str = Form(...),
    description: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    try:
        edit_deck_goal(session, current_user.id, goal_id, label, description)
    except ValueError:
        return RedirectResponse(url="/decks?goal_error=label", status_code=303)
    return RedirectResponse(url="/decks", status_code=303)


@router.post("/decks/goals/{goal_id}/move")
async def decks_move_goal(
    goal_id: int,
    direction: str = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    move_deck_goal(session, current_user.id, goal_id, direction)
    return RedirectResponse(url="/decks", status_code=303)


@router.post("/decks/goals/{goal_id}/remove")
async def decks_remove_goal(
    goal_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    # Soft-delete (the primary "remove"). Hard delete is the separate route below.
    deactivate_deck_goal(session, current_user.id, goal_id)
    return RedirectResponse(url="/decks", status_code=303)


@router.post("/decks/goals/{goal_id}/delete")
async def decks_delete_goal(
    goal_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_deck_goal(session, current_user.id, goal_id)
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


def _split_commanders(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split `_build_deck_card_items` output into (commanders, deck_cards).

    Every caller that renders the deck card list needs this split — the
    commander has its own panel and must never appear in the main list. It
    lives here so the full-page render and the HTMX partials can't drift
    (they did: the partials rendered the raw list, so re-sorting made the
    commander appear among the deck cards).
    """
    commanders = [i for i in items if i["role"] == "commander"]
    return commanders, [i for i in items if i["role"] != "commander"]


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
    own_rows = deck_query.all()

    # issue #27 — variant-group sharing. The deck's card list is its own rows
    # UNION the rows shared INTO it from sibling builds (a reference, never a
    # copy — the physical row still lives in its source deck). Both sets are
    # sorted/grouped TOGETHER into one unified decklist; the shared-in rows are
    # flagged ``is_shared_in`` so the macro renders them read-only (badge +
    # Unshare only — deck/bulk actions never touch a sibling's physical row).
    # ``outbound_share_map`` badges this deck's own rows that are shared OUT.
    # All three helpers short-circuit to empty for a deck with no variant group,
    # so a non-variant deck's grid is byte-for-byte unchanged.
    shared_out = outbound_share_map(session, deck)
    inbound_pairs = inbound_shared_rows_for_deck(session, deck, search)
    shared_from_by_row = {row.id: source_name for row, source_name in inbound_pairs}

    deck_rows = sort_spec.sort_inventory_rows(
        own_rows + [row for row, _ in inbound_pairs], sort or "name", direction
    )

    for row in deck_rows:
        is_shared_in = row.id in shared_from_by_row
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
                # A shared-in row keeps its source-deck role out of THIS deck's
                # commander split (a card that's a commander elsewhere isn't this
                # deck's commander), so force role to None for the unified list.
                "role": None if is_shared_in else row.role,
                "tags": get_row_tags(row),
                "tag_details": get_row_tag_details(row),
                "suggested_tags": suggest_card_roles(row.card, themes=themes),
                "legality_status": get_card_legality(row.card, deck.format),
                "shared_with": [] if is_shared_in else shared_out.get(row.id, []),
                "is_shared_in": is_shared_in,
                "shared_from": shared_from_by_row.get(row.id),
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


# ── #143 Public deck share links ────────────────────────────────


@router.get("/d/{token}")
def public_deck_view(
    request: Request,
    token: str,
    session: Session = Depends(get_db_session),
    viewer: User | None = Depends(get_optional_current_user),
):
    """PUBLIC (no auth) read-only deck view. Loads strictly by share_token; a
    missing/revoked token 404s. The context is a SANITIZED projection — no owner
    data ever reaches the template (see build_public_deck_view)."""
    deck = get_deck_by_share_token(session, token)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    view = build_public_deck_view(session, deck)
    return render(
        request,
        "deck_public.html",
        {"title": view["name"], "view": view, "current_user": viewer},
    )


@router.post("/decks/{deck_id}/share")
async def decks_share(
    request: Request,
    deck_id: int,
    _csrf: None = CsrfRequired,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Owner-only: (re)generate the deck's public share token, then back to detail."""
    generate_deck_share_token(session, deck_id=deck_id, user_id=current_user.id)
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/{deck_id}/unshare")
async def decks_unshare(
    request: Request,
    deck_id: int,
    _csrf: None = CsrfRequired,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """Owner-only: revoke the public share link (NULL the token → /d/{token} 404s)."""
    revoke_deck_share_token(session, deck_id=deck_id, user_id=current_user.id)
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/{deck_id}/wishlist")
async def decks_wishlist(
    request: Request,
    deck_id: int,
    card_name: list[str] = Form(default=[]),
    _csrf: None = CsrfRequired,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """#144 — add buy-list card name(s) to the watchlist (printing-agnostic,
    idempotent skip-duplicates). One name for a per-card add, many for a whole
    section. Redirects back to the deck with an added/skipped banner. Owner-scoped
    via get_deck; a non-owner deck id is a no-op redirect (no watches written)."""
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        return RedirectResponse(url="/decks", status_code=303)
    result = add_names_to_watchlist(session, current_user.id, card_name)
    return RedirectResponse(
        url=f"/decks/{deck_id}?wl_added={result['added']}&wl_skipped={result['skipped']}",
        status_code=303,
    )


@router.get("/decks/{deck_id}")
def deck_detail_page(
    request: Request,
    deck_id: int,
    search: str = "",
    sort: str = "name",
    direction: str = "asc",
    collection_search: str = "",
    health_filter: str = "",
    group: str = "",
    materialized: int = -1,
    remaining: int = 0,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    # View mode is a stored per-user preference — written ONLY by the Grid/List
    # toggle (POST /account/deck-view-pref) — NOT a URL axis. Reading it from the
    # URL let a stale ?view= pin the mode and deadlock the toggle (#149). Group-by
    # IS a URL axis (the search-form select): query param over pref over default.
    view_mode = current_user.deck_view_mode or "grid"
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

    use_drawer_sorter = has_sortable_setup(session, current_user.id)

    analytics = None
    health = None
    consistency = None
    deck_legality = None
    if deck and deck.storage_location_id:
        # issue #57 — analytics run over the FULL decklist (own rows UNION rows
        # shared in from sibling decks), so Card Types / Color Pips / mana curve
        # match the header's shared-inclusive total instead of the own-only set.
        all_deck_rows = resolved_deck_rows(session, deck, current_user.id)
        if all_deck_rows:
            analytics = compute_deck_analytics(all_deck_rows)
            health = compute_deck_health(all_deck_rows)
            consistency = compute_consistency(all_deck_rows)
            # #176 — legality findings from persisted columns only. Gated on
            # `if all_deck_rows:` like every other content-dependent surface,
            # which is the real condition (an empty deck has nothing to judge).
            deck_legality = check_deck_legality(session, deck, all_deck_rows)

    # #178 — the play profile is how the deck wants to be PILOTED, so it is
    # deliberately NOT gated on `all_deck_rows` the way the analytics block above
    # is. A profile describes intent, not contents: a deck whose list nobody has
    # typed in yet still has a plan, and the AI gauntlet pilots it from this.
    play_profile = None
    if deck:
        _pp_row = deck_service.get_play_profile(session, deck.id)
        if _pp_row:
            play_profile = {
                "profile": json.loads(_pp_row.profile_data),
                "is_custom": _pp_row.is_custom,
                "raw": _pp_row.profile_data,
                "updated_at": _pp_row.updated_at,
            }

    # #103 Phase B — hero bracket badge from the PERSISTED estimate only (the
    # request path never estimates; v3.27.9 invariant). Staleness = the deck's
    # current fingerprint differs from the one the daemon last evaluated.
    # #121 — the chip shows the owner's DECLARED bracket beside the computed
    # floor; declared < floor renders the violation state. Never a guess.
    bracket_badge = None
    if deck and deck.storage_location_id:
        _est = load_persisted_estimate(session, deck.id)
        _floor = _est.get("floor_bracket") if _est else None
        if _est or deck.declared_bracket:
            _cs = (
                deck_combo_status(session, deck.id, all_deck_rows)
                if all_deck_rows
                else {"stale": True}
            )
            bracket_badge = {
                "declared": deck.declared_bracket,
                "floor": _floor,
                "violation": bool(
                    deck.declared_bracket and _floor and deck.declared_bracket < _floor
                ),
                # #123 — stale also when the GC list moved since evaluation
                "stale": _cs["stale"]
                or bool(_est and _est["rules_version"] != gc_list_version(session)),
            }

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

    commanders, deck_cards = _split_commanders(items)

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

    # Brew Mode — owned/missing buy-list for a brew deck. Reuses the
    # already-loaded all_deck_rows; buckets the brew's own rows by proxy status
    # (real = owned, proxy = to buy, owner decision 2026-06-11) and (v3.38.x
    # Option B) splits fully-proxy cards the user owns inside ANOTHER deck into
    # an "owned_elsewhere" bucket so the buy-list never lists a deck-resident
    # card as "to buy". None for non-brew decks → panel hidden.
    brew_buylist = None
    if deck and deck.is_brew and deck.storage_location_id:
        brew_buylist = build_brew_buylist(
            session,
            current_user.id,
            locals().get("all_deck_rows") or [],
            deck.storage_location_id,
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

    # issue #27 — cards shared INTO this deck from sibling builds are folded
    # directly into the unified `items` list by `_build_deck_card_items` (sorted
    # and grouped alongside the deck's own cards, each flagged `is_shared_in` so
    # the macro renders it read-only). `total_cards` therefore ALREADY includes
    # them (the FULL decklist) — no separate addition here. The share/unshare
    # MANAGEMENT controls live in the deck-edit popouts on /decks (decks.html);
    # this page just renders the unified list + per-card Unshare. Export still
    # reads `get_inbound_shares_for_deck` directly (a separate seam).

    # #148 — the Considering holding area (own separate location; excluded from the
    # deck's own rows/counts/stats by construction).
    _considering_groups, _considering_total = (
        _build_considering_items(session, deck, current_user.id) if deck else ([], 0)
    )
    return render(
        request,
        "deck_detail.html",
        {
            "title": deck.name if deck else "Deck",
            "deck": deck,
            "considering_groups": _considering_groups,
            "considering_total": _considering_total,
            "variant_group": deck.variant_group if deck else None,
            "variant_siblings": variant_siblings,
            "brew_buylist": brew_buylist,
            "materialized": materialized,
            "materialized_remaining": remaining,
            "color_identity": color_identity,
            "commanders": commanders if deck else [],
            "items": deck_cards if deck else [],
            "deck_total_value": deck_total_value if deck else 0.0,
            "deck_total_cards": total_cards if deck else 0,
            "bracket_v2": bracket_v2,
            "bracket_badge": bracket_badge,
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
            "deck_legality": deck_legality,
            "play_profile": play_profile,
            "deck_record": deck_record,
            "goal_stats": deck_goal_stats(session, current_user.id, deck.id) if deck else [],
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
    _, items = _split_commanders(items)
    use_drawer_sorter = has_sortable_setup(session, current_user.id)
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

    view_mode = current_user.deck_view_mode or "grid"
    group_by = group if group in DECK_GROUP_BY_OPTIONS else (current_user.deck_group_by or "type")

    # Side-effect persistence: the group-by selector in the search form
    # auto-persists on Apply. View mode is deliberately NOT written here — its
    # sole writer is the Grid/List toggle. The old view back-write overwrote the
    # value the toggle had just saved whenever a stale ?view= sat in the URL,
    # keeping the toggle dead (#149).
    if group in DECK_GROUP_BY_OPTIONS and current_user.deck_group_by != group:
        current_user.deck_group_by = group
        session.commit()

    items, _, _ = _build_deck_card_items(session, deck, current_user.id, search, sort, direction)
    _, items = _split_commanders(items)
    use_drawer_sorter = has_sortable_setup(session, current_user.id)
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
    # view is intentionally omitted — it is a stored pref, not a URL axis (#149).
    qs_params = {"search": search, "sort": sort, "direction": direction}
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

    # issue #57 — panels analytics (tokens/synergy/dead-cards) and the panels
    # cache key must consume the SAME shared-inclusive row set the detail render
    # uses, or the fragment writes under a key the detail read never matches.
    all_deck_rows = resolved_deck_rows(session, deck, current_user.id)

    # #103 Phase B — combos come from the deck_combos table (written only by the
    # combo-refresh daemon; v3.27.9's request-path Spellbook POST stays dead).
    # The synergy classifier regains its "Direct via combo membership" path, and
    # the Win Conditions panel returns with an as-of timestamp + staleness chip.
    # The panels file-cache keeps its v3.27.9 placeholder combos shape (goldfish
    # quick-add reads that dict) — persisted combos are a separate read.
    bracket = None
    synergy = None
    combos: dict = {"included": [], "almost": []}
    tokens: list = []
    combo_status = {"combos": None, "computed_at": None, "stale": True}

    if all_deck_rows:
        ck = _panels_cache_key(all_deck_rows)
        cached = _read_panels_cache(deck_id, ck)

        if cached:
            tokens = cached.get("tokens", [])
        else:
            tokens = compute_deck_tokens(all_deck_rows)
            _write_panels_cache(deck_id, ck, {"tokens": tokens, "combos": combos})

        combo_status = deck_combo_status(session, deck.id, all_deck_rows)
        synergy = compute_deck_synergy(all_deck_rows, combo_status["combos"] or {"included": []})
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
            "combo_status": combo_status,
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
        if not has_sortable_setup(session, current_user.id):
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
    is_brew: bool = Form(False),
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
            is_brew=is_brew,
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

    # delete_deck disbands rather than destroys: real (claimed) rows return to
    # the collection as pending. For drawer-sorter users, re-file them into
    # their drawers so the round trip is byte-identical (mirrors decks_return).
    if has_sortable_setup(session, current_user.id):
        resort_collection(session, user_id=current_user.id)

    return RedirectResponse(url="/decks", status_code=303)


@router.post("/decks/{deck_id}/materialize")
async def decks_materialize(
    deck_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    result = materialize_brew(session, user_id=current_user.id, deck_id=deck_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Brew deck not found")
    return RedirectResponse(
        url=f"/decks/{deck_id}?materialized={result['claimed']}"
        f"&remaining={result['remaining_proxies']}",
        status_code=303,
    )


# --- Play profile (piloting intent) — issue: deck_play_profiles --------------- #
# JSON in, JSON out: the consumers are programmatic (the Forge AI-player
# simulation's profile generator, and later an editor UI), not a template.
# Owner-scoped like every other deck route; the POST carries the session-bound
# CSRF token as a form field alongside the JSON payload, same double-submit
# contract as all authenticated writes.


@router.get("/decks/{deck_id}/play-profile")
def deck_play_profile_get(
    deck_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    row = deck_service.get_play_profile(session, deck.id)
    if not row:
        return JSONResponse({"deck_id": deck.id, "profile": None, "is_custom": False})
    return JSONResponse(
        {
            "deck_id": deck.id,
            "profile": json.loads(row.profile_data),
            "is_custom": row.is_custom,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    )


@router.post("/decks/{deck_id}/play-profile")
def deck_play_profile_save(
    deck_id: int,
    profile_data: str = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    try:
        profile = json.loads(profile_data)
        row = deck_service.save_play_profile(session, deck, profile, is_custom=True)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return JSONResponse(
        {"deck_id": deck.id, "profile": json.loads(row.profile_data), "is_custom": row.is_custom}
    )


@router.get("/decks/{deck_id}/export")
def decks_export(
    deck_id: int,
    format: str = "txt",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    rows = (
        session.query(InventoryRow)
        .join(Card)
        .options(joinedload(InventoryRow.card))
        .filter(
            InventoryRow.user_id == current_user.id,
            InventoryRow.storage_location_id == deck.storage_location_id,
        )
        .order_by(Card.name.asc())
        .all()
    )
    # issue #27 — the same inbound variant-group shares the text export folds in,
    # so both formats cover the identical row set.
    inbound = get_inbound_shares_for_deck(session, deck)

    if format == "json":
        # LLM-parseable variant — per-card gameplay metadata (persisted columns,
        # no Scryfall call) + inventory context, plus a server-computed deck
        # rollup. Covers own rows UNION inbound shares.
        cards = []
        for row in rows:
            price = effective_price(row.card, row.finish)
            cards.append(
                {
                    **card_metadata(row.card),
                    "quantity": row.quantity,
                    "finish": row.finish or "normal",
                    "role": row.role or None,
                    "tags": row.tags or None,
                    "is_proxy": bool(row.is_proxy),
                    "price": round(price, 2) if price else None,
                    "shared_from": None,
                }
            )
        for share in inbound:
            card = share["card"]
            price = effective_price(card, share["finish"])
            cards.append(
                {
                    **card_metadata(card),
                    "quantity": share["quantity"],
                    "finish": share["finish"] or "normal",
                    "role": None,
                    "tags": None,
                    "is_proxy": share["is_proxy"],
                    "price": round(price, 2) if price else None,
                    "shared_from": share["source_deck_name"],
                }
            )
        # Deck-level rollups computed server-side over the full card set.
        color_union: set[str] = set()
        type_counts: dict[str, int] = {}
        mv_histogram: dict[str, int] = {}
        for c in cards:
            color_union.update(c["color_identity"])
            qty = c["quantity"]
            # Type bucket = the type word(s) before the em-dash, normalized.
            type_head = (c["type_line"] or "").split("—")[0].strip()
            for word in type_head.split():
                type_counts[word] = type_counts.get(word, 0) + qty
            mv = c["mana_value"]
            mv_key = "unknown" if mv is None else f"{mv:g}"
            mv_histogram[mv_key] = mv_histogram.get(mv_key, 0) + qty
        # #179 — commanders come from game_service.deck_commander_cards, the ONE
        # answer to "who is this deck's commander" (v4.12.40): role rows first,
        # falling back to #163's deck_commanders ANCHOR.
        #
        # WITHOUT the fallback a consumer would have to read commanders off each
        # card's `role`, which is the SAME defect v4.12.40 ended — a deck whose
        # commander was never TAGGED as an inventory row reports none, silently,
        # with nothing distinguishing it from a deck that genuinely has none.
        # Measured on prod 2026-08-03: 41 of 49 live decks carry a role row, 4
        # are anchor-only, 4 have no commander anywhere. Those 4 anchor-only
        # decks are exactly the "intermittent" population, and shipping the
        # role-only read would have recreated the bug in a fourth surface.
        #
        # A commander resolved from the anchor has NO row in `cards` (there is no
        # inventory row to have one) — deliberate, and the reason this is a
        # top-level key rather than a flag on a card. It states who the commander
        # IS without claiming the deck physically holds a copy.
        #
        # The TEXT export above is deliberately NOT given the same treatment: it
        # is an MTGA round-trip format, so a Commander line there asserts a card
        # the deck contains, and emitting an anchor-only commander would make
        # export → re-import add a card that was never in the deck.
        commanders = [card_metadata(c) for c in deck_commander_cards(session, deck)]
        return JSONResponse(
            {
                "deck": {"id": deck.id, "name": deck.name},
                "commanders": commanders,
                "rollup": {
                    "color_identity": sorted(color_union),
                    "type_counts": type_counts,
                    "mana_value_histogram": mv_histogram,
                },
                "cards": cards,
            }
        )

    def _export_line(card, quantity, finish, role):
        set_code = (card.set_code or "???").upper()
        collector = card.collector_number or "0"
        line = f"{quantity} {card.name} ({set_code}) {collector}"
        # Preserve finish so an export→re-import round-trip matches the copy
        # back to its ``(card_id, finish)`` inventory row instead of treating
        # it as a brand-new card. The MTGA-style ``*F*`` (foil) / ``*E*``
        # (etched) markers are the importer's own grammar (`_parse_list_line`
        # detects them anywhere on the line); a line without a marker parses as
        # normal, so older exports remain backward compatible and non-foil/
        # etched rows are unchanged.
        finish_marker = {"foil": " *F*", "etched": " *E*"}.get((finish or "normal").lower())
        if finish_marker:
            line += finish_marker
        return line

    commander_lines: list[str] = []
    deck_lines: list[str] = []
    for row in rows:
        line = _export_line(row.card, row.quantity, row.finish, row.role)
        if row.role == "commander":
            commander_lines.append(line)
        else:
            deck_lines.append(line)

    # issue #27 — append cards shared INTO this deck from sibling variant
    # builds so the export is the COMPLETE decklist (own cards + inbound
    # shares). Each shared row carries its own finish marker, so foil survives
    # export → re-import. No-op for a deck with no variant group.
    for share in inbound:
        deck_lines.append(_export_line(share["card"], share["quantity"], share["finish"], None))

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


@router.post("/decks/{deck_id}/share-card")
def decks_share_card(
    deck_id: int,
    inventory_row_id: int = Form(...),
    target_deck_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    # issue #27 — share one of THIS deck's own cards into a sibling variant
    # build. Creates a deck_card_share reference; the physical row never moves
    # (one-card-one-location preserved). Ownership + same-group + not-self are
    # validated in share_card_to_deck (ValueError → ignored, redirect back).
    try:
        share_card_to_deck(
            session,
            current_user.id,
            inventory_row_id=inventory_row_id,
            target_deck_id=target_deck_id,
        )
    except ValueError:
        pass
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/{deck_id}/unshare-card")
def decks_unshare_card(
    deck_id: int,
    inventory_row_id: int = Form(...),
    target_deck_id: int = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    # issue #27 — drop a share (the row reverts to membership in its source
    # deck only). Never touches the physical row.
    unshare_card_from_deck(
        session,
        current_user.id,
        inventory_row_id=inventory_row_id,
        target_deck_id=target_deck_id,
    )
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


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


@router.get("/decks/api/card-autocomplete-grouped")
def decks_card_autocomplete_grouped(
    q: str = "",
    finish: str = "normal",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """#119 — Add-tab default results: one row per unique card name.

    Grouped over the local scryfall_cards mirror (no network); each row
    carries the cheapest printing for the selected finish ("from $X.XX"),
    its image, and the printing count for the expand affordance.
    """
    return JSONResponse(grouped_card_search(session, q, finish=normalize_finish(finish)))


@router.post("/decks/{deck_id}/add-card")
async def decks_add_card(
    request: Request,
    deck_id: int,
    scryfall_id: str = Form(""),
    card_name: str = Form(""),
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
    quantity = max(1, min(int(quantity), 99))
    finish_normalized = normalize_finish(finish)

    # #119 — name-level add from a collapsed Add-tab row: resolve the
    # printing server-side (variant-group identity > owned loose copy >
    # cheapest for the selected finish). A specific-printing add
    # (scryfall_id present) is unchanged.
    resolution = None
    if not scryfall_id and card_name.strip():
        resolution = resolve_add_printing(
            session,
            user_id=current_user.id,
            deck=deck,
            name=card_name.strip(),
            finish=finish_normalized,
        )
        if not resolution:
            raise HTTPException(status_code=404, detail="Card not found")
        scryfall_id = resolution["scryfall_id"]
        finish_normalized = resolution["finish"]
    if not scryfall_id:
        raise HTTPException(status_code=400, detail="scryfall_id or card_name is required")

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

    # v3.37.0 Brew Mode: adding an UNOWNED card (recommended_action ==
    # "import_new") to a brew deck creates a PROXY row so it never counts toward
    # owned totals / the buy-list. That proxy-flagging now lives in the single
    # source — _commit_deck_import_with_reconciliation, which both this route
    # and the paste/CSV deck import funnel through — so it is NOT injected here.
    # When the user owns copies (move_* / covered_by_variant) normal pull
    # semantics apply (no proxy).

    # Lazy import to avoid a circular import (app.routes.imports imports nothing
    # from here, but keeping it lazy matches the established precedent and is
    # robust to future wiring). The reconciliation-commit helper lives with the
    # import flow in app/routes/imports.py (extracted from main.py in the v4 reorg).
    from app.routes.imports import _commit_deck_import_with_reconciliation

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

    # #119 — nothing about a name-level resolution is silent: the toast says
    # which printing resolved, from where, and flags a finish that differs
    # from the radio. Sent as a response header the Add-tab JS surfaces.
    toast = None
    if resolution:
        printing = (
            f"{(resolution['set_code'] or '?').upper()} #{resolution['collector_number'] or '?'}"
        )
        if action in ("move_existing", "move_existing_plus_new") and rc["matches"]:
            src = rc["matches"][0]["location_name"]
            toast = f"Added {printing} — moved from {src}"
        elif resolution["rule"] == "variant":
            toast = f"Added {printing} — imported, matches your variant group's printing"
        elif resolution["rule"] == "cheapest" and resolution["price"] is not None:
            toast = f"Added {printing} — imported cheapest, ${resolution['price']:.2f}"
        else:
            toast = f"Added {printing} — imported"
        if resolution["finish_differs"]:
            toast += f" ({resolution['finish']} — differs from selected finish)"

    if request.headers.get("HX-Request"):
        # Reuse the shared partial renderer rather than re-assembling the
        # context here: this site used to omit view_mode/group_by, so adding a
        # card while in list view silently swapped the user back to grid.
        response = _deck_cards_partial_response(request, session, current_user, deck_id)
        response.headers["HX-Push-Url"] = f"/decks/{deck_id}"
        if toast:
            response.headers["X-Add-Resolution"] = json.dumps(toast)
        return response

    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


# --------------------------------------------------------------------------- #
# #148 — the "Considering" holding area (rendered below the deck card list)
# --------------------------------------------------------------------------- #

_CONSIDERING_TYPE_ORDER = [
    "Creature",
    "Instant",
    "Sorcery",
    "Artifact",
    "Enchantment",
    "Planeswalker",
    "Battle",
    "Land",
    "Other",
]


def _considering_type_group(card) -> str:
    """Coarse card-type bucket for grouping the Considering list (the type word(s)
    before the em-dash — same idea as the deck rollup, kept simple/self-contained)."""
    tl = (getattr(card, "type_line", "") or "").split("—")[0].split("//")[0].lower()
    for t in (
        "creature",
        "instant",
        "sorcery",
        "artifact",
        "enchantment",
        "planeswalker",
        "battle",
        "land",
    ):
        if t in tl:
            return t.capitalize()
    return "Other"


def _build_considering_items(session, deck, user_id):
    """(grouped_items, total_qty) for the Considering section. Each item carries
    the row, card, quantity, and is_proxy (the ownership signal: proxy = a
    placeholder for a card you don't own, real = an owned copy staged here)."""
    rows = list_considering_rows(session, deck, user_id)
    groups: dict[str, list] = {}
    total = 0
    for r in rows:
        total += r.quantity
        item = {"row": r, "card": r.card, "quantity": r.quantity, "is_proxy": r.is_proxy}
        groups.setdefault(_considering_type_group(r.card), []).append(item)
    ordered = [
        (g, sorted(groups[g], key=lambda i: (i["card"].name or "").lower()))
        for g in _CONSIDERING_TYPE_ORDER
        if g in groups
    ]
    return ordered, total


def _considering_section_response(request: Request, session, current_user, deck) -> HTMLResponse:
    ordered, total = _build_considering_items(session, deck, current_user.id)
    return render(
        request,
        "_considering_section.html",
        {"deck": deck, "considering_groups": ordered, "considering_total": total},
    )


@router.post("/decks/{deck_id}/considering/add")
async def decks_considering_add(
    request: Request,
    deck_id: int,
    scryfall_id: str = Form(""),
    card_name: str = Form(""),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Add a card to the deck's Considering area. Owned copies are pulled in; on a
    brew deck an unowned card becomes a placeholder; on a non-brew deck an unowned
    add is refused (scope #148). HTMX → the re-rendered section; else 303."""
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    scryfall_id = scryfall_id.strip()
    finish_normalized = normalize_finish(finish)
    quantity = max(1, min(int(quantity), 99))
    if not scryfall_id and card_name.strip():
        resolution = resolve_add_printing(
            session,
            user_id=current_user.id,
            deck=deck,
            name=card_name.strip(),
            finish=finish_normalized,
        )
        if not resolution:
            raise HTTPException(status_code=404, detail="Card not found")
        scryfall_id = resolution["scryfall_id"]
        finish_normalized = resolution["finish"]
    if not scryfall_id:
        raise HTTPException(status_code=400, detail="scryfall_id or card_name is required")

    card = get_or_create_card(session, scryfall_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    result = add_card_to_considering(
        session, current_user.id, deck.id, card.id, finish_normalized, quantity
    )
    if request.headers.get("HX-Request"):
        response = _considering_section_response(request, session, current_user, deck)
        if result == "not_owned":
            response.headers["X-Considering-Msg"] = json.dumps(
                "You don't own that card — mark this deck as a brew to stage unowned cards."
            )
        return response
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/{deck_id}/considering/{row_id}/promote")
async def decks_considering_promote(
    request: Request,
    deck_id: int,
    row_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Promote a Considering row into the deck's main list. Crosses zones (both the
    deck list and the section change) so it does a full 303 re-render."""
    promote_from_considering(session, current_user.id, row_id)
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/{deck_id}/considering/{row_id}/remove")
async def decks_considering_remove(
    request: Request,
    deck_id: int,
    row_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Remove a row from Considering (placeholder discarded, real returned to
    collection). HTMX → the re-rendered section; else 303."""
    remove_from_considering(session, current_user.id, row_id)
    if request.headers.get("HX-Request"):
        deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
        if not deck:
            raise HTTPException(status_code=404, detail="Deck not found")
        return _considering_section_response(request, session, current_user, deck)
    return RedirectResponse(url=f"/decks/{deck_id}", status_code=303)


@router.post("/decks/{deck_id}/rows/{row_id}/demote")
async def decks_row_demote(
    request: Request,
    deck_id: int,
    row_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Demote a deck main-list row into the deck's Considering area (full 303
    re-render — both the deck list and the section change)."""
    demote_to_considering(session, current_user.id, row_id)
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
    owned_by_key: dict[tuple[str, str], dict[str, dict[str, int]]] = {}
    for entry in owned_printings:
        key = (entry["set_code"], entry["collector_number"])
        owned_by_key.setdefault(key, {})[entry["finish"]] = {
            "total": entry["quantity"],
            "loose": entry["loose_quantity"],
        }
    # Annotate every printing with owned_finishes so the template renders
    # toggle buttons with owned/loose hints and disables what can't be swapped.
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
        raise HTTPException(status_code=400, detail="You don't own a loose copy of that printing.")

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
    next: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Persist the bracket intent survey answers for a deck. Empty -> NULL.

    The survey lives on both the deck detail page and the #82 bracket page; the
    latter passes `next` so submitting returns to the bracket page instead of
    bouncing to deck detail. `next` is honored only as a same-origin path.
    """
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        return RedirectResponse(url="/decks", status_code=303)
    deck.intent_pod = intent_pod.strip() or None
    deck.intent_speed = intent_speed.strip() or None
    deck.intent_combo = intent_combo.strip() or None
    deck.intent_winning = intent_winning.strip() or None
    deck.intent_played = intent_played.strip() or None
    session.commit()
    dest = next if next.startswith("/") and not next.startswith("//") else f"/decks/{deck_id}"
    return RedirectResponse(url=dest, status_code=303)


@router.get("/decks/{deck_id}/bracket")
def deck_bracket_page(
    request: Request,
    deck_id: int,
    refresh_error: str = "",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    """#82 — dedicated Bracket Evaluation page. Read-only: shows the last
    persisted estimate (or an empty state), never computes on the request path
    (the estimator's per-deck compute is what got it pulled off deck detail in
    v3.27.9). Re-evaluation is an explicit POST to /bracket/refresh."""
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    estimate = load_persisted_estimate(session, deck.id)
    return render(
        request,
        "deck_bracket.html",
        {
            "title": f"Bracket · {deck.name}",
            "deck": deck,
            "bracket_v2": estimate,
            "refresh_error": bool(refresh_error),
            "current_user": current_user,
        },
    )


@router.post("/decks/{deck_id}/declare-bracket")
def deck_declare_bracket(
    deck_id: int,
    declared_bracket: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """#121 — the owner's bracket declaration (1-5; empty = undeclared).

    The app verifies a declaration against the computed floor; it never fills
    this in. Declaring below the floor is ALLOWED and surfaces the violation
    state — the owner may be mid-edit toward a lower bracket.
    """
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    value = declared_bracket.strip()
    if value:
        try:
            parsed = int(value)
        except ValueError:
            raise HTTPException(status_code=400, detail="declared_bracket must be 1-5") from None
        if not 1 <= parsed <= 5:
            raise HTTPException(status_code=400, detail="declared_bracket must be 1-5")
        deck.declared_bracket = parsed
    else:
        deck.declared_bracket = None
    session.commit()
    return RedirectResponse(url=f"/decks/{deck_id}/bracket", status_code=303)


@router.post("/decks/{deck_id}/bracket/refresh")
def deck_bracket_refresh(
    deck_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """#82 — recompute + persist the bracket estimate for a deck, then 303 back
    to the read-only page. combos stay None: the Spellbook combo compute (per-
    deck network call) was the actual cold-load offender, so re-eval is
    mechanics + intent only, matching "local-only and fast"."""
    deck = get_deck(session, deck_id=deck_id, user_id=current_user.id)
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")
    try:
        # #121 — the floor's two-card-combo input comes from the PERSISTED
        # combo payload (a local read; the daemon owns the Spellbook network
        # call, so re-eval stays "local-only and fast").
        combos = load_deck_combos(session, deck.id)
        estimate = estimate_bracket_v2(session, deck, current_user.id, combos=combos)
        persist_estimate(session, deck.id, estimate)
    except Exception as exc:  # noqa: BLE001 — surface a banner, never 500 the page
        print(f"[bracket_v2] refresh failed deck={deck.id}: {exc}", flush=True)
        return RedirectResponse(url=f"/decks/{deck_id}/bracket?refresh_error=1", status_code=303)
    return RedirectResponse(url=f"/decks/{deck_id}/bracket", status_code=303)


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

    if has_sortable_setup(session, current_user.id):
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
                # issue #57 — warm the cache under the same shared-inclusive key
                # the panels fragment + detail render compute, or the warm-up
                # writes a key they never read.
                all_rows = resolved_deck_rows(session, deck, current_user.id)
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
        row.updated_at = utc_now()
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
        row.updated_at = utc_now()
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
