"""Exercise the running image over HTTP, with real cookies and CSRF."""

import os
import re

import requests

base = os.environ.get("SMOKE_URL", "http://127.0.0.1:5581")
client = requests.Session()


def get(path):
    response = client.get(base + path, timeout=15)
    assert response.status_code == 200, (path, response.status_code)
    return response


def csrf(html):
    return re.search(r'name="csrf_token"\s+value="([^"]+)"', html)[1]


assert get("/health").json() == {"status": "ok"}
if (expected := os.getenv("EXPECTED_VERSION", "")).startswith("v"):
    assert get("/version").json()["version"] == expected
response = client.post(
    base + "/login",
    data={
        "username": "smoke@example.invalid",
        "password": "local-smoke-password",
        "csrf_token": csrf(get("/login").text),
    },
    allow_redirects=False,
    timeout=15,
)
assert response.status_code == 303, response.text
for path in (
    "/",
    "/account",
    "/collection",
    "/decks",
    "/decks/1",
    "/games",
    "/locations",
    "/trades",
):
    get(path)
menu = get("/decks/1/rows/1/actions").text
assert 'name="quantity"' in menu
response = client.post(
    base + "/decks/1/rows/1/set-qty",
    data={"quantity": 12, "csrf_token": csrf(menu)},
    headers={"HX-Request": "true"},
    timeout=15,
)
assert response.status_code == 200, response.text
assert "111 Total Cards" in response.text
assert "111 Total Cards" in get("/decks/1").text
print(
    "PASS image startup, readiness, authenticated routes, CSRF, lazy actions and quantity mutation"
)
