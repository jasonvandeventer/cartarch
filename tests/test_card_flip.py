"""Flip control for a double-faced card (requested 2026-08-16).

"Flip cards don't seem to be supported anywhere on the site. Maybe just have a
little flip icon on the corner of the card that you can click to switch to the
other side."

**The whole difficulty is deciding WHICH cards get the control**, and the
obvious answers are both wrong:

* ``" // " in type_line`` is wrong. 161 of the catalog's 335 multi-face cards
  are ``adventure`` / ``split`` / ``prepare`` / ``flip`` — they carry the
  separator and have exactly ONE printed image. This is #160's mistake again,
  where a combined ``type_line`` made Battles and Sagas look double-faced.
* ``layout == "flip"`` is wrong, and is the trap the feature's own NAME sets.
  A flip card (Nezumi Graverobber) is one face read upside-down. It has no
  second image, and the mirror 404s for it.

The layout set was MEASURED against the live image mirror on 2026-08-16, one
card per layout, rather than recalled:

    transform 200 · modal_dfc 200 · art_series 200 · reversible_card 200 ·
    double_faced_token 200      adventure 404 · split 404 · prepare 404 · flip 404

Every layout string below is a REAL value stored in ``cards.layout``.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.dependencies import BACKED_LAYOUTS, has_back_face, mirror_image_url

_TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "app" / "templates"


class _Card:
    def __init__(self, layout=None):
        self.layout = layout


# --------------------------------------------------------------------------- #
# The discriminator
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "layout", ["transform", "modal_dfc", "double_faced_token", "reversible_card", "art_series"]
)
def test_layouts_with_a_real_second_image_get_the_control(layout):
    assert has_back_face(_Card(layout)) is True


@pytest.mark.parametrize("layout", ["adventure", "split", "prepare", "flip"])
def test_layouts_that_only_look_double_faced_do_not(layout):
    """All four carry ' // ' in type_line and have ONE image. `flip` is the
    dangerous one: it is named for the feature and still has no back face."""
    assert has_back_face(_Card(layout)) is False


def test_a_plain_card_gets_nothing():
    assert has_back_face(_Card("normal")) is False


def test_missing_or_null_layout_fails_closed():
    """One catalog row has a NULL layout, and the sanitized public projections
    expose no layout attribute at all. Unknown must mean NO control — a button
    pointing at a 404 is worse than no button."""
    assert has_back_face(_Card(None)) is False
    assert has_back_face(_Card("")) is False
    assert has_back_face(object()) is False


def test_layout_matching_is_case_and_space_tolerant():
    assert has_back_face(_Card(" Transform ")) is True


def test_the_backed_set_is_the_measured_one():
    """Pinned so a later 'tidy-up' cannot quietly add `flip` (the obvious wrong
    move) or drop a working layout. Changing this set means re-measuring
    against the mirror, not reasoning about it."""
    assert BACKED_LAYOUTS == {
        "transform",
        "modal_dfc",
        "double_faced_token",
        "reversible_card",
        "art_series",
    }


# --------------------------------------------------------------------------- #
# The URLs the control is handed
# --------------------------------------------------------------------------- #


def test_the_back_url_is_the_mirrors_back_path():
    """The script never builds a URL — it swaps ones the server handed it. This
    pins the contract both sides depend on."""
    sid = "b5c9649e-9ae5-4926-bf08-71ba23aa37f1"
    assert mirror_image_url(sid, "normal", "back").endswith(f"/{sid}/back/normal.jpg")
    assert mirror_image_url(sid, "normal", "front").endswith(f"/{sid}/normal.jpg")


# --------------------------------------------------------------------------- #
# #168 — emitting the attributes without loading the engine is a dead control
# --------------------------------------------------------------------------- #


def _pages_emitting_flip() -> set[str]:
    """Templates that render the button, following {% include %} one level so a
    partial's control is attributed to the page that carries the script."""
    emitters = {p.name for p in _TEMPLATES.rglob("*.html") if "data-card-flip" in p.read_text()}
    pages = set()
    for p in _TEMPLATES.rglob("*.html"):
        text = p.read_text()
        used = set(re.findall(r'(?:include|import)\s+"([^"]+)"', text))
        if any(pathlib.Path(u).name in emitters for u in used) or p.name in emitters:
            pages.add(p.name)
    return pages


def test_every_page_rendering_the_button_loads_the_script():
    """A control whose engine never loads renders, looks right, and does
    nothing — #168's exact shape, which is why this is a guard and not a
    comment. Requires a real <script src=...>, because the earlier version of
    that guard matched a bare string and a Jinja COMMENT satisfied it."""
    script = re.compile(r'<script[^>]+src="/static/card-flip\.js')
    macro_definers = {"_macros.html"}  # defines the button, renders no page
    missing = []
    for name in sorted(_pages_emitting_flip() - macro_definers):
        p = next(_TEMPLATES.rglob(name))
        text = p.read_text()
        if script.search(text):
            continue
        # A partial is fine if every page including it loads the script.
        includers = [
            q for q in _TEMPLATES.rglob("*.html") if f'"{name}"' in q.read_text() and q.name != name
        ]
        if includers and all(script.search(q.read_text()) for q in includers):
            continue
        missing.append(name)
    assert not missing, (
        "these templates emit data-card-flip but never load card-flip.js, so the "
        f"button is inert: {missing}"
    )


def test_the_script_guard_regex_actually_matches():
    """Self-check: a regex that stopped matching would make the guard above
    pass for every template, including broken ones."""
    script = re.compile(r'<script[^>]+src="/static/card-flip\.js')
    assert script.search('<script src="/static/card-flip.js?v=abc"></script>')
    assert not script.search("{# card-flip.js #}")


def test_the_button_is_not_nested_inside_the_card_link():
    """#174 — a <button> inside an <a> is invalid, and the tile's image is
    already wrapped in a link to the card page. The control must be a SIBLING."""
    macro = (_TEMPLATES / "_macros.html").read_text()
    start = macro.index("data-card-flip")
    # Walk back to the nearest anchor boundary; a </a> must come first.
    before = macro[:start]
    assert before.rindex("</a>") > before.rindex("<a href"), (
        "the flip button appears to be inside the card's <a> — invalid nesting (#174)"
    )


def test_the_script_does_not_look_for_a_class_specific_image():
    """The grid tile's image is .inventory-thumb; the card-detail page's is
    .card-detail-art-img. The first cut queried the former by class, which made
    the button silently INERT on card detail — it rendered, it was clickable,
    and nothing happened. The wrapper holds only the artwork and the button, so
    a bare `img` selector is both correct and unambiguous."""
    src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "card-flip.js"
    ).read_text()
    assert 'querySelector("img")' in src
    assert "img.inventory-thumb" not in src, (
        "a class-specific image selector makes the control inert on any surface "
        "using a different image class"
    )


def test_card_detail_renders_the_button_for_a_two_faced_card(client, db):
    """Route-level, on the surface added last. #152's failure mode: a template
    change that never gets rendered by a test can drift indefinitely."""
    from app.models import Card

    card = Card(
        scryfall_id="b5c9649e-9ae5-4926-bf08-71ba23aa37f1",
        name="Aberrant Researcher // Perfected Form",
        set_code="soi",
        collector_number="52",
        type_line="Creature — Human Insect",
        image_url="http://x/y.jpg",
        layout="transform",
    )
    db.add(card)
    db.commit()

    page = client.get(f"/cards/{card.id}").text
    assert "data-card-flip" in page
    # The displayed image on this page is `large`, so the swap targets must be too.
    assert "/large.jpg" in page and "/back/large.jpg" in page
    assert "card-flip.js" in page, "the button is inert without its engine (#168)"


def test_card_detail_shows_no_button_for_a_single_faced_card(client, db):
    from app.models import Card

    card = Card(
        scryfall_id="00000000-0000-0000-0000-0000000000aa",
        name="Elite Interceptor // Rejoinder",
        set_code="tst",
        collector_number="1",
        type_line="Creature — Soldier // Instant",
        image_url="http://x/y.jpg",
        layout="prepare",  # carries " // " and has ONE image
    )
    db.add(card)
    db.commit()

    assert "data-card-flip" not in client.get(f"/cards/{card.id}").text
