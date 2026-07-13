"""Watchlist price-alert emails (#99).

Runs daily after the price ingest (piggybacked in ``price_ingest.main()``). Each
opted-in user gets ONE digest listing the watched cards that have reached their
``target_price`` since the last alert. Dedup lives on the watch row
(``last_alerted_at``): a crossing fires once and re-arms only when the price
rises back above target, so a card sitting below target never re-spams.

Target-cross only (#99a). Movement/±% alerts (#99b) are a fast-follow now that
the price history exists.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.email import send_email
from app.models import User, WatchlistItem
from app.timeutil import utc_now
from app.watchlist_service import list_watchlist

_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://cartarch.com").rstrip("/")


def _digest_body(user: User, crossings: list[dict]) -> str:
    hello = f"Hi {user.display_name}," if user.display_name else "Hi,"
    lines = [hello, "", "These watched cards have reached your target price:", ""]
    for r in crossings:
        cur = r.get("current_min_price") or 0.0
        tgt = r.get("target_price") or 0.0
        lines.append(f"  - {r['display_name']}: now ${cur:.2f} (target ${tgt:.2f})")
    lines += [
        "",
        f"Your wishlist: {_BASE_URL}/watchlist",
        "",
        "You're getting this because price alerts are on. Turn them off any time in Account settings.",
    ]
    return "\n".join(lines)


def run_alerts(session: Session) -> int:
    """Evaluate opted-in users' watchlists, email fresh target-crosses as a
    per-user digest, and update dedup state. Returns the number of emails sent."""
    now = utc_now()
    sent = 0
    users = (
        session.query(User)
        .filter(User.price_alerts_enabled.is_(True), User.is_active.is_(True))
        .all()
    )
    for user in users:
        crossings = []  # (dict_row, watch) for fresh, not-yet-alerted crossings
        for r in list_watchlist(session, user.id):
            watch = session.get(WatchlistItem, r["id"])
            if watch is None:
                continue
            if not r.get("target_met"):
                watch.last_alerted_at = None  # re-arm once back above target
                continue
            if watch.last_alerted_at is None:  # a fresh crossing
                crossings.append((r, watch))
        if not crossings or not user.username:
            continue
        # Stamp the dedup state only on a successful send, so a provider failure
        # retries next run instead of silently swallowing the alert.
        if send_email(
            user.username,
            "Cartarch price alert: watched cards hit your target",
            _digest_body(user, [r for r, _ in crossings]),
        ):
            for r, watch in crossings:
                watch.last_alerted_at = now
                watch.last_alerted_price = r.get("current_min_price")
            sent += 1
    session.commit()
    return sent


def main() -> None:
    session = SessionLocal()
    try:
        n = run_alerts(session)
        print(f"[price-alerts] sent {n} digest email(s)", flush=True)
    finally:
        session.close()


if __name__ == "__main__":
    main()
