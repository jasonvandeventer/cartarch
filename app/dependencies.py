from __future__ import annotations

import os
import re
import secrets
import subprocess
from collections.abc import Generator
from datetime import UTC, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.deck_service import CARD_ROLE_TAGS
from app.models import InventoryRow, User

# Users who get drawer-centric features (auto-sorter, Drawers page, Audit page).
# Update here to add or remove users — no other changes needed.
DRAWER_SORTER_USERNAMES: frozenset[str] = frozenset({"jason@vanfreckle.com", "test"})

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
templates.env.globals["drawer_sorter_usernames"] = DRAWER_SORTER_USERNAMES
templates.env.globals["card_role_tags"] = CARD_ROLE_TAGS


def static_v(path: str) -> str:
    """Cache-buster keyed on the static file's mtime so working-tree edits
    invalidate browser caches without needing a git commit. Falls back to
    app_version if the file is missing."""
    full = os.path.join("app", "static", path.lstrip("/"))
    try:
        return str(int(os.path.getmtime(full)))
    except OSError:
        return os.getenv("APP_VERSION") or _dev_version()


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
    # Naive-UTC → aware-UTC → Chicago. ``replace(tzinfo=UTC)`` on an
    # already-aware datetime would overwrite the existing tz, but the
    # project only ever stores naive UTC, so this is safe.
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
            d = datetime.strptime(str(value), "%Y-%m-%d").date()
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
    """Render a semantic version as 'Folio X · Issue Y · Entry Z' (Roman).
    Falls back to the raw input for non-semantic inputs (dev-builds,
    empty strings). Roman is presentation-only — never use this value
    in URLs or internal references."""
    if not version:
        return ""
    match = _VERSION_RE.match(version)
    if not match:
        return version  # dev-build identity, pass through unchanged
    folio, issue, entry = (int(g) for g in match.groups())
    return f"Folio {to_roman(folio)} · Issue {to_roman(issue)} · Entry {to_roman(entry)}"


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


def csrf_state(request: Request, csrf_token: str) -> str:
    """Classify a submitted CSRF token against the session, without raising.

    - ``"ok"``        — the form token matches the session token.
    - ``"mismatch"``  — a session token exists but the form token differs
                        (a stale form posted against a still-live session, or
                        a forgery). The strict, suspicious case.
    - ``"no_session"``— the session carries NO csrf_token at all. This is the
                        "first contact" state: the request arrived with no
                        usable session cookie, so the server never had a token
                        to match against. Kept distinct from ``"mismatch"`` so
                        the public auth forms can recover instead of dead-
                        ending the user on a 403 (see ``require_csrf_or_reissue``).
    """
    expected = request.session.get("csrf_token", "")
    if not expected:
        return "no_session"
    return "ok" if csrf_token == expected else "mismatch"


def require_csrf_or_reissue(
    request: Request,
    csrf_token: str,
    template: str,
    ctx: dict | None = None,
):
    """CSRF guard for the PUBLIC, pre-auth forms (login/register/forgot/reset).

    Same strict double-submit as :func:`require_csrf_token`, with a single
    softening that is only safe *before* a user is authenticated: when the
    session has no established token at all (``"no_session"``), re-render the
    form with a freshly issued token instead of returning 403.

    Why: v3.31.0 surfaced an "Invalid CSRF token" sign-in regression
    (POST /login -> 403) that was NOT reproducible from the request/response
    code — that path is byte-identical to the known-good v3.30.x line. It is
    a *cookie-continuity* failure: a logged-out browser reaches POST /login
    carrying no usable session cookie (a stale/expired cookie the server now
    drops as an empty session, a cookie scoped to the pre-cutover host, or a
    login page served from an edge cache without its per-user Set-Cookie). The
    form then has no session token to match, so strict double-submit hard-
    fails — and because GET /login alone can't repair a cookie the browser
    won't replace, the user is stuck on a permanent 403 dead-end.

    The reissue path is safe here precisely because the session is empty:
    there is no authenticated state to protect, and login/register are not
    state-changing for an existing account. Calling :func:`render` runs
    :func:`get_csrf_token`, which mints a fresh token into the (empty) session;
    SessionMiddleware then emits a new Set-Cookie, so the user's immediate
    resubmit carries a matching cookie + token and succeeds. A genuine
    ``"mismatch"`` (live session, wrong token) still hard-fails with 403.

    Returns ``None`` when validation passes (the caller proceeds with its
    handler body), or a ``TemplateResponse`` the caller must return as-is.
    """
    state = csrf_state(request, csrf_token)
    if state == "ok":
        return None
    if state == "mismatch":
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    # "no_session": re-render with a fresh token + cookie so the retry works.
    reissue_ctx = {"error": "Your session expired before you submitted. Please try again."}
    if ctx:
        reissue_ctx.update(ctx)
    return render(request, template, reissue_ctx)


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


def render(
    request: Request,
    template: str,
    ctx: dict | None = None,
    status_code: int = 200,
):
    user_id = request.session.get("user_id")
    context = {
        "csrf_token": get_csrf_token(request),
        "pending_count": _pending_count_for(user_id),
        "trade_pending_count": _trade_pending_count_for(user_id),
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
    # Static assets keep their own caching (StaticFiles + the ?v= mtime
    # buster); JSON/redirect responses don't go through render() and are
    # unaffected.
    response.headers["Cache-Control"] = "no-store"
    return response


def get_db_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


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
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user
