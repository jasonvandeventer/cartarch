"""#179 — the read-only /api/v1 surface and its bearer-token gate.

The routes themselves are thin delegations into handlers that already have their
own suites (`test_collection_export.py`, `test_deck_export.py`), so what needs
pinning here is the AUTH boundary — every way a bad or absent token could be
mistaken for a good one, and the owner scoping of what a good one reaches.
"""

from __future__ import annotations

import pytest

from app.models import Card, Deck, InventoryRow, StorageLocation, User

TOKEN = "test-api-token-aaaaaaaaaaaaaaaaaaaaaaa"
OTHER_TOKEN = "test-api-token-bbbbbbbbbbbbbbbbbbbbbbb"

ALL_ROUTES = ["/api/v1/me", "/api/v1/collection", "/api/v1/decks", "/api/v1/decks/1"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def tokened_user(db, user):
    user.api_token = TOKEN
    db.commit()
    return user


def _seed_card(db, name="Rhystic Study", scryfall_id="sid-1", collector="1"):
    card = Card(
        scryfall_id=scryfall_id,
        name=name,
        set_code="tst",
        collector_number=collector,
        type_line="Enchantment",
        color_identity="U",
        colors="U",
    )
    db.add(card)
    db.commit()
    return card


def _seed_deck(db, owner, name="Test Deck", deck_format="Commander"):
    loc = StorageLocation(user_id=owner.id, name=name, type="deck", mode="manual")
    db.add(loc)
    db.commit()
    deck = Deck(user_id=owner.id, storage_location_id=loc.id, name=name, format=deck_format)
    db.add(deck)
    db.commit()
    return deck, loc


# --------------------------------------------------------------------------
# The gate: every way in that must NOT work.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_ROUTES)
def test_no_header_is_401_json_never_a_redirect(client, tokened_user, path):
    """The whole reason require_api_user is not get_current_user.

    ``get_current_user`` answers a missing session with a **303 to /login** (the
    ``?next=`` deep-link behaviour). A bot receiving a redirect and an HTML login
    page in answer to a bad token has been told nothing.
    """
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")
    assert "login" not in resp.headers.get("location", "")


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},  # scheme, no credential
        {"Authorization": "Bearer "},  # scheme, empty credential
        {"Authorization": TOKEN},  # credential, no scheme
        {"Authorization": f"Basic {TOKEN}"},  # wrong scheme
        {"Authorization": f"Bearer {TOKEN}x"},  # near-miss credential
        {"Authorization": f"Bearer {TOKEN.upper()}"},  # credential is case-SENSITIVE
    ],
    ids=[
        "absent",
        "empty",
        "scheme-only",
        "empty-credential",
        "no-scheme",
        "wrong-scheme",
        "near-miss",
        "wrong-case-credential",
    ],
)
def test_malformed_authorization_is_rejected(client, tokened_user, header):
    assert client.get("/api/v1/me", headers=header, follow_redirects=False).status_code == 401


def test_an_empty_credential_never_authenticates(client, db, user):
    """The ``if supplied:`` guard, tested where it actually bites.

    A NULL ``api_token`` alone would not prove this: ``NULL = ''`` is NULL, not
    true, in both dialects, so an unguarded query happens to miss and the test
    passes vacuously. An EMPTY-STRING token is the case the guard exists for —
    without it, one degenerate row would authenticate every client that sends a
    bare ``Bearer`` (or, once the scheme check blanks a bad scheme, any client
    at all).
    """
    user.api_token = ""
    db.commit()

    for header in ({}, {"Authorization": "Bearer "}, {"Authorization": "Basic x"}):
        resp = client.get("/api/v1/me", headers=header, follow_redirects=False)
        assert resp.status_code == 401, header


def test_bearer_scheme_is_case_insensitive(client, tokened_user):
    """RFC 7235 — the auth-scheme token is case-insensitive, the credential is not."""
    for scheme in ("Bearer", "bearer", "BEARER", "BeArEr"):
        resp = client.get("/api/v1/me", headers={"Authorization": f"{scheme} {TOKEN}"})
        assert resp.status_code == 200, scheme


def test_401_carries_www_authenticate(client, tokened_user):
    resp = client.get("/api/v1/me", follow_redirects=False)
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_an_inactive_user_cannot_use_a_live_token(client, db, tokened_user):
    tokened_user.is_active = False
    db.commit()
    assert client.get("/api/v1/me", headers=_bearer(TOKEN)).status_code == 401


@pytest.mark.parametrize("path", ALL_ROUTES)
def test_every_response_is_no_store(client, db, tokened_user, path):
    """Per-user payloads must carry a cache directive, as ``render()`` gives pages.

    Nothing on this router goes through ``render()``, so without ``_no_store``
    the API would be the only per-user surface behind the CDN shipping none at
    all. Parametrized over every route because the two DELEGATING ones are the
    easy misses — they return a JSONResponse the handler built, which an
    injected ``Response`` parameter would never touch.
    """
    deck, _loc = _seed_deck(db, tokened_user)
    path = path.replace("/decks/1", f"/decks/{deck.id}")  # a real 200, not a 404
    resp = client.get(path, headers=_bearer(TOKEN))
    assert resp.status_code == 200, path
    assert resp.headers.get("cache-control") == "no-store", path


# --------------------------------------------------------------------------
# The gate: what a good token reaches.
# --------------------------------------------------------------------------


def test_me_identifies_the_token_owner(client, tokened_user):
    body = client.get("/api/v1/me", headers=_bearer(TOKEN)).json()
    assert body == {
        "id": tokened_user.id,
        "username": tokened_user.username,
        "display_name": tokened_user.display_name,
    }


def test_collection_returns_the_callers_cards(client, db, tokened_user):
    card = _seed_card(db)
    db.add(InventoryRow(user_id=tokened_user.id, card_id=card.id, quantity=2, finish="normal"))
    db.commit()

    body = client.get("/api/v1/collection", headers=_bearer(TOKEN)).json()
    names = [c["name"] for c in body["cards"]]
    assert names == ["Rhystic Study"]
    assert body["cards"][0]["quantity"] == 2


def test_collection_honours_the_search_filter(client, db, tokened_user):
    """Taking ``collection_filter`` as a dependency is what buys this for free —
    a bot answering "do I own X" fetches one card, not the 4.9 MB collection."""
    keep = _seed_card(db, name="Rhystic Study", scryfall_id="sid-keep", collector="1")
    drop = _seed_card(db, name="Sol Ring", scryfall_id="sid-drop", collector="2")
    for c in (keep, drop):
        db.add(InventoryRow(user_id=tokened_user.id, card_id=c.id, quantity=1, finish="normal"))
    db.commit()

    body = client.get("/api/v1/collection?search=Rhystic", headers=_bearer(TOKEN)).json()
    assert [c["name"] for c in body["cards"]] == ["Rhystic Study"]


def test_decks_lists_name_format_and_count(client, db, tokened_user):
    deck, loc = _seed_deck(db, tokened_user)
    card = _seed_card(db)
    db.add(
        InventoryRow(
            user_id=tokened_user.id,
            card_id=card.id,
            quantity=3,
            finish="normal",
            storage_location_id=loc.id,
        )
    )
    db.commit()

    body = client.get("/api/v1/decks", headers=_bearer(TOKEN)).json()
    assert body["decks"] == [
        {"id": deck.id, "name": "Test Deck", "format": "Commander", "card_count": 3}
    ]


def test_decks_excludes_retired_decks(client, db, tokened_user):
    """#163 — a retired deck is invisible everywhere a deleted one used to be.

    The four game surfaces that hand-rolled ``session.query(Deck)`` and missed
    this filter are why the API goes through ``list_decks_basic``.
    """
    from app.timeutil import utc_now

    deck, _loc = _seed_deck(db, tokened_user)
    deck.retired_at = utc_now()
    db.commit()

    assert client.get("/api/v1/decks", headers=_bearer(TOKEN)).json()["decks"] == []


def test_deck_detail_returns_cards_and_rollup(client, db, tokened_user):
    deck, loc = _seed_deck(db, tokened_user)
    card = _seed_card(db)
    db.add(
        InventoryRow(
            user_id=tokened_user.id,
            card_id=card.id,
            quantity=1,
            finish="normal",
            storage_location_id=loc.id,
        )
    )
    db.commit()

    body = client.get(f"/api/v1/decks/{deck.id}", headers=_bearer(TOKEN)).json()
    assert body["deck"]["id"] == deck.id
    assert [c["name"] for c in body["cards"]] == ["Rhystic Study"]
    assert body["rollup"]["color_identity"] == ["U"]


# --------------------------------------------------------------------------
# Owner scoping — one user's token must never reach another user's data.
# --------------------------------------------------------------------------


@pytest.fixture
def other_user(db):
    u = User(username="other@example.com", password_hash="x", api_token=OTHER_TOKEN)
    db.add(u)
    db.commit()
    return u


def test_a_token_never_reads_another_users_collection(client, db, tokened_user, other_user):
    card = _seed_card(db)
    db.add(InventoryRow(user_id=other_user.id, card_id=card.id, quantity=1, finish="normal"))
    db.commit()

    body = client.get("/api/v1/collection", headers=_bearer(TOKEN)).json()
    assert body["cards"] == []


def test_a_token_never_reads_another_users_deck(client, db, tokened_user, other_user):
    deck, _loc = _seed_deck(db, other_user, name="Someone Elses Deck")

    assert client.get("/api/v1/decks", headers=_bearer(TOKEN)).json()["decks"] == []
    # 404, not 403 — another user's deck id is indistinguishable from a
    # nonexistent one, which is get_deck's existing posture.
    assert client.get(f"/api/v1/decks/{deck.id}", headers=_bearer(TOKEN)).status_code == 404


# --------------------------------------------------------------------------
# Token lifecycle from the account page.
# --------------------------------------------------------------------------


def test_generate_then_revoke_flips_api_access(client, db, user):
    """The token IS the toggle — revoking must 401 the very next request."""
    assert client.get("/api/v1/me", follow_redirects=False).status_code == 401

    client.post("/account/api-token", follow_redirects=False)
    db.refresh(user)
    token = user.api_token
    assert token
    assert client.get("/api/v1/me", headers=_bearer(token)).status_code == 200

    client.post("/account/api-token/revoke", follow_redirects=False)
    db.refresh(user)
    assert user.api_token is None
    assert (
        client.get("/api/v1/me", headers=_bearer(token), follow_redirects=False).status_code == 401
    )


def test_regenerating_invalidates_the_previous_token(client, db, user):
    """Regenerate is the revocation story for a leaked token — the old one must die."""
    client.post("/account/api-token", follow_redirects=False)
    db.refresh(user)
    first = user.api_token

    client.post("/account/api-token", follow_redirects=False)
    db.refresh(user)
    second = user.api_token

    assert first != second
    assert (
        client.get("/api/v1/me", headers=_bearer(first), follow_redirects=False).status_code == 401
    )
    assert client.get("/api/v1/me", headers=_bearer(second)).status_code == 200


def test_the_account_page_renders_the_token_controls(client, db, user):
    """Route-level, not service-level — the #152 failure mode (a service test
    cannot see a context key that never reaches the template)."""
    page = client.get("/account").text
    assert "/account/api-token" in page
    assert "Generate API token" in page

    client.post("/account/api-token", follow_redirects=False)
    db.refresh(user)
    page = client.get("/account").text
    assert user.api_token in page
    assert "/account/api-token/revoke" in page
