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
