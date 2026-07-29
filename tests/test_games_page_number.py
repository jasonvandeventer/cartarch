"""The Games list shows the game number.

Games get referenced by number in conversation ("what happened in 46?"), and the
list gave no way to find one short of typing `/games/46` into the address bar.
Route-level, because the id has to reach the rendered table — a service test
cannot see a missing column.
"""

from __future__ import annotations

from app.models import Game, GameSeat


def _game(db, owner, status="finalized"):
    g = Game(user_id=owner.id, format="Commander", status=status)
    db.add(g)
    db.commit()
    db.add(
        GameSeat(game_id=g.id, seat_number=1, player_name="P1", starting_life=40, user_id=owner.id)
    )
    db.commit()
    return g


def test_the_game_number_is_listed_and_links_to_the_game(client, db, user):
    g = _game(db, user)

    body = client.get("/games").text

    assert f'<a href="/games/{g.id}">{g.id}</a>' in body


def test_the_column_is_headed(client, db, user):
    _game(db, user)

    body = client.get("/games").text
    head = body[body.index("<thead>") : body.index("</thead>")]

    assert "<th>#</th>" in head
    assert head.index("<th>#</th>") < head.index("<th>Date</th>"), "the number leads the row"
