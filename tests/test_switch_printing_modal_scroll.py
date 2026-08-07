"""The Switch Printing modal's section heading must not scroll away.

Reported 2026-08-07 with a screenshot: opening Switch Printing on a phone and
scrolling the owned list made "In your collection" disappear behind the modal
header, leaving a half-clipped "Pick a printing you already own".

Reproduced in Chromium at 412x915: with `overflow-y: auto` on the SECTION, the
section's own <h4> and blurb are inside the scroller, so at scrollTop=40 the
heading sat at top 102 against a section top of 130 — clipped, and under the
opaque header. The list is the scroller now, so the heading holds still.

`min-height: 0` is required at BOTH levels. That is the v4.12.5 sidebar lesson:
one missing level anywhere in a flex-shrink chain defeats the internal scroll
below it, and nothing flags it.
"""

import pathlib
import re

_CSS = (pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text()


def _block(selector: str) -> str:
    """The DECLARATIONS of the first rule whose selector matches exactly.

    Comments are stripped: the rule below explains itself by naming the very
    declaration it must not contain, and a raw substring check reads that prose
    as code. (It did — this test failed against a correct stylesheet first time.)
    """
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{(.*?)\}}", _CSS, re.S)
    assert match, f"{selector} rule not found — did the class get renamed?"
    return re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)


def test_the_section_is_not_the_scroller():
    """The heading lives inside the section. If the section scrolls, the heading
    scrolls away with the list — which is the reported bug."""
    block = _block(".switch-printing-section")
    assert "overflow-y: auto" not in block, (
        "the section is the scroller again — its own heading will scroll under "
        "the modal header (2026-08-07 report)"
    )
    assert "overflow: hidden" in block
    assert "min-height: 0" in block, "a flex child without min-height: 0 cannot shrink"
    assert "flex-direction: column" in block


def test_the_list_is_the_scroller():
    block = _block(".switch-printing-list")
    assert "overflow-y: auto" in block, "nothing scrolls; a long printing list would be unreachable"
    assert "min-height: 0" in block, (
        "the inner level of the flex-shrink chain — without it the list grows "
        "instead of scrolling and the modal clips it (v4.12.5)"
    )


def test_the_heading_markup_is_still_inside_the_section():
    """The CSS fix assumes the heading is a SIBLING of the list inside the
    section. If the template ever moves it, the rules above stop meaning what
    this test checks."""
    html = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "templates"
        / "_switch_printing_modal.html"
    ).read_text()
    section_start = html.index('class="switch-printing-section switch-printing-section-owned"')
    list_start = html.index('class="switch-printing-list"', section_start)
    title_start = html.index('class="switch-printing-section-title"', section_start)
    assert title_start < list_start, "the heading must precede the list inside the section"
