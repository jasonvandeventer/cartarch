"""Who goes first is decided BEFORE START, not at creation (v4.12.26).

`/games/new` used to gate its Create button behind a "Who goes first?" modal. That
asked the question at the one moment it cannot be answered: with #165 seat claiming,
the players who will hold seats 2..N have not joined yet, so the host was choosing
between placeholders named `Player 2` and `Player 3`.

**Nothing in the model had to move**, which is the load-bearing fact. Both start
paths — `live_game_service._first_seat_id` and the local tracker's `firstSeatNumber`
— read `game.first_seat_number` at START time and fall back to the first seat when
it is NULL. Only the moment of asking was wrong.

The picker is therefore an OFFER, not a gate: leaving it unset must still start the
game, or the old defect comes back wearing a different button.
"""

from __future__ import annotations

import itertools

from app.game_service import set_first_seat
from app.models import Game, GameSeat, User

_seq = itertools.count(1)


def _game(db, owner, seats=4, status="created"):
    g = Game(user_id=owner.id, format="Commander", status=status)
    db.add(g)
    db.commit()
    for i in range(1, seats + 1):
        db.add(GameSeat(game_id=g.id, seat_number=i, player_name=f"Player {i}", starting_life=40))
    db.commit()
    return g


# ── The creation page no longer asks ────────────────────────────────────────


def test_create_game_is_a_plain_submit_with_no_first_player_modal(client):
    """The gate is gone from /games/new — button, modal and JS alike."""
    body = client.get("/games/new").text
    assert "Create Game" in body
    assert "Who goes first?" not in body
    assert "openFirstPlayerModal" not in body
    assert 'id="first-player-modal"' not in body


def test_creating_a_game_without_choosing_a_first_player_succeeds(client, db, user):
    """The whole point: creation must not require the answer."""
    before = db.query(Game).count()
    r = client.post(
        "/games",
        data={
            "format": "Commander",
            "starting_life": "40",
            "player_count": "2",
            "player_names": ["Alex", "Bo"],
            "deck_ids": ["", ""],
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert db.query(Game).count() == before + 1
    game = db.query(Game).order_by(Game.id.desc()).first()
    assert game.first_seat_number is None


# ── The game page asks instead ──────────────────────────────────────────────


def test_the_picker_renders_on_the_game_page_while_created(client, db, user):
    """A service-level test cannot see this: the route has to reach the template.

    Same failure shape as #152, where `record` never reached `playgroup_detail.html`
    while every service test passed.
    """
    g = _game(db, user)
    body = client.get(f"/games/{g.id}").text
    assert "Who goes first?" in body
    assert f'action="/games/{g.id}/first-seat"' in body
    assert "Roll for first player" in body


def test_the_owner_sets_the_first_seat_from_the_game_page(client, db, user):
    g = _game(db, user)
    r = client.post(
        f"/games/{g.id}/first-seat",
        data={"seat_number": "3"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.refresh(g)
    assert g.first_seat_number == 3


def test_an_empty_value_clears_the_choice(client, db, user):
    g = _game(db, user)
    g.first_seat_number = 2
    db.commit()
    client.post(f"/games/{g.id}/first-seat", data={"seat_number": ""}, follow_redirects=False)
    db.refresh(g)
    assert g.first_seat_number is None


# ── Guards ──────────────────────────────────────────────────────────────────


def test_a_seat_from_another_game_is_refused_not_normalized(client, db, user):
    """A wrong starting player is silently wrong for the entire game.

    The rest of game creation is deliberately non-blocking (a bad format falls
    back, a bad attribution is dropped). That posture is WRONG here — the caller
    named a specific player, and quietly starting someone else is exactly the
    failure this change is about.
    """
    g = _game(db, user, seats=4)
    r = client.post(f"/games/{g.id}/first-seat", data={"seat_number": "9"})
    assert r.status_code == 400
    db.refresh(g)
    assert g.first_seat_number is None


def test_a_non_owner_cannot_set_the_first_player(db, user):
    other = User(username=f"fp{next(_seq)}@ex.com", password_hash="x")
    db.add(other)
    db.commit()
    g = _game(db, user)
    assert set_first_seat(db, g.id, other.id, 2) is False
    db.refresh(g)
    assert g.first_seat_number is None


def test_a_started_game_refuses_the_change(db, user):
    """Once live, turn order lives in the live blob — rewriting the column here
    would desync the rotation from what the table is looking at."""
    g = _game(db, user, status="in_progress")
    assert set_first_seat(db, g.id, user.id, 2) is False
    db.refresh(g)
    assert g.first_seat_number is None


def test_the_picker_is_gone_once_the_game_is_no_longer_created(client, db, user):
    g = _game(db, user, status="in_progress")
    assert "/first-seat" not in client.get(f"/games/{g.id}").text


# ── The companion QR (v4.12.27) ─────────────────────────────────────────────


def test_a_live_game_offers_the_companion_link_as_a_qr(client, db, user):
    """Once live, the overlay stops offering a join code and offers the companion
    URL — which was a link to retype off a tablet."""
    g = _game(db, user, status="in_progress")
    body = client.get(f"/games/{g.id}").text
    # Slice from the block, not split() on it — "companion-share" appears four
    # times (wrapper, two labels, the url input), so split()[1] is a ten-character
    # fragment between the first two and would pass or fail for the wrong reason.
    block = body[body.index('class="companion-share"') :][:4000]
    assert "<svg" in block, "the companion QR is not inside the share block"
    assert "/companion" in body


def test_the_companion_qr_is_absent_before_the_game_starts(client, db, user):
    """`created` offers the JOIN code instead — two different hand-offs, and
    showing both at once would ask a player to scan the wrong one."""
    g = _game(db, user, status="created")
    r = client.get(f"/games/{g.id}")
    assert "companion-share" not in r.text


# ── Retired decks stay out of the pickers (v4.12.28) ────────────────────────


def test_a_retired_deck_is_not_offered_in_any_game_deck_picker(client, db, user):
    """#163 made deck deletion a RETIREMENT so game history survives. Four game
    surfaces hand-rolled `session.query(Deck)` instead of going through
    `list_decks`, so they never learned about it — a deck you "deleted" kept
    appearing everywhere you pick one, which is the whole user-visible point of
    deletion undone.

    Covers the new-game picker (cross-user), the manual log, the game-detail
    seat picker and the companion picker in one pass.
    """
    from app.deck_service import delete_deck
    from app.game_service import list_user_decks_for_companion
    from app.models import Deck, StorageLocation

    loc = StorageLocation(user_id=user.id, name="Ghost Deck", type="deck", mode="manual")
    db.add(loc)
    db.commit()
    d = Deck(user_id=user.id, name="Ghost Deck", storage_location_id=loc.id)
    db.add(d)
    db.commit()
    deck_id = d.id

    assert "Ghost Deck" in client.get("/games/new").text
    delete_deck(db, deck_id, user.id)
    db.expire_all()

    assert "Ghost Deck" not in client.get("/games/new").text
    assert "Ghost Deck" not in client.get("/games/manual-log").text
    assert all(x["name"] != "Ghost Deck" for x in list_user_decks_for_companion(db, user.id))

    g = _game(db, user)
    assert "Ghost Deck" not in client.get(f"/games/{g.id}").text


def test_a_placeholder_deck_is_not_offered_in_any_game_deck_picker(client, db, user):
    """#164's placeholders are real decks anchoring real game history — but they
    are not decks you can bring to a table, and they were showing up in every
    picker (three of them on the owner's own account, from the backfill).

    Owner decision 2026-07-28: keep them tracked, stop offering them. NOT by
    deleting, which would also drop them out of deck analytics and stop
    `resolve_commander_to_deck` matching them, so a replay of that commander
    would mint a SECOND placeholder and split the lineage.
    """
    from app.game_service import list_user_decks_for_companion
    from app.models import Deck, StorageLocation

    loc = StorageLocation(user_id=user.id, name="Ghost Cmdr", type="deck", mode="manual")
    db.add(loc)
    db.commit()
    d = Deck(user_id=user.id, name="Ghost Cmdr", storage_location_id=loc.id, contents_tracked=False)
    db.add(d)
    db.commit()

    assert "Ghost Cmdr" not in client.get("/games/new").text
    assert "Ghost Cmdr" not in client.get("/games/manual-log").text
    assert all(x["name"] != "Ghost Cmdr" for x in list_user_decks_for_companion(db, user.id))

    g = _game(db, user)
    assert "Ghost Cmdr" not in client.get(f"/games/{g.id}").text


def test_a_placeholder_still_matches_its_commander_so_the_lineage_holds(db, user):
    """The other half of the decision: invisible in pickers, still the anchor.

    If it stopped matching, the next game logged with that commander would create
    a second placeholder beside the first and the history would fork.
    """
    from app.deck_service import resolve_commander_to_deck
    from app.models import Card, Deck, DeckCommander, StorageLocation

    card = Card(
        scryfall_id="ph-1",
        name="Gorma, the Gullet",
        set_code="tst",
        collector_number="1",
        type_line="Legendary Creature — Horror",
    )
    loc = StorageLocation(user_id=user.id, name="Gorma", type="deck", mode="manual")
    db.add_all([card, loc])
    db.commit()
    d = Deck(user_id=user.id, name="Gorma", storage_location_id=loc.id, contents_tracked=False)
    db.add(d)
    db.commit()
    db.add(DeckCommander(deck_id=d.id, card_id=card.id))
    db.commit()

    found, missing = resolve_commander_to_deck(db, user.id, "Gorma, the Gullet")
    assert found is not None and found.id == d.id
    assert not missing


# ── The Decks page keeps them, in their own section (v4.12.31) ──────────────


def _placeholder(db, user, name):
    from app.models import Deck, StorageLocation

    loc = StorageLocation(user_id=user.id, name=name, type="deck", mode="manual")
    db.add(loc)
    db.commit()
    d = Deck(user_id=user.id, name=name, storage_location_id=loc.id, contents_tracked=False)
    db.add(d)
    db.commit()
    return d


def test_placeholders_render_in_their_own_section_not_the_main_list(client, db, user):
    """Owner decision 2026-07-28: keep them reachable, out of the way.

    Route-level, because a service test cannot see this — `record_only_decks` has
    to actually reach the template (the #152 failure mode).
    """
    _placeholder(db, user, "Anchor Only")
    body = client.get("/decks").text
    assert "Record-only decks (1)" in body
    assert "Anchor Only" in body
    # Not in the main rows: the compact-row markup is what "a real deck" looks like.
    head = body.split("Record-only decks")[0]
    assert "Anchor Only" not in head, "a placeholder leaked into the main deck list"


def test_placeholders_do_not_inflate_the_page_totals(client, db, user):
    """A count that includes decks you cannot play misstates what you own.

    Splitting in the ROUTE rather than the template is what makes this hold for
    the header totals, the featured pick and the rows at once.
    """
    from app.models import Deck, StorageLocation

    loc = StorageLocation(user_id=user.id, name="Real One", type="deck", mode="manual")
    db.add(loc)
    db.commit()
    db.add(Deck(user_id=user.id, name="Real One", storage_location_id=loc.id))
    db.commit()
    _placeholder(db, user, "Anchor Only")

    head = client.get("/decks").text.split("Record-only decks")[0]
    assert ">1<" in head, "the deck total should count the one real deck, not two"


def test_a_placeholder_is_still_deletable_from_the_page(client, db, user):
    """The reason they are not simply hidden. Hiding them would strand them."""
    d = _placeholder(db, user, "Anchor Only")
    assert f'action="/decks/{d.id}/delete"' in client.get("/decks").text
