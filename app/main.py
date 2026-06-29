"""FastAPI route entrypoint for Mana Archive.

Routes are grouped by feature flow rather than alphabetically. User-owned
operations receive `current_user.id` at the route boundary and pass it into the
service layer.
"""

from __future__ import annotations

import hmac
import html
import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import hash_password, validate_password_strength
from app.dashboard_service import get_dashboard_data
from app.db import SessionLocal, checkpoint_and_dispose, init_db, shutdown_event
from app.decklist_service import (
    compare_entries_to_owned,
    parse_decklist_text,
    resolve_short_form_lines,
)
from app.dependencies import (
    DRAWER_SORTER_USERNAMES,
    CsrfRequired,
    client_ip_for,
    get_current_user,
    get_db_session,
    get_optional_current_user,
    log_auth_diagnostic,
    render,
    render_auth_page,
    require_preauth_csrf,
    safe_redirect_url,
)
from app.inventory_service import (
    PRICE_STALE_DAYS,
)
from app.models import Card, InventoryRow, User
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
    imports,
    playgroups,
    recommendations,
    sharing,
    trades,
)
from app.scryfall import (
    _bulk_data_loop,
    bulk_refresh_prices,
    fetch_payloads_uncached,
)
from app.timeutil import utc_now
from app.watchlist_service import (
    add_to_watchlist,
    list_watchlist,
    remove_from_watchlist,
    update_note,
    update_target_price,
)

_daemon_threads: list[threading.Thread] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle (replaces the deprecated ``@app.on_event`` pair).

    The startup and shutdown bodies below are moved here VERBATIM from the former
    ``on_startup`` / ``on_shutdown`` handlers. The shutdown ordering (stop the
    writer daemons → join → checkpoint the WAL → dispose the pool) is load-bearing
    for the SQLite-on-Longhorn clean-detach story — preserve it exactly.
    """
    # --- startup ---
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
    # Schema is owned by Alembic (``alembic upgrade head``), applied by the ArgoCD
    # PreSync migration hook in vanfreckle-platform BEFORE the app rolls. The hook
    # is live and proven (v4.0.36 applied game_goal_results through it). ``init_db``
    # no longer creates schema in prod: ``Base.metadata.create_all`` is gated to the
    # SQLite (dev) branch (see app/db.py). On Postgres a fresh/never-migrated DB
    # boots into a missing-table error by design — fail loud rather than silently
    # half-build via create_all (the v4.0.30 ledger-drift incident). The 7 raw-SQL
    # tables in app/legacy_tables.py were never created by create_all anyway
    # (imported only by alembic/env.py), so the hook is now their sole creator too.
    # ``init_db`` still validates that at least one user exists.
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

    yield

    # --- shutdown ---
    # Clean SQLite shutdown so the volume detaches on a consistent file.
    #
    # Stop the writer daemons (they watch ``shutdown_event``), wait briefly for any
    # in-flight batch to finish, then checkpoint the WAL into the main DB and close
    # the pool. Pairs with a generous ``terminationGracePeriodSeconds`` on the
    # Deployment so Kubernetes waits for this before SIGKILL + volume detach. See
    # the SQLite-on-Longhorn corruption mitigation.
    shutdown_event.set()
    for thread in _daemon_threads:
        thread.join(timeout=10)
    checkpoint_and_dispose()


app = FastAPI(title="Cartarch", lifespan=lifespan)


def require_metrics_token(request: Request) -> None:
    """Gate /metrics behind a shared-secret bearer token (issue #20).

    Prometheus metrics leak internal state (request counts, error rates, active
    users), and the route was reachable by anyone — on green/Talos it is exposed
    via the NodePort with no restriction. This dependency requires
    ``Authorization: Bearer <METRICS_TOKEN>`` so only the scraper (which carries
    the secret) can read it.

    Fails CLOSED: if ``METRICS_TOKEN`` is unset OR the supplied token doesn't
    match, the route returns 403 — an unconfigured deploy keeps metrics private
    rather than silently public. Compared with ``hmac.compare_digest`` to avoid
    leaking the token via timing. This is the ONLY auth-gated public route — no
    other endpoint changes (the app's per-route ``get_current_user`` auth is
    untouched).
    """
    expected = os.getenv("METRICS_TOKEN", "")
    auth = request.headers.get("Authorization", "")
    # RFC 7235: the auth-scheme token ("Bearer") is case-INSENSITIVE — split off
    # the scheme leniently so a scraper sending "bearer"/"BEARER" still works,
    # while the credential itself stays exact. Only a single space is consumed.
    scheme, _, supplied = auth.partition(" ")
    if scheme.lower() != "bearer":
        supplied = ""
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


# Expose Prometheus metrics at /metrics for the kube-prometheus-stack
# ServiceMonitor (platform observability). include_in_schema=False keeps it out
# of the public OpenAPI surface. The route is token-gated via
# require_metrics_token (issue #20) — the scraper must send the METRICS_TOKEN
# bearer secret; the kwargs flow through expose() into the FastAPI route.
Instrumentator().instrument(app).expose(
    app,
    include_in_schema=False,
    dependencies=[Depends(require_metrics_token)],
)

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
app.include_router(recommendations.router)
app.include_router(sharing.router)
app.include_router(trades.router)
app.include_router(goldfish.router)
app.include_router(games.router)
app.include_router(decks.router)
app.include_router(collections.router)
app.include_router(cards.router)
app.include_router(imports.router)


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


@app.get("/health", include_in_schema=False)
def health(session: Session = Depends(get_db_session)):
    """Unauthenticated readiness probe.

    No ``get_current_user`` dependency — this app has no auth middleware (auth is
    enforced per-route via that dependency), so omitting it leaves the route
    public, exactly the way /favicon.ico above is public. Kept out of the OpenAPI
    schema (``include_in_schema=False``).

    Touches the DB (``SELECT 1`` via the session dependency) so this is a real
    *readiness* signal, not just a liveness ping: if the session can't be
    acquired or the query fails, the request raises and the probe is non-200.
    Gives k8s a probe target and clears the cartarch-mcp probe 404 noise. The
    platform-repo probe config pointing here is a separate follow-up.
    """
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


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

# Release-sync guard. If the deployed image advertises a version with no
# matching Chronicle entry, the release shipped without its Chronicle update
# (e.g. the harness doc-sync step silently no-op'd, as v4.0.12 did). Warn at
# boot so a best-effort miss is visible instead of silent. APP_VERSION is unset
# in dev (the dev-<git> fallback in dependencies.py), so this only fires on real
# deployed builds — no dev-noise. Pure log line; no behavior change.
_deployed_version = os.getenv("APP_VERSION")
if _deployed_version and CHRONICLE_ENTRIES:
    _dep = _deployed_version.lstrip("v")
    _top = CHRONICLE_ENTRIES[0]["version"].lstrip("v")
    if _dep != _top:
        print(
            f"[chronicle] WARNING: deployed version {_deployed_version} has no "
            f"Chronicle entry (newest entry is {_top}). The release shipped "
            f"without its Chronicle update.",
            flush=True,
        )


_PRICE_REFRESH_INTERVAL_SECONDS = 600  # 10 minutes
_PRICE_REFRESH_BATCH = 75


def _run_price_refresh_batch() -> None:
    """Refresh up to 75 of the oldest-priced cards that are owned by any user."""
    session = SessionLocal()
    try:
        cutoff = utc_now() - timedelta(days=PRICE_STALE_DAYS)
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
        now = utc_now()
        updated = 0
        for card in stale:
            fresh = fresh_by_id.get(card.scryfall_id)
            if fresh:
                # Price columns are NOT written here — price comes from the
                # MTGJSON ingest (app.jobs.price_ingest); this loop now only
                # refreshes metadata/traits so it can't clobber MTGJSON prices.
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
        now = utc_now()
        for card in pending:
            fresh = fresh_by_id.get(card.scryfall_id)
            if fresh:
                # Price columns are NOT written here — price comes from the
                # MTGJSON ingest (app.jobs.price_ingest); this backfill only
                # populates traits/metadata.
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
        # v4.1.7 #63-followup diagnostic: did the session cookie come back? (Are
        # we bouncing a just-logged-in privacy browser to the splash?) Log-only.
        log_auth_diagnostic(request, "home_unauth")
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

    Each entry carries its ``patch`` number, rendered directly as "Entry N"
    (so the Entry label always equals the version's patch). The old
    Roman-numeral labels are gone: Roman has no zero, so ``to_roman()`` mapped
    both patch 0 and 1 to "I" — which forced a position-based workaround for
    entries and still left a duplicate "Issue I" once a folio had both a .0 and
    .1 minor. Folio (major) stays Roman in the template; Issue/Entry are plain.
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
    groups = _chronicle_archive_groups()
    # The focused stamp reads Folio/Issue/Entry straight off entry.version in
    # the template (Entry == patch), so no numbering is computed here.
    return render(
        request,
        "chronicle.html",
        {
            "title": "Chronicle — Cartarch",
            "current_user": current_user,
            "entry": entry,
            # Sidebar shows the current Folio only; older folios stay reachable
            # by direct link (CHRONICLE_BY_VERSION is untouched). groups is
            # folios newest-first, so groups[0] is the current Folio.
            "archive": groups[:1],
            "older_folios": [g["folio"] for g in groups[1:]],
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
    # Stateless pre-auth CSRF (#63) — see require_preauth_csrf. Origin/Referer +
    # signed token, no session-cookie dependency.
    csrf_token: str = Form(""),
):
    reissue = require_preauth_csrf(request, csrf_token, "register.html", {"title": "Register"})
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
    # render_auth_page: bfcache-hostile headers (no-store + Pragma) — issue #31.
    return render_auth_page(request, "register.html", {"title": "Register"})


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
    # Inline add-by-search outcome (?wl=added|dup&name=...) set by the
    # from_search branch of /watchlist/add — surfaces a one-line notice.
    return render(
        request,
        "watchlist.html",
        {
            "title": "Wishlist",
            "items": items,
            "current_user": current_user,
            "wl_outcome": request.query_params.get("wl", ""),
            "wl_name": request.query_params.get("name", ""),
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

    # v3.37.0 — the count + per-printing detail + bucketing is now the
    # shared decklist_service.compare_entries_to_owned unit (one GROUP BY +
    # one batched detail fetch, stress-tested at 20–50k rows). No exclusions
    # here → byte-identical to the pre-extraction path; the Brew Mode buy-list
    # reuses the same unit with exclusions.
    buckets = compare_entries_to_owned(session, current_user.id, all_entries)

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
    from_search: str = Form(""),
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

    ``from_search`` marks an add originating from the Wishlist page's
    add-by-search box (v3.39.x) — an **any-printing** add by name, the same
    identity mode as the card-detail "Watch any printing" button. That path
    redirects back to /watchlist with a ``?wl=added|dup&name=...`` outcome so
    the page can show an inline notice (empty selection → quiet no-op). The
    card-detail path below is unchanged.
    """
    cid_int: int | None = None
    if card_id.strip():
        try:
            cid_int = int(card_id.strip())
        except ValueError:
            cid_int = None
    name_str: str | None = card_name.strip() or None
    note_str: str | None = note.strip() or None

    if from_search.strip():
        if not name_str:
            # Nothing selected (empty/blank query submit) — no-op.
            return RedirectResponse(url="/watchlist", status_code=303)
        outcome = "added"
        try:
            add_to_watchlist(session, current_user.id, card_name=name_str, note=note_str)
            session.commit()
        except IntegrityError:
            session.rollback()
            outcome = "dup"
        return RedirectResponse(
            url=f"/watchlist?wl={outcome}&name={quote(name_str)}", status_code=303
        )

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


@app.get("/forgot-password")
def forgot_password_page(request: Request):
    # render_auth_page: bfcache-hostile headers (no-store + Pragma) — issue #31.
    return render_auth_page(request, "forgot_password.html", {"title": "Forgot password"})


@app.post("/forgot-password")
def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    session: Session = Depends(get_db_session),
    # Stateless pre-auth CSRF (#63) — see require_preauth_csrf.
    csrf_token: str = Form(""),
):
    """Identical response shape for registered vs unregistered emails.

    See app/password_reset_service.py for the full security-critical
    rationale. The neutral response is rendered regardless of whether
    we find a user, queue a token, get rate-limited, or fail any
    intermediate check — only the side-effects differ.
    """
    reissue = require_preauth_csrf(
        request, csrf_token, "forgot_password.html", {"title": "Forgot password"}
    )
    if reissue is not None:
        return reissue

    email_clean = (email or "").strip().lower()
    client_ip = client_ip_for(request)
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
                expires_at=utc_now() + timedelta(minutes=30),
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
        # render_auth_page: bfcache-hostile headers (no-store + Pragma) — issue #31.
        return render_auth_page(
            request,
            "reset_password.html",
            {
                "title": "Reset password",
                "invalid": True,
            },
        )
    return render_auth_page(
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
    # Stateless pre-auth CSRF (#63) — see require_preauth_csrf. Re-render keeps
    # the reset token so the user can resubmit (re-validated in the body anyway).
    csrf_token: str = Form(""),
):
    """Re-validate the token, set the new password, mark used.

    The re-validate step is essential — the token might have been used
    or expired between GET render and POST submit (different tab open
    for hours, etc.). Same find_valid_token call, same invalid-state
    branch on the rendered page.
    """
    reissue = require_preauth_csrf(
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
