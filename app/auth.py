import secrets

from fastapi import Request
from pwdlib import PasswordHash
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import GUEST_USERNAME_DOMAIN, User

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


def create_guest_user(db: Session, display_name: str, *, commit: bool = True) -> User:
    """#172 — mint the account behind a guest seat claim.

    A guest is a REAL user row, not a second identity system (see the note on
    ``GUEST_USERNAME_DOMAIN``): their seat then attributes exactly like anyone
    else's, their commander resolves to a deck they own, and companion mode,
    turn authorization and the playgroup record all work with no change.

    **The password is unusable by construction** — a random secret nobody holds,
    hashed so the column format stays valid and ``verify_password`` simply
    returns False. Nobody signs in as a guest; the browser session IS the
    identity, and losing the cookie loses the identity. Reusing the same phone
    for the next game reuses the same guest, which is the useful half of that.

    The generated username is not user-supplied, so this opens no enumeration
    surface. Caller is responsible for the rate limit that matters: a claim
    needs a valid join code AND a free seat in a game that has not started.

    ``commit=False`` FLUSHES instead — the id is usable for dependent rows but
    nothing is written until the caller commits, so a claim that gets refused
    (game already started, seat taken) leaves no stray account. Same shape as
    ``create_deck(commit=False)``, and for the same reason #164 learned the hard
    way: a function that commits unconditionally makes its caller's guarantees a
    lie.
    """
    name = (display_name or "").strip()
    if not name:
        raise ValueError("A guest needs a name")
    user = User(
        username=f"guest-{secrets.token_hex(8)}@{GUEST_USERNAME_DOMAIN}",
        password_hash=hash_password(secrets.token_urlsafe(32)),
        display_name=name[:64],
        is_active=True,
    )
    db.add(user)
    if commit:
        db.commit()
    else:
        db.flush()
    return user


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
    # v3.33.1 — case-insensitive lookup so a case typo can't block sign-in.
    # Only caller is authenticate_user (login). Usernames are canonical
    # lowercase (registration / forgot-password / update-profile all lower()),
    # so a case-only collision isn't reachable; func.lower also rescues any
    # legacy mixed-case row. The users table is tiny, so the non-indexed
    # comparison is negligible.
    return (
        db.query(User).filter(func.lower(User.username) == (username or "").strip().lower()).first()
    )


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
