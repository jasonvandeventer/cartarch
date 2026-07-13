"""Shared transactional email via Resend (#99).

Used by the watchlist price-alert job. The password-reset flow keeps its own
inline copy for now; it can adopt this later. Synchronous (batch callers, not a
request path), never raises, and no-ops in DEV_MODE or with no RESEND_API_KEY.
"""

from __future__ import annotations

import os

import requests

from app.timeutil import utc_now

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10
RESEND_FROM = "noreply@cartarch.com"
RESEND_REPLY_TO = "support@cartarch.com"


def send_email(to: str, subject: str, text: str) -> bool:
    """Send a plain-text email. In DEV_MODE or with no RESEND_API_KEY, log and
    no-op (returns True). A provider error logs and returns False — never raises,
    so a batch keeps sending the rest."""
    api_key = os.getenv("RESEND_API_KEY")
    if os.getenv("DEV_MODE", "false").lower() == "true" or not api_key:
        print(f"[email] dev/no-key path: would send to {to!r} subject={subject!r}", flush=True)
        return True
    try:
        resp = requests.post(
            RESEND_API_URL,
            json={
                "from": RESEND_FROM,
                "to": [to],
                "reply_to": RESEND_REPLY_TO,
                "subject": subject,
                "text": text,
            },
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=RESEND_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            print(
                f"[email] Resend non-2xx for {to}: {resp.status_code} {resp.text[:300]}", flush=True
            )
            return False
        print(f"[email] sent to {to} at {utc_now().isoformat()}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 — batch send must not raise
        print(f"[email] send error for {to}: {exc}", flush=True)
        return False
