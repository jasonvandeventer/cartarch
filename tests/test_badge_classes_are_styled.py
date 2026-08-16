"""A badge class a template emits must have a CSS rule behind it.

`.share-badge` was emitted by the `inventory_card` macro from issue #27 onward
and **never had a rule at all**, while its three siblings in the same meta line
(`.finish-badge`, `.language-badge`, `.proxy-badge`) were all styled pills. So
"SHARED FROM <deck>" rendered as bare prose next to three shaped badges. On a
variant-group deck nearly every row carries one — 55 of 96 on the deck that was
reported — which is why it read as "shared card indicators are kinda messy"
rather than as one odd-looking element.

This is the #168 shape: **emitting the markup without the thing that makes it
work is a silent defect.** Nothing raises, nothing logs, the page renders — it
just looks wrong, so it survives until a user complains. A class name is cheap
to typo and cheap to orphan when a rule is renamed, and neither shows up in any
test that only asserts the badge's TEXT is present.

Scope is the badge FAMILIES rather than every class in the app: these are
closed, conventional sets whose members should all look alike, so an unstyled
member is unambiguously a bug. A general "every class has a rule" scan would
drown in layout and JS-hook classes that legitimately have none.

Two families, because the shared indicator has TWO implementations — grid view
emits `.share-badge` from the `inventory_card` macro, list view emits
`.dlr-tag.share` from `_deck_card_list_text.html`. **Both shipped unstyled**,
and a guard covering only the first would have passed while the reported view
stayed broken. `.dlr-tag` variants are modifier classes on a styled base, so
they fail quietly *worse*: the tag still looks like a tag, just the wrong one.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CSS = _ROOT / "app" / "static" / "style.css"
_TEMPLATES = _ROOT / "app" / "templates"

# `(?![-\w])` and NOT `\b`: a word boundary still matches `.share-badge` inside
# `.share-badge-deck`, so a rule for a LONGER class would wrongly satisfy the
# shorter one. Caught by mutation-testing this very guard.
_RULE = re.compile(r"\.([a-z][a-z0-9-]*-badge)(?![-\w])")
_DLR_RULE = re.compile(r"\.dlr-tag\.([a-z][a-z0-9-]*)(?![-\w])")
# Selector list preceding a declaration block; skips at-rules like @media.
_SELECTORS = re.compile(r"(?:^|[}{;])\s*([^{}@;]+?)\s*\{", re.M)
_CLASS_ATTR = re.compile(r'class="([^"]*)"')
_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _styled_classes() -> set[str]:
    """Classes with a rule in style.css OR in a template's inline <style>.

    Inline styles count: `.tc-momir-badge` is defined inside game_detail.html
    and is perfectly fine. A scan that only read style.css would flag it and
    train the reader to add allowlist entries, which is how a guard rots.
    """
    sources = [_CSS.read_text()]
    sources += [
        block
        for p in _TEMPLATES.rglob("*.html")
        for block in re.findall(r"<style[^>]*>(.*?)</style>", p.read_text(), re.S)
    ]
    styled: set[str] = set()
    for src in sources:
        clean = _COMMENT.sub("", src)
        for selector_list in _SELECTORS.findall(clean):
            for selector in selector_list.split(","):
                # Only the SUBJECT of the rule counts — the final compound after
                # the last combinator. `.share-badge .share-badge-deck {}` styles
                # the deck span, NOT `.share-badge`, and counting the mention let
                # a mutation that deleted the real rule pass this guard.
                subject = re.split(r"[\s>+~]+", selector.strip())[-1]
                styled |= set(_RULE.findall(subject))
                styled |= {f"dlr-tag.{m}" for m in _DLR_RULE.findall(subject)}
    return styled


def _emitted_classes() -> dict[str, str]:
    """Badge-family classes in a template `class="…"`, mapped to where.

    Two families: `*-badge` standalone classes, and `.dlr-tag` MODIFIERS (the
    second class on a `dlr-tag` span, e.g. `class="dlr-tag share"`). The base
    `dlr-tag` itself is styled and is not a modifier, so it is skipped.
    """
    found: dict[str, str] = {}
    for p in sorted(_TEMPLATES.rglob("*.html")):
        for m in _CLASS_ATTR.finditer(p.read_text()):
            classes = m.group(1).split()
            if "{" in m.group(1):
                continue  # Jinja-interpolated — the value isn't knowable here
            for cls in classes:
                if cls.endswith("-badge"):
                    found.setdefault(cls, p.name)
            if "dlr-tag" in classes:
                for cls in classes:
                    if cls != "dlr-tag":
                        found.setdefault(f"dlr-tag.{cls}", p.name)
    return found


def test_the_scan_finds_the_badges_it_exists_for():
    """Self-check: a scan matching nothing looks identical to a clean repo."""
    emitted = _emitted_classes()
    assert len(emitted) > 10, f"class scan collapsed, found only {sorted(emitted)}"
    assert "share-badge" in emitted, "the class this guard was written for vanished"
    # BOTH implementations must be in scope — covering only the grid one is how
    # the list view (the view actually reported) stayed broken.
    assert "dlr-tag.share" in emitted, "the list-view shared tag left the scan"
    assert len(_styled_classes()) > 10, "CSS rule scan collapsed"


def test_the_scan_would_catch_an_unstyled_badge():
    """Mutation-proof the matcher itself: a class with no rule must be caught."""
    styled = _styled_classes()
    assert "definitely-not-a-real-badge" not in styled


def test_every_emitted_badge_class_has_a_rule():
    styled = _styled_classes()
    orphans = {c: where for c, where in _emitted_classes().items() if c not in styled}
    assert not orphans, (
        "badge classes emitted by a template with no CSS rule anywhere "
        "(they render as bare text beside real pills):\n  "
        + "\n  ".join(f"{c} — emitted in {where}" for c, where in sorted(orphans.items()))
    )
