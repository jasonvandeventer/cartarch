"""A token has a finish, from the same closed vocabulary a card does.

Requested 2026-08-14: "while regular cards have a normal/foil/surge selector,
tokens do not". The request said *surge*; the canonical set is
``FINISH_OPTIONS = ("normal", "foil", "etched")``, and it is canonical because
``card_prices.finish`` (the MTGJSON ingest key) uses the same three — inventing
a fourth token-only value would put a finish in the database that the price
join and the label map do not know about.

Finish is NOT part of a merge key here: ``create_token`` always INSERTs, so a
foil and a normal printing of one token were already separate rows and the new
column cannot re-group, split or double anything.
"""

import pytest

from app.inventory_service import FINISH_OPTIONS
from app.models import TokenInventory
from app.token_service import create_token, get_token, update_token


def _mk(db, user, **kw):
    return create_token(db, user_id=user.id, name=kw.pop("name", "Treasure"), **kw)


def test_a_token_defaults_to_normal(db, user):
    t = _mk(db, user)
    assert t.finish == "normal"


@pytest.mark.parametrize("finish", FINISH_OPTIONS)
def test_every_canonical_finish_round_trips(db, user, finish):
    t = _mk(db, user, name=f"Soldier {finish}", finish=finish)
    assert get_token(db, t.id, user.id).finish == finish


def test_an_unknown_finish_floors_to_normal_instead_of_being_written(db, user):
    """The column is closed-vocabulary; a hand-posted value must not widen it."""
    t = _mk(db, user, name="Forged", finish="surge")
    assert t.finish == "normal"


def test_update_normalizes_too_and_never_writes_null(db, user):
    """The generic update loop maps "" to None — NOT NULL would blow up."""
    t = _mk(db, user, finish="foil")
    update_token(db, token_id=t.id, user_id=user.id, name=t.name, finish="")
    assert get_token(db, t.id, user.id).finish == "normal"

    update_token(db, token_id=t.id, user_id=user.id, name=t.name, finish="etched foil")
    assert get_token(db, t.id, user.id).finish == "etched"  # alias, not a new value


def test_finish_is_not_a_merge_key_two_finishes_stay_two_rows(db, user):
    """create_token never merges; the column must not change that."""
    _mk(db, user, name="Clue", finish="normal")
    _mk(db, user, name="Clue", finish="foil")
    rows = db.query(TokenInventory).filter(TokenInventory.name == "Clue").all()
    assert sorted(r.finish for r in rows) == ["foil", "normal"]
    assert all(r.quantity == 1 for r in rows)


def test_the_add_form_offers_the_finishes(client, db, user):
    html = client.get("/tokens/new").text
    assert 'name="finish"' in html
    for f in FINISH_OPTIONS:
        assert f'value="{f}"' in html


def test_the_route_persists_a_posted_finish(client, db, user):
    resp = client.post(
        "/tokens/create",
        data={"name": "Zombie", "quantity": "2", "finish": "foil", "csrf_token": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    t = db.query(TokenInventory).filter(TokenInventory.name == "Zombie").one()
    assert t.finish == "foil"


def test_the_list_badges_a_non_normal_finish_only(client, db, user):
    _mk(db, user, name="Plainwalker", finish="normal")
    _mk(db, user, name="Shinything", finish="foil")
    html = client.get("/tokens").text
    # The badge marks the exception; badging every row makes it noise.
    assert html.count('class="finish-badge">foil') == 1
    assert ">normal<" not in html.replace('value="normal"', "")
