"""Login brute-force throttling (S1).

A per-IP + per-username sliding-window limit on ``POST /login`` failed
attempts. After ``LOGIN_RATE_LIMIT_MAX`` failures within
``LOGIN_RATE_LIMIT_WINDOW`` for either key, the next attempt is throttled
(the route returns 429) BEFORE the password is checked, so an attacker
can't keep guessing for free.

Design notes:

- **Counts FAILURES, not all attempts.** Unlike the password-reset
  throttle (which counts every request), only a *failed* login records a
  timestamp. A successful login RESETS the username counter, so a
  legitimate user who finally types the right password isn't locked out
  by their own earlier typos.
- **Two keys, OR semantics.** Throttled if EITHER the IP or the username
  has exceeded the limit. Per-IP stops a single host hammering many
  usernames; per-username stops a botnet (many IPs) hammering one
  account.
- **In-memory only (v1).** In-process time-windowed counters, same shape
  as ``password_reset_service``. With the current single-pod deployment
  this is the effective limit; a multi-replica deployment would need
  shared state (Redis or a table). Acceptable for current scale — no
  Redis dependency introduced.
- **No CAPTCHA, no account lockout.** Purely throttling — a throttled
  attacker is told to wait; the account is never disabled.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta

# Policy — change here if it changes. 5 failures per 15 minutes per key;
# the 6th attempt within the window is throttled.
LOGIN_RATE_LIMIT_WINDOW = timedelta(minutes=15)
LOGIN_RATE_LIMIT_MAX = 5

_fail_log: dict[str, list[datetime]] = defaultdict(list)
_fail_lock = threading.Lock()


def _username_key(username: str) -> str:
    # Match the case-insensitive login canonicalization (get_user_by_username
    # lowercases), so casing variants share one counter.
    return f"user:{(username or '').strip().lower()}"


def _ip_key(client_ip: str | None) -> str:
    return f"ip:{(client_ip or '').strip() or 'unknown'}"


def _pruned_count(key: str, now: datetime) -> int:
    """Prune timestamps older than the window (in place) and return the
    surviving count. Caller must hold ``_fail_lock``."""
    log = _fail_log[key]
    cutoff = now - LOGIN_RATE_LIMIT_WINDOW
    log[:] = [ts for ts in log if ts > cutoff]
    return len(log)


def is_login_throttled(username: str, client_ip: str | None) -> bool:
    """Return True if this attempt should be blocked (429) before auth.

    Throttled when EITHER the username OR the IP has already accumulated
    ``LOGIN_RATE_LIMIT_MAX`` failures in the window. Does NOT record
    anything — only a confirmed failure (``record_failed_login``) counts.
    Prunes on every check so the dicts don't grow unbounded.
    """
    now = datetime.now(UTC)
    with _fail_lock:
        user_count = _pruned_count(_username_key(username), now)
        ip_count = _pruned_count(_ip_key(client_ip), now)
    return user_count >= LOGIN_RATE_LIMIT_MAX or ip_count >= LOGIN_RATE_LIMIT_MAX


def record_failed_login(username: str, client_ip: str | None) -> None:
    """Record a failed login against both the username and IP counters."""
    now = datetime.now(UTC)
    with _fail_lock:
        _pruned_count(_username_key(username), now)
        _fail_log[_username_key(username)].append(now)
        _pruned_count(_ip_key(client_ip), now)
        _fail_log[_ip_key(client_ip)].append(now)


def reset_login_attempts(username: str) -> None:
    """Clear the username's failure counter after a successful login.

    The IP counter is intentionally left intact: a shared NAT / proxy IP
    succeeding for one user shouldn't wipe the throttle protecting other
    accounts behind the same IP.
    """
    with _fail_lock:
        _fail_log.pop(_username_key(username), None)
