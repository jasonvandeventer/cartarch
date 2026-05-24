from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import Generator
from datetime import UTC, datetime
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


def _dev_version() -> str:
    try:
        tag = (
            subprocess.check_output(
                ["git", "describe", "--tags", "--exact-match"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except subprocess.CalledProcessError:
        tag = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    return f"dev-{tag}"


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
# The project's convention is ``datetime.utcnow()`` for all stored timestamps
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


def render(request: Request, template: str, ctx: dict | None = None):
    context = {
        "csrf_token": get_csrf_token(request),
        "pending_count": _pending_count_for(request.session.get("user_id")),
    }
    if ctx:
        context.update(ctx)
    return templates.TemplateResponse(request=request, name=template, context=context)


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
