"""A deep link must survive the login wall.

A phone that scans a game's join QR while signed out used to land on `/login`
with the code DISCARDED — the player standing at the table had no way back to
the game they had just scanned, and somebody without an account fared worse
still: register → /login → the dashboard, code long gone. That reads as "people
without a Cartarch account cannot join games", and most of it is this, not the
guest question (#172).

Fixed in `get_current_user`, the one place every auth wall goes through, so a
bookmarked deck or companion link is mended by the same change.

`next` is attacker-controlled at every hop, so it is validated on the way IN
(query param), on the way OUT (form field), and never trusted in between.
"""

from __future__ import annotations

from app.dependencies import safe_next_path

from .test_auth_csrf import _client_and_user, _csrf_token


def _login(client, token, next_value=None):
    data = {"username": "alice@example.com", "password": "pw123456", "csrf_token": token}
    if next_value is not None:
        data["next"] = next_value
    return client.post("/login", data=data, follow_redirects=False)


# ── The validator ───────────────────────────────────────────────────────────


def test_a_local_path_survives():
    assert safe_next_path("/join/CODE123") == "/join/CODE123"
    assert safe_next_path("/games/7?tab=seats") == "/games/7?tab=seats"


def test_an_offsite_destination_is_refused():
    """`startswith("/")` alone is NOT enough — a browser follows both of these
    off-host, which is the classic open redirect on a login form."""
    for hostile in ("//evil.com", "/\\evil.com", "https://evil.com", "evil.com", ""):
        assert safe_next_path(hostile) == "/", hostile


def test_control_characters_are_refused():
    """Nothing may smuggle a second header into the Location."""
    assert safe_next_path("/join/A\r\nSet-Cookie: x=1") == "/"


# ── The wall carries the destination ────────────────────────────────────────


def test_the_login_wall_remembers_where_you_were_going():
    client, main, dep = _client_and_user()
    try:
        # /join is deliberately PUBLIC since #172, so the wall is exercised on a
        # route that actually has one.
        r = client.get("/collection?page=2", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2Fcollection%3Fpage%3D2"
    finally:
        main.app.dependency_overrides.pop(dep, None)


# ── …and hands it back after sign-in ────────────────────────────────────────


def test_signing_in_lands_on_the_destination():
    client, main, dep = _client_and_user()
    try:
        page = client.get("/login?next=%2Fjoin%2FCODE123")
        assert 'name="next" value="/join/CODE123"' in page.text
        r = _login(client, _csrf_token(page.text), next_value="/join/CODE123")
        assert r.status_code == 303
        assert r.headers["location"] == "/join/CODE123"
    finally:
        main.app.dependency_overrides.pop(dep, None)


def test_signing_in_without_a_destination_still_lands_home():
    client, main, dep = _client_and_user()
    try:
        r = _login(client, _csrf_token(client.get("/login").text))
        assert r.headers["location"] == "/"
    finally:
        main.app.dependency_overrides.pop(dep, None)


def test_a_forged_destination_in_the_form_is_refused():
    """The hidden field is as attacker-controlled as the query param was — an
    emailed `/login?next=//evil.com` must not turn sign-in into a redirector."""
    client, main, dep = _client_and_user()
    try:
        r = _login(client, _csrf_token(client.get("/login").text), next_value="//evil.com")
        assert r.headers["location"] == "/"
    finally:
        main.app.dependency_overrides.pop(dep, None)


# ── Sign-up is part of the same journey ─────────────────────────────────────


def test_the_sign_up_link_carries_the_destination():
    client, main, dep = _client_and_user()
    try:
        body = client.get("/login?next=%2Fjoin%2FCODE123").text
        assert "/register?next=/join/CODE123" in body.replace("%2F", "/")
    finally:
        main.app.dependency_overrides.pop(dep, None)


def test_registering_hands_the_destination_on_to_login():
    """Registration deliberately does NOT auto-login (v3.27.17's enumeration
    defence), so it must pass `next` along rather than consume it."""
    client, main, dep = _client_and_user()
    try:
        page = client.get("/register?next=%2Fjoin%2FCODE123")
        assert 'name="next" value="/join/CODE123"' in page.text
        r = client.post(
            "/register",
            data={
                "username": "newbie@example.com",
                "password": "pw123456",
                "display_name": "Newbie",
                "csrf_token": _csrf_token(page.text),
                "next": "/join/CODE123",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2Fjoin%2FCODE123"
    finally:
        main.app.dependency_overrides.pop(dep, None)


def test_the_duplicate_email_response_stays_byte_identical():
    """The neutral duplicate response is an enumeration defence — carrying
    `next` must not make the two outcomes distinguishable."""
    client, main, dep = _client_and_user()
    try:
        page = client.get("/register?next=%2Fjoin%2FCODE123")
        r = client.post(
            "/register",
            data={
                "username": "alice@example.com",  # already seeded
                "password": "pw123456",
                "display_name": "Not Alice",
                "csrf_token": _csrf_token(page.text),
                "next": "/join/CODE123",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2Fjoin%2FCODE123"
    finally:
        main.app.dependency_overrides.pop(dep, None)
