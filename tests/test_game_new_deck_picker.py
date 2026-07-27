"""The new-game deck picker offers borrowed decks, scoped to seatable people (#156).

Two things, one seam.

**The affordance.** A seat records its pilot (`user_ids`) and the deck played
(`deck_ids`) as independent facts, and `POST /games` has always accepted a deck
whose owner is not the seat-holder ("cross-user permissive stance"). Only the
dropdown implied otherwise, by offering the seat user's decks and nothing else, so
borrowing a deck meant misattributing the seat or dropping the deck link. The picker
now carries every seatable person's decks plus the owner name to group them by.

**The scoping.** That payload used to be `session.query(Deck).all()` — every deck in
the system serialised into the page for the JS to filter. Since the seat picker only
ever offers `get_pickable_users`, no other deck was reachable; the extra rows were
pure disclosure of other users' deck names in the page source. Now filtered
server-side.
"""

from __future__ import annotations

import json
import re

from app.models import Deck, StorageLocation, User


def _deck(db, owner, name):
    loc = StorageLocation(user_id=owner.id, name=name, type="deck", mode="manual")
    db.add(loc)
    db.commit()
    d = Deck(user_id=owner.id, name=name, storage_location_id=loc.id)
    db.add(d)
    db.commit()
    return d


def _user(db, username, display=None, active=True):
    u = User(username=username, password_hash="x", display_name=display, is_active=active)
    db.add(u)
    db.commit()
    return u


def _decks_payload(client) -> dict:
    """Pull the decksByUser JSON literal back out of the rendered page."""
    body = client.get("/games/new").text
    m = re.search(r"const decksByUser = (\{.*?\});", body, re.S)
    assert m, "decksByUser payload not found on /games/new"
    return json.loads(m.group(1))


def test_another_seatable_players_decks_are_offered(client, db, user):
    """The affordance: a borrowed deck must be reachable from the picker."""
    mate = _user(db, "mate@example.com", display="Mason")
    _deck(db, user, "My Deck")
    _deck(db, mate, "Mason's Deck")

    payload = _decks_payload(client)

    assert str(user.id) in payload
    assert str(mate.id) in payload, "a co-seatable player's decks must be offered"
    assert [d["name"] for d in payload[str(mate.id)]] == ["Mason's Deck"]


def test_each_deck_carries_its_owner_name_for_grouping(client, db, user):
    """The dropdown groups borrowed decks under "Borrowed from <name>"."""
    mate = _user(db, "mate@example.com", display="Mason")
    _deck(db, mate, "Lathril's Last Hunt")

    payload = _decks_payload(client)

    assert payload[str(mate.id)][0]["owner"] == "Mason"


def test_owner_name_falls_back_to_username_without_a_display_name(client, db, user):
    mate = _user(db, "nodisplay@example.com", display=None)
    _deck(db, mate, "Anon Deck")

    payload = _decks_payload(client)

    assert payload[str(mate.id)][0]["owner"] == "nodisplay@example.com"


def test_decks_of_people_who_cannot_be_seated_are_not_shipped(client, db, user):
    """The disclosure fix.

    `get_pickable_users` returns only ACTIVE users (falling back to the global
    active list when the viewer has no playgroup co-members). An inactive user can
    never be seated, so their deck names have no business in the page source.
    """
    ghost = _user(db, "ghost@example.com", display="Ghost", active=False)
    _deck(db, ghost, "Secret Tech Brew")

    body = client.get("/games/new").text
    payload = _decks_payload(client)

    assert str(ghost.id) not in payload
    assert "Secret Tech Brew" not in body, "an unseatable user's deck name leaked into the page"


def test_the_viewers_own_decks_are_always_present(client, db, user):
    """Even with no other users, the seat-holder's own decks must be offered."""
    _deck(db, user, "Solo Deck")

    payload = _decks_payload(client)

    assert [d["name"] for d in payload[str(user.id)]] == ["Solo Deck"]


def test_deck_names_are_escaped_in_the_dropdown_builder(client, db, user):
    """Deck names are user-controlled and were interpolated raw into innerHTML."""
    _deck(db, user, "<img src=x onerror=alert(1)>")

    body = client.get("/games/new").text

    # The builder must route names through escHtml, not interpolate them bare.
    assert "escHtml(d.name)" in body
    # And the JSON payload itself must not break out of the <script> block.
    assert "</script>" not in json.dumps(_decks_payload(client))


def test_borrowed_framing_is_conditional_on_a_pilot_being_chosen(client, db, user):
    """With no pilot on the seat, "Borrowed from Jason" for your own deck is nonsense.

    LIMIT OF THIS TEST: the grouping runs in JS and this repo has no JS harness, so
    this asserts the source carries the conditional rather than the rendered result.
    The three states (seat = viewer / no pilot / seat = someone else) were driven in
    Chromium via Playwright and verified by hand; see the commit message.
    """
    mate = _user(db, "mate@example.com", display="Mason")
    _deck(db, mate, "Lathril's Last Hunt")

    body = client.get("/games/new").text

    assert "seatUserId ? `Borrowed from" in body, (
        "the borrowed framing must depend on a pilot being selected"
    )
