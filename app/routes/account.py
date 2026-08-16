from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import hash_password, validate_password_strength, verify_password
from app.dependencies import CsrfRequired, get_current_user, get_db_session, render
from app.models import User
from app.routes.api import hash_api_token

router = APIRouter(prefix="/account")


@router.get("")
def account_page(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    error = request.query_params.get("error")
    success = request.query_params.get("success")
    # One-shot: POP it, so a refresh does not redisplay the secret and a shared
    # screen does not keep it up. Showing it once is the cost of not storing it.
    new_api_token = request.session.pop("new_api_token", None)
    return render(
        request,
        "account.html",
        {
            "title": "My Account",
            "current_user": current_user,
            "error": error,
            "success": success,
            "new_api_token": new_api_token,
        },
    )


@router.post("/change-password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    if not verify_password(current_password, current_user.password_hash):
        return RedirectResponse(url="/account?error=wrong_password", status_code=303)

    # v3.27.14 — route the third password-set path through the shared
    # validator from app/auth.py. The pre-v3.27.14 hardcoded `len < 8`
    # check enforced the same minimum but lived independently — three
    # separate password-set paths drift apart over time if they each
    # carry their own rules. Now all three (/register, /reset-password,
    # /account/change-password) call validate_password_strength.
    strength_error = validate_password_strength(new_password)
    if strength_error:
        return RedirectResponse(url="/account?error=password_too_short", status_code=303)

    if new_password != confirm_password:
        return RedirectResponse(url="/account?error=passwords_dont_match", status_code=303)

    user = session.query(User).filter(User.id == current_user.id).first()
    if user:
        user.password_hash = hash_password(new_password)
        session.commit()

    return RedirectResponse(url="/account?success=password_changed", status_code=303)


@router.post("/update-profile")
def update_profile(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    real_name: str = Form(""),
    price_alerts_enabled: bool = Form(False),  # #99 — checkbox: present only when on
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    # v3.33.1 — canonicalize to lowercase, mirroring registration
    # (main.py) and forgot-password. Without this a mixed-case edit would
    # store a mixed-case username and lock the user out at login.
    email = email.strip().lower()
    display_name = display_name.strip() or None
    # Member-facing only — never rendered on /w/{token} or /d/{token}. Blank
    # clears it and the handle takes over again (User.player_label).
    real_name = real_name.strip() or None

    if "@" not in email or "." not in email.split("@", 1)[1]:
        return RedirectResponse(url="/account?error=bad_email", status_code=303)

    if email != current_user.username:
        existing = (
            session.query(User).filter(User.username == email, User.id != current_user.id).first()
        )
        if existing:
            return RedirectResponse(url="/account?error=email_taken", status_code=303)

    user = session.query(User).filter(User.id == current_user.id).first()
    if user:
        user.username = email
        user.display_name = display_name
        user.real_name = real_name
        user.price_alerts_enabled = bool(price_alerts_enabled)  # #99
        session.commit()

    return RedirectResponse(url="/account?success=profile_updated", status_code=303)


# #179 — the /api/v1 bearer token. Token-as-toggle (Deck.share_token,
# User.wishlist_share_token, Game.join_code): generating enables the read-only
# API for this user, revoking sets it back to NULL and 401s the next request.
# Regenerate is just generate again — it overwrites, so a leaked token is
# revoked by minting a new one.
@router.post("/api-token")
def generate_api_token(
    request: Request,
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    """Mint a token, store only its hash, and hand the plaintext back ONCE (#182).

    The raw value rides the **session cookie**, not the redirect URL. A secret in
    a query string lands in access logs, in the browser's history, and in the
    Referer of the next request — which would undo most of what hashing at rest
    just bought.
    """
    user = session.query(User).filter(User.id == current_user.id).first()
    if user:
        # 32 bytes → a 43-char URL-safe string, well inside String(64).
        raw = secrets.token_urlsafe(32)
        user.api_token_hash = hash_api_token(raw)
        session.commit()
        request.session["new_api_token"] = raw
    return RedirectResponse(url="/account?success=api_token_generated", status_code=303)


@router.post("/api-token/revoke")
def revoke_api_token(
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    user = session.query(User).filter(User.id == current_user.id).first()
    if user:
        user.api_token_hash = None
        session.commit()
    return RedirectResponse(url="/account?success=api_token_revoked", status_code=303)
