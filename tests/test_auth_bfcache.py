"""Issue #31 (hypothesis #1) — bfcache hardening on the public pre-auth forms.

Firefox can restore a pre-auth form (login / register / forgot- / reset-password)
from its back/forward cache (bfcache); if the session cookie has since changed,
the restored form submits and surfaces as "session expired". The fix is two
defenses applied at the auth GET seam:

  1. ``render_auth_page`` sets ``Cache-Control: no-store`` + ``Pragma: no-cache``
     on those GET responses (discouraging bfcache).
  2. ``_auth_layout.html`` carries a ``pageshow`` handler that forces a fresh
     load (``location.reload()``) when ``event.persisted`` is true, so a page a
     browser restores anyway re-fetches its session cookie + CSRF token.

NOT unit-testable here: the bfcache restore behaviour itself is a browser
concern (`event.persisted`) and requires MANUAL FIREFOX VERIFICATION —
open login, navigate away, hit Back, submit (and idle-then-submit). These
tests pin the server-side header contract and the presence of the client
handler, which is what regressed-away would silently re-break the fix.

Deliberately NOT asserted: CSRF-token rotation per GET. Option A (see the
issue #31 discussion) dropped rotation — the logged-out token is sticky, so a
restored form still matches its session; rotating would only create a multi-tab
mismatch on the path ``tests/test_auth_csrf.py`` pins to 403.

    pytest tests/test_auth_bfcache.py
"""

from __future__ import annotations

import pytest

# Every public pre-auth GET that renders a CSRF-carrying form. ``?token=`` is
# empty on reset-password → the invalid-token branch, which also renders via
# render_auth_page, so the header contract still applies.
AUTH_GET_PATHS = [
    "/login",
    "/register",
    "/forgot-password",
    "/reset-password",
]


@pytest.mark.parametrize("path", AUTH_GET_PATHS)
def test_auth_get_sets_bfcache_hostile_headers(client, path):
    """Each pre-auth GET carries no-store + Pragma: no-cache (defense #1)."""
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    assert r.headers.get("cache-control") == "no-store", (
        f"{path} cache-control={r.headers.get('cache-control')!r}"
    )
    assert r.headers.get("pragma") == "no-cache", f"{path} pragma={r.headers.get('pragma')!r}"


@pytest.mark.parametrize("path", AUTH_GET_PATHS)
def test_auth_page_carries_pageshow_reload_handler(client, path):
    """The shared _auth_layout.html ships the bfcache pageshow reload (defense
    #2) on every pre-auth page — not just login."""
    html = client.get(path).text
    assert "pageshow" in html, f"{path} missing pageshow handler"
    assert "event.persisted" in html, f"{path} pageshow not gated on event.persisted"
    assert "location.reload" in html, f"{path} pageshow does not force a reload"


def test_pragma_is_auth_scoped_not_global(client):
    """``Pragma: no-cache`` is added at the auth seam ONLY. An authenticated
    app page still gets no-store (from render(), v3.31.0) but NOT Pragma — pins
    the 'not globally' scope from the issue #31 plan.
    """
    r = client.get("/collection")
    assert r.status_code == 200, f"/collection -> {r.status_code}"
    assert r.headers.get("cache-control") == "no-store"
    assert r.headers.get("pragma") is None, (
        f"/collection unexpectedly carries Pragma={r.headers.get('pragma')!r}"
    )
