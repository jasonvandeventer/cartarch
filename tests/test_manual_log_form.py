"""#173 — the Log a Game form: overlapping header, unstyled Notes, unwired labels.

Four separate faults in one template, each with a distinct cause:

1. An inline `margin-top:-0.5rem` assumed the heading above had a bottom margin to
   absorb. `.panel-title` sets `margin: 0`, so adjacent-sibling collapsing resolved
   to `0 + (-8px)`, pulling the line into the heading's descenders — compounded by
   `line-height: 1.05`, which already puts descenders below the line box.
2. The Notes label and textarea were direct children of the `<form>`, outside any
   `.stack-form`. Every other control is full width only because `.stack-form` is a
   column flex container whose items stretch; outside it the textarea got UA
   defaults (default `cols`, monospace). The base `input, select, textarea` rule
   sets padding, border, background and colour but never `width` or `font-family`.
3. Not one label was associated with its control — no `for`, no `id`, no wrapping.
4. `<button>` nested inside `<a>` inside a `<form>`: invalid HTML, since
   interactive content cannot contain interactive content.

These are markup facts, so they are asserted against the rendered HTML. The
geometry itself was verified in Chromium (6px gap, textarea at the form's full
777px, computed font Montserrat) — reading CSS cannot tell you which of clip,
squash or wrap an engine picks, and this project has been bitten by that.
"""

from __future__ import annotations

import re

import pytest


def _form(page: str) -> str:
    """The manual-log <form> only.

    `page.index("</form>")` finds an EARLIER form in the layout chrome, so the
    naive slice came back EMPTY and two assertions below passed against "" —
    the vacuous-test shape this project keeps rediscovering. Search for the
    close tag AFTER the opening action, and assert the slice is non-trivial.
    """
    start = page.index('action="/games/manual-log"')
    body = page[start : page.index("</form>", start)]
    assert len(body) > 500, "form slice looks wrong; the assertions below would be vacuous"
    return body


@pytest.fixture
def page(client):
    return client.get("/games/manual-log").text


def test_the_opponents_subtitle_no_longer_pulls_into_the_heading(page):
    """A negative top margin here has nothing to collapse against."""
    assert "margin-top:-0.5rem" not in page
    assert "At least one opponent" in page


def test_the_notes_field_lives_inside_a_stack_form(page):
    """`.stack-form` is what makes every other control full width. Outside it the
    textarea shrinks to its `cols` default, which is what "unstyled" meant."""
    block = page[page.index('id="ml-notes"') - 400 : page.index('id="ml-notes"')]
    assert 'class="stack-form"' in block


def test_the_notes_field_uses_the_app_face_not_the_UA_monospace(page):
    """Scoped to this field on purpose. The base `textarea` rule covers 21
    textareas and `.decklist-textarea` legitimately wants monospace for pasted
    decklists, so a global `font: inherit` is a separate change with its own
    visual pass — the #173 non-goal."""
    assert "font-family: inherit" in page


def test_every_label_is_associated_with_its_control(page):
    """Clicking a label must focus its field, and a screen reader must be able to
    name it. Zero labels carried `for` before this."""
    ids = set(re.findall(r'\bid="([^"]+)"', page))
    fors = re.findall(r"<label[^>]*\bfor=\"([^\"]+)\"", page)
    assert fors, "no label carries a for= attribute"
    missing = [f for f in fors if f not in ids]
    assert not missing, f"label(s) pointing at nothing: {missing}"

    # And no bare <label> is left orphaned inside the form's own markup.
    form = _form(page)
    bare = re.findall(r"<label(?![^>]*\bfor=)[^>]*>", form)
    assert not bare, f"{len(bare)} label(s) with no for= and no wrapped control"


def test_the_repeated_opponent_inputs_carry_an_accessible_name(page):
    """Five rows, so visible labels would be ten duplicated lines of noise — but a
    placeholder is not a name. `aria-label` gives one without the visual cost."""
    assert page.count('aria-label="Opponent') == 10  # 5 rows x (name + deck)


def test_the_winner_radios_are_a_fieldset_not_a_label(page):
    """A `<label>` may reference exactly ONE control; this names a group of radios.
    The radios themselves stay wrapped by their own labels, which is a correct
    implicit association."""
    assert '<fieldset id="winner-block"' in page
    assert "<legend" in page


def test_no_button_is_nested_inside_an_anchor(page):
    """Invalid HTML — interactive content inside interactive content, which
    browsers recover from inconsistently."""
    form = _form(page)
    assert not re.search(r"<a\b[^>]*>\s*<button", form)
    assert 'href="/games"' in form  # the Cancel affordance survives
