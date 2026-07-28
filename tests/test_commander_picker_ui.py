"""#168 + #169 — the commander picker's filter actually runs, and rows carry hover data.

**#168:** the picker emitted `data-list-filter` and `data-list-filter-target` but was the
only one of the eight templates emitting them that never loaded `/static/list-filter.js`
— and `base.html` does not load it either. The box was inert. The list is unpaginated:
539 rows on the largest account, so a dead filter is a wall.

**#169:** hovering a row previews the card. The image URLs are emitted server-side from
the Jinja globals so `card-hover.js` never builds one — the same split `list-filter.js`
documents for filter semantics.

The hover behaviour itself is DOM work with no JS harness in this repo, so these tests
pin the server-side contract the script depends on: the script is served, the wiring
attributes are present, and every row carries both URLs. The interactive behaviour was
driven in Chromium; see the commit message.
"""

from __future__ import annotations

import re

from app.models import Card, InventoryRow, User


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


# ── #169: hover data comes from the server ──────────────────────────────────


def test_each_row_carries_both_image_urls(client, db, user):
    _commander(db, user, "Atraxa, Praetors' Voice", "sc-atraxa")

    body = client.get("/recommendations/commander").text

    assert 'data-card-image="' in body
    assert 'data-card-image-alt="' in body
    # The mirror URL is built by mirror_image_url(), not by JS.
    assert "sc-atraxa/normal.jpg" in body


def test_the_fallback_url_points_at_scryfall_not_the_mirror(client, db, user):
    """The onerror path must resolve a printing the mirror has not cached."""
    _commander(db, user, "Atraxa, Praetors' Voice", "sc-atraxa")

    body = client.get("/recommendations/commander").text
    alt = re.search(r'data-card-image-alt="([^"]+)"', body).group(1)

    assert "scryfall" in alt.lower()
    assert alt != re.search(r'data-card-image="([^"]+)"', body).group(1)


def test_the_list_is_wired_for_delegated_hover(client, db, user):
    """One container attribute — the script attaches ONE listener, not one per row."""
    _commander(db, user, "Atraxa, Praetors' Voice", "sc-atraxa")

    body = client.get("/recommendations/commander").text

    assert "data-card-hover" in body
    assert 'data-card-hover-target="li"' in body
    assert body.count("data-card-hover-target") == 1, "hover wiring must be per-list"


def test_the_hover_script_is_served(client, db, user):
    _commander(db, user, "Atraxa, Praetors' Voice", "sc-atraxa")

    body = client.get("/recommendations/commander").text

    assert re.search(r"/static/card-hover\.js\?v=[^\"']+", body)


def test_no_per_row_img_tag_is_emitted(client, db, user):
    """539 rows must not mean 539 <img> elements — one preview node, set on hover."""
    for i in range(6):
        _commander(db, user, f"Commander {i}", f"sc-{i}", collector=str(i))

    body = client.get("/recommendations/commander").text
    listing = body.split('id="commander-list"', 1)[1].split("</ul>", 1)[0]

    assert listing.count("<img") == 0, "the picker emitted per-row images"
    assert listing.count("data-card-image=") == 6


def test_an_empty_collection_renders_without_the_scripts_or_a_crash(client, db, user):
    """Both scripts sit inside the `{% if candidates %}` branch."""
    body = client.get("/recommendations/commander").text

    assert "don't own any Commander-legal legendary creatures" in body
    assert "card-hover.js" not in body


def test_the_hover_script_documents_its_contract_like_list_filter(client, db, user):
    """#169 asked for the same header-comment convention."""
    import pathlib

    src = pathlib.Path("app/static/card-hover.js").read_text()
    head = src[: src.index("(function")]

    assert "data-card-image" in head, "the data-attribute contract is undocumented"
    assert "Used by:" in head
    assert "commander_picker.html" in head
    # The two constraints most likely to be "simplified" away by a later reader.
    assert "pointer-events" in head
    assert "hover: hover" in head


def test_other_users_commanders_never_appear(client, db, user):
    """Guard: the picker is owner-scoped and hover data must not widen that."""
    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    _commander(db, other, "Secret Tech Commander", "sc-secret")

    body = client.get("/recommendations/commander").text

    assert "Secret Tech Commander" not in body
    assert "sc-secret" not in body
