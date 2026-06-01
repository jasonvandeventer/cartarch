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
import secrets
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from app.audit_service import list_transaction_logs, log_transaction
from app.auth import hash_password, validate_password_strength
from app.dashboard_service import get_dashboard_data
from app.db import DATA_DIR, SessionLocal, checkpoint_and_dispose, init_db, shutdown_event
from app.deck_service import (
    CARD_ROLE_TAGS,
    DECK_GROUP_BY_OPTIONS,
    DECK_VIEW_MODES,
    add_auto_tags,
    bump_deck_row_quantity,
    compute_consistency,
    compute_dead_cards,
    compute_deck_analytics,
    compute_deck_game_stats,
    compute_deck_health,
    compute_deck_synergy,
    compute_deck_tokens,
    create_deck,
    delete_deck,
    extract_commander_themes,
    find_inventory_matches_for_deck_import,
    get_card_legality,
    get_deck,
    get_row_tag_details,
    get_row_tags,
    group_deck_items,
    list_decks,
    list_decks_basic,
    list_user_printings_for_card,
    pull_card_to_deck,
    return_card_from_deck,
    set_row_tags,
    suggest_card_roles,
    suggest_card_roles_with_confidence,
    switch_deck_row_printing,
    update_deck,
)
from app.decklist_service import (
    bucket_decklist_results,
    name_owned_counts,
    owned_inventory_for_names,
    parse_decklist_text,
    resolve_short_form_lines,
)
from app.dependencies import (
    DRAWER_SORTER_USERNAMES,
    CsrfRequired,
    get_current_user,
    get_db_session,
    get_optional_current_user,
    render,
    require_csrf_or_reissue,
)
from app.game_service import (
    create_game,
    delete_game,
    end_game,
    get_deck_record,
    get_game,
    get_seat_commander_image_urls,
    get_viewable_game,
    list_games,
    normalize_game_format,
    reassign_seat_user,
    set_game_playgroup,
    toggle_seat_art_background,
    update_game_notes,
)
from app.import_service import (
    _distinct_locations_from_rows,
    auto_create_locations,
    compute_duplicate_counts_for_resolved,
    normalize_finish,
    normalize_language,
    parse_scanner_csv,
    parse_text_list,
    persist_import_rows,
    resolve_location_names,
)
from app.inventory_service import (
    PRICE_STALE_DAYS,
    adjust_inventory_row_quantity,
    apply_collection_search_filters,
    bulk_delete_inventory_rows,
    confirm_all_pending,
    confirm_pending_row,
    delete_inventory_row,
    find_inventory_matches_for_collection_import,
    get_collection_facet_counts,
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
from app.models import Card, Deck, ImportBatch, InventoryRow, TransactionLog, User
from app.password_reset_service import (
    check_rate_limits,
    consume_token,
    create_reset_token,
    find_valid_token,
    queue_reset_email,
)
from app.presentation_service import build_pending_batch_groups, build_pending_view_model
from app.pricing import effective_price
from app.routes import account, admin, auth, drawers, goldfish, playgroups, sharing, trades
from app.scryfall import (
    _bulk_data_loop,
    autocomplete_cards_for_add,
    autocomplete_token_names,
    bulk_refresh_prices,
    fetch_card_by_scryfall_id,
    fetch_card_by_set_and_number,
    fetch_card_printings,
    fetch_token_by_set_number,
    refresh_card_from_scryfall,
    search_cards_by_name,
    search_tokens_by_name,
)
from app.set_service import get_set_completion
from app.token_service import (
    add_deck_token_requirement,
    create_token,
    deck_requirement_exists_for_name,
    deck_token_status,
    delete_deck_token_requirement,
    delete_token,
    list_token_subtypes,
    list_tokens,
    parse_bulk_token_lines,
    resolve_token_inventory_id_by_name,
    total_token_count,
    update_token,
)
from app.watchlist_service import (
    add_to_watchlist,
    list_watchlist,
    remove_from_watchlist,
    update_note,
    update_target_price,
)
from scripts.run_migrations import run as run_migrations

app = FastAPI(title="Cartarch")

# Expose Prometheus metrics at /metrics for the kube-prometheus-stack
# ServiceMonitor (platform observability). include_in_schema=False keeps it out
# of the public OpenAPI surface. NOTE: the route is still reachable via the
# public ingress (cartarch.com/metrics) — restrict at Traefik if that matters.
Instrumentator().instrument(app).expose(app, include_in_schema=False)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "dev-only-change-me"),
    same_site="lax",
    https_only=os.getenv("DEV_MODE", "false").lower() != "true",
)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(account.router)
app.include_router(drawers.router)
app.include_router(playgroups.router)
app.include_router(sharing.router)
app.include_router(trades.router)
app.include_router(goldfish.router)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> HTMLResponse:
    return HTMLResponse(
        f"<h2>Error</h2><p>{html.escape(str(exc))}</p><a href='/collection'>Back to collection</a>",
        status_code=400,
    )


app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Resolve the browser's root /favicon.ico probe.

    v3.27.17 — every browser hits /favicon.ico on first contact regardless of
    what the document's <link rel="icon"> tags say. StaticFiles is mounted at
    /static, so without this explicit route the root probe would 404. Serves
    the bundled .ico from the new brand directory.
    """
    return FileResponse("app/static/brand/icon/favicon.ico")


# v3.27.17 — host allowlist for Referer-based redirect validation.
# Same-host (request.url.netloc) is always implicitly allowed; this set
# names the additional hosts cartarch.com lives behind so a user on the
# legacy hostname can follow a link that bounces them into cartarch.com
# (or vice versa) without safe_redirect_url() treating the cross-host
# Referer as an open-redirect attempt and dropping back to the default.
# No TrustedHostMiddleware in use; this is the only host-allow surface.
_REDIRECT_ALLOWED_HOSTS: frozenset[str] = frozenset({"cartarch.com", "www.cartarch.com"})


# v3.28.2 — Chronicle content artifact. Loaded ONCE at module import.
# The file is curated (not auto-generated from release-history.md): the
# release-history.md is the engineering record; chronicle.json is the
# public-facing reader reframing. Each future release adds an entry as
# part of the project-memory update — see CLAUDE.md "Forward process".
# Sorted newest-first; CHRONICLE_ENTRIES[0] is the current release.
_CHRONICLE_PATH = os.path.join(os.path.dirname(__file__), "data", "chronicle.json")
try:
    with open(_CHRONICLE_PATH, encoding="utf-8") as _f:
        CHRONICLE_ENTRIES: list[dict] = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    # A startup-time read miss must not break the app — the Chronicle
    # surface degrades to "no entries" rather than 500.
    CHRONICLE_ENTRIES = []
CHRONICLE_BY_VERSION: dict[str, dict] = {e["version"]: e for e in CHRONICLE_ENTRIES}


def safe_redirect_url(request: Request, default: str = "/collection") -> str:
    # Validate before using Referer as redirect target — an attacker can set it to an external URL.
    referer = request.headers.get("referer", "")
    if not referer:
        return default
    parsed = urlparse(referer)
    if (
        parsed.netloc
        and parsed.netloc != request.url.netloc
        and parsed.netloc not in _REDIRECT_ALLOWED_HOSTS
    ):
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

        _bulk = bulk_refresh_prices([c.scryfall_id for c in stale])
        fresh_by_id = _bulk.cards
        if _bulk.not_found or _bulk.failed:
            print(
                f"[price-refresh] bulk: {len(fresh_by_id)} resolved, "
                f"{len(_bulk.not_found)} not_found, {len(_bulk.failed)} failed",
                flush=True,
            )
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
                card.full_art = fresh.get("full_art")
                card.frame_effects = fresh.get("frame_effects")
                card.set_type = fresh.get("set_type")
                card.layout = fresh.get("layout")
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
    if shutdown_event.wait(60):  # let the app finish starting; bail if stopping
        return
    while not shutdown_event.is_set():
        _run_price_refresh_batch()
        shutdown_event.wait(_PRICE_REFRESH_INTERVAL_SECONDS)


_TRAIT_BACKFILL_BATCH = 75
_TRAIT_BACKFILL_BUSY_SECONDS = 3  # between batches while work remains
_TRAIT_BACKFILL_IDLE_SECONDS = 600  # re-check after catching up (new imports)


def _run_trait_backfill_batch() -> int:
    """Backfill Card printing traits (set_type/layout/full_art/frame_effects)
    for owned cards that don't have them yet.

    Runs entirely off the request path — the drawer sorter resolves traits
    strictly from these local columns, so this loop is what makes it
    accurate without ever putting Scryfall I/O in a request (the v3.23.8
    pod-lockup fix). Commits per batch so progress is durable across pod
    restarts; a card Scryfall won't return gets set_type="" so it's marked
    done and the loop always makes forward progress. Returns the batch
    size processed (0 when nothing left to backfill).
    """
    session = SessionLocal()
    try:
        pending = (
            session.query(Card)
            .join(InventoryRow, InventoryRow.card_id == Card.id)
            .filter(Card.set_type == None)  # noqa: E711
            .order_by(Card.id.asc())
            .limit(_TRAIT_BACKFILL_BATCH)
            .distinct()
            .all()
        )
        if not pending:
            return 0

        _bulk = bulk_refresh_prices([c.scryfall_id for c in pending])
        fresh_by_id = _bulk.cards
        if _bulk.not_found or _bulk.failed:
            print(
                f"[trait-backfill] bulk: {len(fresh_by_id)} resolved, "
                f"{len(_bulk.not_found)} not_found, {len(_bulk.failed)} failed",
                flush=True,
            )
        now = datetime.utcnow()
        for card in pending:
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
                card.full_art = fresh.get("full_art")
                card.frame_effects = fresh.get("frame_effects")
                card.set_type = fresh.get("set_type")
                card.layout = fresh.get("layout")
                card.updated_at = now
            else:
                # Scryfall didn't return it — mark done (non-NULL) so the
                # loop doesn't re-select it forever and stall convergence.
                card.set_type = ""
        session.commit()
        print(f"[trait-backfill] processed {len(pending)} cards")
        return len(pending)
    except Exception as exc:
        session.rollback()
        print(f"[trait-backfill] error: {exc}")
        return 0
    finally:
        session.close()


def _trait_backfill_loop() -> None:
    if shutdown_event.wait(30):  # after migrations/init; bail if stopping
        return
    while not shutdown_event.is_set():
        processed = _run_trait_backfill_batch()
        shutdown_event.wait(
            _TRAIT_BACKFILL_BUSY_SECONDS if processed else _TRAIT_BACKFILL_IDLE_SECONDS
        )


_daemon_threads: list[threading.Thread] = []


@app.on_event("startup")
def on_startup() -> None:
    # Prevent accidental deploys with the default dev secret — sessions would be forgeable.
    if (
        os.getenv("DEV_MODE", "false").lower() != "true"
        and os.getenv("SESSION_SECRET_KEY", "dev-only-change-me") == "dev-only-change-me"
    ):
        raise RuntimeError("SESSION_SECRET_KEY must be set in production (DEV_MODE is not 'true')")
    # v3.27.14 — Resend API key startup check. Same shape as the
    # SESSION_SECRET_KEY check above: refuse to boot in production
    # without it. The key is consumed by app/password_reset_service.py
    # for outbound /forgot-password emails. DEV_MODE skips the check
    # and falls back to logging the reset URL to stdout instead of
    # sending — preserves the local-dev story without a fake-SMTP
    # dependency.
    if os.getenv("DEV_MODE", "false").lower() != "true" and not os.getenv("RESEND_API_KEY"):
        raise RuntimeError(
            "RESEND_API_KEY must be set in production (DEV_MODE is not 'true'). "
            "Wire it via Kubernetes Secret + secretKeyRef — see the "
            "mana-archive-platform deployment.yaml for the SESSION_SECRET_KEY "
            "pattern this mirrors."
        )
    run_migrations()
    init_db()
    for _target, _name in (
        (_price_refresh_loop, "price-refresh"),
        (_trait_backfill_loop, "trait-backfill"),
        (_bulk_data_loop, "bulk-data"),
    ):
        thread = threading.Thread(target=_target, daemon=True, name=_name)
        thread.start()
        _daemon_threads.append(thread)


@app.on_event("shutdown")
def on_shutdown() -> None:
    """Clean SQLite shutdown so the volume detaches on a consistent file.

    Stop the writer daemons (they watch ``shutdown_event``), wait briefly for any
    in-flight batch to finish, then checkpoint the WAL into the main DB and close
    the pool. Pairs with a generous ``terminationGracePeriodSeconds`` on the
    Deployment so Kubernetes waits for this before SIGKILL + volume detach. See
    the SQLite-on-Longhorn corruption mitigation.
    """
    shutdown_event.set()
    for thread in _daemon_threads:
        thread.join(timeout=10)
    checkpoint_and_dispose()


@app.get("/")
def home(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User | None = Depends(get_optional_current_user),
):
    # v3.27.17 — anon visitors get the public marketing landing page; signed-in
    # users get the existing dashboard. Two completely different templates: the
    # landing page does NOT extend base.html (no app shell, no sidebar — it is
    # a separate marketing surface). The authenticated branch below is the
    # pre-v3.27.17 behavior unchanged.
    if current_user is None:
        return render(
            request,
            "landing.html",
            {"title": "Cartarch — The ruler of your collection"},
        )

    # v3.27.8 — empty-state signal for the dashboard homepage. Mirrors the
    # established show_onboarding pattern from collection_page (main.py:1775)
    # and decks_page (main.py:2500): a single-row existence check, cheap
    # under SQLite, used by the template to switch between the populated
    # dashboard and the new-user welcome state.
    show_onboarding = (
        session.query(InventoryRow.id).filter(InventoryRow.user_id == current_user.id).first()
        is None
    )
    # v3.28.5 — Folio dashboard. ``get_dashboard_data`` returns the full
    # nine-panel data shape (replaces the v3.27.10/v3.27.11 three-tile
    # ``get_dashboard_tiles`` shape; the legacy function is kept in
    # dashboard_service.py for backward-compat but is no longer called
    # from the home route). Computed only when the populated dashboard
    # renders — the show_onboarding empty state skips the work since a
    # brand-new account has nothing to surface. Total query cost
    # ~30 ms on prod data shape per the dashboard_service module header.
    dashboard = None if show_onboarding else get_dashboard_data(session, user_id=current_user.id)
    return render(
        request,
        "home.html",
        {
            "title": "Cartarch",
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            "show_onboarding": show_onboarding,
            "dashboard": dashboard,
        },
    )


@app.get("/privacy")
def privacy_page(
    request: Request,
    current_user: User | None = Depends(get_optional_current_user),
):
    """v3.27.17 — placeholder privacy policy. Linked from landing-page footer.

    Uses ``get_optional_current_user`` so authed users see the full app
    shell (with sidebar) while anon users get the anon shell (topbar
    only, no sidebar). Without threading current_user into the template
    context, base.html's ``{% if not current_user %}`` would always
    evaluate truthy and authed users would lose their sidebar on these
    pages.
    """
    return render(
        request,
        "privacy.html",
        {"title": "Privacy — Cartarch", "current_user": current_user},
    )


@app.get("/terms")
def terms_page(
    request: Request,
    current_user: User | None = Depends(get_optional_current_user),
):
    """v3.27.17 — placeholder terms of service. Linked from landing-page footer.

    Same ``get_optional_current_user`` pattern as ``privacy_page`` — see
    that handler's docstring for rationale.
    """
    return render(
        request,
        "terms.html",
        {"title": "Terms — Cartarch", "current_user": current_user},
    )


def _chronicle_archive_groups() -> list[dict]:
    """Group Chronicle entries by Folio (major version) → Issue (minor) for
    the Archive sidebar. Entries within each Issue are listed newest-first
    by Entry (patch); the order of CHRONICLE_ENTRIES is preserved.

    Returns a list shaped like:
        [
          {"folio": 3, "issues": [
            {"issue": 28, "entries": [
              {"version": "3.28.2", "patch": 2, "date": "...", "summary": "..."},
              ...
            ]},
            ...
          ]},
          ...
        ]

    ``summary`` is the first non-empty bullet from added → refined → resolved,
    so the archive sidebar can show a short label per entry without needing
    a separate field in chronicle.json.
    """
    by_folio: dict[int, dict[int, list[dict]]] = {}
    for entry in CHRONICLE_ENTRIES:
        parts = entry["version"].split(".")
        if len(parts) != 3:
            continue
        try:
            folio, issue, patch = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        bullets = entry.get("added") or entry.get("refined") or entry.get("resolved") or []
        summary = (bullets[0].split(".")[0] if bullets else "").strip()
        if len(summary) > 80:
            summary = summary[:77].rstrip() + "…"
        by_folio.setdefault(folio, {}).setdefault(issue, []).append(
            {
                "version": entry["version"],
                "patch": patch,
                "date": entry.get("date", ""),
                "summary": summary or "—",
            }
        )
    # Folios newest-first; Issues newest-first within folio.
    return [
        {
            "folio": f,
            "issues": [
                {"issue": i, "entries": by_folio[f][i]}
                for i in sorted(by_folio[f].keys(), reverse=True)
            ],
        }
        for f in sorted(by_folio.keys(), reverse=True)
    ]


def _render_chronicle(
    request: Request,
    current_user: User | None,
    entry: dict | None,
    not_found_version: str | None = None,
    status_code: int = 200,
):
    """Shared Chronicle render path — used by both the latest-entry route
    and the per-version route's not-found state."""
    return render(
        request,
        "chronicle.html",
        {
            "title": "Chronicle — Cartarch",
            "current_user": current_user,
            "entry": entry,
            "archive": _chronicle_archive_groups(),
            "not_found_version": not_found_version,
        },
        status_code=status_code,
    )


@app.get("/chronicle")
def chronicle_page(
    request: Request,
    current_user: User | None = Depends(get_optional_current_user),
):
    """v3.28.2 — Chronicle landing route. Shows the latest entry as the
    focused page; the Archive of Issues sidebar lists every prior entry.

    Public, anon-reachable (uses ``get_optional_current_user`` so authed
    users see the full app shell with sidebar; anon users get the anon
    shell — same pattern as ``/privacy`` and ``/terms``)."""
    entry = CHRONICLE_ENTRIES[0] if CHRONICLE_ENTRIES else None
    return _render_chronicle(request, current_user, entry)


@app.get("/chronicle/v{version}")
def chronicle_entry_page(
    request: Request,
    version: str,
    current_user: User | None = Depends(get_optional_current_user),
):
    """v3.28.2 — per-version Chronicle entry. URL pattern is
    ``/chronicle/vX.Y.Z`` — the literal ``v`` is part of the path, the
    ``X.Y.Z`` is captured by the path param.

    Roman-numeral URLs (e.g. ``/chronicle/III/XXVIII/II``) do NOT match
    this single-segment pattern and return 404 from the router — semantic
    is canonical; Roman is presentation-only.

    An unknown version renders the Chronicle page with a clean
    not-found notice in place of the focused entry, plus the full
    Archive sidebar — never a 500 or a blank page."""
    entry = CHRONICLE_BY_VERSION.get(version)
    if entry is None:
        return _render_chronicle(
            request,
            current_user,
            entry=None,
            not_found_version=version,
            status_code=404,
        )
    return _render_chronicle(request, current_user, entry)


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    session: Session = Depends(get_db_session),
    # Graceful CSRF on this public pre-auth form — see auth.login / the
    # require_csrf_or_reissue docstring. No-session => re-serve with a fresh
    # token+cookie; a real mismatch still 403s.
    csrf_token: str = Form(""),
):
    reissue = require_csrf_or_reissue(request, csrf_token, "register.html", {"title": "Register"})
    if reissue is not None:
        return reissue

    username = username.strip().lower()
    display_name = display_name.strip()

    if "@" not in username or "." not in username.split("@")[-1]:
        return render(
            request,
            "register.html",
            {"title": "Register", "error": "Please enter a valid email address."},
        )

    # v3.27.14 — shared password-strength check; same rules now apply
    # to /reset-password.
    password_error = validate_password_strength(password)
    if password_error:
        return render(
            request,
            "register.html",
            {"title": "Register", "error": password_error},
        )

    if not display_name:
        display_name = username.split("@")[0]

    # v3.27.17 — enumeration-oracle fix (Option A). Pre-v3.27.17 a duplicate-
    # email POST returned a distinguishable "An account with that email
    # already exists." error, leaking which addresses are registered. Closes
    # the Known Problem recorded in v3.27.14. The neutral response below is
    # truthful for both cases: a brand-new account ("an account is ready
    # for you, sign in") AND a no-op duplicate ("the account exists, sign
    # in or use forgot-password"). Both lead to /login, where the
    # appropriate path is one click away.
    #
    # Timing parity: the duplicate-email path runs an equivalent-cost
    # hash_password() and discards the result, so it does not return
    # measurably faster than the real account-creation path. Without
    # this throwaway hash, the duplicate path would shortcut around the
    # bcrypt/argon2 cost of password hashing and become a side-channel
    # oracle even with the response shapes byte-identical.
    #
    # /register does NOT auto-login on success (returns 303 → /login,
    # NOT a session cookie); so Option A leaves no residual oracle that
    # would distinguish a real signup landing on the dashboard vs a
    # duplicate not. Verified pre-implementation.
    existing = session.query(User).filter(User.username == username).first()
    if existing:
        # Equivalent-cost throwaway hash so the timing matches the
        # account-creation path below.
        _throwaway = hash_password(password)
        del _throwaway
        return RedirectResponse("/login", status_code=303)

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
    # [import-preview] diagnostic instrumentation (no logic changes) — splits
    # parser time from render/serialization time to localize the 524 timeout.
    _t0 = time.perf_counter()
    file_bytes = await file.read()
    _t_read = time.perf_counter()
    result = parse_scanner_csv(file_bytes)
    _t_parsed = time.perf_counter()

    # v3.30.15 — resolve per-row Location values against the user's
    # StorageLocations. Surfaces ambiguities (2+ matches), missing names
    # (auto-create confirm), and a duplicate warning for clean matches.
    distinct_loc_names = _distinct_locations_from_rows(result["valid_rows"])
    location_resolutions = resolve_location_names(session, current_user.id, distinct_loc_names)
    duplicate_counts = compute_duplicate_counts_for_resolved(
        session, current_user.id, result["valid_rows"], location_resolutions
    )

    context = {
        "title": "Import Preview",
        "valid_rows": result["valid_rows"],
        "invalid_rows": result["invalid_rows"],
        "format_name": result["format_name"],
        "filename": file.filename,
        "current_user": current_user,
        "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
        "locations": list_locations(session, current_user.id),
        "decks": list_decks_basic(session, user_id=current_user.id),
        "location_resolutions": location_resolutions,
        "duplicate_counts": duplicate_counts,
        "auto_create_error": None,
    }
    _t_ctx = time.perf_counter()
    response = render(request, "import_preview.html", context)
    _t_rendered = time.perf_counter()
    print(
        f"[import-preview] route: file.read={_t_read - _t0:.2f}s "
        f"parse_scanner_csv={_t_parsed - _t_read:.2f}s "
        f"context_build(locations+decks)={_t_ctx - _t_parsed:.2f}s "
        f"render={_t_rendered - _t_ctx:.2f}s "
        f"valid={len(result['valid_rows'])} invalid={len(result['invalid_rows'])}",
        flush=True,
    )
    return response


@app.post("/import/list/preview")
async def import_list_preview(
    request: Request,
    card_list: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    result = parse_text_list(card_list)
    # v3.30.15 — paste-list flow won't typically carry Location values, but
    # the template branches on the resolution context keys, so they must be
    # present. The helpers degrade to empty gracefully.
    distinct_loc_names = _distinct_locations_from_rows(result["valid_rows"])
    location_resolutions = resolve_location_names(session, current_user.id, distinct_loc_names)
    duplicate_counts = compute_duplicate_counts_for_resolved(
        session, current_user.id, result["valid_rows"], location_resolutions
    )
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
            "decks": list_decks_basic(session, user_id=current_user.id),
            "location_resolutions": location_resolutions,
            "duplicate_counts": duplicate_counts,
            "auto_create_error": None,
        },
    )


def _parsed_rows_from_form(
    line_number: list[str],
    name: list[str],
    scryfall_id: list[str],
    set_code: list[str],
    collector_number: list[str],
    finish: list[str],
    quantity: list[str],
    location: list[str],
    language: list[str] | None = None,
    location_type: list[str] | None = None,
    role: list[str] | None = None,
    tags: list[str] | None = None,
    is_proxy: list[str] | None = None,
) -> list[dict]:
    """Rebuild the parsed-row dicts from the parallel-array form fields.

    Shared by /import/commit, /import/reconcile-preview, and any future
    handler that receives the same field shape from import_preview.html.

    v3.30.16 — extended with four new optional parallel arrays
    (location_type / role / tags / is_proxy). Each falls back to the
    safe default when the array is absent or short (matches old 6-column
    CSVs that never carried these fields). The new fields are written
    into the parsed-row dict alongside the existing ones so
    persist_import_rows / the resolution helpers consume them
    transparently.
    """
    rows = []
    languages = language or []
    location_types = location_type or []
    roles = role or []
    tags_list = tags or []
    is_proxy_list = is_proxy or []
    for i in range(len(line_number)):
        # v3.30.16 — is_proxy form field carries the string "true"/"false";
        # parse_proxy_bool returns (bool, valid). Form values come from
        # hidden inputs we wrote ourselves so they're always one of the
        # two strings — invalid values would have already been routed to
        # invalid_rows at parse_scanner_csv time and never reach a form.
        raw_proxy = is_proxy_list[i] if i < len(is_proxy_list) else ""
        proxy_value, _ = normalize_proxy_value_for_commit(raw_proxy)
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
                "location_type": (location_types[i] if i < len(location_types) else "")
                .strip()
                .lower(),
                "language": normalize_language(languages[i]) if i < len(languages) else "en",
                "role": (roles[i] if i < len(roles) else "").strip(),
                "tags": tags_list[i] if i < len(tags_list) else "",
                "is_proxy": proxy_value,
            }
        )
    return rows


def normalize_proxy_value_for_commit(raw: str) -> tuple[bool, bool]:
    """Mirror of import_service.parse_proxy_bool used at commit-form-rebuild
    time. Form values are always one of the two recognized strings (we
    write them ourselves into the hidden inputs); any other value falls
    back to False with valid=True (the form has no untrusted path here).
    """
    cleaned = (raw or "").strip().lower()
    if cleaned == "true":
        return (True, True)
    return (False, True)


def _build_line_to_location_map(
    session: Session,
    user_id: int,
    parsed_rows: list[dict],
    choice_names: list[str],
    choice_ids: list[str],
    auto_create_confirm: str,
    choice_types: list[str] | None = None,
) -> tuple[dict[int, int], list[str], list[str]]:
    """v3.30.15 — build the per-row line_number → StorageLocation.id map.

    Consumes the parallel ``location_choice_name[]`` / ``location_choice_id[]``
    arrays the preview step emits and the ``auto_create_confirm`` checkbox
    value, plus the user's existing StorageLocations (so a clean single-match
    name resolves without requiring a UI choice).

    Each ``choice_id`` value carries one of:
      * ``"<positive int>"`` — use this StorageLocation.id directly
        (picker-resolved or single-match).
      * ``"0"`` — auto-create the corresponding name (gated by
        ``auto_create_confirm == "yes"``).
      * ``"-1"`` or empty — skip per-row resolution for this name; rows
        with this Location fall through to ``target_location_id``.

    v3.30.16 — new optional ``choice_types`` parallel array carries the
    per-name type for auto-create (``""`` / ``"box"`` / ``"binder"`` /
    ``"drawer"`` / ``"deck"`` / ``"other"``). For names with ``cid_int == 0``
    the type is threaded through to ``auto_create_locations`` so the
    auto-created location gets the right type instead of defaulting to
    ``"other"``. For non-zero choice_id values (picker/clean/skip) the
    type is ignored — the existing location's type wins per Decision 12.

    Returns ``(line_to_location_id, needs_confirm_names, skipped_deck_conflicts)``:
      * ``line_to_location_id`` — map of CSV line_number → resolved id.
        Empty dict if the caller should take the existing 3-branch dispatch.
      * ``needs_confirm_names`` — non-empty list of names whose auto-create
        was requested but ``auto_create_confirm != "yes"``. The route MUST
        re-render the preview with no writes in this case (Decision 3
        "User cancels → batch is rejected without writes").
      * ``skipped_deck_conflicts`` (v3.30.20) — display names of deck
        auto-create rows that the user opted into but that auto_create_locations
        silently dropped (the v3.30.18 try/except IntegrityError fallback
        for the pre-v3.1.0 legacy decks.name UNIQUE auto-index — fires when
        another user owns the name). The route handler threads this list
        to import_result.html so the result page can warn the user that
        N decks were skipped due to legacy name conflicts. Belt-and-
        suspenders alongside the v3.30.20 pre-warn in the preview UI: if
        a tampered submission slips a conflicting deck-create past the
        pre-warn, the try/except still catches it AND the result page
        notes it.
    """
    name_to_id: dict[str, int] = {}
    auto_create_names: list[str] = []
    # v3.30.16 — per-name type override map; only populated for names
    # entering the auto-create flow. Keyed by lowercased name to match
    # auto_create_locations' lookup convention.
    auto_create_type_overrides: dict[str, str] = {}
    # v3.30.20 — display-form lookup for skipped-deck-conflict reporting.
    # Keyed by lowercased name; value is the original CSV display form.
    display_by_normalized: dict[str, str] = {}

    choice_types = choice_types or []
    for i, (raw_name, raw_id) in enumerate(zip(choice_names, choice_ids, strict=False)):
        normalized = (raw_name or "").strip().lower()
        if not normalized:
            continue
        try:
            cid_int = int(raw_id)
        except (ValueError, TypeError):
            continue
        if cid_int > 0:
            name_to_id[normalized] = cid_int
        elif cid_int == 0:
            auto_create_names.append(raw_name.strip())
            display_by_normalized[normalized] = raw_name.strip()
            raw_type = (choice_types[i] if i < len(choice_types) else "").strip().lower()
            if raw_type:
                auto_create_type_overrides[normalized] = raw_type

    # v3.30.17 — deck-type auto-create rows have their own per-row opt-in
    # checkbox in the preview UI (Part B); the global auto_create_confirm
    # only gates NON-deck rows. Per-deck checkboxes work by toggling the
    # row's location_choice_id between "0" (create) and "-1" (skip),
    # so by the time we reach this code an unchecked deck row already has
    # cid_int=-1 and never enters auto_create_names. The split below
    # preserves the same shape for the global-confirm gate: only non-deck
    # names trigger the "must confirm" re-render. Deck rows that DID make
    # it into auto_create_names (checkbox checked) proceed straight to
    # auto_create_locations regardless of the global confirm state.
    auto_create_non_deck_names = [
        n for n in auto_create_names if auto_create_type_overrides.get(n.lower(), "") != "deck"
    ]
    if auto_create_non_deck_names and auto_create_confirm != "yes":
        return ({}, auto_create_non_deck_names, [])

    # v3.30.20 — record the set of names the user intended to create as
    # decks BEFORE calling auto_create_locations. After the call, anything
    # missing from the resolution map is a silently-skipped row — either
    # the v3.30.18 IntegrityError fallback fired (legacy cross-user
    # constraint) or the orphaned-Deck-no-paired-SL edge case. Both are
    # surfaced on the result page so the user knows the cards landed in
    # Pending rather than into the deck they ticked.
    intended_deck_creates = {
        n.lower(): display_by_normalized.get(n.lower(), n)
        for n in auto_create_names
        if auto_create_type_overrides.get(n.lower(), "") == "deck"
    }

    if auto_create_names:
        # Validation barrier already passed; create the missing locations.
        # auto_create_locations validates each requested type against
        # VALID_LOCATION_TYPES (minus root) and raises ValueError on miss.
        # The route handler catches ValueError and surfaces it via the
        # v3.30.15 auto-create-not-confirmed re-render pattern.
        # v3.30.17 — auto_create_locations routes type="deck" through
        # deck_service.create_deck so the paired Deck row lands atomically.
        created = auto_create_locations(
            session, user_id, auto_create_names, name_to_type=auto_create_type_overrides
        )
        name_to_id.update(created)

    # v3.30.20 — diff intended-deck-creates against actually-created.
    # Names that were requested but never landed in name_to_id are the
    # skipped-due-to-conflict rows (the v3.30.18 try/except fallback
    # caught them).
    skipped_deck_conflicts = [
        display_by_normalized.get(lname, lname)
        for lname in intended_deck_creates
        if lname not in name_to_id
    ]

    line_to_loc: dict[int, int] = {}
    for r in parsed_rows:
        raw = (r.get("location") or "").strip().lower()
        if not raw:
            continue
        if raw in name_to_id:
            try:
                line_num = int(r.get("line_number"))
            except (ValueError, TypeError):
                continue
            line_to_loc[line_num] = name_to_id[raw]

    return (line_to_loc, [], skipped_deck_conflicts)


def _deck_for_storage_location(
    session: Session, user_id: int, storage_location_id: int
) -> Deck | None:
    """If the given storage_location is a deck-type location owned by the user,
    return the Deck record that owns it. Otherwise None.
    """
    if storage_location_id <= 0:
        return None
    loc = get_location(session, location_id=storage_location_id, user_id=user_id)
    if loc is None or loc.type != "deck":
        return None
    return session.query(Deck).filter(Deck.storage_location_id == loc.id).first()


def _annotate_collection_dupes(rows: list[dict]) -> None:
    """Tag each collection-mode reconciliation row with display flags so the
    partial can render a focused "show only duplicates" view.

    Adds two booleans per row (mutating in place):

      - ``has_owned_match``  — total_user_owned > 0; the row is a duplicate
                               of something the user already owns somewhere.
      - ``is_deck_only_dupe`` — owned_breakdown has entries and ALL of them
                                are deck-type. This is the case where the
                                user's "duplicate" is allocated to a deck
                                rather than a free-collection location, and
                                a re-import shouldn't silently skip — the
                                deck copy is in use, the user probably wants
                                a new copy. The collection-mode template
                                auto-expands the per-row review when any
                                row has this flag set.
    """
    for r in rows:
        breakdown = r.get("owned_breakdown") or []
        r["has_owned_match"] = bool(breakdown) and (r.get("total_user_owned", 0) > 0)
        r["is_deck_only_dupe"] = r["has_owned_match"] and all(
            (b.get("location_type") == "deck") for b in breakdown
        )


@app.post("/import/reconcile-preview")
async def import_reconcile_preview(
    request: Request,
    target_location_id: int = Form(0),
    line_number: list[str] = Form([]),
    name: list[str] = Form([]),
    scryfall_id: list[str] = Form([]),
    set_code: list[str] = Form([]),
    collector_number: list[str] = Form([]),
    finish: list[str] = Form([]),
    quantity: list[str] = Form([]),
    location: list[str] = Form([]),
    language: list[str] = Form([]),
    # v3.30.16 — receive the five new parallel arrays so HTMX-included
    # reconciliation requests preserve them through the round trip. Not
    # used by the reconciliation logic itself (matching the v3.30.15
    # "reconciliation paths bypassed in v3.30.15 path" contract), but
    # consumed by _parsed_rows_from_form to keep the form-state shape
    # consistent.
    location_type: list[str] = Form([]),
    role: list[str] = Form([]),
    tags: list[str] = Form([]),
    is_proxy: list[str] = Form([]),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """HTMX endpoint: fires when the destination dropdown changes on the
    import preview. Returns the inner HTML of #reconciliation-panel.

    Dispatches on destination type:
      - Deck destination → deck-reconciliation path (Session 2 / v3.16.13-14).
        Renders the partial in `reconcile_mode="deck"` with movable matches,
        target-deck and other-deck breakdowns, and move/move-plus-new/import
        action options.
      - Non-deck destination (drawer/binder/box/other) OR auto-sort
        (target_location_id == 0) → collection-reconciliation path
        (Session A / v3.16.15+). Renders the partial in
        `reconcile_mode="collection"` with skip/delta/new actions based on
        total cross-location ownership.

    The wrapper div #reconciliation-panel lives in import_preview.html and
    is untouched by hx-swap=innerHTML — only its inner content changes.
    """
    parsed_rows = _parsed_rows_from_form(
        line_number,
        name,
        scryfall_id,
        set_code,
        collector_number,
        finish,
        quantity,
        location,
        language,
        location_type,
        role,
        tags,
        is_proxy,
    )

    # Decorate each parsed row's resolved card with a display_name for
    # the partial template. Same logic used by both reconciliation paths.
    def _decorate_display_names(rows: list[dict]) -> None:
        name_by_index = {r.get("line_number"): r.get("name") for r in parsed_rows}
        card_ids = [r["card_id"] for r in rows if r.get("card_id")]
        card_name_by_id: dict[int, str] = {}
        if card_ids:
            for c in session.query(Card.id, Card.name).filter(Card.id.in_(card_ids)).all():
                card_name_by_id[c.id] = c.name
        for row in rows:
            from_form = name_by_index.get(row.get("line_number")) or ""
            row["display_name"] = (
                from_form or card_name_by_id.get(row.get("card_id")) or row.get("scryfall_id", "")
            )

    deck = _deck_for_storage_location(session, current_user.id, target_location_id)

    if deck is not None:
        # Deck destination — existing path.
        matches_rows = find_inventory_matches_for_deck_import(
            session, current_user.id, deck.id, parsed_rows
        )
        _decorate_display_names(matches_rows)

        total_to_move = sum(r["recommended_move_qty"] for r in matches_rows)
        # Anticipated auto-merge: when target deck already has a row for
        # this (card, finish), the import_new path folds ALL of
        # recommended_new_qty into that existing row instead of creating
        # a duplicate.
        total_to_merge = sum(
            r["recommended_new_qty"] for r in matches_rows if r["total_in_target_deck"] > 0
        )
        total_to_import_new = sum(r["recommended_new_qty"] for r in matches_rows) - total_to_merge

        return render(
            request,
            "_import_reconciliation.html",
            {
                "reconcile_mode": "deck",
                "rows": matches_rows,
                "deck_name": deck.name,
                "total_to_move": total_to_move,
                "total_to_import_new": total_to_import_new,
                "total_to_merge": total_to_merge,
            },
        )

    # Non-deck destination (auto-sort or any non-deck location).
    matches_rows = find_inventory_matches_for_collection_import(
        session, current_user.id, parsed_rows
    )
    _decorate_display_names(matches_rows)
    _annotate_collection_dupes(matches_rows)

    total_to_skip = 0
    total_to_delta = 0
    total_to_new = 0
    for r in matches_rows:
        action = r["recommended_action"]
        if action == "skip_already_owned":
            total_to_skip += r["quantity_needed"]
        elif action == "import_delta":
            total_to_delta += r["recommended_new_qty"]
            total_to_skip += r["quantity_needed"] - r["recommended_new_qty"]
        else:  # import_new
            total_to_new += r["recommended_new_qty"]

    has_deck_only_dupes = any(r.get("is_deck_only_dupe") for r in matches_rows)

    # Destination name for the summary (when not auto-sort).
    destination_name: str | None = None
    if target_location_id > 0:
        loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
        if loc is not None:
            destination_name = loc.name

    return render(
        request,
        "_import_reconciliation.html",
        {
            "reconcile_mode": "collection",
            "rows": matches_rows,
            "deck_name": destination_name,  # template treats this as a generic destination label
            "total_to_skip": total_to_skip,
            "total_to_delta": total_to_delta,
            "total_to_new": total_to_new,
            "manual_mode": False,
            "has_deck_only_dupes": has_deck_only_dupes,
        },
    )


def _commit_deck_import_with_reconciliation(
    session: Session,
    user_id: int,
    deck: Deck,
    parsed_rows: list[dict],
    actions: list[str],
    move_qtys: list[int],
    new_qtys: list[int],
    filename: str,
) -> dict:
    """Per-row dispatch for deck imports under Refined Model A.

    For each parsed row, the user's reconciliation choice (or its default)
    selects one of three paths:

    - move_existing:        all copies come from existing inventory
                            (pull_card_to_deck loop, multi-source in order)
    - move_existing_plus_new: some copies move, some are imported new
    - import_new:           the existing persist_import_rows path

    After persist_import_rows creates new pending rows for the import_new
    portion, this handler does an auto-merge pass: for each new row whose
    (card_id, finish) already has a placed row in the target deck, the
    new row's quantity is added to the existing deck row and the new row
    is deleted (rather than placed alongside as a duplicate). The merged
    count is tracked separately from imported_count and reported back to
    the result page. Singleton-format decks (Commander etc.) never end
    up with two rows for the same printing as a result.

    Returns a dict mirroring persist_import_rows' shape plus extra counts
    used by import_result.html:

      imported_count       — unique (card, finish) rows that ended up as
                             NEW deck rows (after the auto-merge pass)
      total_quantity       — total copies of those new deck rows
      moved_count          — total copies moved from non-deck inventory
                             via pull_card_to_deck
      merged_count         — total copies merged into existing deck rows
                             instead of creating duplicates
      failed_rows          — list of rows that couldn't be resolved
      stale_match_rows     — rows where the preview said "move N" but
                             inventory had less than N at commit time;
                             the shortfall fell back to import_new
      batch_id             — most recent ImportBatch created for the new
                             import portion (None if no new copies were
                             imported)
      imported_row_ids     — IDs of rows that actually became new deck
                             rows (merged-then-deleted rows are excluded)
    """
    moved_count = 0
    stale_match_rows: list[dict] = []
    new_import_rows: list[dict] = []
    new_import_indices: list[int] = []  # position in parsed_rows for the new portion

    for idx, row in enumerate(parsed_rows):
        action = actions[idx] if idx < len(actions) else "import_new"
        move_qty = int(move_qtys[idx]) if idx < len(move_qtys) else 0
        new_qty = int(new_qtys[idx]) if idx < len(new_qtys) else int(row["quantity"])

        if action == "import_new":
            new_import_rows.append(row)
            new_import_indices.append(idx)
            continue

        # Re-resolve matches at commit time (preview state may be stale).
        recheck = find_inventory_matches_for_deck_import(session, user_id, deck.id, [row])[0]
        available = recheck["total_available"]

        # How many can we actually move now, capped by the user's
        # requested move_qty AND the current inventory.
        actual_move_qty = min(move_qty, available)
        shortfall = move_qty - actual_move_qty

        # Walk matches in order, draining each until actual_move_qty is hit.
        remaining_to_move = actual_move_qty
        for match in recheck["matches"]:
            if remaining_to_move <= 0:
                break
            pull_qty = min(remaining_to_move, match["quantity_available"])
            pulled_ok = pull_card_to_deck(
                session=session,
                user_id=user_id,
                deck_id=deck.id,
                inventory_row_id=match["inventory_row_id"],
                quantity=pull_qty,
            )
            if pulled_ok:
                moved_count += pull_qty
                remaining_to_move -= pull_qty

        # If the move shortfall was non-zero, we promised the user N moves
        # but only delivered some. Cover the rest as new imports and flag
        # this row in the result so import_result.html can warn.
        compensating_new = shortfall + new_qty
        if shortfall > 0:
            stale_match_rows.append(
                {
                    "line_number": row.get("line_number"),
                    "name": row.get("name") or row.get("scryfall_id"),
                    "expected_move": move_qty,
                    "actual_move": actual_move_qty,
                }
            )
        if compensating_new > 0:
            # Schedule a new import for the remaining quantity.
            row_for_new = dict(row)
            row_for_new["quantity"] = compensating_new
            new_import_rows.append(row_for_new)
            new_import_indices.append(idx)

    # Run the existing import path for everything that didn't get moved.
    new_imported_count = 0
    new_total_quantity = 0
    merged_count = 0
    failed_rows: list[dict] = []
    batch_id = None
    imported_row_ids: list[int] = []
    if new_import_rows:
        result = persist_import_rows(session, new_import_rows, filename=filename, user_id=user_id)
        new_imported_count = result["imported_count"]
        new_total_quantity = result.get("total_quantity", new_imported_count)
        failed_rows = result["failed_rows"]
        batch_id = result["batch_id"]
        imported_row_ids = result.get("imported_row_ids", [])

        # Auto-merge pass: for each new pending row whose (card_id, finish)
        # already has a placed row in the target deck, increment the
        # existing row's quantity and delete the new one. Otherwise queue
        # it for normal placement. Singleton-correct behavior — never two
        # rows for the same printing in the same deck.
        if imported_row_ids and deck.storage_location_id:
            new_pending_rows = (
                session.query(InventoryRow).filter(InventoryRow.id.in_(imported_row_ids)).all()
            )
            existing_deck_rows = (
                session.query(InventoryRow)
                .filter(
                    InventoryRow.user_id == user_id,
                    InventoryRow.storage_location_id == deck.storage_location_id,
                    InventoryRow.is_pending.is_(False),
                )
                .all()
            )
            existing_by_key: dict[tuple[int, str], InventoryRow] = {
                (r.card_id, r.finish): r for r in existing_deck_rows
            }
            rows_to_place: list[int] = []
            merged_row_ids: set[int] = set()
            now_ts = datetime.utcnow()
            for new_row in new_pending_rows:
                key = (new_row.card_id, new_row.finish)
                existing = existing_by_key.get(key)
                if existing is None:
                    rows_to_place.append(new_row.id)
                    continue
                existing.quantity += new_row.quantity
                existing.updated_at = now_ts
                merged_count += new_row.quantity
                merged_row_ids.add(new_row.id)
                log_transaction(
                    session=session,
                    user_id=user_id,
                    event_type="import_merge",
                    card_id=new_row.card_id,
                    finish=new_row.finish,
                    quantity_delta=new_row.quantity,
                    source_location="import",
                    destination_location=f"deck:{deck.name}",
                    batch_id=batch_id,
                    inventory_row_id=existing.id,
                    note=(
                        f"Merged {new_row.quantity} new copies into existing "
                        f"deck row in {deck.name}"
                    ),
                    flush=False,
                )
                session.delete(new_row)

            if rows_to_place:
                place_imported_rows(
                    session,
                    rows_to_place,
                    user_id=user_id,
                    location_id=deck.storage_location_id,
                )

            # Update reported counts: imported_count/total_quantity reflect
            # rows that ended up as NEW deck rows. Rows that merged into an
            # existing deck row instead are tracked in merged_count.
            if merged_row_ids:
                merged_rows_total_qty = sum(
                    r.quantity for r in new_pending_rows if r.id in merged_row_ids
                )
                new_imported_count = max(0, new_imported_count - len(merged_row_ids))
                new_total_quantity = max(0, new_total_quantity - merged_rows_total_qty)
                imported_row_ids = [rid for rid in imported_row_ids if rid not in merged_row_ids]

            session.commit()

    return {
        "imported_count": new_imported_count,
        "total_quantity": new_total_quantity,
        "moved_count": moved_count,
        "merged_count": merged_count,
        "failed_rows": failed_rows,
        "stale_match_rows": stale_match_rows,
        "batch_id": batch_id,
        "imported_row_ids": imported_row_ids,
    }


def _commit_collection_import_with_reconciliation(
    session: Session,
    user_id: int,
    target_location_id: int,
    parsed_rows: list[dict],
    actions: list[str],
    new_qtys: list[int],
    filename: str,
) -> dict:
    """Per-row dispatch for non-deck imports under the sync model
    (Refined Model A, design doc collection_import_sync.md §4).

    Sibling of ``_commit_deck_import_with_reconciliation`` for non-deck
    destinations. The user's choices map to three actions:

    - ``skip_already_owned``  → no ``InventoryRow`` created. Increment
                                ``skipped_count`` and emit an
                                ``import_skipped`` ``TransactionLog`` event
                                for audit.
    - ``import_delta``        → create + place ``new_qty`` copies (the user
                                owns some but fewer than they're importing).
    - ``import_new``          → create + place full ``quantity_needed`` copies
                                (override: the user explicitly wants new
                                copies even though they may already own
                                some).

    Re-resolves matches at commit time via
    ``find_inventory_matches_for_collection_import``. If a row's recommended
    action was ``skip_already_owned`` at preview but ``total_user_owned``
    has decreased below ``quantity_needed`` by commit (a concurrent change
    — user sold a card between preview and commit), fall back to
    ``import_delta`` with ``new_qty = quantity_needed - actual_owned`` and
    record the adjustment in ``stale_match_rows``. Stale-match fallback
    only triggers in the dangerous direction (less owned than expected);
    if the user gained inventory between preview and commit, their
    explicit ``import_delta`` / ``import_new`` choice is honored as-is.

    Args:
        session, user_id:       per-user-scoped session
        target_location_id:     0 = auto-sort (no ``place_imported_rows``
                                call; caller handles drawer-sorter resort);
                                >0 = specific non-deck location to place new
                                rows into. Per design doc §8.1, new rows are
                                placed alongside any existing rows for the
                                same (card, finish) at the destination —
                                merge-into-existing is a v3.16.X polish
                                target.
        parsed_rows:            same shape as
                                ``persist_import_rows`` input
        actions, new_qtys:      parallel arrays from the reconciliation
                                form; ``actions[i]`` is the user's choice
                                for ``parsed_rows[i]``, ``new_qtys[i]`` is
                                the qty to import as new (0 for skip).
        filename:               passed through to ``persist_import_rows``.

    Returns:
        Dict shaped for ``import_result.html``::

            {
                "imported_count":     int,  # unique new InventoryRows created
                "total_quantity":     int,  # total copies imported as new
                "skipped_count":      int,  # total copies skipped (skip + delta-portion)
                "failed_rows":        list[dict],
                "stale_match_rows":   list[dict],
                "batch_id":           int | None,
                "imported_row_ids":   list[int],
            }
    """
    skipped_count = 0
    stale_match_rows: list[dict] = []
    new_import_rows: list[dict] = []

    # Re-resolve at commit time so we can detect inventory drift since
    # preview. Single batched query — same shape as the read function.
    recheck = find_inventory_matches_for_collection_import(session, user_id, parsed_rows)
    recheck_by_line = {r["line_number"]: r for r in recheck}

    for idx, row in enumerate(parsed_rows):
        action = actions[idx] if idx < len(actions) else "import_new"
        form_new_qty = int(new_qtys[idx]) if idx < len(new_qtys) else int(row.get("quantity") or 1)
        quantity_needed = max(1, int(row.get("quantity") or 1))
        line_number = row.get("line_number")
        rc = recheck_by_line.get(line_number, {})
        actual_owned = rc.get("total_user_owned", 0)
        rc_card_id = rc.get("card_id")

        if action == "skip_already_owned":
            # Stale-match check: did the user's actual ownership drop below
            # the expected count between preview and commit?
            if actual_owned < quantity_needed:
                fallback_new_qty = quantity_needed - actual_owned
                stale_match_rows.append(
                    {
                        "line_number": line_number,
                        "name": row.get("name") or row.get("scryfall_id"),
                        "expected_skip": quantity_needed,
                        "actual_new_qty": fallback_new_qty,
                        "reason": "inventory_decreased",
                    }
                )
                row_for_new = dict(row)
                row_for_new["quantity"] = fallback_new_qty
                new_import_rows.append(row_for_new)
                skipped_count += actual_owned  # the rest is now imported
            else:
                # Still safe to skip.
                skipped_count += quantity_needed
                if rc_card_id is not None:
                    log_transaction(
                        session=session,
                        user_id=user_id,
                        event_type="import_skipped",
                        card_id=rc_card_id,
                        finish=(row.get("finish") or "normal").strip().lower(),
                        quantity_delta=0,
                        source_location="import",
                        destination_location="(skipped — already owned)",
                        batch_id=None,
                        inventory_row_id=None,
                        note=f"Skipped {quantity_needed} — already own {actual_owned}",
                        flush=False,
                    )
            continue

        # import_delta or import_new — trust the form's new_qty.
        if form_new_qty <= 0:
            # User overrode action to non-skip but set qty to 0. Treat as skip.
            skipped_count += quantity_needed
            continue

        row_for_new = dict(row)
        row_for_new["quantity"] = form_new_qty
        new_import_rows.append(row_for_new)
        # Any remaining quantity beyond new_qty is implicitly skipped (the
        # user owns enough already — for import_delta only; import_new with
        # the full qty contributes 0 to skipped_count).
        skipped_count += max(0, quantity_needed - form_new_qty)

    # Run the existing import path for everything that didn't get skipped.
    imported_count = 0
    total_quantity = 0
    failed_rows: list[dict] = []
    batch_id = None
    imported_row_ids: list[int] = []

    if new_import_rows:
        result = persist_import_rows(session, new_import_rows, filename=filename, user_id=user_id)
        imported_count = result["imported_count"]
        total_quantity = result.get("total_quantity", imported_count)
        failed_rows = result["failed_rows"]
        batch_id = result["batch_id"]
        imported_row_ids = result.get("imported_row_ids", [])

        # Place at destination if one was selected. target_location_id == 0
        # (auto-sort) skips placement — the route's drawer-sorter logic
        # handles those rows via resort_collection on the parent flow.
        if imported_row_ids and target_location_id > 0:
            place_imported_rows(
                session,
                imported_row_ids,
                user_id=user_id,
                location_id=target_location_id,
            )

    # Always commit so the import_skipped TransactionLog entries land even
    # when there are no new imports (pure-skip case).
    session.commit()

    return {
        "imported_count": imported_count,
        "total_quantity": total_quantity,
        "skipped_count": skipped_count,
        "failed_rows": failed_rows,
        "stale_match_rows": stale_match_rows,
        "batch_id": batch_id,
        "imported_row_ids": imported_row_ids,
    }


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
    language: list[str] = Form([]),
    location_type: list[str] = Form([]),
    role: list[str] = Form([]),
    tags: list[str] = Form([]),
    is_proxy: list[str] = Form([]),
    target_location_id: int = Form(0),
    reconcile_action: list[str] = Form([]),
    reconcile_move_qty: list[str] = Form([]),
    reconcile_new_qty: list[str] = Form([]),
    location_choice_name: list[str] = Form([]),
    location_choice_id: list[str] = Form([]),
    location_choice_type: list[str] = Form([]),
    auto_create_confirm: str = Form("no"),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    rows = _parsed_rows_from_form(
        line_number,
        name,
        scryfall_id,
        set_code,
        collector_number,
        finish,
        quantity,
        location,
        language,
        location_type,
        role,
        tags,
        is_proxy,
    )

    placed_in = None
    placed_in_url = "/pending"
    placed_in_kind = None
    moved_count = 0
    stale_match_rows: list[dict] = []

    # v3.30.15 — per-row Location resolution.
    #
    # Build the line_number → resolved_StorageLocation.id map from the
    # parallel choice arrays surfaced by the preview step. If the user
    # asked to auto-create one or more missing locations but did not check
    # the confirm box, abort the batch with NO writes and re-render the
    # preview with an explicit error (per Decision 3).
    #
    # If the resulting map is non-empty, take the v3.30.15 resolution path:
    # rows with a resolved Location land directly placed; rows without
    # (blank Location, or user opted-out per-name) fall through to the
    # existing target_location_id / drawer-sorter behavior for the unresolved
    # rows only. The reconciliation paths are bypassed in this case — the
    # CSV is treated as carrying its own destination semantics.
    # v3.30.16 — _build_line_to_location_map may raise ValueError when an
    # auto-create choice carries an invalid type (e.g. tampered form field
    # with location_choice_type="dungeon"). Caught here and routed back
    # through the same preview re-render shape as the not-confirmed path,
    # surfacing the failure to the user without crashing the request.
    try:
        line_to_location_id, needs_confirm_names, skipped_deck_conflicts = (
            _build_line_to_location_map(
                session,
                current_user.id,
                rows,
                location_choice_name,
                location_choice_id,
                auto_create_confirm,
                choice_types=location_choice_type,
            )
        )
        invalid_type_error: str | None = None
    except ValueError as exc:
        line_to_location_id = {}
        needs_confirm_names = []
        skipped_deck_conflicts = []
        invalid_type_error = str(exc)

    if invalid_type_error or needs_confirm_names:
        # Auto-create requested but not confirmed → reject batch, re-render
        # preview with the same row state + an explicit error message.
        # v3.30.16 — the same re-render shape also handles the
        # invalid-Location-Type-on-auto-create failure surfaced as a
        # ValueError above.
        distinct_loc_names = _distinct_locations_from_rows(rows)
        location_resolutions = resolve_location_names(session, current_user.id, distinct_loc_names)
        duplicate_counts = compute_duplicate_counts_for_resolved(
            session, current_user.id, rows, location_resolutions
        )
        if invalid_type_error:
            error_message = invalid_type_error
        else:
            error_message = (
                "Confirm the auto-create of new locations before importing, "
                "or change those rows' choices."
            )
        return render(
            request,
            "import_preview.html",
            {
                "title": "Import Preview",
                "valid_rows": rows,
                "invalid_rows": [],
                "format_name": "(re-confirmation needed)",
                "filename": filename,
                "current_user": current_user,
                "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
                "locations": list_locations(session, current_user.id),
                "decks": list_decks_basic(session, user_id=current_user.id),
                "location_resolutions": location_resolutions,
                "duplicate_counts": duplicate_counts,
                "auto_create_error": error_message,
            },
        )

    if line_to_location_id:
        result = persist_import_rows(
            session,
            rows,
            user_id=current_user.id,
            filename=filename,
            line_to_location_id=line_to_location_id,
        )

        # Run place_imported_rows per resolved location so newly-placed rows
        # merge with any existing placed copy at the same destination
        # (Decision 5 — merge behavior unchanged; reused via the established
        # place_imported_rows code path rather than duplicated inside
        # persist_import_rows).
        placed_by_loc = result.get("placed_row_ids_by_location") or {}
        for loc_id, row_ids_at_loc in placed_by_loc.items():
            if row_ids_at_loc:
                place_imported_rows(
                    session,
                    row_ids_at_loc,
                    user_id=current_user.id,
                    location_id=loc_id,
                )

        # Unresolved rows (no per-row Location) fall through to the existing
        # target_location_id / drawer-sorter behavior. Reconciliation paths
        # are deliberately bypassed in the v3.30.15 path — a CSV carrying
        # Location values is treated as carrying its own destination
        # semantics. Reconciliation remains intact for blank-Location CSVs.
        pending_row_ids = result.get("pending_row_ids") or []
        merged_count = 0
        if pending_row_ids and target_location_id:
            place_imported_rows(
                session,
                pending_row_ids,
                user_id=current_user.id,
                location_id=target_location_id,
            )
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
        elif pending_row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            resort_collection(session, user_id=current_user.id)
            return RedirectResponse(url="/pending", status_code=303)
        elif placed_by_loc:
            # All rows resolved per-row; surface the first resolved location
            # as the placed_in for the result page, with a "multiple
            # locations" hint when more than one was used.
            first_loc_id = next(iter(placed_by_loc.keys()))
            loc = get_location(session, location_id=first_loc_id, user_id=current_user.id)
            if len(placed_by_loc) == 1 and loc:
                placed_in = loc.name
                placed_in_url = f"/locations/{first_loc_id}"
                placed_in_kind = "deck" if loc.type == "deck" else "location"
            else:
                placed_in = f"{len(placed_by_loc)} locations"
                placed_in_url = "/collection"
                placed_in_kind = "multiple"

        return render(
            request,
            "import_result.html",
            {
                "title": "Import Results",
                "imported_count": result["imported_count"],
                "total_quantity": result.get("total_quantity", result["imported_count"]),
                "moved_count": moved_count,
                "merged_count": merged_count,
                "skipped_count": 0,
                "stale_match_rows": stale_match_rows,
                "failed_rows": result["failed_rows"],
                "batch_id": result["batch_id"],
                "placed_in": placed_in,
                "placed_in_url": placed_in_url,
                "placed_in_kind": placed_in_kind,
                "skipped_deck_conflicts": skipped_deck_conflicts,
                "current_user": current_user,
            },
        )

    # 3-branch dispatch (no per-row resolution):
    #  (a) deck destination + reconciliation → deck per-row helper
    #  (b) non-deck destination + reconciliation → collection per-row helper
    #  (c) no reconciliation fields → existing path, byte-identical
    deck = _deck_for_storage_location(session, current_user.id, target_location_id)
    has_reconciliation = any(reconcile_action)
    skipped_count = 0

    if deck is not None and has_reconciliation:
        result = _commit_deck_import_with_reconciliation(
            session=session,
            user_id=current_user.id,
            deck=deck,
            parsed_rows=rows,
            actions=reconcile_action,
            move_qtys=[int(q or 0) for q in reconcile_move_qty],
            new_qtys=[int(q or 0) for q in reconcile_new_qty],
            filename=filename,
        )
        moved_count = result["moved_count"]
        merged_count = result.get("merged_count", 0)
        stale_match_rows = result["stale_match_rows"]
        placed_in = deck.name
        placed_in_url = f"/locations/{target_location_id}"
        placed_in_kind = "deck"
    elif deck is None and has_reconciliation:
        result = _commit_collection_import_with_reconciliation(
            session=session,
            user_id=current_user.id,
            target_location_id=target_location_id,
            parsed_rows=rows,
            actions=reconcile_action,
            new_qtys=[int(q or 0) for q in reconcile_new_qty],
            filename=filename,
        )
        merged_count = 0
        skipped_count = result.get("skipped_count", 0)
        stale_match_rows = result["stale_match_rows"]
        row_ids = result.get("imported_row_ids", [])

        if target_location_id:
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
            # No resort here: an explicit destination (box/binder/other or a
            # deck) was chosen, so the cards belong THERE. Running the drawer
            # sorter would yank them straight back out into the drawers. The
            # sorter only runs on the "Auto-sort to drawers" path (the elif
            # below, where no target_location_id was selected).
        elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            resort_collection(session, user_id=current_user.id)
            return RedirectResponse(url="/pending", status_code=303)
    else:
        result = persist_import_rows(session, rows, filename=filename, user_id=current_user.id)
        merged_count = 0
        row_ids = result.get("imported_row_ids", [])

        if row_ids and target_location_id:
            place_imported_rows(
                session, row_ids, user_id=current_user.id, location_id=target_location_id
            )
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
            # No resort here: an explicit destination (box/binder/other or a
            # deck) was chosen, so the cards belong THERE. Running the drawer
            # sorter would yank them straight back out into the drawers. The
            # sorter only runs on the "Auto-sort to drawers" path (the elif
            # below, where no target_location_id was selected).

        elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            resort_collection(session, user_id=current_user.id)
            return RedirectResponse(url="/pending", status_code=303)

    return render(
        request,
        "import_result.html",
        {
            "title": "Import Results",
            "imported_count": result["imported_count"],
            "total_quantity": result.get("total_quantity", result["imported_count"]),
            "moved_count": moved_count,
            "merged_count": merged_count,
            "skipped_count": skipped_count,
            "stale_match_rows": stale_match_rows,
            "failed_rows": result["failed_rows"],
            "batch_id": result["batch_id"],
            "placed_in": placed_in,
            "placed_in_url": placed_in_url,
            "placed_in_kind": placed_in_kind,
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
    language: str = Form("en"),
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
            "language": normalize_language(language),
            "set_code": set_code,
            "collector_number": collector_number,
            "current_user": current_user,
            "use_drawer_sorter": current_user.username in DRAWER_SORTER_USERNAMES,
            "locations": list_locations(session, current_user.id),
            "decks": list_decks_basic(session, user_id=current_user.id),
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


@app.post("/import/manual/reconcile-preview")
async def manual_import_reconcile_preview(
    request: Request,
    target_location_id: int = Form(0),
    scryfall_id: str = Form(""),
    set_code: str = Form(""),
    collector_number: str = Form(""),
    finish: str = Form("normal"),
    quantity: int = Form(1),
    language: str = Form("en"),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """HTMX endpoint for the single-card (manual) import preview.

    Dispatch shape mirrors /import/reconcile-preview (deck vs non-deck),
    but for the manual flow we always set `manual_mode=True` in the
    collection-mode render context. That flips the action-select default
    to `import_new` (acquisition semantics) instead of `skip_already_owned`,
    per design doc §5.5: manual single-card entries are usually
    acquisitions, not sync operations.

    For deck destinations the manual flow uses the same defaults as the
    CSV flow — manual_mode is collection-mode-only.
    """
    parsed_rows = [
        {
            "line_number": 1,
            "scryfall_id": scryfall_id,
            "set_code": set_code,
            "collector_number": collector_number,
            "finish": normalize_finish(finish),
            "quantity": max(1, quantity),
            "location": "",
            "name": "",
            "language": normalize_language(language),
        }
    ]

    def _decorate_single(rows: list[dict]) -> None:
        if rows and rows[0].get("card_id"):
            c = session.query(Card.name).filter(Card.id == rows[0]["card_id"]).first()
            rows[0]["display_name"] = c.name if c else scryfall_id
        elif rows:
            rows[0]["display_name"] = scryfall_id

    deck = _deck_for_storage_location(session, current_user.id, target_location_id)

    if deck is not None:
        matches_rows = find_inventory_matches_for_deck_import(
            session, current_user.id, deck.id, parsed_rows
        )
        _decorate_single(matches_rows)

        total_to_move = sum(r["recommended_move_qty"] for r in matches_rows)
        total_to_merge = sum(
            r["recommended_new_qty"] for r in matches_rows if r["total_in_target_deck"] > 0
        )
        total_to_import_new = sum(r["recommended_new_qty"] for r in matches_rows) - total_to_merge

        return render(
            request,
            "_import_reconciliation.html",
            {
                "reconcile_mode": "deck",
                "rows": matches_rows,
                "deck_name": deck.name,
                "total_to_move": total_to_move,
                "total_to_import_new": total_to_import_new,
                "total_to_merge": total_to_merge,
            },
        )

    # Non-deck destination — collection mode with manual_mode=True.
    matches_rows = find_inventory_matches_for_collection_import(
        session, current_user.id, parsed_rows
    )
    _decorate_single(matches_rows)
    _annotate_collection_dupes(matches_rows)

    total_to_skip = 0
    total_to_delta = 0
    total_to_new = 0
    for r in matches_rows:
        action = r["recommended_action"]
        if action == "skip_already_owned":
            total_to_skip += r["quantity_needed"]
        elif action == "import_delta":
            total_to_delta += r["recommended_new_qty"]
            total_to_skip += r["quantity_needed"] - r["recommended_new_qty"]
        else:
            total_to_new += r["recommended_new_qty"]

    has_deck_only_dupes = any(r.get("is_deck_only_dupe") for r in matches_rows)

    destination_name: str | None = None
    if target_location_id > 0:
        loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
        if loc is not None:
            destination_name = loc.name

    return render(
        request,
        "_import_reconciliation.html",
        {
            "reconcile_mode": "collection",
            "rows": matches_rows,
            "deck_name": destination_name,
            "total_to_skip": total_to_skip,
            "total_to_delta": total_to_delta,
            "total_to_new": total_to_new,
            "manual_mode": True,  # default flips to import_new
            "has_deck_only_dupes": has_deck_only_dupes,
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
    language: str = Form("en"),
    target_location_id: int = Form(0),
    reconcile_action: list[str] = Form([]),
    reconcile_move_qty: list[str] = Form([]),
    reconcile_new_qty: list[str] = Form([]),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    parsed_rows = [
        {
            "line_number": 1,
            "scryfall_id": scryfall_id,
            "set_code": set_code,
            "collector_number": collector_number,
            "finish": normalize_finish(finish),
            "quantity": max(1, quantity),
            "location": "",
            "name": "",
            "language": normalize_language(language),
        }
    ]

    placed_in = None
    placed_in_url = "/pending"
    placed_in_kind = None
    moved_count = 0
    stale_match_rows: list[dict] = []

    deck = _deck_for_storage_location(session, current_user.id, target_location_id)
    has_reconciliation = any(reconcile_action)
    skipped_count = 0

    if deck is not None and has_reconciliation:
        result = _commit_deck_import_with_reconciliation(
            session=session,
            user_id=current_user.id,
            deck=deck,
            parsed_rows=parsed_rows,
            actions=reconcile_action,
            move_qtys=[int(q or 0) for q in reconcile_move_qty],
            new_qtys=[int(q or 0) for q in reconcile_new_qty],
            filename="manual import",
        )
        moved_count = result["moved_count"]
        merged_count = result.get("merged_count", 0)
        stale_match_rows = result["stale_match_rows"]
        placed_in = deck.name
        placed_in_url = f"/locations/{target_location_id}"
        placed_in_kind = "deck"
    elif deck is None and has_reconciliation:
        result = _commit_collection_import_with_reconciliation(
            session=session,
            user_id=current_user.id,
            target_location_id=target_location_id,
            parsed_rows=parsed_rows,
            actions=reconcile_action,
            new_qtys=[int(q or 0) for q in reconcile_new_qty],
            filename="manual import",
        )
        merged_count = 0
        skipped_count = result.get("skipped_count", 0)
        stale_match_rows = result["stale_match_rows"]
        row_ids = result.get("imported_row_ids", [])
        if target_location_id:
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
            # No resort here: an explicit destination (box/binder/other or a
            # deck) was chosen, so the cards belong THERE. Running the drawer
            # sorter would yank them straight back out into the drawers. The
            # sorter only runs on the "Auto-sort to drawers" path (the elif
            # below, where no target_location_id was selected).
        elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            resort_collection(session, user_id=current_user.id)
    else:
        result = persist_import_rows(
            session, parsed_rows, filename="manual import", user_id=current_user.id
        )
        merged_count = 0
        row_ids = result.get("imported_row_ids", [])
        if row_ids and target_location_id:
            place_imported_rows(
                session, row_ids, user_id=current_user.id, location_id=target_location_id
            )
            loc = get_location(session, location_id=target_location_id, user_id=current_user.id)
            placed_in = loc.name if loc else None
            placed_in_url = f"/locations/{target_location_id}" if loc else "/pending"
            placed_in_kind = ("deck" if loc.type == "deck" else "location") if loc else None
            # No resort here: an explicit destination (box/binder/other or a
            # deck) was chosen, so the cards belong THERE. Running the drawer
            # sorter would yank them straight back out into the drawers. The
            # sorter only runs on the "Auto-sort to drawers" path (the elif
            # below, where no target_location_id was selected).
        elif row_ids and current_user.username in DRAWER_SORTER_USERNAMES:
            resort_collection(session, user_id=current_user.id)

    return render(
        request,
        "import_result.html",
        {
            "title": "Import Results",
            "imported_count": result["imported_count"],
            "total_quantity": result.get("total_quantity", result["imported_count"]),
            "moved_count": moved_count,
            "merged_count": merged_count,
            "skipped_count": skipped_count,
            "stale_match_rows": stale_match_rows,
            "failed_rows": result["failed_rows"],
            "batch_id": result["batch_id"],
            "placed_in": placed_in,
            "placed_in_url": placed_in_url,
            "placed_in_kind": placed_in_kind,
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


@app.post("/inventory/rows/{row_id}/toggle-proxy")
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
    # "mana pips don't filter" bug. Joining the list below makes both
    # producers converge on the same joined string the rest of the
    # pipeline already expects.
    colors: list[str] = Query(default=[]),
    types: list[str] = Query(default=[]),
    status: list[str] = Query(default=[]),
    finishes: list[str] = Query(default=[]),
    price_min: str = "",
    price_max: str = "",
    view: str = "grid",
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    per_page = 50

    # v3.31.0 — collapse the repeated facet params into the single
    # joined-token form the downstream query + facet-set construction
    # expect. Colors are letter tokens joined with no separator ("WU");
    # the CSV facets join with commas. Works whether each list element
    # is an individual checkbox value ("W", "Creature") or an already-
    # joined token from the toolbar/pagination ("WU", "Creature,Instant").
    colors = "".join(c.strip() for c in colors if c.strip())
    types = ",".join(t.strip() for t in types if t.strip())
    status = ",".join(s.strip() for s in status if s.strip())
    finishes = ",".join(f.strip() for f in finishes if f.strip())

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

    # v3.27.19 — when count-sorting, run the name-level owned-count
    # aggregation ONCE up front. Pass the dict into list_inventory_rows
    # for the sort key AND into the template for three-level group
    # header rendering (name → printing → location). Single GROUP BY
    # query per page load — the spec's N+1 failure mode is the
    # per-InventoryRow count, not a single pre-computed pass.
    owned_counts: dict[str, int] | None = None
    if sort == "count":
        owned_counts = name_owned_counts(session, current_user.id)

    # v3.28.8 — parse facet params. Price floats are tolerant: empty / non-
    # numeric → None (skip facet). View is whitelisted.
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
    view_mode = "rows" if view == "rows" else "grid"

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
        owned_counts=owned_counts,
        facet_colors=colors,
        facet_types=types,
        facet_status=status,
        facet_finishes=finishes,
        facet_price_min=facet_price_min,
        facet_price_max=facet_price_max,
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
        location_id=location_id if selected_location and selected_location.type != "drawer" else 0,
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
    # v3.30.16 — expanded schema. The first six columns are UNCHANGED in
    # name, order, and content (downstream parsers consuming the v3.30.15
    # 6-column shape continue to work). Five new columns appended at end:
    # Location Type / Language / Role / Tags / Is Proxy. The importer
    # recognizes the new headers via HEADER_ALIASES (case + space
    # tolerant); old 6-column CSVs round-trip as before with the new
    # fields defaulted on re-import.
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
        loc = row.storage_location
        writer.writerow(
            [
                card.name or "",
                (card.set_code or "").upper(),
                card.collector_number or "",
                row.finish or "normal",
                row.quantity,
                loc.name if loc else "",
                loc.type if loc else "",
                row.language or "en",
                row.role or "",
                row.tags or "",
                "true" if row.is_proxy else "false",
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


@app.post("/pending/confirm")
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


@app.post("/decks/create-inline")
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


@app.post("/locations/create-inline")
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


@app.post("/locations/{location_id}/delete")
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


@app.post("/locations/{location_id}/edit")
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


@app.post("/locations/{location_id}/bulk-delete-preview")
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


@app.post("/locations/{location_id}/bulk-delete-commit")
def bulk_delete_location_commit(
    location_id: int,
    row_ids: list[int] = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    bulk_delete_inventory_rows(session, row_ids=row_ids, user_id=current_user.id)
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
            "current_user": current_user,
            "locations": all_locations,
            "decks": decks,
            "showcases": showcases,
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
                location.name,
                location.type or "",
                row.language or "en",
                row.role or "",
                row.tags or "",
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

    reverse = direction == "desc"
    if sort == "type":
        deck_query = deck_query.order_by(Card.type_line.desc() if reverse else Card.type_line.asc())
    elif sort == "cmc":
        deck_query = deck_query.order_by(Card.cmc.desc() if reverse else Card.cmc.asc())
    elif sort == "value":
        # Computed in Python after fetch (price is a per-finish attribute)
        deck_rows = deck_query.all()
        deck_rows.sort(key=lambda r: effective_price(r.card, r.finish) or 0.0, reverse=reverse)
    else:
        deck_query = deck_query.order_by(Card.name.desc() if reverse else Card.name.asc())

    if sort != "value":
        deck_rows = deck_query.all()

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


@app.get("/decks/{deck_id}")
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


@app.post("/account/deck-view-pref")
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


@app.get("/decks/{deck_id}/cards-partial")
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

    # v3.27.9: combos + bracket no longer compute on the panels endpoint.
    # compute_deck_combos issued a Spellbook /find-my-combos POST per deck
    # (request-path network invariant violation surfaced on /decks cold load;
    # see roadmap.md Deferred / latent items "Deck Analytics Rebuild").
    # Synergy + dead-cards still render: synergy reads combos as a dict but
    # tolerates an empty .included gracefully (loses the "Direct via combo
    # membership" classification path, keeps tribal / payoff / engine paths).
    # tokens / synergy / dead_cards are all local computations against the
    # bulk Scryfall cache and stay on the request path. compute_deck_combos
    # / compute_deck_bracket / bracket_v2_service are dormant code — left
    # importable for the analytics rebuild to reuse.
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


@app.post("/decks/{deck_id}/bulk-delete-preview")
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


@app.post("/decks/{deck_id}/bulk-delete-commit")
def bulk_delete_deck_commit(
    deck_id: int,
    row_ids: list[int] = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    bulk_delete_inventory_rows(session, row_ids=row_ids, user_id=current_user.id)
    return RedirectResponse(f"/decks/{deck_id}", status_code=303)


@app.post("/decks/{deck_id}/edit")
def decks_edit(
    deck_id: int,
    name: str = Form(...),
    format_name: str = Form(""),
    notes: str = Form(""),
    blurb: str = Form(""),
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


@app.get("/decks/api/card-autocomplete")
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


@app.post("/decks/{deck_id}/add-card")
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


@app.get("/decks/{deck_id}/rows/{row_id}/printings-modal")
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


@app.post("/decks/{deck_id}/rows/{row_id}/switch-printing")
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


@app.post("/decks/{deck_id}/rows/{row_id}/bump-qty")
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
        resort_collection(session, user_id=current_user.id)

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


@app.post("/decks/{deck_id}/rows/{row_id}/review-tag")
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

    # § III — Prices (static; 1d/7d/30d deltas DEFERRED).
    prices = {
        "regular": target_card.price_usd,
        "foil": target_card.price_usd_foil,
        "etched": target_card.price_usd_etched,
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
            "legality_map": legality_map,
            "history_rows": history_rows,
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


@app.post("/decks/{deck_id}/tokens/auto-add")
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
    # v3.29.0 — picker scopes to the user's playgroup co-members via
    # ``playgroup_service.get_pickable_users``. C2 transition fallback:
    # when the user has no co-members (no playgroups yet, or alone in a
    # solo playgroup), the wrapper returns the global active-user list
    # — preserves pre-v3.29.0 behavior for users who haven't joined any
    # playgroup. The shared primitive ``co_members_of`` (consumed by
    # v3.29.1 sharing / v3.29.2 trading) does NOT carry this fallback —
    # only the people-picker does.
    from app import playgroup_service

    all_users = playgroup_service.get_pickable_users(session, current_user.id)
    all_decks = session.query(Deck).order_by(Deck.name).all()
    # JSON-safe: users list and deck lookup by user_id for JS filtering
    users_json = [{"id": u.id, "name": u.display_name or u.username} for u in all_users]
    decks_by_user_json = {}
    for d in all_decks:
        decks_by_user_json.setdefault(str(d.user_id), []).append({"id": d.id, "name": d.name})
    # v3.32.0 — optional playgroup link picker. Linking a game to a playgroup
    # lets every member view it (read-only). Only the user's own playgroups
    # are offered. Empty list → the template hides the picker.
    user_playgroups = playgroup_service.list_playgroups_for_user(session, current_user.id)
    return render(
        request,
        "game_new.html",
        {
            "title": "New Game",
            "users_json": users_json,
            "decks_by_user_json": decks_by_user_json,
            "user_playgroups": user_playgroups,
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
    user_ids: list[str] = Form(default=[]),
    grid_positions: list[str] = Form(default=[]),
    starting_life: int = Form(40),
    first_seat_number: int | None = Form(None),
    playgroup_id: str = Form(""),
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
        # v3.27.5 — seat→user attribution. ``user_ids`` has been submitted
        # by game_new.html since well before this patch but was silently
        # dropped by the route handler (the bug surfaced in v3.25.1 recon).
        # Parse as nullable int; invalid / absent / unauthorized values
        # resolve to None and the seat ships unattributed — game creation
        # never fails over an attribution problem (mirrors the v3.25.1
        # first_seat_number non-blocking philosophy). Validation that the
        # id refers to a real User happens inside ``_capture_user_attribution``
        # in game_service.py — same pattern as deck_id validation, and same
        # cross-user permissive stance (a seat may legitimately reference
        # another user's account, matching the existing all-decks dropdown
        # precedent in game_new.html).
        uid_raw = user_ids[i] if i < len(user_ids) else ""
        try:
            user_id = int(uid_raw) if uid_raw else None
        except ValueError:
            user_id = None
        pos_raw = grid_positions[i].strip() if i < len(grid_positions) else ""
        seats.append(
            {
                "player_name": name or f"Player {i + 1}",
                "deck_id": deck_id,
                "user_id": user_id,
                "starting_life": starting_life,
                "grid_position": pos_raw or None,
            }
        )

    # First-player pick is optional and non-critical: an absent or
    # out-of-range value falls back to None so the game tracker keeps its
    # existing clockwise-seat default rather than blocking game creation.
    fsn = first_seat_number
    if fsn is not None and not (1 <= fsn <= player_count):
        fsn = None

    # v3.27.0 — collision-proof localStorage key namespace. Generated
    # server-side exactly once per game and never regenerated. Pairs with
    # the bare ``games.id`` rowid (which SQLite reuses after a game is
    # deleted) to form ``mana-game-${gameId}-${clientToken}`` in the
    # tracker, so a recycled id cannot resurface a deleted game's saved
    # state. Key-only — NOT added to the saved-state blob; the
    # gameFingerprint() (``_fp``) value stays unchanged.
    # v3.27.2 — format normalization. Trim + case-fold + match against
    # CANONICAL_GAME_FORMATS; unknown / empty / form-tampered values
    # resolve to DEFAULT_GAME_FORMAT (Commander). Game creation must
    # never fail due to a format problem, matching the v3.25.1 non-
    # blocking philosophy for first_seat_number.
    canonical_format = normalize_game_format(format)

    game = create_game(
        session,
        user_id=current_user.id,
        format=canonical_format,
        seats=seats,
        first_seat_number=fsn,
        client_token=secrets.token_urlsafe(8),
    )
    # v3.32.0 — optional playgroup link. set_game_playgroup validates the
    # owner is a member of the target playgroup; a bad / non-member / empty
    # value simply leaves the game private (non-blocking, mirroring the
    # first_seat_number / format philosophy).
    pg_raw = playgroup_id.strip()
    if pg_raw:
        try:
            set_game_playgroup(session, game.id, current_user.id, int(pg_raw))
        except ValueError:
            pass
    return RedirectResponse(f"/games/{game.id}", status_code=303)


@app.get("/games/{game_id}")
def game_detail_page(
    request: Request,
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    # v3.32.0 — viewer-scoped: owner, seat-attributed players, and members of
    # a linked playgroup may all view. Mutation controls stay owner-only,
    # gated on ``is_owner`` in the template.
    game = get_viewable_game(session, game_id, current_user.id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    is_owner = game.user_id == current_user.id
    seat_commander_images = get_seat_commander_image_urls(session, game)
    # Owner-only controls need supporting data; participants get none of it.
    decks: list[Deck] = []
    pickable_users: list[User] = []
    user_playgroups: list[dict] = []
    if is_owner:
        from app import playgroup_service

        decks = (
            session.query(Deck).filter(Deck.user_id == current_user.id).order_by(Deck.name).all()
        )
        # People picker for retroactive seat→user attribution + playgroup
        # picker to open the game up to a group.
        pickable_users = playgroup_service.get_pickable_users(session, current_user.id)
        user_playgroups = playgroup_service.list_playgroups_for_user(session, current_user.id)
    return render(
        request,
        "game_detail.html",
        {
            "title": f"Game {game_id}",
            "game": game,
            "decks": decks,
            "is_owner": is_owner,
            "pickable_users": pickable_users,
            "user_playgroups": user_playgroups,
            "current_user": current_user,
            "seat_commander_images": seat_commander_images,
        },
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


@app.post("/games/{game_id}/seats/{seat_id}/art-toggle")
def game_seat_art_toggle(
    request: Request,
    game_id: int,
    seat_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Flip ``GameSeat.art_background_hidden`` for a single seat (v3.26.6).

    Per-seat opt-out for the v3.26.1 commander art panel background.
    Ownership enforced via :func:`toggle_seat_art_background` — game must
    belong to ``current_user`` and the seat must be on that game; either
    miss → 404.

    Returns 303 back to the game detail page; the v3.26.1 art-rendering
    JS reads the new value from the freshly-rendered ``seatDefs`` array
    on the next page paint.
    """
    new_value = toggle_seat_art_background(session, game_id, seat_id, current_user.id)
    if new_value is None:
        raise HTTPException(status_code=404, detail="Game or seat not found")
    return RedirectResponse(url=f"/games/{game_id}", status_code=303)


@app.post("/games/{game_id}/seats/{seat_id}/assign-user")
def game_seat_assign_user(
    request: Request,
    game_id: int,
    seat_id: int,
    user_id: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Owner-only: retroactively attribute (or clear) a seat's user (v3.32.0).

    Fixes name-only seats — e.g. a Draft game recorded with free-text player
    names but no linked accounts. Empty / ``0`` / invalid ``user_id`` clears
    the attribution (back to name-only). Ownership + seat membership enforced
    in :func:`reassign_seat_user`; either miss → 404. Once a seat is attributed
    to a user, that user can view the game (hybrid visibility).
    """
    uid_raw = user_id.strip()
    try:
        target_user_id = int(uid_raw) if uid_raw else None
    except ValueError:
        target_user_id = None
    result = reassign_seat_user(session, game_id, seat_id, current_user.id, target_user_id)
    if result is None or result is False:
        raise HTTPException(status_code=404, detail="Game or seat not found")
    return RedirectResponse(url=f"/games/{game_id}", status_code=303)


@app.post("/games/{game_id}/playgroup")
def game_set_playgroup(
    request: Request,
    game_id: int,
    playgroup_id: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Owner-only: link the game to a playgroup, or clear the link (v3.32.0).

    Linking opens the game to every member of that playgroup (read-only).
    Empty value clears the link. :func:`set_game_playgroup` enforces that the
    caller owns the game AND is a member of the target playgroup; a violation
    → 404 (non-leaky, matching the game-not-found path).
    """
    pg_raw = playgroup_id.strip()
    try:
        target_pg_id = int(pg_raw) if pg_raw else None
    except ValueError:
        target_pg_id = None
    if not set_game_playgroup(session, game_id, current_user.id, target_pg_id):
        raise HTTPException(status_code=404, detail="Game or playgroup not found")
    return RedirectResponse(url=f"/games/{game_id}", status_code=303)


@app.post("/games/{game_id}/notes")
def game_update_notes(
    request: Request,
    game_id: int,
    notes: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Update ``Game.notes`` independent of finalization state (v3.26.0).

    Lets users revise notes after a game is finalized without touching
    placements/turn_count — :func:`end_game` couples notes to those fields
    and would clobber recorded results.

    Redirect target is referer-based via :func:`safe_redirect_url` so the
    games-list modal returns the user to ``/games``; the game-detail
    fallback default preserves prior behavior when Referer is missing or
    invalid.
    """
    game = get_game(session, game_id, current_user.id)
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")
    update_game_notes(session, game_id, current_user.id, notes)
    return RedirectResponse(
        url=safe_redirect_url(request, default=f"/games/{game_id}"), status_code=303
    )


@app.post("/games/{game_id}/delete")
def game_delete(
    game_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    delete_game(session, game_id, current_user.id)
    return RedirectResponse("/games", status_code=303)


# v3.27.12 — Watchlist. A per-user list of cards the user wants to track.
# Two identity modes (XOR-shaped per row): printing-specific (card_id) or
# printing-agnostic (card_name). Service-layer XOR enforcement; partial-
# unique indexes from the v3.27.12 migration enforce one-row-per-identity.
# v1 add-flow is card-detail-only — broader add surfaces (collection card
# actions, manual name autocomplete on /watchlist) are deferred.
@app.get("/watchlist")
def watchlist_page(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
):
    items = list_watchlist(session, current_user.id)
    return render(
        request,
        "watchlist.html",
        {
            "title": "Watchlist",
            "items": items,
            "current_user": current_user,
        },
    )


# ---------------------------------------------------------------------------
# Decklist collection check (v3.27.19)
# ---------------------------------------------------------------------------


@app.get("/decklist")
def decklist_page(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Empty paste form. v3.27.19 — stateless; the pasted list isn't
    persisted (no saved wantlists in v1; that's a possible follow-up)."""
    return render(
        request,
        "decklist.html",
        {
            "title": "Decklist Check",
            "current_user": current_user,
            "submitted": False,
        },
    )


@app.post("/decklist")
def decklist_check(
    request: Request,
    decklist: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Parse the pasted decklist, match against the user's owned
    inventory locally (NEVER touches Scryfall — request-path network
    invariant), and render the buckets.

    Re-uses the import flow's ``_parse_list_line`` so a list that
    imports cleanly via ``/import`` matches cleanly here (matching
    parity per spec). The owned-count aggregation is one indexed
    GROUP BY query; per-printing detail is one batched fetch.
    """
    name_entries, short_entries = parse_decklist_text(decklist)
    # Resolve any short-form (SET COLLECTOR [qty]) lines via local Card
    # lookup so they slot into the normal name-aggregated pipeline
    # alongside paste lines that carried names. NEVER calls Scryfall.
    resolved, unresolved_short = resolve_short_form_lines(session, short_entries)
    # Merge resolved-by-short-form into name_entries, summing quantities
    # when the same name appears in both (e.g. user pasted "4x Sol Ring"
    # and "CMR 333" with quantity 1 — total wanted = 5).
    merged: dict[str, dict] = {}
    for e in name_entries + resolved:
        key = e["name"].lower()
        if key in merged:
            merged[key]["quantity"] += e["quantity"]
            merged[key]["line_numbers"].extend(e.get("line_numbers", []))
        else:
            merged[key] = dict(e)
    all_entries = list(merged.values())

    # Single GROUP BY aggregation against the user's full inventory,
    # narrowed to the decklist's names. Returns a name→total-owned dict
    # for O(1) application-side lookup. Stress-tested at 20–50k rows
    # (see release-history entry for measurement details).
    owned_names = [e["name"] for e in all_entries]
    owned_counts = name_owned_counts(session, current_user.id, names=owned_names)
    # Per-row detail fetch for the matched names (single batched query
    # with LEFT JOIN to StorageLocation; sorted tradeable-first inside
    # the service helper).
    owned_detail = owned_inventory_for_names(session, current_user.id, owned_names)

    buckets = bucket_decklist_results(all_entries, owned_counts, owned_detail)

    return render(
        request,
        "decklist.html",
        {
            "title": "Decklist Check",
            "current_user": current_user,
            "submitted": True,
            "raw_decklist": decklist,
            "have": buckets["have"],
            "partial": buckets["partial"],
            "missing": buckets["missing"],
            "basics": buckets["basics"],
            "unresolved_short": unresolved_short,
            "total_unique": len(all_entries),
            "total_wanted": sum(e["quantity"] for e in all_entries),
        },
    )


@app.post("/watchlist/add")
def watchlist_add(
    request: Request,
    card_id: str = Form(""),
    card_name: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Add a watchlist entry. Form posts EITHER card_id OR card_name.

    Empty / blank inputs are treated as not-provided so the XOR check
    in ``add_to_watchlist`` can validate correctly. Duplicate-watch
    (IntegrityError from the partial-unique indexes) is caught and
    converted to a quiet redirect — the user has already expressed
    the intent to watch this card; no need to surface an error.
    """
    cid_int: int | None = None
    if card_id.strip():
        try:
            cid_int = int(card_id.strip())
        except ValueError:
            cid_int = None
    name_str: str | None = card_name.strip() or None
    note_str: str | None = note.strip() or None
    try:
        add_to_watchlist(
            session,
            current_user.id,
            card_id=cid_int,
            card_name=name_str,
            note=note_str,
        )
        session.commit()
    except IntegrityError:
        # Duplicate — partial-unique index hit. Already on the watchlist;
        # treat as a no-op.
        session.rollback()
    redirect_target = safe_redirect_url(request, default="/watchlist")
    return RedirectResponse(url=redirect_target, status_code=303)


@app.post("/watchlist/{watchlist_id}/delete")
def watchlist_delete(
    request: Request,
    watchlist_id: int,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    remove_from_watchlist(session, current_user.id, watchlist_id)
    session.commit()
    redirect_target = safe_redirect_url(request, default="/watchlist")
    return RedirectResponse(url=redirect_target, status_code=303)


@app.post("/watchlist/{watchlist_id}/note")
def watchlist_update_note(
    request: Request,
    watchlist_id: int,
    note: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    update_note(session, current_user.id, watchlist_id, note)
    session.commit()
    redirect_target = safe_redirect_url(request, default="/watchlist")
    return RedirectResponse(url=redirect_target, status_code=303)


@app.post("/watchlist/{watchlist_id}/target")
def watchlist_update_target_price(
    request: Request,
    watchlist_id: int,
    target_price: str = Form(""),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """v3.28.11 — Update the target_price on one watchlist row.

    Shape-mirrors ``/watchlist/{id}/note`` exactly: CSRF-protected POST,
    per-user scoped via the service layer, empty / non-numeric input
    clears the target. The form value arrives as a string because the
    user types it; ``update_target_price`` parses + stores as REAL.
    Redirect uses the same ``safe_redirect_url`` pattern.
    """
    update_target_price(session, current_user.id, watchlist_id, target_price)
    session.commit()
    redirect_target = safe_redirect_url(request, default="/watchlist")
    return RedirectResponse(url=redirect_target, status_code=303)


# v3.27.14 — Password recovery routes. Four routes total: request form,
# request POST (queues async email + neutral identical response for
# enumeration defense), reset form (validates token from URL), reset
# POST (re-validates, sets new password, marks token used).
#
# Security-critical contract — do NOT change these without re-reading
# app/password_reset_service.py:
# 1. POST /forgot-password is identical in response shape and timing
#    for registered vs unregistered emails (enumeration defense). The
#    email send is asynchronous via daemon thread; the request handler
#    never blocks on the Resend API call.
# 2. Tokens are hashed at rest (SHA-256). Only the raw token sees the
#    network — in the emailed link.
# 3. Rate-limited per-email AND per-IP. Exceeded limits silently drop
#    (still return the neutral response) — leaking rate-limit info
#    would itself be an enumeration oracle.
# 4. CSRF protection via the existing CsrfRequired dependency on every
#    POST.


def _client_ip_for(request: Request) -> str | None:
    """Best-effort client IP for rate-limiting.

    Prefers X-Forwarded-For (Cloudflare Tunnel sets it) → falls back
    to request.client.host. Used only for rate-limit keying — not for
    auth, not surfaced anywhere user-facing.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


@app.get("/forgot-password")
def forgot_password_page(request: Request):
    return render(request, "forgot_password.html", {"title": "Forgot password"})


@app.post("/forgot-password")
def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    session: Session = Depends(get_db_session),
    # Graceful CSRF on this public pre-auth form — see the
    # require_csrf_or_reissue docstring.
    csrf_token: str = Form(""),
):
    """Identical response shape for registered vs unregistered emails.

    See app/password_reset_service.py for the full security-critical
    rationale. The neutral response is rendered regardless of whether
    we find a user, queue a token, get rate-limited, or fail any
    intermediate check — only the side-effects differ.
    """
    reissue = require_csrf_or_reissue(
        request, csrf_token, "forgot_password.html", {"title": "Forgot password"}
    )
    if reissue is not None:
        return reissue

    email_clean = (email or "").strip().lower()
    client_ip = _client_ip_for(request)
    neutral_response = render(
        request,
        "forgot_password.html",
        {
            "title": "Forgot password",
            "submitted": True,
            "submitted_email": email_clean,
        },
    )

    # Rate limit silently — exceeded → still neutral response, no
    # token created, no email queued. Failed requests get logged
    # inside check_rate_limits via the printed warning if you want
    # to add one; for now silent drop is enough.
    if not check_rate_limits(email_clean, client_ip):
        print(
            f"[password-reset] rate-limited request for email={email_clean!r} " f"ip={client_ip!r}",
            flush=True,
        )
        return neutral_response

    # Look up user. Missing → just return neutral response (no token,
    # no email, no nothing). Found → create token + queue email.
    if email_clean and "@" in email_clean:
        user = session.query(User).filter(User.username == email_clean).first()
        if user is not None and user.is_active:
            raw_token = create_reset_token(session, user)
            session.commit()
            # Build the reset URL on the same base the request came in on.
            # Use request.url_for so we work behind the Cloudflare Tunnel
            # without hard-coding cartarch.com.
            reset_path = request.url_for("reset_password_page").include_query_params(
                token=raw_token
            )
            reset_url = str(reset_path)
            queue_reset_email(
                email=email_clean,
                reset_url=reset_url,
                expires_at=datetime.utcnow() + timedelta(minutes=30),
            )

    return neutral_response


@app.get("/reset-password")
def reset_password_page(
    request: Request,
    token: str = "",
    session: Session = Depends(get_db_session),
):
    """Validate the token from the URL; render the new-password form
    or the invalid/expired/used branch.

    Never 500, never blank. Invalid / expired / used → clear message
    page with a link to /forgot-password to request a new one.
    """
    token_row = find_valid_token(session, token)
    if token_row is None:
        return render(
            request,
            "reset_password.html",
            {
                "title": "Reset password",
                "invalid": True,
            },
        )
    return render(
        request,
        "reset_password.html",
        {
            "title": "Reset password",
            "invalid": False,
            "token": token,
        },
    )


@app.post("/reset-password")
def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    session: Session = Depends(get_db_session),
    # Graceful CSRF on this public pre-auth form — see the
    # require_csrf_or_reissue docstring. Re-render keeps the reset token so
    # the user can resubmit (the token is re-validated in the body anyway).
    csrf_token: str = Form(""),
):
    """Re-validate the token, set the new password, mark used.

    The re-validate step is essential — the token might have been used
    or expired between GET render and POST submit (different tab open
    for hours, etc.). Same find_valid_token call, same invalid-state
    branch on the rendered page.
    """
    reissue = require_csrf_or_reissue(
        request,
        csrf_token,
        "reset_password.html",
        {"title": "Reset password", "invalid": False, "token": token},
    )
    if reissue is not None:
        return reissue

    token_row = find_valid_token(session, token)
    if token_row is None:
        return render(
            request,
            "reset_password.html",
            {
                "title": "Reset password",
                "invalid": True,
            },
        )

    if password != password_confirm:
        return render(
            request,
            "reset_password.html",
            {
                "title": "Reset password",
                "invalid": False,
                "token": token,
                "error": "Passwords don't match.",
            },
        )

    strength_error = validate_password_strength(password)
    if strength_error:
        return render(
            request,
            "reset_password.html",
            {
                "title": "Reset password",
                "invalid": False,
                "token": token,
                "error": strength_error,
            },
        )

    user = session.query(User).filter(User.id == token_row.user_id).first()
    if user is None:
        # The token's user was deleted between issue and reset.
        # Same invalid-state page — don't leak account-existence info.
        return render(
            request,
            "reset_password.html",
            {
                "title": "Reset password",
                "invalid": True,
            },
        )

    user.password_hash = hash_password(password)
    consume_token(session, token_row)
    session.commit()

    return render(
        request,
        "reset_password.html",
        {
            "title": "Reset password",
            "done": True,
        },
    )
