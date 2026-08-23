"""Every STATIC form action in a template must be a route the app registers.

v4.16.1 shipped a Grid/List toggle whose form posted to
`/account/trade-view-pref` while the route was registered on the `/trades`-
prefixed router — so the real path was `/trades/account/trade-view-pref` and the
button answered 404 in production. The feature's own tests passed, because they
posted to the ROUTE they had just written rather than to the action the page
renders: they tested the handler and never the control.

This is the class guard. A behaviour test can only cover the pages someone
thought to write a test for; this walks every template and fails on any static
action with no matching route, so the next mis-prefixed router is caught before
anyone clicks it.

Actions containing Jinja (`/decks/{{ deck.id }}/delete`) are matched against the
route TABLE by shape, since their concrete value is only known at render time.
"""

from __future__ import annotations

import pathlib
import re

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "app" / "templates"

# A Jinja expression inside an action — replaced by a placeholder so the result
# can be compared against a route's own `{param}` segments.
_JINJA = re.compile(r"\{\{.*?\}\}")
_ACTION = re.compile(r'\baction="([^"]+)"')


def _routes() -> set[str]:
    from app import main

    return {
        re.sub(r"\{[^}]+\}", "*", r.path)
        for r in main.app.routes
        if getattr(r, "path", "").startswith("/")
    }


_LITERAL = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _candidates(action: str) -> list[str]:
    """Every concrete path an action can render as, as route-table shapes.

    `/decks/{{ deck.id }}/delete` has one shape (`/decks/*/delete`), but
    `/watchlist/{{ 'unshare-playgroup' if x else 'share-playgroup' }}` has TWO
    real paths and BOTH must exist — an expression that picks between literal
    endpoints is exactly where one branch quietly goes missing. Expanding them
    checks both instead of shrugging and calling the segment dynamic.
    """
    action = action.split("?", 1)[0]
    exprs = _JINJA.findall(action)
    variants = [action]
    for expr in exprs:
        literals = [a or b for a, b in _LITERAL.findall(expr)]
        replacements = literals if literals else ["*"]
        variants = [v.replace(expr, r, 1) for v in variants for r in replacements][:8]
    # Anything still holding an expression is a genuinely dynamic segment.
    return [
        "/".join("*" if "{{" in seg or seg == "*" else seg for seg in v.split("/"))
        for v in variants
    ]


def _template_actions() -> list[tuple[str, str]]:
    out = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for m in _ACTION.finditer(path.read_text()):
            action = m.group(1)
            if not action.startswith("/"):
                continue  # external or relative — not ours to resolve
            out.append((path.name, action))
    return out


def test_every_form_action_resolves_to_a_route():
    routes = _routes()
    missing = [
        (name, action)
        for name, action in _template_actions()
        if not all(c in routes for c in _candidates(action))
    ]
    assert not missing, "form actions with no matching route:\n  " + "\n  ".join(
        f"{name}: {action}" for name, action in missing
    )


def test_the_scan_actually_finds_forms():
    """A guard that matched nothing would pass on a repo full of dead buttons."""
    actions = _template_actions()
    assert len(actions) > 40, f"only found {len(actions)} form actions — the scan is broken"
    assert any("view-pref" in a for _n, a in actions), "the toggle this was written for is missing"


@pytest.mark.parametrize(
    "action,expected",
    [
        ("/account/trade-view-pref", "/account/trade-view-pref"),
        ("/decks/{{ deck.id }}/delete", "/decks/*/delete"),
        ("/showcase/items/{{ item.id }}/quantity", "/showcase/items/*/quantity"),
        ("/trades/{{ trade.id }}/counter?x=1", "/trades/*/counter"),
    ],
)
def test_the_normaliser(action, expected):
    assert _candidates(action) == [expected]


def test_a_conditional_action_checks_BOTH_endpoints():
    """The wishlist share toggle picks between two literal routes. A guard that
    collapsed it to a wildcard would pass even if one branch were deleted."""
    both = _candidates("/watchlist/{{ 'unshare-playgroup' if x else 'share-playgroup' }}")
    assert sorted(both) == ["/watchlist/share-playgroup", "/watchlist/unshare-playgroup"]
