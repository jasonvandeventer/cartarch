"""FastAPI route entrypoint for Mana Archive.

Routes are grouped by feature flow rather than alphabetically. User-owned
operations receive `current_user.id` at the route boundary and pass it into the
service layer.
"""

from __future__ import annotations

import html
import json
import os
import threading
import time
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.audit_service import log_transaction
from app.auth import hash_password, validate_password_strength
from app.dashboard_service import get_dashboard_data
from app.db import SessionLocal, checkpoint_and_dispose, init_db, shutdown_event
from app.deck_service import (
    find_inventory_matches_for_deck_import,
    list_decks_basic,
    pull_card_to_deck,
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
    safe_redirect_url,
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
    find_inventory_matches_for_collection_import,
    place_imported_rows,
    resort_collection,
)
from app.location_service import (
    get_location,
    list_locations,
)
from app.models import Card, Deck, InventoryRow, User
from app.password_reset_service import (
    check_rate_limits,
    consume_token,
    create_reset_token,
    find_valid_token,
    queue_reset_email,
)
from app.routes import (
    account,
    admin,
    auth,
    cards,
    collections,
    decks,
    drawers,
    games,
    goldfish,
    playgroups,
    sharing,
    trades,
)
from app.scryfall import (
    _bulk_data_loop,
    bulk_refresh_prices,
    fetch_card_by_scryfall_id,
    fetch_card_by_set_and_number,
    fetch_payloads_uncached,
    search_cards_by_name,
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
app.include_router(games.router)
app.include_router(decks.router)
app.include_router(collections.router)
app.include_router(cards.router)


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
            # v4-prep (pg-readiness NULL-ordering): the audit flagged this
            # work-queue ORDER BY for the SQLite-NULLs-first vs Postgres-NULLs-
            # last divergence. In fact Card.updated_at is NOT NULL (the
            # Mapped[datetime] annotation infers it; prod schema notnull=1, 0
            # NULL rows), so the divergence cannot occur today -- nulls_first()
            # is a DEFENSIVE pin: it makes the intended "stalest first" order
            # explicit and stays correct if the column is ever made nullable at
            # the Alembic baseline. No-op and neutral on SQLite either way.
            .order_by(Card.updated_at.asc().nulls_first())
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
                # v3.36.x — carry the v3.36.1 seam columns onto the working
                # `cards` row too, so the goldfish loyalty/defense auto-init
                # (which reads Card, not scryfall_cards) populates as cards
                # naturally re-refresh on the staleness cycle.
                card.loyalty = fresh.get("loyalty")
                card.defense = fresh.get("defense")
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
                # v3.36.x — carry the v3.36.1 seam columns (see price-refresh).
                card.loyalty = fresh.get("loyalty")
                card.defense = fresh.get("defense")
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


_LOYALTY_BACKFILL_BATCH = 75
_LOYALTY_BACKFILL_BUSY_SECONDS = 3  # between batches while work remains


def _run_loyalty_defense_backfill_batch(after_id: int) -> tuple[int, int]:
    """Backfill ``cards.loyalty`` / ``cards.defense`` for OWNED planeswalkers
    and battles that still lack them — the one-shot catch-up for collections
    that predate v3.36.1.

    The goldfish loyalty/defense auto-init reads the working ``cards`` table
    (via ``InventoryRow.card``), NOT the daemon-populated ``scryfall_cards``
    cache. The price-refresh / trait-backfill loops now carry these columns
    too, but only re-touch a card when it next goes stale; this loop
    populates the existing owned planeswalkers/battles immediately on deploy.

    PAGINATED BY ``Card.id`` (not by a "still NULL" cursor) so the pass
    always terminates: a DFC planeswalker whose loyalty lives on the back
    face normalizes to NULL and simply stays NULL — it is advanced past,
    never looped on. Selection still filters to rows that NEED a value so a
    converged collection re-scans cheaply on restart. Off the request path;
    one batched Scryfall lookup per batch (never per-row); commit per batch.
    Returns ``(rows_processed, new_after_id)``.

    v3.36.3 — fetches via ``fetch_payloads_uncached`` (a direct Scryfall
    lookup), NOT the cache-first ``bulk_refresh_prices``. The v3.36.2 version
    used the cache, which returns the cached row even when ``loyalty`` /
    ``defense`` have not been re-streamed into ``scryfall_cards`` yet — so on
    a deploy where the daily bulk backfill hasn't run, every fetch came back
    NULL and the backfill was a silent no-op. Reading the live source makes
    this authoritative regardless of cache freshness, and decouples the
    feature from the (lock-contention-prone) 2 GB bulk re-stream.
    """
    session = SessionLocal()
    try:
        pending = (
            session.query(Card)
            .join(InventoryRow, InventoryRow.card_id == Card.id)
            .filter(
                Card.id > after_id,
                # v4-prep (pg-readiness case-sensitivity): ilike, not like.
                # SQLite LIKE is case-insensitive for ASCII (so these matched
                # today); Postgres LIKE is case-SENSITIVE. type_line is canonical
                # title-case from Scryfall so PG LIKE would happen to work, but
                # ilike is the portable-intent form and is neutral on SQLite.
                (Card.type_line.ilike("%Planeswalker%") & (Card.loyalty == None))  # noqa: E711
                | (Card.type_line.ilike("%Battle%") & (Card.defense == None)),  # noqa: E711
            )
            .order_by(Card.id.asc())
            .limit(_LOYALTY_BACKFILL_BATCH)
            .distinct()
            .all()
        )
        if not pending:
            return 0, after_id
        # Advance the cursor unconditionally to the last id in this batch so a
        # batch whose fetch failed (or returned NULL) is never re-selected in
        # THIS pass — guarantees termination. Transient misses are retried on
        # the next process start, and the price-refresh loop is the backstop.
        new_after = pending[-1].id
        # v3.36.3 — cache-BYPASS fetch (authoritative for not-yet-streamed
        # seam columns). Off-request daemon, so the request-path invariant
        # against per-row Scryfall I/O is respected (this is batched + bounded).
        fresh_by_id = fetch_payloads_uncached([c.scryfall_id for c in pending])
        for card in pending:
            fresh = fresh_by_id.get(card.scryfall_id)
            if not fresh:
                continue
            if card.loyalty is None:
                card.loyalty = fresh.get("loyalty")
            if card.defense is None:
                card.defense = fresh.get("defense")
        session.commit()
        print(
            f"[loyalty-backfill] processed {len(pending)} cards (through id {new_after})",
            flush=True,
        )
        return len(pending), new_after
    except Exception as exc:
        session.rollback()
        print(f"[loyalty-backfill] error: {exc}", flush=True)
        # Advance past the attempted batch so a poison row can't wedge the pass.
        return 0, after_id
    finally:
        session.close()


def _loyalty_defense_backfill_loop() -> None:
    """One-shot catch-up: page through owned planeswalkers/battles once per
    process, then exit the thread. Ongoing coverage is the price-refresh /
    trait-backfill loops (which now carry loyalty/defense) plus request-path
    Card construction for new imports — so this does not need to run forever.
    """
    if shutdown_event.wait(45):  # after migrations/init; bail if stopping
        return
    after_id = 0
    while not shutdown_event.is_set():
        processed, after_id = _run_loyalty_defense_backfill_batch(after_id)
        if not processed:
            print("[loyalty-backfill] catch-up complete", flush=True)
            return
        if shutdown_event.wait(_LOYALTY_BACKFILL_BUSY_SECONDS):
            return


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
        (_loyalty_defense_backfill_loop, "loyalty-backfill"),
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
        # v3.33.0 — copies covered by a sibling variant deck (no move, no import).
        total_covered_by_variant = sum(r.get("variant_covered_qty", 0) for r in matches_rows)

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
                "total_covered_by_variant": total_covered_by_variant,
                "is_variant_group": deck.variant_group_id is not None,
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
        # v3.33.0 — copies covered by a sibling variant deck (no move, no import).
        total_covered_by_variant = sum(r.get("variant_covered_qty", 0) for r in matches_rows)

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
                "total_covered_by_variant": total_covered_by_variant,
                "is_variant_group": deck.variant_group_id is not None,
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
            f"[password-reset] rate-limited request for email={email_clean!r} ip={client_ip!r}",
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
