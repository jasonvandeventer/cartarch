"""The actions-drawer shrink rules must live OUTSIDE a media query.

They existed for years inside ``@media (max-width: 768px)``, so a <select>
holding a long deck/location/showcase name could not shrink on desktop and
painted up to 132px past the card (Chromium-measured), where the spilled half
hit-tested to the NEXT card in the grid — the control you could see did
nothing. Phone widths were fine, which is what hid it.

There is no headless-browser harness in this repo, so this pins the rule's
POSITION the way ``test_deck_list_row_bfc.py`` pins its rule.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "style.css"
SELECTOR = ".card-actions-body .inline-form select"


def _media_depth_at(css: str, index: int) -> int:
    """How many @media blocks enclose ``index``."""
    depth = 0
    media_depths: list[int] = []
    for m in re.finditer(r"@media|[{}]", css[:index]):
        tok = m.group()
        if tok == "@media":
            media_depths.append(depth)  # opens at the NEXT brace
        elif tok == "{":
            depth += 1
        else:
            depth -= 1
            if media_depths and depth == media_depths[-1]:
                media_depths.pop()
    return len(media_depths)


def test_shrink_rules_apply_at_every_width():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)  # comments name the rule too
    hits = [m.start() for m in re.finditer(re.escape(SELECTOR), css)]
    assert hits, f"{SELECTOR} not found — did the drawer markup change?"
    for i in hits:
        assert _media_depth_at(css, i) == 0, (
            f"{SELECTOR} is inside a @media block. A <select> has min-width:auto, "
            "so without min-width:0 at ALL widths the drawer spills over the next card."
        )


def test_the_depth_helper_actually_detects_media_blocks():
    """Self-check: a guard that can't see a media query would pass on anything."""
    sample = "a{color:red}\n@media (max-width: 768px){\n  b{color:blue}\n}\nc{color:green}"
    assert _media_depth_at(sample, sample.index("a{")) == 0
    assert _media_depth_at(sample, sample.index("b{")) == 1
    assert _media_depth_at(sample, sample.index("c{")) == 0
