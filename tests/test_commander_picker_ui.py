"""#168 — the commander picker's filter actually runs.

**#168:** the picker emitted `data-list-filter` and `data-list-filter-target` but was the
only one of the eight templates emitting them that never loaded `/static/list-filter.js`
— and `base.html` does not load it either. The box was inert. The list is unpaginated:
539 rows on the largest account, so a dead filter is a wall.

"""

from __future__ import annotations

import re

from app.models import Card, InventoryRow


def _commander(db, user, name, scryfall_id, collector="1"):
    card = Card(
        scryfall_id=scryfall_id,
        name=name,
        set_code="tst",
        collector_number=collector,
        type_line="Legendary Creature — Human Wizard",
        legalities='{"commander": "legal"}',
    )
    db.add(card)
    db.commit()
    db.add(
        InventoryRow(
            user_id=user.id,
            card_id=card.id,
            finish="normal",
            quantity=1,
            is_pending=False,
            is_proxy=False,
        )
    )
    db.commit()
    return card


# ── #168: the filter engine is actually loaded ──────────────────────────────


def test_the_picker_serves_the_list_filter_script(client, db, user):
    _commander(db, user, "Atraxa, Praetors' Voice", "sc-atraxa")

    body = client.get("/recommendations/commander").text

    assert "/static/list-filter.js" in body, "the filter box is inert without the engine"
    # Cache-busted like every other per-page script.
    assert re.search(r"/static/list-filter\.js\?v=[^\"']+", body)


def test_the_filter_control_and_its_target_are_still_emitted(client, db, user):
    """The script is useless without the attributes it binds to."""
    _commander(db, user, "Atraxa, Praetors' Voice", "sc-atraxa")

    body = client.get("/recommendations/commander").text

    assert "data-list-filter" in body
    assert 'data-list-filter-target="#commander-list li"' in body
    assert "data-filter-text=" in body


def test_every_template_emitting_the_filter_also_loads_the_engine(client, db, user):
    """The class of bug, not just this instance.

    #168 existed because one of eight templates emitted the attribute without the
    script. This fails if a ninth is ever added the same way.
    """
    import pathlib

    root = pathlib.Path("app/templates")
    offenders = []
    for path in root.rglob("*.html"):
        text = path.read_text()
        if "data-list-filter" not in text:
            continue
        # A partial may rely on its including page; only flag full pages.
        if "{% extends" not in text:
            continue
        if "list-filter.js" not in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"emit data-list-filter but never load the engine: {offenders}"
