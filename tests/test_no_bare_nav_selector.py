"""No bare element selector may silently style every ``<nav>`` in the app.

A bare ``nav { display: flex; flex-wrap: wrap; gap: 1rem }`` — a retired v3.x
horizontal site-header leftover — contributed ONE property to a component that
overrode every other property it declared, and nothing flagged the leak.
``.sidebar-nav`` overrode display, direction and gap but not ``flex-wrap``, so it
was a WRAPPING column flex container: ``.nav-donate`` rendered in a second column
past the 236px sidebar edge and ``overflow: hidden`` erased it (v4.12.5). The
symptom was "Donate disappears", and reading ``.sidebar-nav`` could never explain
it — the cause was in a rule 900 lines away that named no class.

Scoped in v4.13.22 to a ``:where()`` list of the five navs that existed. Verified
in Chromium: **zero computed-style differences** across landing + authed pages at
1280x900 and 390x844, and a newly created unclassed ``<nav>`` now computes
``display: block``.

**``:where()`` is required, not stylistic.** A plain class list is specificity
(0,1,0) and ties the component rules it sits after, so it starts WINNING — the
first attempt flipped ``.sidebar-nav`` back to ``flex-wrap: wrap``, reintroducing
the original bug. ``:where()`` is zero-specificity, so the cascade is unchanged.
"""

import pathlib
import re

_CSS = (pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text()

# Element selectors that must never appear bare (unclassed, unscoped) at the
# start of a rule. Layout containers whose components are expected to own their
# own display/flex behaviour.
_FORBIDDEN = ("nav", "header", "footer", "aside", "main", "section")


def _bare_rules() -> list[str]:
    """Rules whose selector is exactly one of the forbidden bare elements."""
    found = []
    for selector in _FORBIDDEN:
        # Start of a line, the element, optional whitespace, then `{`.
        # `nav a {`, `.x nav {`, `:where(...) {` and `nav, .y {` do not match.
        if re.search(rf"(?m)^{selector}\s*\{{", _CSS):
            found.append(selector)
    return found


def test_the_pattern_matches_the_shape_it_guards():
    """Pin the regex — a guard that matches nothing passes forever."""
    assert re.search(r"(?m)^nav\s*\{", "nav {\n  display: flex;\n}")
    assert not re.search(r"(?m)^nav\s*\{", "nav a {\n  color: red;\n}")
    assert not re.search(r"(?m)^nav\s*\{", ":where(.sidebar-nav) {\n  gap: 1rem;\n}")


def test_no_bare_layout_element_selector():
    bare = _bare_rules()
    assert not bare, (
        "bare element selector(s) styling every instance app-wide: "
        + ", ".join(f"{s} {{...}}" for s in bare)
        + " — scope with :where(<class list>) so components keep their own cascade"
    )


def test_the_scoped_nav_rule_uses_where_for_zero_specificity():
    """A plain class list would TIE the component rules and, sitting after them,
    win — which is how the first attempt at this cleanup put `.sidebar-nav` back
    to `flex-wrap: wrap`."""
    match = re.search(r":where\(\s*\.landing-header-nav.*?\)\s*\{", _CSS, re.S)
    assert match, "the scoped nav rule is gone or no longer uses :where()"
    assert ".sidebar-nav" in match.group(0)
