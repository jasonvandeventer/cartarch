"""`overflow-x: hidden` on html/body silently kills every sticky element.

The mobile hammer at <=768px was `html, body { overflow-x: hidden }`, which
prevents rogue horizontal scroll — and also makes body a SCROLL CONTAINER.
`position: sticky` looks for its nearest scroll container to stick within, and
body's own box never scrolls, so nothing inside it ever sticks. Measured
2026-08-23: the pinned trade balance held at exactly 72px on a 1400px viewport
and scrolled away from the first screenful at 760px.

`overflow-x: clip` prevents the same horizontal scroll WITHOUT creating a scroll
container, so sticky keeps working. Verified in Chromium at 1400 / 760 / 420 px:
the bar pins at every width and `scrollWidth <= clientWidth` still holds.

There is no headless-browser harness in this repo, so this pins the declaration
the way `test_deck_list_row_bfc.py` pins its rule. **Note the two are opposite
calls on the same pair of keywords**: `.deck-list-row` needs `hidden` precisely
BECAUSE of the formatting context it creates. Same keywords, different jobs.
"""

from __future__ import annotations

import pathlib
import re

CSS = pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"

# The `html, body { ... }` rule inside the <=768px mobile block.
_HTML_BODY = re.compile(
    r"@media \(max-width: 768px\) \{\s*\n\s*html,\s*\n\s*body \{(.*?)\n\s*\}", re.S
)


def _rule() -> str:
    m = _HTML_BODY.search(CSS.read_text())
    assert m, "the mobile html/body rule moved or was renamed"
    return re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)


def test_the_regex_finds_a_real_rule():
    body = _rule()
    assert "overflow-x" in body and "max-width" in body, body


def test_mobile_html_body_does_not_create_a_scroll_container():
    body = _rule()
    assert not re.search(r"overflow-x:\s*hidden", body), (
        "overflow-x: hidden on html/body makes it a scroll container, which "
        "disables position: sticky for everything inside it below 769px"
    )
    assert re.search(r"overflow-x:\s*clip", body), (
        "the horizontal-scroll hammer is still needed — clip is how it keeps "
        "working without breaking sticky"
    )


def test_the_pinned_balance_is_still_sticky_at_both_tiers():
    """The rule the fix exists for: a top offset at each tier, never `static`."""
    css = CSS.read_text()
    m = re.search(r"\.trade-balance-pinned \{(.*?)\}", css, re.S)
    assert m and "position: sticky" in m.group(1) and "top: 72px" in m.group(1)
    mobile = re.search(
        r"@media \(max-width: 768px\) \{[^}]*?\.trade-balance-pinned \{(.*?)\}", css, re.S
    )
    assert mobile and "top: 0" in mobile.group(1), (
        "no mobile offset — 72px would hang below nothing"
    )
