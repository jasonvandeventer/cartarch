"""`.deck-list-row` must establish a block formatting context (Firefox multicol).

Reported 8/3, "fixed", reported again 8/14: *"on cards with only a single
printing, the actions dropdown doesn't work"*. Printings were a red herring —
Alex narrowed it himself: **the first item of the LAST column**, whichever card
happened to be there.

Measured in Firefox against a real 87-card deck: that row's ⇄ button is in the
DOM with `width: 24px`, but Firefox hands it a **0x0 box and a used width of
2px** (its borders and nothing else) when `.deck-list-rows` is 3 columns. At 1
or 2 columns it is 24px. Chromium never reproduces it at any width 700-2000,
which is why the first fix — which targeted the popover — did not touch this
and the bug came straight back.

`overflow: hidden` on the row fixes it by establishing a block formatting
context. **`overflow: clip` does NOT** (measured), which is how we know the
cure is the formatting context and not the clipping — so this rule must not be
"modernised" to `clip`. Nothing is clipped in practice (scrollWidth ==
clientWidth) and the kebab popover is `position: fixed`, so it still escapes.

There is no headless-browser harness in this repo, so this pins the rule and
its reason. The behaviour itself was verified in Firefox and Chromium across
viewport widths 700-2000; see the commit message.
"""

from __future__ import annotations

import pathlib
import re

CSS = pathlib.Path("app/static/style.css")

# The `.deck-list-row { ... }` block, up to its closing brace.
_ROW_BLOCK = re.compile(r"^\.deck-list-row\s*\{(.*?)^\}", re.S | re.M)


def _row_block() -> str:
    """The rule's DECLARATIONS, comments stripped.

    Stripping matters: this rule carries a long comment that names
    `overflow: clip` in order to warn against it, and a naive substring check
    matches the warning instead of a declaration. (Caught by mutation-testing
    this file — the clip assertion failed against correct CSS.)
    """
    m = _ROW_BLOCK.search(CSS.read_text())
    assert m, "the .deck-list-row rule moved or was renamed"
    return re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)


def test_the_regex_actually_finds_a_nontrivial_block():
    """A guard that silently stops matching is indistinguishable from a clean repo."""
    block = _row_block()
    assert len(block) > 100, block
    assert "break-inside" in block  # a known member of that rule


def test_the_row_establishes_a_block_formatting_context():
    assert re.search(r"overflow:\s*hidden", _row_block()), (
        "`.deck-list-row` lost `overflow: hidden`. In Firefox the first row of the "
        "LAST multicol column then renders its ⇄ button as a 0x0 box, and the ⋮ "
        "beside it shifts into the wrong place."
    )


def test_it_is_not_overflow_clip():
    """`clip` looks equivalent and is not: it does not create the BFC."""
    block = _row_block()
    assert not re.search(r"overflow:\s*clip", block), (
        "overflow:clip does NOT fix the Firefox fragmentation — measured. "
        "The fix is the block formatting context that `hidden` creates."
    )


def test_the_rows_container_is_still_multicol():
    """If this ever stops being multicol the rule above is dead weight — say so here."""
    css = CSS.read_text()
    m = re.search(r"^\.deck-list-rows\s*\{(.*?)^\}", css, re.S | re.M)
    assert m and "column-count" in m.group(1), (
        "`.deck-list-rows` is no longer a multi-column container — re-check whether "
        "`.deck-list-row { overflow: hidden }` is still needed."
    )
