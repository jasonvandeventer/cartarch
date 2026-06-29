import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import authenticate_user
from app.dependencies import (
    CsrfRequired,
    client_ip_for,
    get_db_session,
    log_auth_diagnostic,
    render,
    render_auth_page,
    require_preauth_csrf,
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
def login_page(request: Request):
    # render_auth_page: bfcache-hostile headers (no-store + Pragma) — issue #31.
    return render_auth_page(request, "login.html", {"error": None})


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
):
    # v4.1.7 #63-followup diagnostic: cookie/origin state as the POST arrives.
    log_auth_diagnostic(request, "login_post")

    reissue = require_preauth_csrf(request, csrf_token, "login.html")
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
            {"error": "Too many failed login attempts. Please wait a few minutes and try again."},
            status_code=429,
        )

    user = authenticate_user(db, username, password)

    if not user:
        record_failed_login(username, client_ip)
        return render(request, "login.html", {"error": "Invalid username or password."})

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

    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
def logout(
    request: Request,
    _: None = CsrfRequired,
):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
