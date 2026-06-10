"""Password reset service (v3.27.14).

Self-service password recovery. Generates and validates reset tokens
and queues the reset email asynchronously off the request path.

Security-critical design (do NOT improvise away from these — the
v3.27.14 spec calls these out by name):

- **Tokens are hashed at rest.** ``secrets.token_urlsafe(32)`` produces
  the raw token; only ``hashlib.sha256(token).hexdigest()`` is stored.
  The raw token exists only in the emailed link. Reset validation
  hashes the incoming URL token and looks up by hash.

- **SHA-256 is the right hash for tokens.** The raw token is ~43
  characters of base64 from a CSPRNG — high entropy. A slow password
  hasher (bcrypt/argon2/scrypt) would slow every verification with
  no security gain; we're not protecting a low-entropy user secret.
  ``app/auth.py:hash_password`` (pwdlib) stays the right choice for
  user passwords.

- **30-minute lifetime.** ``expires_at = created_at + 30min``; checked
  at validation time. Past-expiry tokens never validate.

- **Single-use.** ``used_at`` is set on successful reset. A row with
  ``used_at IS NOT NULL`` is dead — never validates again.

- **Invalidate-on-new-request.** A new reset request DELETEs the
  user's existing unused tokens before creating the new one. At most
  one outstanding token per user.

- **Email send is asynchronous.** The request handler validates,
  queues the send via a daemon thread, returns immediately. The
  Resend API call NEVER blocks the request handler. Required by:
  (1) the request-path network invariant; (2) the enumeration
  defense — a synchronous send on the registered-email path would
  leak timing info that distinguishes "registered" from "not
  registered" even when responses are byte-identical.

- **Enumeration parity.** ``POST /forgot-password`` returns the
  IDENTICAL response for registered vs unregistered emails — same
  page, same message, same status. Registered → token + queued email.
  Unregistered → nothing. The async send means no timing leak.

- **Rate limiting.** In-process time-windowed counter, keyed by
  email AND by IP. A few requests per hour. Exceeded limits silently
  drop the request (still return the neutral response — leaking
  rate-limit info would itself be an enumeration oracle). Failed
  requests are logged.

The Resend API key is read from ``RESEND_API_KEY`` env var (wired in
``mana-archive-platform`` via Kubernetes Secret + secretKeyRef).
``app/main.py``'s startup check refuses to boot in production
without it. In DEV_MODE the absence is tolerated; emails simply
log instead of being sent (preserves the local-dev story without
adding a fake-SMTP dependency).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import requests
from sqlalchemy.orm import Session

from app.models import PasswordResetToken, User
from app.timeutil import utc_now

# Token lifetime + email retry config — change here if the policy changes.
TOKEN_LIFETIME = timedelta(minutes=30)
RESEND_API_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10
RESEND_FROM = "noreply@cartarch.com"
RESEND_REPLY_TO = "support@cartarch.com"

# Rate limiting — a few requests per hour per key. Two keys: email + IP.
# Exceeded → silently drop (still return neutral response). Sufficient to
# stop abuse without inventing a denial-of-service oracle for attackers.
# In-process state: with a single-pod deployment (the current shape) this
# is the per-replica limit; multi-replica deployments would need shared
# state (Redis, or a dedicated rate-limit table). Acceptable for current
# scale.
RESET_RATE_LIMIT_WINDOW = timedelta(hours=1)
RESET_RATE_LIMIT_MAX = 5
_rate_log: dict[str, list[datetime]] = defaultdict(list)
_rate_lock = threading.Lock()


def _hash_token(raw_token: str) -> str:
    """SHA-256 hex digest of the raw token. The only form we store."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _check_and_record_rate_limit(key: str) -> bool:
    """Return True if within the limit, False if exceeded.

    Time-windowed counter; prunes timestamps older than the window
    on every check so the dict doesn't grow unbounded. Thread-safe
    via the module lock (multiple daemon threads or concurrent
    requests could otherwise race on the list mutation).
    """
    now = datetime.now(UTC)
    with _rate_lock:
        log = _rate_log[key]
        cutoff = now - RESET_RATE_LIMIT_WINDOW
        # Prune in place.
        log[:] = [ts for ts in log if ts > cutoff]
        if len(log) >= RESET_RATE_LIMIT_MAX:
            return False
        log.append(now)
        return True


def check_rate_limits(email: str, client_ip: str | None) -> bool:
    """Combined per-email + per-IP rate-limit check.

    Returns True if BOTH limits have headroom, False otherwise. Both
    sides record a request on success; one of them recording while
    the other returns False would leak signal via remaining-quota
    asymmetry. (Pragmatically the cost is one extra "request" being
    counted toward the side that succeeded; acceptable.)

    Callers should treat a False return as "silently drop this
    reset request" — DO NOT change the response shape. Rate-limit
    info must not become an enumeration oracle.
    """
    email_key = f"email:{(email or '').strip().lower()}"
    ip_key = f"ip:{(client_ip or '').strip() or 'unknown'}"
    email_ok = _check_and_record_rate_limit(email_key)
    ip_ok = _check_and_record_rate_limit(ip_key)
    return email_ok and ip_ok


def _invalidate_existing_tokens(session: Session, user_id: int) -> None:
    """DELETE the user's unused tokens. Caller is responsible for commit.

    Used by ``create_reset_token`` to enforce at-most-one-outstanding.
    Used tokens (``used_at IS NOT NULL``) are preserved as audit
    breadcrumbs even though they no longer validate.
    """
    session.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user_id,
        PasswordResetToken.used_at.is_(None),
    ).delete(synchronize_session=False)


def create_reset_token(session: Session, user: User) -> str:
    """Create a new reset token for the user. Returns the RAW token.

    The raw token is the only place the unhashed value exists — it
    goes into the emailed link, never persisted. Caller is
    responsible for session.commit().

    Invalidates the user's existing unused tokens first
    (at-most-one-outstanding invariant).
    """
    _invalidate_existing_tokens(session, user.id)
    raw_token = secrets.token_urlsafe(32)
    now = utc_now()
    row = PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        expires_at=now + TOKEN_LIFETIME,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return raw_token


def find_valid_token(session: Session, raw_token: str) -> PasswordResetToken | None:
    """Look up an unexpired, unused token by hashing the raw value.

    Returns the row if the token is valid (not expired, not used);
    returns None otherwise. Does NOT mark the token used — that's
    ``consume_token``'s job, called by the POST /reset-password route
    after the password is successfully changed.
    """
    if not raw_token:
        return None
    token_hash = _hash_token(raw_token.strip())
    row = (
        session.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == token_hash)
        .first()
    )
    if row is None:
        return None
    if row.used_at is not None:
        return None
    if row.expires_at <= utc_now():
        return None
    return row


def consume_token(session: Session, token_row: PasswordResetToken) -> None:
    """Mark a token used + invalidate the user's other unused tokens.

    Called by POST /reset-password after the password is successfully
    changed. Caller is responsible for session.commit(). Sets
    ``used_at`` on the row (preserving audit breadcrumb) and DELETEs
    other unused tokens for the same user (defense in depth — an
    attacker who somehow has a second outstanding token can't use it
    after the first one resets the password).
    """
    token_row.used_at = utc_now()
    session.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == token_row.user_id,
        PasswordResetToken.id != token_row.id,
        PasswordResetToken.used_at.is_(None),
    ).delete(synchronize_session=False)


# ---- Email send (async, off the request path) ----------------------


def queue_reset_email(email: str, reset_url: str, expires_at: datetime) -> None:
    """Spawn a daemon thread to send the reset email. Returns immediately.

    Mirrors the daemon-thread precedent from ``_price_refresh_loop``
    etc. in ``app/main.py``. Per-send threads are fine for this
    volume (rate-limited above; low-traffic feature); a full queue +
    worker pool is overkill for one email per request. Failure inside
    the thread is logged with diagnostic detail — never silently
    swallowed.

    DEV_MODE handling: when ``DEV_MODE=true`` or ``RESEND_API_KEY`` is
    unset, the function logs the reset URL to stdout instead of
    sending. Lets local dev exercise the full flow without a real
    email provider.
    """
    thread = threading.Thread(
        target=_send_reset_email,
        args=(email, reset_url, expires_at),
        daemon=True,
        name=f"reset-email-{email[:16]}",
    )
    thread.start()


def _send_reset_email(email: str, reset_url: str, expires_at: datetime) -> None:
    """Body of the daemon-thread send. Catches and logs all failures.

    DO NOT raise — this runs in a daemon thread; an unhandled
    exception would die silently while looking like the email was
    queued. Catch broadly, log with diagnostic detail, return.
    """
    api_key = os.getenv("RESEND_API_KEY")
    dev_mode = os.getenv("DEV_MODE", "false").lower() == "true"

    if dev_mode or not api_key:
        # Local dev / missing key: log the reset URL and exit. The
        # production startup check in app/main.py enforces that
        # RESEND_API_KEY is set when DEV_MODE is not "true", so this
        # branch is only reachable in dev or in a misconfigured
        # production (which the startup check prevents from booting).
        print(
            f"[password-reset] dev/no-key path: would send to {email}; "
            f"reset_url={reset_url} expires_at={expires_at.isoformat()}",
            flush=True,
        )
        return

    body_text = _build_reset_email_body(reset_url, expires_at)
    payload = {
        "from": RESEND_FROM,
        "to": [email],
        "reply_to": RESEND_REPLY_TO,
        "subject": "Reset your Cartarch password",
        "text": body_text,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            RESEND_API_URL,
            json=payload,
            headers=headers,
            timeout=RESEND_TIMEOUT_SECONDS,
        )
        if not response.ok:
            # Log the error body for diagnostic detail. Never raise
            # — the user already saw the neutral success response.
            print(
                f"[password-reset] Resend API non-2xx for {email}: "
                f"status={response.status_code} body={response.text[:500]}",
                flush=True,
            )
            return
        # Success — log lightly so we have an audit trail.
        print(
            f"[password-reset] sent to {email} at {utc_now().isoformat()}",
            flush=True,
        )
    except requests.RequestException as exc:
        print(
            f"[password-reset] Resend send failed for {email}: {type(exc).__name__}: {exc}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 — daemon thread, never raise
        print(
            f"[password-reset] unexpected error sending to {email}: {type(exc).__name__}: {exc}",
            flush=True,
        )


def _build_reset_email_body(reset_url: str, expires_at: datetime) -> str:
    """Plain-text reset email body. No marketing, no HTML.

    States the 30-minute expiry plainly, includes the ignore-if-not-
    requested note, and points support questions at the real Proton
    mailbox.
    """
    return (
        "You (or someone using your email) requested a password reset for "
        "your Cartarch account.\n"
        "\n"
        f"Reset link: {reset_url}\n"
        "\n"
        f"This link expires in 30 minutes (at "
        f"{expires_at.strftime('%Y-%m-%d %H:%M UTC')}). It can only be "
        "used once.\n"
        "\n"
        "If you didn't request this, ignore this email — your password "
        "hasn't been changed.\n"
        "\n"
        "Questions: reply to this email or write to support@cartarch.com.\n"
    )
