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
- **Bounded against memory-exhaustion.** The tracking store is a bounded
  LRU (an ``OrderedDict`` capped at ``LOGIN_RATE_LIMIT_MAX_KEYS``), and
  empty windows are dropped on prune. An attacker spraying millions of
  randomized usernames or spoofed IPs can therefore only ever cost a
  fixed amount of memory: the oldest-touched key is evicted once the cap
  is reached. Crucially, ``is_login_throttled`` (the read path) NEVER
  inserts a key — only a confirmed failure does — so merely *probing*
  with fresh keys can't grow the store at all.
- **No CAPTCHA, no account lockout.** Purely throttling — a throttled
  attacker is told to wait; the account is never disabled.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

# Policy — change here if it changes. 5 failures per 15 minutes per key;
# the 6th attempt within the window is throttled.
LOGIN_RATE_LIMIT_WINDOW = timedelta(minutes=15)
LOGIN_RATE_LIMIT_MAX = 5

# Hard cap on the number of distinct (ip:/user:) keys tracked at once. The
# store is an LRU: once full, recording a new key evicts the least-recently-
# touched one. This bounds memory regardless of how many unique usernames /
# spoofed IPs an attacker throws at the endpoint. 50k keys × a handful of
# small datetimes each is a trivial footprint, well above any legitimate
# concurrent-failure population.
LOGIN_RATE_LIMIT_MAX_KEYS = 50_000

# LRU: most-recently-touched key at the end. Plain OrderedDict (NOT a
# defaultdict) so a read can never auto-insert an empty entry.
_fail_log: OrderedDict[str, list[datetime]] = OrderedDict()
_fail_lock = threading.Lock()


def _username_key(username: str) -> str:
    # Match the case-insensitive login canonicalization (get_user_by_username
    # lowercases), so casing variants share one counter.
    return f"user:{(username or '').strip().lower()}"


def _ip_key(client_ip: str | None) -> str:
    return f"ip:{(client_ip or '').strip() or 'unknown'}"


def _pruned_count(key: str, now: datetime) -> int:
    """Return the count of failures still inside the window for ``key``,
    pruning expired timestamps. Caller must hold ``_fail_lock``.

    READ-ONLY w.r.t. key creation: a missing key returns 0 WITHOUT being
    inserted (this is what keeps the read path from leaking memory). A key
    whose window has fully expired is DELETED so empty entries don't linger.
    """
    log = _fail_log.get(key)
    if log is None:
        return 0
    cutoff = now - LOGIN_RATE_LIMIT_WINDOW
    log[:] = [ts for ts in log if ts > cutoff]
    if not log:
        del _fail_log[key]
        return 0
    return len(log)


def is_login_throttled(username: str, client_ip: str | None) -> bool:
    """Return True if this attempt should be blocked (429) before auth.

    Throttled when EITHER the username OR the IP has already accumulated
    ``LOGIN_RATE_LIMIT_MAX`` failures in the window. Does NOT record
    anything — only a confirmed failure (``record_failed_login``) counts —
    and never inserts a tracking key, so probing can't exhaust memory.
    """
    now = datetime.now(UTC)
    with _fail_lock:
        user_count = _pruned_count(_username_key(username), now)
        ip_count = _pruned_count(_ip_key(client_ip), now)
    return user_count >= LOGIN_RATE_LIMIT_MAX or ip_count >= LOGIN_RATE_LIMIT_MAX


def _record_one(key: str, now: datetime) -> None:
    """Append a failure timestamp to ``key`` and mark it most-recently-used,
    evicting the oldest key if the store is at capacity. Caller holds lock."""
    _pruned_count(key, now)  # prune (and possibly drop) before touching
    _fail_log.setdefault(key, []).append(now)
    _fail_log.move_to_end(key)  # most-recently-touched → end of the LRU
    # Enforce the cap: evict least-recently-touched keys until under it.
    while len(_fail_log) > LOGIN_RATE_LIMIT_MAX_KEYS:
        _fail_log.popitem(last=False)


def record_failed_login(username: str, client_ip: str | None) -> None:
    """Record a failed login against both the username and IP counters."""
    now = datetime.now(UTC)
    with _fail_lock:
        _record_one(_username_key(username), now)
        _record_one(_ip_key(client_ip), now)


def reset_login_attempts(username: str) -> None:
    """Clear the username's failure counter after a successful login.

    The IP counter is intentionally left intact: a shared NAT / proxy IP
    succeeding for one user shouldn't wipe the throttle protecting other
    accounts behind the same IP.
    """
    with _fail_lock:
        _fail_log.pop(_username_key(username), None)
