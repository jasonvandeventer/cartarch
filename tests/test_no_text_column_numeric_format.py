"""A TEXT column must never reach Jinja's numeric ``format`` filter.

``'%.2f'|format("3.50")`` raises ``TypeError: must be real number, not str`` and
500s the whole page. It has happened once for real — v4.13.4 wired
``Card.price_usd`` (a ``String(32)``, written as text by the MTGJSON ingest) into
the hover ``data-card-info`` on ``pending.html`` and ``drawer_detail.html``, and
`/pending` was dead for every drawerless user until v4.13.12. It was nearly
repeated a third time in v4.13.16.

The invariant is documented in CLAUDE.md and each past instance has its own
regression test, but neither stops a NEW template from doing it. This does.

**The TEXT column set is INTROSPECTED from the ORM, not hardcoded**, so a column
added tomorrow is covered without anyone remembering this file exists. Prices are
the live hazard; ``power``/``toughness``/``loyalty``/``defense`` are text for a
real reason (``*``, ``1+*``, ``X``) and would fail the same way.
"""

import pathlib
import re

from sqlalchemy import String, Text

import app.legacy_tables  # noqa
from app.db import Base

_TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "app" / "templates"

# Any numeric format filter — %.2f, %d, %05.1f — and the expression it is
# applied to. Templates put these on one line, so no DOTALL needed; the pattern
# is asserted below so a reformat cannot silently stop matching.
_NUMERIC_FORMAT = re.compile(r"""["']%[0-9.]*[fdeg]["']\s*\|\s*format\(\s*([^)]*?)\s*\)""")


def _text_column_names() -> set[str]:
    """Every String/Text column name across all mapped models."""
    names: set[str] = set()
    for mapper in Base.registry.mappers:
        for column in mapper.local_table.columns:
            if isinstance(column.type, (String, Text)):
                names.add(column.name)
    return names


def test_the_regex_still_matches_the_shape_it_guards():
    """Pin the pattern itself — a guard that matches nothing passes forever."""
    sample = """{{ '%.2f'|format(item.card.price_usd) }}"""
    assert _NUMERIC_FORMAT.search(sample).group(1) == "item.card.price_usd"
    assert _NUMERIC_FORMAT.search("""{{ "%d" | format( x ) }}""").group(1) == "x"


def test_no_template_formats_a_text_column_as_a_number():
    text_columns = _text_column_names()
    assert "price_usd" in text_columns, "introspection found no price column — check the models"

    offenders = []
    checked = 0
    for path in sorted(_TEMPLATES.rglob("*.html")):
        for match in _NUMERIC_FORMAT.finditer(path.read_text()):
            checked += 1
            arg = match.group(1)
            for column in text_columns:
                # `.column` — an attribute read off a model instance. A bare
                # local named the same thing (a float the route computed) is not
                # a hit, which is what keeps this free of false positives.
                if re.search(rf"\.{re.escape(column)}\b", arg):
                    offenders.append(
                        f"{path.relative_to(_TEMPLATES)}: "
                        f"'{arg}' formats TEXT column '{column}' as a number"
                    )

    assert checked > 20, f"only {checked} format sites found — the scan is not reaching templates"
    assert not offenders, "TEXT column formatted as a number (this is a 500):\n  " + "\n  ".join(
        offenders
    )
