from fastapi import Request
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from app.models import User

password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


# v3.27.14 — shared password strength validation. Used by /register
# (POST /register in app/main.py) and the new /reset-password POST.
# NIST SP 800-63B-aligned: enforce a reasonable minimum length and a
# reasonable maximum (to prevent absurd-payload DoS), but NO
# composition requirements (forced upper/lower/digit/symbol mixes).
# Modern guidance is that length matters and composition rules push
# users toward weaker, easier-to-attack patterns. Returns None on
# success or a human-readable error message string on failure.
#
# Existing users whose passwords don't meet these rules continue to
# log in fine — verify_password just compares against the stored
# hash. The validator only fires on /register and /reset-password
# WRITE paths.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256


def validate_password_strength(password: str) -> str | None:
    if not password:
        return "Password is required."
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters."
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Password must be {PASSWORD_MAX_LENGTH} characters or fewer."
    return None


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False

    return password_hash.verify(password, stored_hash)


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(User.username == username).first()


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = get_user_by_username(db, username)

    if not user:
        return None

    if not user.is_active:
        return None

    if not verify_password(password, user.password_hash):
        return None

    return user


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")

    if not user_id:
        return None

    return db.query(User).filter(User.id == user_id).first()


def require_user(request: Request, db: Session) -> User:
    user = get_current_user(request, db)

    if not user:
        raise PermissionError("Authentication required")

    return user
