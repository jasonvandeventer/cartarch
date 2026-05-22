from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import authenticate_user
from app.dependencies import CsrfRequired, get_db_session, render

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", {"error": None})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db_session),
    _: None = CsrfRequired,
):
    user = authenticate_user(db, username, password)

    if not user:
        return render(request, "login.html", {"error": "Invalid username or password."})

    # v3.27.4 — track actual sign-ins directly. Drives the "Last Signed In"
    # column on the Admin page (replaces the misleading TransactionLog-
    # aggregate proxy). Naive UTC to match the project-wide datetime
    # convention; format_local_datetime in dependencies.py converts at
    # render time.
    user.last_signed_in_at = datetime.utcnow()
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
