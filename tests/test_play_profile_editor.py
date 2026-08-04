"""#178 — the play-profile editor on the deck page.

The schema, the owner-scoped GET/POST API and the deploy-time seeding all
shipped in v4.12.42–.45; this issue was the human surface. So what needs pinning
is the SURFACE: that the panel renders, that it renders the profile's real
content, and that the editor's round-trip does not quietly lose the parts of the
payload it does not draw.

Measured on prod 2026-08-03 and the reason the editor exists: 38 profiles, 29 of
them model-inferred, and **`is_custom` false on every one** — nobody had ever
reviewed a profile, because there was nowhere to do it.
"""

from __future__ import annotations

import json

import pytest

from app.models import Deck, DeckPlayProfile, InventoryRow, StorageLocation

SEEDED = {
    "role": "value_tempo",
    "primary_plan": [
        "Attack early with Pako to exile cards, then cast them with Haldan.",
        "Attack early with Pako to exile cards,",
        "Convert the established engine into a decisive combat finish.",
    ],
    "secondary_plan": ["When the primary engine is answered twice, shift to independent threats"],
    "hard_rules": ["Protect: the core commander engine"],
    "confidence": "medium",
    "source": "pilot-bible 2026-08-01 + hand-authored corrections",
}


@pytest.fixture
def deck(db, user):
    loc = StorageLocation(user_id=user.id, name="Brew", type="deck", mode="manual")
    db.add(loc)
    db.commit()
    d = Deck(user_id=user.id, storage_location_id=loc.id, name="Brew", format="Commander")
    db.add(d)
    db.commit()
    return d


def _seed_profile(db, deck, profile=None, *, is_custom=False):
    row = DeckPlayProfile(
        deck_id=deck.id,
        profile_data=json.dumps(profile or SEEDED),
        is_custom=is_custom,
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------
# The panel renders — the #152 failure mode a service test cannot see.
# --------------------------------------------------------------------------


def test_the_panel_renders_the_profile_on_the_deck_page(client, db, deck):
    """Route-level, not service-level.

    `get_play_profile` returning a row proves nothing about the page: the route
    enumerates its template context key by key, so a new service value that is
    never added to that dict renders the empty state while every service test
    passes. That is exactly how #152 shipped an empty playgroup record.
    """
    _seed_profile(db, deck)
    page = client.get(f"/decks/{deck.id}").text

    assert "How to pilot this deck" in page
    assert "value_tempo" in page
    assert "Attack early with Pako" in page
    assert "shift to independent threats" in page
    assert "Protect: the core commander engine" in page


def test_an_unreviewed_profile_is_badged_and_says_why_it_matters(client, db, deck):
    """29 of 38 prod profiles are inferred; the badge is the whole prompt to fix them."""
    _seed_profile(db, deck)
    page = client.get(f"/decks/{deck.id}").text

    assert "inferred — unreviewed" in page
    assert "simulated games that rate this deck" in page


def test_a_pilot_edited_profile_is_not_nagged(client, db, deck):
    """Once a pilot has written it, the nudge would be noise — and a panel that
    nags on every deck is one people stop reading (the #176 rule)."""
    _seed_profile(db, deck, {**SEEDED, "confidence": "high"}, is_custom=True)
    page = client.get(f"/decks/{deck.id}").text

    assert "yours" in page
    assert "inferred — unreviewed" not in page
    assert "simulated games that rate this deck" not in page


def test_a_deck_with_no_profile_offers_to_write_one(client, db, deck):
    """11 of 49 live decks have no profile. The panel must be a way in, not a blank."""
    page = client.get(f"/decks/{deck.id}").text

    assert "How to pilot this deck" in page
    assert "No profile yet" in page
    assert 'name="primary_plan"' in page


def test_the_panel_is_not_gated_on_the_deck_having_cards(client, db, deck):
    """A profile is intent, not contents.

    The analytics block above it is gated on `if all_deck_rows:` — the right
    condition for anything computed FROM the cards. A plan is not computed from
    the cards: a #164 placeholder still has one, and the gauntlet still pilots it.
    """
    _seed_profile(db, deck)
    assert db.query(InventoryRow).filter(InventoryRow.user_id == deck.user_id).count() == 0

    page = client.get(f"/decks/{deck.id}").text
    assert "value_tempo" in page


def test_list_entries_render_one_per_line_in_the_textarea(client, db, deck):
    """The editor is a textarea-of-lines, so the join must emit real newlines.

    A Jinja `join('\\n')` that emitted a literal backslash-n would collapse every
    entry onto one line, and the pilot would silently save a one-element list —
    destroying the structure rather than editing it.
    """
    _seed_profile(db, deck)
    page = client.get(f"/decks/{deck.id}").text

    start = page.index('name="primary_plan"')
    body = page[start : page.index("</textarea>", start)]
    assert "\\n" not in body, "join emitted a literal backslash-n, not a newline"
    assert body.count("\n") >= 2, "three plan entries should span three lines"


# --------------------------------------------------------------------------
# The save contract the editor depends on.
# --------------------------------------------------------------------------


def test_saving_preserves_keys_the_editor_does_not_draw(client, db, deck):
    """`source` is in the seed today and the seed may add more tomorrow.

    The editor merges over the ORIGINAL payload for this reason. Pinned at the
    API because that is the contract the JS relies on — an editor that drops
    what it does not render loses information every time somebody fixes a typo.
    """
    _seed_profile(db, deck)
    edited = {**SEEDED, "role": "combo", "confidence": "high"}

    resp = client.post(
        f"/decks/{deck.id}/play-profile",
        data={"profile_data": json.dumps(edited)},
    )
    assert resp.status_code == 200

    saved = client.get(f"/decks/{deck.id}/play-profile").json()["profile"]
    assert saved["source"] == SEEDED["source"]
    assert saved["role"] == "combo"


def test_saving_marks_the_row_custom_so_the_reseed_cannot_overwrite_it(client, db, deck):
    """`is_custom` is what protects a pilot's words from the next deploy's seed.

    It had never been exercised in production — 0 of 38 rows — because there was
    no surface that set it.
    """
    _seed_profile(db, deck)
    assert db.query(DeckPlayProfile).filter_by(deck_id=deck.id).one().is_custom is False

    client.post(
        f"/decks/{deck.id}/play-profile",
        data={"profile_data": json.dumps({**SEEDED, "role": "combo"})},
    )
    db.expire_all()
    assert db.query(DeckPlayProfile).filter_by(deck_id=deck.id).one().is_custom is True


def test_another_users_deck_is_a_404(client, db, user):
    """Owner scoping — the panel is an editor, so this is a write boundary."""
    from app.models import User

    other = User(username="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    loc = StorageLocation(user_id=other.id, name="Theirs", type="deck", mode="manual")
    db.add(loc)
    db.commit()
    theirs = Deck(user_id=other.id, storage_location_id=loc.id, name="Theirs", format="Commander")
    db.add(theirs)
    db.commit()

    assert client.get(f"/decks/{theirs.id}/play-profile").status_code == 404
    assert (
        client.post(
            f"/decks/{theirs.id}/play-profile",
            data={"profile_data": json.dumps(SEEDED)},
        ).status_code
        == 404
    )


def test_a_non_object_payload_is_a_400_not_a_persisted_mess(client, db, deck):
    """The simulation trusts this text; junk must not reach it."""
    assert (
        client.post(
            f"/decks/{deck.id}/play-profile",
            data={"profile_data": json.dumps(["not", "a", "dict"])},
        ).status_code
        == 400
    )
    assert (
        client.post(f"/decks/{deck.id}/play-profile", data={"profile_data": "{"}).status_code == 400
    )
