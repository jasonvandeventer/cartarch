"""A confirm() message must be built by `tojson`, never by interpolation.

Reported 2026-08-22: *"every once in a while when I hit the remove button, the
card is just removed without the confirmation dialog."* The tell was that it was
always the SAME cards. A wishlist row emitted

    onclick="return confirm('Remove {{ item.display_name }} from your wishlist?')"

so "Gaea's Cradle" closed the JS string, Chromium failed to parse the handler
("missing ) after argument list"), the handler never ran, and the button did its
default: submit, and the card was gone. Magic is full of apostrophes — Urza's,
Yawgmoth's, Gaea's, Sensei's — so this fired constantly on some collections and
never on others.

Twelve sites across nine templates had the shape, including *Delete deck*,
*Delete location*, *Remove playgroup member* and *Delete user* — every one a
destructive action whose only guard was the dialog that failed to appear.

The idiom is a SINGLE-quoted attribute plus ``| tojson``: tojson escapes the
apostrophe (\\u0027) so it cannot close anything, and its own double quotes are
safe inside a single-quoted attribute. Verified in Chromium against a card named
``Gaea's Cradle`` and one named ``Say "Ahh"``.
"""

from __future__ import annotations

import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "app" / "templates"

# A confirm() whose argument is a QUOTED STRING LITERAL containing a Jinja
# expression — the broken shape. `confirm({{ ... | tojson }})` has no quote
# before the `{{`, so it does not match.
_INTERPOLATED = re.compile(r"confirm\(\s*(['\"])[^\n]*?\{\{.*?\1", re.S)


def _sites() -> list[tuple[str, str]]:
    hits = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for m in _INTERPOLATED.finditer(path.read_text()):
            hits.append((path.name, m.group(0)[:90]))
    return hits


def test_no_confirm_message_is_built_by_interpolation():
    hits = _sites()
    assert not hits, (
        "these confirm() calls interpolate into a JS string literal — one "
        "apostrophe in the data and the dialog silently stops appearing:\n  "
        + "\n  ".join(f"{name}: {frag}" for name, frag in hits)
    )


def test_the_regex_actually_matches_the_broken_shape():
    """A guard that matches nothing would pass on a repo full of the bug."""
    broken = """<button onclick="return confirm('Remove {{ item.name }} from your wishlist?');">"""
    assert _INTERPOLATED.search(broken), "the guard no longer detects its own bug"

    fixed = """<button onclick='return confirm({{ ("Remove " ~ item.name ~ "?") | tojson }});'>"""
    assert not _INTERPOLATED.search(fixed), "the guard rejects the correct idiom"

    # A static message with no data in it is fine and must not be flagged.
    static = """<form onsubmit="return confirm('Decline this trade?')">"""
    assert not _INTERPOLATED.search(static)


def test_every_confirm_that_names_data_uses_tojson():
    """The positive half: the sites that DO name a card/deck/person still do —
    a fix that silently dropped the name from every dialog would pass the test
    above while making the confirmations less useful."""
    named = 0
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text()
        for m in re.finditer(r"confirm\(\{\{(.*?)\}\}\)", text, re.S):
            assert "tojson" in m.group(1), f"{path.name}: confirm() without tojson"
            named += 1
    assert named >= 10, f"expected the ~12 data-bearing confirms, found {named}"
