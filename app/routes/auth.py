import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import authenticate_user
from app.dependencies import (
    CsrfRequired,
    client_ip_for,
    get_db_session,
    render,
    render_auth_page,
    require_preauth_csrf,
    safe_next_path,
)
from app.login_throttle import (
    is_login_throttled,
    record_failed_login,
    reset_login_attempts,
)
from app.timeutil import utc_now

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str = ""):
    # render_auth_page: bfcache-hostile headers (no-store + Pragma) — issue #31.
    # `next` is the deep link that hit the auth wall (get_current_user sets it);
    # it rides the form so a sign-in lands where the user was actually going.
    return render_auth_page(
        request, "login.html", {"error": None, "next_url": safe_next_path(next, "")}
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db_session),
    # Stateless pre-auth CSRF (#63): validated by Origin/Referer + a server-signed
    # token, NOT the session cookie — privacy iOS browsers (FxiOS/OPT) drop our
    # cookie on the POST. Expired token re-renders the form; tamper/cross-site 403.
    csrf_token: str = Form(""),
    next: str = Form(""),
):
    # Re-validated on the way out, never trusted as submitted — the form field is
    # as attacker-controlled as the query param was.
    next_url = safe_next_path(next, "")
    reissue = require_preauth_csrf(request, csrf_token, "login.html", {"next_url": next_url})
    if reissue is not None:
        return reissue

    # Brute-force throttle (S1): per-IP + per-username sliding window over
    # FAILED attempts. Checked BEFORE authenticating so a throttled attacker
    # can't keep guessing. No account lockout — purely a 429 wait.
    client_ip = client_ip_for(request)
    if is_login_throttled(username, client_ip):
        logger.warning(
            "login throttled: username=%r ip=%r", (username or "").strip().lower(), client_ip
        )
        return render(
            request,
            "login.html",
            {
                "error": "Too many failed login attempts. Please wait a few minutes and try again.",
                "next_url": next_url,
            },
            status_code=429,
        )

    user = authenticate_user(db, username, password)

    if not user:
        record_failed_login(username, client_ip)
        return render(
            request,
            "login.html",
            {"error": "Invalid username or password.", "next_url": next_url},
        )

    # Successful login clears this username's failure counter so earlier
    # typos don't lock out a legitimate user.
    reset_login_attempts(username)

    # v3.27.4 — track actual sign-ins directly. Drives the "Last Signed In"
    # column on the Admin page (replaces the misleading TransactionLog-
    # aggregate proxy). Naive UTC to match the project-wide datetime
    # convention; format_local_datetime in dependencies.py converts at
    # render time.
    user.last_signed_in_at = utc_now()
    db.commit()

    request.session["user_id"] = user.id

    return RedirectResponse(url=next_url or "/", status_code=303)


@router.post("/logout")
def logout(
    request: Request,
    _: None = CsrfRequired,
):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
