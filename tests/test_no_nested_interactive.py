"""#174 — no template nests a <button> inside an <a>.

Invalid HTML: interactive content cannot contain interactive content, and browsers
recover from it inconsistently. 43 navigation controls were written this way across
19 templates.

**The multi-line match is the point of this test.** The issue that filed this
counted 12 instances across 9 templates, because it was built from a same-line
`grep` — and more than two thirds of the real instances put the `<a>` and the
`<button>` on separate lines. A line-oriented search reports itself complete while
leaving 31 behind.
"""

from __future__ import annotations

import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"

# re.S so a newline between the tags does not hide the nesting.
_NESTED = re.compile(r"<a\b[^>]*>\s*<button\b", re.S)


def test_no_button_is_nested_inside_an_anchor_in_any_template():
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text()
        for m in _NESTED.finditer(text):
            offenders.append(
                f"{path.relative_to(TEMPLATES)}:{text[: m.start()].count(chr(10)) + 1}"
            )
    assert not offenders, (
        "<button> nested inside <a> is invalid HTML. Use an <a class='btn'> "
        f"(or .btn-like) instead. Offenders: {offenders}"
    )


def test_the_guard_would_actually_catch_the_pattern_across_a_newline():
    """The regex is the whole test, so prove it matches the shape that was missed.

    Without `re.S` and `\\s*` this passes while 31 real instances go unnoticed.
    """
    assert _NESTED.search('<a href="/x">\n  <button type="button">Go</button>\n</a>')
    assert _NESTED.search('<a href="/x"><button type="button">Go</button></a>')
    assert not _NESTED.search('<a href="/x" class="btn">Go</a>')
    # A <button> merely following an anchor is fine.
    assert not _NESTED.search('<a href="/x">Go</a>\n<button type="submit">Save</button>')


def test_the_replacement_class_exists_and_mirrors_the_button_box():
    """`.btn` is what makes the sweep visually inert.

    Swapping to `.btn-like` — the obvious move — would have restyled all 43, since
    that is a small outlined chip while a bare `<button>` is the accent-filled
    primary. If `.btn` ever loses these declarations the sweep silently becomes a
    restyle.
    """
    css = (TEMPLATES.parent / "static" / "style.css").read_text()
    block = css[css.index("\n.btn {") : css.index("\n.btn-like {")]
    for decl in ("background: var(--accent)", "border-radius: 10px", "font-weight: 600"):
        assert decl in block, f".btn no longer mirrors the button element rule: {decl}"
    # Without this an anchor keeps its underline inside the button box.
    assert "text-decoration: none" in block


def test_ghost_button_anchors_also_carry_btn():
    """`.ghost-button` only overrides background/border/colour — it inherits the
    box from the `button {}` element rule, so on an anchor it renders with NO
    padding unless `.btn` comes with it."""
    for path in sorted(TEMPLATES.rglob("*.html")):
        for m in re.finditer(r'<a\b[^>]*class="([^"]*)"', path.read_text()):
            classes = m.group(1).split()
            if "ghost-button" in classes:
                assert "btn" in classes, f"{path.name}: ghost-button anchor without .btn"


def test_the_current_page_number_emphasis_is_still_conditional():
    """A mechanical sweep dropped the `{% if p == page %}` guard around the
    pagination emphasis, so every page number rendered bold and the
    current-page indicator stopped indicating anything. Caught by reading the
    diff, not by any test — hence this one."""
    html = (TEMPLATES / "collection.html").read_text()
    i = html.index('href="/collection?page={{ p }}')
    assert "{% if p == page %}" in html[i : i + 400]


# ── Two cascade traps the sweep walked into, both caught in a browser ────────


def _css() -> str:
    return (TEMPLATES.parent / "static" / "style.css").read_text()


def test_the_filter_row_font_rule_carries_no_specificity():
    """`.filter-row button` is (0,1,1) and BEAT class rules that pin a size — the
    `▦ Grid` toggle jumped 11px → 16px.

    `:where()` contributes zero specificity, so the rule still reaches an
    otherwise-unstyled button but loses to any class that has an opinion. Drop the
    `:where()` and the toggle silently resizes again.
    """
    css = _css()
    assert ":where(.filter-row) button {" in css
    assert "\n.filter-row button {" not in css


def test_ghost_button_is_defined_after_the_box_it_overrides():
    """`.ghost-button` only overrides background/border/colour, so it must come
    after both `button {}` and `.btn`. It sat ~400 lines EARLIER, and the moment
    `.btn ghost-button` appeared on an anchor the ghost lost its transparent
    background to `.btn` — the Back to Import control turned solid accent.

    Ordering is the whole mechanism here; equal specificity means last one wins.
    """
    css = _css()
    assert css.index("\n.btn {") < css.index("\n.ghost-button {")


def test_btn_like_declares_its_own_weight():
    """It used to inherit 600 from `button {}`, so the same class rendered at 600
    on its <button> uses and 400 on its <summary> uses. Moving 28 controls to <a>
    made that visible by dropping them to 400."""
    css = _css()
    block = css[css.index("\n.btn-like {") : css.index("\n.btn-like::")]
    assert "font-weight: 600" in block
