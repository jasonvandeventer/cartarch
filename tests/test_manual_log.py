"""Manual game logging for external matches (#42).

Log a game played outside Cartarch — a Game *born finalized*, composing
create_game + end_game so it is data-identical to a live-tracked, finalized
game. Covers the service function (log_game), the route, and the security guards
(forged deck / playgroup, bad input) that must create nothing on failure.

    DATA_DIR=dev-data DEV_MODE=true pytest tests/test_manual_log.py
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import game_service
from app.models import Deck, Game, Playgroup, PlaygroupMember, User


def _deck(db, user_id, name="My Deck") -> Deck:
    d = Deck(user_id=user_id, name=name)
    db.add(d)
    db.commit()
    return d


def _opps(*names):
    return [{"name": n, "deck_name": f"{n}'s deck"} for n in names]


# ── Core functionality ───────────────────────────────────────────────


def test_win_two_seats(db, user):
    d = _deck(db, user.id)
    g = game_service.log_game(db, user.id, "won", datetime(2026, 7, 8), _opps("Alex"), deck_id=d.id)
    seats = list(g.seats)
    assert g.status == "finalized"
    assert len(seats) == 2
    assert seats[0].user_id == user.id and seats[0].deck_id == d.id
    assert seats[0].placement == 1  # logger won
    assert seats[1].placement == 2
    # opponent free-text deck name reuses deck_name_at_game, no FK
    assert seats[1].deck_id is None
    assert seats[1].deck_name_at_game == "Alex's deck"


def test_loss_explicit_winner(db, user):
    g = game_service.log_game(
        db, user.id, "lost", datetime(2026, 7, 8), _opps("Alex", "Bo"), winner_index=1
    )
    seats = list(g.seats)
    assert seats[0].placement == 2  # logger lost
    assert seats[2].placement == 1  # Bo (opponent index 1 → seat 2) won
    assert seats[1].placement == 2


def test_draw_three_seats(db, user):
    g = game_service.log_game(db, user.id, "draw", datetime(2026, 7, 8), _opps("Alex", "Bo"))
    seats = list(g.seats)
    assert len(seats) == 3
    assert all(s.placement == 1 for s in seats)  # tie for first, nobody lost


def test_loss_unknown_winner(db, user):
    g = game_service.log_game(
        db, user.id, "lost", datetime(2026, 7, 8), _opps("Alex", "Bo"), winner_index=None
    )
    seats = list(g.seats)
    assert all(s.placement == 2 for s in seats)  # no winner recorded
    assert not any(s.placement == 1 for s in seats)


def test_played_at_and_elapsed_sane(db, user):
    past = datetime(2025, 1, 1)
    g = game_service.log_game(db, user.id, "won", past, _opps("Alex"))
    assert g.played_at == past
    # ended_at anchored to played_at → elapsed is "<1m", never days-since.
    assert g.ended_at == past


# ── Security / validation ────────────────────────────────────────────


def test_forged_deck_rejected(db, user):
    other = User(username="other@x.com", password_hash="x")
    db.add(other)
    db.commit()
    stolen = _deck(db, other.id, name="Not Yours")
    with pytest.raises(ValueError):
        game_service.log_game(
            db, user.id, "won", datetime(2026, 7, 8), _opps("Alex"), deck_id=stolen.id
        )
    assert db.query(Game).count() == 0  # nothing created


def test_forged_playgroup_rejected(db, user):
    other = User(username="other2@x.com", password_hash="x")
    db.add(other)
    db.commit()
    pg = Playgroup(name="Their Group", created_by=other.id)
    db.add(pg)
    db.commit()
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=other.id, role="owner"))
    db.commit()
    with pytest.raises(ValueError):
        game_service.log_game(
            db, user.id, "won", datetime(2026, 7, 8), _opps("Alex"), playgroup_id=pg.id
        )
    assert db.query(Game).count() == 0


def test_accessible_playgroup_links(db, user):
    pg = Playgroup(name="My Group", created_by=user.id)
    db.add(pg)
    db.commit()
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=user.id, role="owner"))
    db.commit()
    g = game_service.log_game(
        db, user.id, "won", datetime(2026, 7, 8), _opps("Alex"), playgroup_id=pg.id
    )
    assert g.playgroup_id == pg.id


def test_empty_opponents_rejected(db, user):
    with pytest.raises(ValueError):
        game_service.log_game(db, user.id, "won", datetime(2026, 7, 8), [])
    assert db.query(Game).count() == 0


def test_too_many_opponents_rejected(db, user):
    with pytest.raises(ValueError):
        game_service.log_game(
            db, user.id, "won", datetime(2026, 7, 8), _opps(*[f"P{i}" for i in range(7)])
        )
    assert db.query(Game).count() == 0


def test_blank_opponent_name_rejected(db, user):
    with pytest.raises(ValueError):
        game_service.log_game(db, user.id, "won", datetime(2026, 7, 8), [{"name": "  "}])


def test_bad_result_rejected(db, user):
    with pytest.raises(ValueError):
        game_service.log_game(db, user.id, "forfeited", datetime(2026, 7, 8), _opps("Alex"))


# ── Route + rendering ────────────────────────────────────────────────


def test_route_creates_and_summarizes(client, db, user):
    d = _deck(db, user.id)
    r = client.post(
        "/games/manual-log",
        data={
            "played_date": "2026-07-08",
            "format": "Commander",
            "my_deck_id": str(d.id),
            "result": "won",
            "opp_names": ["Alex", "", "Bo"],  # middle blank dropped
            "opp_decks": ["Elves", "", "Goblins"],
            "notes": "great game",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    gid = int(r.headers["location"].rsplit("/", 1)[1])
    game = db.query(Game).get(gid)
    assert game.status == "finalized"
    assert len(game.seats) == 3  # logger + 2 non-blank opponents

    # Finalized → summary page, NOT the live tracker (no life counter).
    page = client.get(f"/games/{gid}")
    assert page.status_code == 200
    assert 'id="game-app"' not in page.text


def test_route_malformed_date_400(client, db, user):
    r = client.post(
        "/games/manual-log",
        data={"played_date": "not-a-date", "result": "won", "opp_names": ["Alex"]},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert db.query(Game).count() == 0


def test_route_lost_without_winner_400(client, db, user):
    r = client.post(
        "/games/manual-log",
        data={"played_date": "2026-07-08", "result": "lost", "winner": "", "opp_names": ["Alex"]},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_manual_log_page_renders(client):
    r = client.get("/games/manual-log")
    assert r.status_code == 200
    assert "Log a Game" in r.text
