"""GET /version — unauthenticated deploy-verification probe (Phase D item 1).

Pins the behaviours the spec names: 200, correct ``{"version": "vX.Y.Z"}`` shape,
version equals the newest Chronicle entry (the single guard-enforced source), and
it works logged out. Also asserts ``no-store`` — a deploy watcher must never read
a cached version.

Uses a bare ``TestClient`` with NO dependency overrides (no pinned user, no DB) —
so a green here is proof the route is genuinely public. /version depends on
neither ``get_current_user`` nor the DB, so no fixtures are needed.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app import main

client = TestClient(main.app)  # no overrides → logged-out, no DB session


def test_version_ok_and_shape():
    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"version"}  # exactly one key
    assert re.fullmatch(r"v\d+\.\d+\.\d+", body["version"]), body


def test_version_matches_chronicle_newest_entry():
    resp = client.get("/version")
    expected = "v" + main.CHRONICLE_ENTRIES[0]["version"].lstrip("v")
    assert resp.json()["version"] == expected


def test_version_is_no_store():
    resp = client.get("/version")
    assert resp.headers["cache-control"] == "no-store"


def test_version_works_logged_out():
    # The module client carries no session and no auth override; a public route
    # 200s, whereas an auth-guarded one would 303 → /login here.
    resp = client.get("/version")
    assert resp.status_code == 200
    assert resp.json()["version"].startswith("v")
