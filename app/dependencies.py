from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import subprocess
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import card_filters, sort_spec
from app.db import SessionLocal
from app.deck_service import CARD_ROLE_TAGS
from app.inventory_service import FINISH_OPTIONS
from app.models import InventoryRow, StorageLocation, User
from app.pricing import effective_price
from app.timeutil import utc_now

logger = logging.getLogger(__name__)

# Throttle window for the per-user ``last_active_at`` stamp (see
# ``_stamp_last_active``). One write at most per this interval per user —
# named constant matching the ``LOGIN_RATE_LIMIT_WINDOW`` convention.
LAST_ACTIVE_THROTTLE = timedelta(minutes=5)

templates = Jinja2Templates(directory="app/templates")

# v3.27.17 — host allowlist for Referer-based redirect validation.
# Same-host (request.url.netloc) is always implicitly allowed; this set
# names the additional hosts cartarch.com lives behind so a user on the
# legacy hostname can follow a link that bounces them into cartarch.com
# (or vice versa) without safe_redirect_url() treating the cross-host
# Referer as an open-redirect attempt and dropping back to the default.
# No TrustedHostMiddleware in use; this is the only host-allow surface.
_REDIRECT_ALLOWED_HOSTS: frozenset[str] = frozenset({"cartarch.com", "www.cartarch.com"})


def safe_redirect_url(request: Request, default: str = "/collection") -> str:
    """Validate a Referer before reusing it as a redirect target.

    Lives here (shared) rather than in main.py so every route module can
    reach it without a circular import. An attacker can set Referer to an
    external URL, so an off-host Referer (not same-host, not in the
    allowlist) falls back to ``default``.
    """
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


def client_ip_for(request: Request) -> str | None:
    """Best-effort client IP for rate-limiting.

    **Spoof-resistant order:**

    1. ``CF-Connecting-IP`` — Cloudflare's canonical real-client-IP header.
       Cloudflare OVERWRITES it on every request at the edge, so a client
       cannot forge it; this is the production path (the app sits behind a
       Cloudflare Tunnel). This is the only fully trustworthy source here.
    2. The **rightmost** entry of ``X-Forwarded-For`` — NOT the leftmost.
       A trusted proxy APPENDS the address it actually received the
       connection from, so the rightmost hop is the least attacker-
       controllable. Taking the leftmost (``xff.split(",")[0]``) reads a
       value the client can fully forge, trivially bypassing the per-IP
       limit — that was the bug this fixes.
    3. ``request.client.host`` — the raw socket peer, when no proxy
       headers are present (e.g. local dev / direct hits).

    Used only for rate-limit keying — not for auth, not surfaced anywhere
    user-facing. Lives here (shared) rather than in main.py so every route
    module can reach it without a circular import (same precedent as
    ``safe_redirect_url``). Used by the password-reset throttle and the
    login brute-force throttle.
    """
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip and cf_ip.strip():
        return cf_ip.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else None


def _git(*args: str) -> str | None:
    """Run a git command, returning stripped stdout or None on any failure
    (non-zero exit, or git missing in the runtime image)."""
    try:
        return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _dev_version() -> str:
    """Version string for builds with no ``APP_VERSION`` env (i.e. Dev).

    - HEAD exactly on a tag → ``dev-<tag>`` verbatim.
    - Otherwise advertise the NEXT patch version off the latest reachable
      tag (matching the project's patch-bump release convention), so a Dev
      build one commit past ``v3.31.0`` reads ``dev-v3.31.1`` rather than a
      bare git sha.
    - No parseable tag / no git → ``dev-<short-sha>`` then ``dev-unknown``.

    NOTE: called at import time (the ``app_version`` global below), before
    ``_VERSION_RE`` is defined — so the semver parse here is inline, not via
    that module-level regex.
    """
    exact = _git("describe", "--tags", "--exact-match")
    if exact:
        return f"dev-{exact}"
    latest = _git("describe", "--tags", "--abbrev=0")
    if latest:
        core = latest[1:] if latest.startswith("v") else latest
        parts = core.split(".")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            major, minor, patch = (int(p) for p in parts)
            prefix = "v" if latest.startswith("v") else ""
            return f"dev-{prefix}{major}.{minor}.{patch + 1}"
    sha = _git("rev-parse", "--short", "HEAD")
    return f"dev-{sha}" if sha else "dev-unknown"


templates.env.globals["app_version"] = os.getenv("APP_VERSION") or _dev_version()
templates.env.globals["card_role_tags"] = CARD_ROLE_TAGS
# issue #52 — single source of truth for the finish-correction control's options
# (the service validates membership against the same FINISH_OPTIONS).
templates.env.globals["finish_options"] = FINISH_OPTIONS

# issue #44 — self-hosted card image mirror. Templates build image src from
# scryfall_id via mirror_image_url; the stored Card.image_url (a Scryfall CDN
# URL) stays in the schema untouched and remains the "card has an image" signal
# the templates guard on. Contract: /{scryfall_id}[/back]/{size}.{ext}, ext=png
# only for size "png"; a not-yet-mirrored printing 404s, so every <img> carries
# a Scryfall-API onerror fallback (the img_fallback macro in _macros.html).
IMAGE_MIRROR_BASE_URL = os.getenv("IMAGE_MIRROR_BASE_URL", "https://img.cartarch.com")


def mirror_image_url(scryfall_id: str, size: str = "normal", face: str = "front") -> str:
    ext = "png" if size == "png" else "jpg"
    back = "/back" if face == "back" else ""
    return f"{IMAGE_MIRROR_BASE_URL}/{scryfall_id}{back}/{size}.{ext}"


def scryfall_image_fallback(scryfall_id: str, size: str = "normal", face: str = "front") -> str:
    """Scryfall's card-image redirect endpoint — the onerror fallback target."""
    url = f"https://api.scryfall.com/cards/{scryfall_id}?format=image&version={size}"
    return url + "&face=back" if face == "back" else url


templates.env.globals["mirror_image_url"] = mirror_image_url
templates.env.globals["scryfall_image_fallback"] = scryfall_image_fallback

# Sort keys for surfaces that sort client-side (the trade construction picker —
# a page reload would drop the in-progress selection, so its sort is JS over
# pre-rendered data-sort-* attributes). The VALUES still come from the one
# sort_spec / pricing source, emitted server-side, so no rank or price rule is
# duplicated in JS.
# Colour/type facet options — Jinja globals so the shared _card_facets.html
# partial never has to be passed them by every route that includes it.
templates.env.globals["color_filter_options"] = card_filters.COLOR_FILTER_OPTIONS
templates.env.globals["type_filter_options"] = card_filters.TYPE_FILTER_OPTIONS
templates.env.globals["card_filter_tokens"] = card_filters.card_filter_tokens
templates.env.globals["rarity_rank"] = sort_spec.rarity_rank
templates.env.globals["effective_price"] = effective_price


# Resolved relative to this module, NOT the process cwd — so the hash is found
# no matter where the app is launched from (a missing file silently falls back
# to app_version, which would mask a real asset edit).
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_static_hash_cache: dict[str, str] = {}


def _hash_static_file(full: str) -> str:
    """SHA256 (truncated) of a file's content, streamed in chunks so a large
    asset never loads whole into memory."""
    h = hashlib.sha256()
    with open(full, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def static_v(path: str) -> str:
    """Cache-buster keyed on the static file's *content* hash, so a backend-only
    deploy (new container = new mtime, same content) keeps the browser/CDN cache,
    while an actual edit busts it. Falls back to app_version if the file is missing.

    Cached per-process ONLY in production (``APP_VERSION`` set) where static files
    are immutable for the container's lifetime. In dev (no ``APP_VERSION``) the hash
    is recomputed every call so working-tree edits to CSS/JS bust the cache live,
    same as the old mtime behaviour."""
    in_prod = bool(os.getenv("APP_VERSION"))
    if in_prod and path in _static_hash_cache:
        return _static_hash_cache[path]
    full = os.path.join(_STATIC_DIR, path.lstrip("/"))
    try:
        digest = _hash_static_file(full)
    except OSError:
        return os.getenv("APP_VERSION") or _dev_version()
    if in_prod:
        _static_hash_cache[path] = digest
    return digest


templates.env.globals["static_v"] = static_v


# v3.27.4 — local-time display filter for naive-UTC ``datetime`` values.
# The project's convention is ``utc_now()`` for all stored timestamps
# (naive UTC); template-side ``strftime`` therefore renders UTC dates labeled
# as if local, which displays evening activity as the following day from a
# Central Time perspective. This filter attaches UTC, converts to
# ``America/Chicago``, then formats. NULL-safe (returns ``''`` for None) so
# templates can chain it without an outer conditional.
#
# Scope: registered globally as a Jinja filter, but only the Admin template
# consumes it in this patch. Broader rollout to other UTC-displaying
# templates (created_at, played_at, imported_at, …) is a separate roadmap
# item — the cost of a project-wide sweep isn't worth taking on for a
# single-tenant install. Upgrade path to per-user timezone is a one-line
# filter swap if a wider user base demands it.
_LOCAL_TZ = ZoneInfo("America/Chicago")


def format_local_datetime(dt: datetime | None, fmt: str = "%Y-%m-%d") -> str:
    if dt is None:
        return ""
    # #130: values are aware UTC now (the UTCDateTime type), so the astimezone
    # below converts straight to Chicago. The naive guard is kept for any legacy
    # naive datetime that still reaches here — it's treated as UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_LOCAL_TZ).strftime(fmt)


templates.env.filters["format_local_datetime"] = format_local_datetime


# v3.28.3 — editorial date format. Renders a datetime or an ISO date
# string (``YYYY-MM-DD``) in the Folio editorial form ("21 May 2026").
# Used on user-facing date surfaces — primarily Chronicle entries
# (which carry their dates as ISO strings in chronicle.json) and
# anywhere else a date renders for readers rather than for ops.
#
# Accepts either a ``datetime`` (any tz-state — naive UTC is the
# project convention) or an ISO date string. Returns ``''`` for None /
# unparseable input; never crashes — same NULL-safe shape as
# ``format_local_datetime`` above.
def format_editorial_date(value) -> str:
    if value is None or value == "":
        return ""
    # datetime → use its date component directly. Naive UTC is the
    # project convention, but the date component is tz-independent
    # enough that timezone math isn't required for an editorial render.
    if isinstance(value, datetime):
        d = value.date()
    else:
        try:
            d = date.fromisoformat(str(value))
        except (ValueError, TypeError):
            return str(value)
    return d.strftime("%-d %B %Y")  # "21 May 2026"


templates.env.filters["editorial_date"] = format_editorial_date


# v3.28.2 — Folio versioning helper. Renders the semantic version
# (``X.Y.Z`` or ``vX.Y.Z``) as ``Folio X · Issue Y · Entry Z`` with Roman
# numerals. The production equivalent of the design package's
# ``utils/folio.js`` (``versionToFolio`` + ``toRoman``). Roman is
# presentation-only; semantic remains canonical everywhere else (URLs,
# internal use, ``app_version`` global, CLI / logs).
#
# Dev-build safety: when ``app_version`` falls through to ``dev-<git>``
# (per ``_dev_version`` above), the input is non-semantic and the filter
# must NOT crash or mangle it — it falls back to rendering the raw input.
# Same goes for an empty string or any other unparseable value.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_ROMAN_PAIRS = [
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
]


def to_roman(n: int) -> str:
    """Convert a non-negative integer to Roman numerals.
    Returns ``'I'`` for ``n <= 0`` to match the design package's convention
    (the design's ``toRoman`` returns ``"I"`` for the falsy / zero case)."""
    n = int(n) if n is not None else 0
    if n <= 0:
        return "I"
    out = []
    for value, symbol in _ROMAN_PAIRS:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def version_to_folio(version: str | None) -> str:
    """Render a semantic version as 'Folio X · Issue Y · Entry Z'.
    Folio (major) stays Roman — it's the masthead flourish and is never 0.
    Issue (minor) and Entry (patch) are plain numbers matching the version:
    Roman has no zero, so Roman'ing them collided '.0'/'.1' on "I" (the
    duplicate-Issue / off-by-one bug). They now read straight off the version,
    so 'Entry Z' always equals the patch number. Falls back to the raw input
    for non-semantic inputs (dev-builds, empty strings). Presentation-only —
    never use this value in URLs or internal references."""
    if not version:
        return ""
    match = _VERSION_RE.match(version)
    if not match:
        return version  # dev-build identity, pass through unchanged
    folio, issue, entry = (int(g) for g in match.groups())
    return f"Folio {to_roman(folio)} · Issue {issue} · Entry {entry}"


templates.env.filters["folio"] = version_to_folio
templates.env.globals["version_to_folio"] = version_to_folio
templates.env.globals["to_roman"] = to_roman


def get_csrf_token(request: Request) -> str:
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_hex(32)
    return request.session["csrf_token"]


def require_csrf_token(
    request: Request,
    # Form("") so missing field returns 403, not a 422 validation error
    csrf_token: str = Form(""),
) -> None:
    expected = request.session.get("csrf_token", "")
    if not expected or csrf_token != expected:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


CsrfRequired = Depends(require_csrf_token)


# --- Stateless pre-auth CSRF (issue #63) ------------------------------------ #
# The four PUBLIC pre-auth forms (login / register / forgot / reset) can't rely
# on the session cookie surviving the GET->POST round trip: privacy iOS browsers
# (Firefox for iOS, Opera Touch) drop our host-only SameSite=Lax `session` cookie
# on the POST, so the session-bound double-submit (require_csrf_token) dead-
# ends them on a 403. This token is server-SIGNED + timestamped instead of
# session-bound, so it validates with NO cookie. Authenticated, state-changing
# endpoints keep the session-bound CsrfRequired / require_csrf_token unchanged.
_PREAUTH_CSRF_SALT = "cartarch-preauth-csrf"
PREAUTH_CSRF_MAX_AGE = 3600  # 1h; replay within the window is accepted for pre-auth


def _preauth_csrf_serializer():
    """URLSafeTimedSerializer keyed on the EXISTING session secret, with a
    distinct salt so the pre-auth token space can't be crossed with the session
    signer. No new env var (reuses SESSION_SECRET_KEY)."""
    from itsdangerous import URLSafeTimedSerializer

    secret = os.getenv("SESSION_SECRET_KEY", "dev-only-change-me")
    return URLSafeTimedSerializer(secret, salt=_PREAUTH_CSRF_SALT)


def mint_preauth_csrf_token() -> str:
    """A signed, timestamped opaque token for a pre-auth form GET. The nonce only
    makes tokens unique; the token carries NO server state, so it survives a
    client that never sends our session cookie back."""
    return _preauth_csrf_serializer().dumps({"n": secrets.token_hex(16)})


def _preauth_cross_site_ok(request: Request) -> bool:
    """Gate (a) of require_preauth_csrf — strict cross-site check. Accept iff the
    Origin OR the Referer confirms our origin (host in the trusted set / the
    request host, scheme https in prod). If BOTH headers are absent, accept
    (degraded — gate (b)'s signed token still proves it was minted by us); a
    present-but-foreign Origin/Referer is a cross-site POST and is rejected.

    History: v4.1.6 made this ADVISORY (fail-open + log) during the FxiOS sign-in
    incident, when Firefox-iOS appeared to send an opaque `Origin: null`. The root
    cause turned out to be the client hitting the site over HTTP (a Secure cookie
    can't persist over http) — fixed at the edge with Cloudflare "Always Use
    HTTPS". Over https, FxiOS sends a valid Origin AND Referer, so this is back to
    strict (v4.1.12). The host is the forwarded Host (request.url.netloc, now
    correct via uvicorn --proxy-headers); the Origin-OR-Referer fallback covers
    clients that send only one of the two."""
    trusted = {"cartarch.com", "www.cartarch.com", "mana.vanfreckle.com"}
    host = request.url.netloc.lower()
    if host:
        trusted.add(host)
    dev = os.getenv("DEV_MODE", "false").lower() == "true"
    allowed_schemes = {"http", "https"} if dev else {"https"}

    def _host_of(value: str | None) -> str | None:
        if not value:
            return None
        try:
            parsed = urlparse(value)
        except Exception:  # noqa: BLE001 — a malformed header is a reject, not a 500.
            return None
        if parsed.scheme not in allowed_schemes or not parsed.netloc:
            return None
        return parsed.netloc.lower()

    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin is None and referer is None:
        return True  # degraded path; the signed token still gates
    return _host_of(origin) in trusted or _host_of(referer) in trusted


def require_preauth_csrf(
    request: Request,
    csrf_token: str,
    template: str,
    ctx: dict | None = None,
):
    """Stateless CSRF guard for the four PUBLIC pre-auth forms (issue #63),
    replacing require_csrf_or_reissue on those paths. Two gates:

      (a) cross-site — Origin/Referer must match our origin (_preauth_cross_site_ok);
          a cross-origin POST is a 403.
      (b) integrity/freshness — the token must be a valid signature no older than
          PREAUTH_CSRF_MAX_AGE. An EXPIRED token re-renders the form with a
          friendly "please try again" (NOT a 403, NOT the cookie "session expired"
          copy); a tampered or missing token is a 403.

    Returns None on pass (the caller proceeds), or a TemplateResponse the caller
    must return as-is. Does NOT depend on the session cookie. SignatureExpired is
    caught before BadData because it is a subclass of it."""
    from itsdangerous import BadData, SignatureExpired

    if not _preauth_cross_site_ok(request):
        raise HTTPException(status_code=403, detail="Cross-site request blocked")
    try:
        _preauth_csrf_serializer().loads(csrf_token, max_age=PREAUTH_CSRF_MAX_AGE)
    except SignatureExpired:
        expired_ctx = {"error": "This form expired, please try again."}
        if ctx:
            expired_ctx.update(ctx)
        return render(request, template, expired_ctx)
    except BadData:
        raise HTTPException(status_code=403, detail="Invalid CSRF token") from None
    return None


def _pending_count_for(user_id: int | None) -> int:
    """Count this user's pending-placement rows. Used by the mobile nav badge.

    Runs once per render; cheap count(*) on a per-user filter.
    """
    if not user_id:
        return 0
    session = SessionLocal()
    try:
        return (
            session.query(InventoryRow)
            .filter(
                InventoryRow.user_id == user_id,
                InventoryRow.is_pending.is_(True),
            )
            .count()
        )
    finally:
        session.close()


def _has_drawers_for(user_id: int | None) -> bool:
    """Whether the user has any drawer location — gates the drawer-specific nav
    (#104, replaces the DRAWER_SORTER_USERNAMES username gate). Short-lived session,
    the _pending_count_for precedent."""
    if not user_id:
        return False
    session = SessionLocal()
    try:
        return (
            session.query(StorageLocation.id)
            .filter(StorageLocation.user_id == user_id, StorageLocation.type == "drawer")
            .first()
            is not None
        )
    finally:
        session.close()


def _trade_pending_count_for(user_id: int | None) -> int:
    """Count trades awaiting this user's action — i.e. proposed trades
    where they are the recipient. Used by the Trades nav badge (v3.29.2).

    Runs once per render; cheap count(*) covered by indexes on
    ``recipient_user_id`` + ``status``. Lazy-imports the service to
    avoid a module-load cycle (trade_service imports models; this
    module is imported by route modules that include trade_service).
    """
    if not user_id:
        return 0
    session = SessionLocal()
    try:
        from app import trade_service

        return trade_service.pending_action_count(session, user_id)
    finally:
        session.close()


# The four PUBLIC pre-auth forms validate a STATELESS signed token
# (require_preauth_csrf, #63), never the session token. So EVERY render of these
# templates — GET, an in-handler error re-render (wrong password, weak password,
# …), or the guard's own expired re-render — must embed a freshly-signed token,
# not get_csrf_token's session hex. Centralising it here (not only in
# render_auth_page) is load-bearing: the error re-renders call render() directly,
# and a session-hex token in those forms is rejected by the stateless POST guard
# as BadData -> 403 (e.g. one mistyped password would 403 the retry).
_PREAUTH_TEMPLATES = frozenset(
    {"login.html", "register.html", "forgot_password.html", "reset_password.html"}
)


def _active_audit_banner_for(user_id: int | None) -> dict | None:
    """Resume-banner data for the user's active/paused audit, if any. Own
    short-lived session (the ``_pending_count_for`` precedent); shows on every
    page via ``render``. ``get_active_audit`` also lazily times out stale
    sessions, so this doubles as the 24h cleanup trigger — hence the commit."""
    if not user_id:
        return None
    from app import audit_service

    session = SessionLocal()
    try:
        audit = audit_service.get_active_audit(session, user_id)
        if audit is None:
            session.commit()  # persist any lazy auto-abandon get_active_audit did
            return None
        location = session.get(StorageLocation, audit.storage_location_id)
        banner = {
            "audit_id": audit.id,
            "location_id": audit.storage_location_id,
            "location_name": location.name if location else "a location",
            "status": audit.status,
        }
        session.commit()
        return banner
    except Exception:  # telemetry-grade; never fail a render over the banner
        session.rollback()
        return None
    finally:
        session.close()


def render(
    request: Request,
    template: str,
    ctx: dict | None = None,
    status_code: int = 200,
):
    user_id = request.session.get("user_id")
    context = {
        "csrf_token": (
            mint_preauth_csrf_token() if template in _PREAUTH_TEMPLATES else get_csrf_token(request)
        ),
        "pending_count": _pending_count_for(user_id),
        "trade_pending_count": _trade_pending_count_for(user_id),
        "audit_banner": _active_audit_banner_for(user_id),
        "has_drawers": _has_drawers_for(user_id),
    }
    if ctx:
        context.update(ctx)
    response = templates.TemplateResponse(
        request=request,
        name=template,
        context=context,
        status_code=status_code,
    )
    # v3.31.0 — dynamic, per-user HTML must never be served from the browser
    # cache. Without this, navigating to a URL the browser has already seen
    # (e.g. re-applying a Collection color filter whose query string matches
    # an earlier visit) renders a STALE cached page instead of the fresh
    # server response — surfaced as "the color filter doesn't refresh after I
    # deselect a pip" even though the URL and server result were correct.
    # Static assets keep their own caching (StaticFiles + the ?v= content-hash
    # buster); JSON/redirect responses don't go through render() and are
    # unaffected.
    response.headers["Cache-Control"] = "no-store"
    return response


def render_auth_page(
    request: Request,
    template: str,
    ctx: dict | None = None,
    status_code: int = 200,
):
    """Render a PUBLIC pre-auth form page (login / register / forgot / reset)
    with bfcache-hostile cache headers.

    Issue #31 / hypothesis #1: Firefox can restore a pre-auth form from its
    back/forward cache (bfcache); if the session cookie has since changed (e.g.
    dropped/expired after a logout), the restored form submits and surfaces as
    "session expired". ``render`` already sets ``Cache-Control: no-store`` on
    every page (v3.31.0); this re-asserts it at the auth seam and adds the
    HTTP/1.0 ``Pragma: no-cache`` to discourage bfcache, paired with the
    ``pageshow`` reload in ``_auth_layout.html`` (which forces a fresh load —
    re-establishing the session cookie + token — if a browser restores the page
    anyway).

    The STATELESS pre-auth CSRF token (#63) is minted by ``render`` itself for
    these four templates (see ``_PREAUTH_TEMPLATES``), so it is present whether a
    form is reached via this helper's GET or an error re-render that calls
    ``render`` directly. Minting a fresh token per render is safe — unlike the old
    session token, validation is signature + 1h-freshness based, not session-
    equality, so independent tabs each carry their own valid token with no
    mismatch (the multi-tab failure mode #31 worried about does not apply). The
    matching POST guard is ``require_preauth_csrf``.
    """
    response = render(request, template, ctx, status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def get_db_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _stamp_last_active(user: User) -> None:
    """Best-effort stamp of ``User.last_active_at`` on an authenticated request.

    **Throttled, DB-self-throttle** — no in-memory store. The already-loaded
    ``user.last_active_at`` is compared against ``utc_now()``: write only when
    it is NULL or older than ``LAST_ACTIVE_THROTTLE``. This is correct across
    restarts and multiple replicas (the throttle state IS the persisted row),
    unlike the single-pod in-memory throttles in ``login_throttle`` /
    ``password_reset_service``. Aware-UTC throughout (#130) — ``utc_now()`` is
    aware and ``last_active_at`` reads back aware (the ``UTCDateTime`` type), so
    the comparison is aware-vs-aware in both prod (timestamptz) and the SQLite
    test suite.

    **Isolated transaction** — the update runs in its OWN short-lived
    ``SessionLocal()`` (the ``_pending_count_for`` precedent), never on the
    request's shared session: committing the timestamp there could flush
    partial route state early, and a route that later raises would roll it back.
    The already-loaded ``user`` instance is also left UNTOUCHED — assigning
    ``user.last_active_at`` would mark it dirty on the shared request session, so
    a later ``session.commit()`` in the route would redundantly re-issue (and
    thus commit on the shared session) the very write we deliberately isolated.
    No in-request consistency fix is needed: FastAPI caches a dependency's return
    value per request (``use_cache=True``), so this runs at most once per request.

    **Best-effort** — any exception (transient DB error, pool exhaustion) is
    caught and logged, never propagated. ``last_active_at`` is telemetry; a
    failed stamp must not fail the authenticated request it rides on, and an
    unhandled exception here would fail *every* authenticated route.
    """
    now = utc_now()
    last = user.last_active_at
    if last is not None and now - last < LAST_ACTIVE_THROTTLE:
        return
    try:
        session = SessionLocal()
        try:
            session.query(User).filter(User.id == user.id).update(
                {User.last_active_at: now}, synchronize_session=False
            )
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.warning("failed to stamp last_active_at for user %s", user.id, exc_info=True)


def get_current_user(
    request: Request,
    session: Session = Depends(get_db_session),
) -> User:
    user_id = request.session.get("user_id")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Redirect to login",
            headers={"Location": "/login"},
        )

    user = session.query(User).filter(User.id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is inactive",
        )

    _stamp_last_active(user)
    return user


def get_optional_current_user(
    request: Request,
    session: Session = Depends(get_db_session),
) -> User | None:
    """Non-redirecting variant of :func:`get_current_user`.

    v3.27.17 — added to support the public landing page at ``/``. The
    standard ``get_current_user`` raises a 303 redirect to /login for anon
    visitors, which is correct for protected routes but wrong for routes
    that want to branch on auth state (e.g. show a marketing page to anon
    visitors and the dashboard to signed-in users from the same path).

    Returns the authenticated ``User`` instance, or ``None`` if no valid
    session exists. Inactive accounts also return ``None`` rather than
    raising — the caller decides what to render in either case.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = session.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        return None
    _stamp_last_active(user)
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user
