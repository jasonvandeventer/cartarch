from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import hash_password, verify_password
from app.dependencies import CsrfRequired, get_current_user, get_db_session, render
from app.models import User

router = APIRouter(prefix="/account")


@router.get("")
def account_page(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    error = request.query_params.get("error")
    success = request.query_params.get("success")
    return render(
        request,
        "account.html",
        {
            "title": "My Account",
            "current_user": current_user,
            "error": error,
            "success": success,
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

    if len(new_password) < 8:
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
    session: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
    _: None = CsrfRequired,
):
    email = email.strip()
    display_name = display_name.strip() or None

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
        session.commit()

    return RedirectResponse(url="/account?success=profile_updated", status_code=303)
