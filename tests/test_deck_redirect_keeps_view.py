"""A POST on the deck page must not throw away the view you were looking at.

Reported 2026-08-16: "Moving a card to reconsidering sorta reloads the page —
it takes you right back to your same position on the deck page, but it resets
the sort order."

Every POST on the deck page answered with a bare ``/decks/{id}``, so any
``?sort=``/``?group=``/``?search=`` in the URL was discarded and the page came
back in default order. **The reported action was one of 17 routes with the
identical line** — Move to Considering was simply the one that got used. Fixing
only that route would have left the same complaint waiting behind Remove from
Deck, Switch Printing, Retag, bump-quantity and the rest.

The POST's own URL has no query (the forms post to action paths), so the view
can only come from the Referer. ``_deck_redirect`` copies **only known view
keys** onto a **locally built** path, so a forged Referer cannot redirect
anyone off-site — which is why it does not reuse ``safe_redirect_url``, whose
whole job is to return the Referer itself.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.routes.decks import _DECK_VIEW_PARAMS, _deck_redirect

_SRC = pathlib.Path(__file__).resolve().parents[1] / "app" / "routes" / "decks.py"


class _FakeURL:
    netloc = "cartarch.test"


class _FakeRequest:
    """Only what ``_deck_redirect`` reads: a Referer header and our own netloc."""

    def __init__(self, referer: str | None):
        self.headers = {"referer": referer} if referer is not None else {}
        self.url = _FakeURL()


def _target(referer: str | None, deck_id: int = 7) -> str:
    return _deck_redirect(_FakeRequest(referer), deck_id).headers["location"]


# --------------------------------------------------------------------------- #
# The behaviour that was reported
# --------------------------------------------------------------------------- #


def test_the_sort_survives_the_redirect():
    got = _target("https://cartarch.test/decks/7?sort=cmc&direction=desc")
    assert "sort=cmc" in got and "direction=desc" in got


@pytest.mark.parametrize("param", _DECK_VIEW_PARAMS)
def test_every_view_axis_survives(param):
    """Parametrized over the axes themselves, so adding one to the page without
    adding it here is visible rather than silently unpreserved."""
    got = _target(f"https://cartarch.test/decks/7?{param}=zzz")
    assert f"{param}=zzz" in got, f"{param} was dropped"


def test_no_referer_falls_back_to_the_bare_deck_page():
    assert _target(None) == "/decks/7"
    assert _target("") == "/decks/7"


def test_a_referer_from_a_different_page_contributes_nothing():
    """Only THIS deck's page describes this deck's view. A sort carried off the
    Decks index or another deck would be someone else's state."""
    assert _target("https://cartarch.test/decks/9?sort=cmc") == "/decks/7"
    assert _target("https://cartarch.test/collection?sort=cmc") == "/decks/7"


# --------------------------------------------------------------------------- #
# The parts that must NOT be carried
# --------------------------------------------------------------------------- #


def test_one_shot_result_flags_are_not_carried():
    """``materialized`` / ``remaining`` report the outcome of ONE action. Carried
    forward they would re-announce a stale result after an unrelated POST."""
    got = _target("https://cartarch.test/decks/7?materialized=3&remaining=2&sort=cmc")
    assert "sort=cmc" in got
    assert "materialized" not in got and "remaining" not in got


def test_an_unknown_param_is_not_carried():
    got = _target("https://cartarch.test/decks/7?sort=cmc&evil=1")
    assert "sort=cmc" in got and "evil" not in got


def test_an_empty_value_is_dropped_rather_than_echoed():
    assert _target("https://cartarch.test/decks/7?sort=&group=type") == "/decks/7?group=type"


def test_an_offsite_referer_cannot_redirect_offsite():
    """The classic Referer hazard. The path is built locally, so the worst a
    forged Referer can do is set the user's own sort — never move them."""
    for hostile in (
        "https://evil.example/decks/7?sort=cmc",
        "//evil.example/decks/7?sort=cmc",
        "https://evil.example/decks/7",
    ):
        got = _target(hostile)
        assert got.startswith("/decks/7"), got
        assert "evil.example" not in got


def test_the_redirect_is_a_303():
    resp = _deck_redirect(_FakeRequest("https://cartarch.test/decks/7?sort=cmc"), 7)
    assert resp.status_code == 303


# --------------------------------------------------------------------------- #
# The class, not the instance
# --------------------------------------------------------------------------- #


def test_no_deck_route_still_hand_rolls_the_bare_redirect():
    """The defect was 17 copies of one line. A new route that reintroduces the
    literal would silently resurrect the bug on its own action only, which is
    exactly how this went unnoticed for so long."""
    src = _SRC.read_text()
    bad = 'RedirectResponse(url=f"/decks/{deck_id}", status_code=303)'
    assert bad not in src, (
        "a deck route redirects to the bare deck page, discarding the user's "
        "sort/group/search — use _deck_redirect(request, deck_id) instead"
    )


def test_every_route_reaching_the_helper_actually_has_a_request():
    """``_deck_redirect`` needs ``request``; a route missing it raises at call
    time, which no import-time check would catch."""
    tree = ast.parse(_SRC.read_text())
    lines = _SRC.read_text().splitlines()
    calls = [i + 1 for i, line in enumerate(lines) if "_deck_redirect(request" in line]
    assert len(calls) >= 17, f"expected the full sweep, found {len(calls)} call sites"

    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not any(node.lineno <= c <= (node.end_lineno or node.lineno) for c in calls):
            continue
        if "request" not in [a.arg for a in node.args.args]:
            missing.append(node.name)
    assert not missing, f"routes calling _deck_redirect without a request param: {missing}"
