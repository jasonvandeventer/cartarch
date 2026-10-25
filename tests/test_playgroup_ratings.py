"""Rank complete shared pods, preserving privacy and explicit result edits."""

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import playgroup_service as svc
from app.models import (
    Deck,
    Game,
    GameEvent,
    GameSeat,
    GameVariant,
    Playgroup,
    PlaygroupMember,
    User,
)


def _event(sid, seconds, *, eliminated=True, action="eliminate", payload=None):
    return SimpleNamespace(
        id=seconds + 1,
        seat_id=sid,
        action_type=action,
        payload=json.dumps(payload or {"eliminated": eliminated}),
        created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds),
    )


@pytest.mark.parametrize("gap,expected", [(60, {1: 1, 2: 2, 3: 2}), (61, {1: 1, 2: 3, 3: 2})])
def test_elimination_gap_boundary_and_no_saved_mutation(gap, expected):
    seats = [
        SimpleNamespace(id=1, placement=1),
        SimpleNamespace(id=2, placement=2),
        SimpleNamespace(id=3, placement=2),
    ]
    game = SimpleNamespace(seats=seats, events=[_event(3, gap), _event(2, 0)])
    ranks, changed = svc._rating_placements(game)
    assert ranks == expected
    assert changed == (gap > 60)
    assert [s.placement for s in seats] == [1, 2, 2]


def test_revives_missing_evidence_and_owner_ties_are_preserved():
    game = SimpleNamespace(
        seats=[
            SimpleNamespace(id=1, placement=1),
            SimpleNamespace(id=2, placement=2),
            SimpleNamespace(id=3, placement=2),
        ],
        events=[_event(2, 0), _event(2, 10, eliminated=False), _event(2, 100), _event(3, 130)],
    )
    assert svc._rating_placements(game) == ({1: 1, 2: 2, 3: 2}, False)
    game.events = [_event(2, 0)]
    assert svc._rating_placements(game)[1] is False
    game.events.append(_event(3, 120))
    assert svc._rating_placements(game)[1] is True
    game.events.append(
        _event(None, 140, action="result_edit", payload={"placements": {"2": 2, "3": 2}})
    )
    assert svc._rating_placements(game)[1] is False
    game.seats[1].placement = 1
    assert svc._rating_placements(game)[0][2] == 1


def test_elimination_replay_uses_last_event_and_payload_seat_fallback():
    game = SimpleNamespace(
        seats=[SimpleNamespace(id=i, placement=1 if i == 1 else 2) for i in range(1, 4)],
        events=[
            _event(2, 0),
            _event(2, 120),
            _event(None, 130, payload={"seat_id": "3", "eliminated": True}),
        ],
    )
    assert svc._rating_placements(game) == ({1: 1, 2: 2, 3: 2}, False)


def test_consecutive_gap_groups_and_multiple_rank_bands():
    game = SimpleNamespace(
        seats=[SimpleNamespace(id=i, placement=(1 if i == 1 else 2)) for i in range(1, 6)],
        events=[_event(2, 0), _event(3, 58), _event(4, 63), _event(5, 180)],
    )
    assert svc._rating_placements(game) == ({1: 1, 2: 3, 3: 3, 4: 3, 5: 2}, True)


@pytest.fixture
def pod(db, user):
    others = [
        User(username=f"rating-{name}@test.invalid", password_hash="x", real_name=name)
        for name in ("Mason", "Patryk", "Alex", "Outside")
    ]
    user.real_name = "Jason V"
    db.add_all(others)
    db.flush()
    pg = Playgroup(name="Rating pod", created_by=user.id)
    db.add(pg)
    db.flush()
    users = [user, *others]
    db.add_all(
        PlaygroupMember(
            playgroup_id=pg.id, user_id=u.id, role="owner" if u.id == user.id else "member"
        )
        for u in users[:4]
    )
    db.commit()
    return pg, users


def _game(db, pg, users, ranks, index=0, fmt="Commander"):
    game = Game(
        user_id=users[0].id,
        playgroup_id=pg.id if pg else None,
        format=fmt,
        status="finalized",
        played_at=datetime(2026, 10, 25, tzinfo=UTC) + timedelta(hours=index),
    )
    db.add(game)
    db.flush()
    for i, (u, rank) in enumerate(zip(users, ranks, strict=True)):
        db.add(
            GameSeat(
                game_id=game.id,
                seat_number=i + 1,
                player_name=u.player_label,
                user_id=u.id,
                placement=rank,
                starting_life=40,
            )
        )
    db.commit()
    return game


def test_page_replays_chronologically_and_updates_after_result_edit(client, db, user, pod):
    pg, users = pod
    ranks = [[2, 1, 3], [3, 1, 2], [2, 2, 1], [1, 2, 3], [3, 2, 1], [2, 3, 1], [3, 2, 1]]
    games = [_game(db, pg, users[:3], ranks[i], i) for i in reversed(range(7))]
    result = svc.playgroup_ratings(db, pg.id, user.id, [u.id for u in users[:4]])
    assert result["rated_games"] == 7
    assert result["adjusted_games"] == 0
    assert [
        (r["label"], round(r["rating"]) if r["rating"] else None) for r in result["ratings"]
    ] == [("Patryk", 1790), ("Mason", 1504), ("Jason V", 1206), ("Alex", None)]
    url = f"/playgroups/{pg.id}/ratings?selection=1&" + "&".join(
        f"players={u.id}" for u in users[:4]
    )
    response = client.get(url)
    assert response.status_code == 200
    for text in ("1790", "1504", "1206", "Unrated", "7 completed games", "OpenSkill", "60 seconds"):
        assert text in response.text
    record_page = client.get(f"/playgroups/{pg.id}")
    assert record_page.status_code == 200
    table = re.search(r"playgroup-record-table.*?</tbody>", record_page.text, re.S)
    assert table is not None
    assert "<th>Rating</th>" in table.group(0)
    for label, rating in (("Patryk", "1790"), ("Mason", "1504"), ("Jason V", "1206")):
        row = next(row for row in re.findall(r"<tr>.*?</tr>", table.group(0), re.S) if label in row)
        assert f">{rating}</span>" in row
    assert ">Player and deck ratings</a>" not in record_page.text
    if os.getenv("RATING_BROWSER_FIXTURES"):
        Path(os.environ["RATING_BROWSER_FIXTURES"]).write_text(response.text)
        Path(os.environ["RATING_BROWSER_FIXTURES"]).with_name("record.html").write_text(
            record_page.text
        )
    games[0].seats[0].placement = 1
    games[0].seats[2].placement = 3
    db.commit()
    changed = client.get(url)
    assert changed.status_code == 200
    assert "1790" not in changed.text
    assert db.query(GameEvent).count() == 0


def test_scope_selection_variants_and_full_opponent_pods(client, db, user, pod):
    pg, users = pod
    a, b, c, d, outsider = users
    selected = [a.id, b.id]
    good = _game(db, pg, [a, b, outsider], [2, 3, 1])
    db.add(GameVariant(game_id=good.id, variant="planechase"))
    _game(db, pg, [a, b], [1, 2], 1, "Draft")
    _game(db, None, [a, b], [1, 2])
    _game(db, pg, [a, c], [1, 2])
    other_pg = Playgroup(name="Private pod", created_by=outsider.id)
    db.add(other_pg)
    db.flush()
    _game(db, other_pg, [a, b], [1, 2])
    result = svc.playgroup_ratings(db, pg.id, a.id, selected, "Commander")
    assert result["rated_games"] == result["planechase_games"] == 1
    assert [r["rating"] for r in result["ratings"]] == pytest.approx(
        [1543.035730211, 1284.821348944], abs=0.001
    )
    assert {r["user_id"] for r in result["ratings"]} == set(selected)
    assert svc.playgroup_ratings(db, pg.id, a.id, selected)["rated_games"] == 2
    assert svc.playgroup_ratings(db, pg.id, outsider.id) is None
    assert svc.playgroup_ratings(db, pg.id, a.id, [a.id, outsider.id])["input_error"]
    assert svc.playgroup_ratings(db, pg.id, a.id, selected, "bogus")["input_error"]
    assert "Select at least two" in client.get(f"/playgroups/{pg.id}/ratings?selection=1").text
    response = client.get(f"/playgroups/{other_pg.id}/ratings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/playgroups?error=not_a_member"


def test_timestamp_adjustment_is_visible_on_page(client, db, user, pod):
    pg, users = pod
    game = _game(db, pg, users[:3], [1, 2, 2])
    for seat, seconds in zip(game.seats[1:], (0, 61), strict=True):
        e = _event(seat.id, seconds)
        db.add(
            GameEvent(
                game_id=game.id,
                seat_id=seat.id,
                action_type=e.action_type,
                payload=e.payload,
                turn=5,
                actor_kind="table",
                created_at=e.created_at,
            )
        )
    db.commit()
    response = client.get(f"/playgroups/{pg.id}/ratings")
    assert response.status_code == 200
    assert "1 game adjusted from elimination timestamps" in response.text
    result = svc.playgroup_ratings(db, pg.id, user.id)
    values = {r["user_id"]: r["rating"] for r in result["ratings"]}
    assert values[users[2].id] > values[users[1].id]
    assert [s.placement for s in game.seats] == [1, 2, 2]


def test_incomplete_duplicate_and_unfinished_games_do_not_distort_ratings(db, user, pod):
    pg, users = pod
    _game(db, pg, users[:2], [1, None])
    _game(db, pg, [users[0], users[1], users[1]], [1, 2, 3])
    unfinished = _game(db, pg, users[:2], [1, 2])
    unfinished.status = "in_progress"
    db.commit()
    result = svc.playgroup_ratings(db, pg.id, user.id)
    assert result["rated_games"] == 0
    assert result["skipped_games"] == 2
    assert all(r["rating"] is None for r in result["ratings"])


def test_decks_replay_full_pods_without_merging_pilots_or_unknown_decks(client, db, user, pod):
    pg, users = pod
    a, b, c, _, _ = users
    alpha = Deck(user_id=a.id, name="Private renamed deck", retired_at=datetime.now(UTC))
    beta = Deck(user_id=b.id, name="Private second deck")
    unused = Deck(user_id=a.id, name="Unplayed private deck")
    db.add_all([alpha, beta, unused])
    db.commit()
    # Seed in reverse order; swap pilots, retaining the same two deck identities.
    for index, pilots in [(1, [b, a, c]), (0, [a, b, c])]:
        game = _game(db, pg, pilots, [1, 2, 3], index)
        for seat, deck, name in zip(
            game.seats, [alpha, beta, None], ["Alpha <deck>", "Beta", None], strict=True
        ):
            seat.deck_id = deck.id if deck else None
            seat.deck_name_at_game = name
            seat.commander_name_at_game = "Same commander"
        db.commit()
    r = svc.playgroup_ratings(db, pg.id, a.id, rank_by="decks")
    assert r["rated_games"] == 2
    assert r["unidentified_deck_seats"] == 2
    assert len(r["ratings"]) == 2
    rows = {row["deck_id"]: row for row in r["ratings"]}
    assert rows[alpha.id]["played"] == rows[beta.id]["played"] == 2
    assert rows[alpha.id]["wins"] == 2
    assert rows[alpha.id]["borrowed_games"] == rows[beta.id]["borrowed_games"] == 1
    assert rows[alpha.id]["rating"] > rows[beta.id]["rating"]
    assert [row["rating"] for row in r["ratings"]] == pytest.approx(
        [1832.115096786042, 1583.6916652513023], abs=0.001
    )
    response = client.get(f"/playgroups/{pg.id}/ratings?rank_by=decks")
    assert response.status_code == 200
    assert 'id="deck-ratings"' in response.text
    assert "Alpha &lt;deck&gt;" in response.text
    assert "2 seats have no linked deck" in response.text
    assert "Borrowed" in response.text and "high uncertainty" in response.text
    assert "Private renamed deck" not in response.text
    assert "Private second deck" not in response.text
    assert "Unplayed private deck" not in response.text
    if os.getenv("DECK_RATING_BROWSER_FIXTURES"):
        Path(os.environ["DECK_RATING_BROWSER_FIXTURES"]).write_text(response.text)
    assert db.get(Deck, alpha.id).retired_at is not None
    # Editing a recorded result updates deck ratings on the next view.
    game.seats[0].placement, game.seats[1].placement = 2, 1
    db.commit()
    changed = svc.playgroup_ratings(db, pg.id, a.id, rank_by="decks")
    assert {row["deck_id"]: row["wins"] for row in changed["ratings"]}[alpha.id] == 1


def test_deck_scope_incomplete_duplicate_and_missing_attribution(client, db, user, pod):
    pg, users = pod
    a, b, c, _, outsider = users
    deck = Deck(user_id=c.id, name="Private owned by another member")
    db.add(deck)
    db.commit()
    good = _game(db, pg, [a, b, c], [1, 2, 3])
    good.seats[2].deck_id = deck.id
    good.seats[2].user_id = None  # A guest borrowing counts too.
    db.commit()
    unknown = _game(db, pg, [a, b], [1, 2], 1, "Draft")
    duplicate = _game(db, pg, [a, b], [1, 2], 2)
    for seat in duplicate.seats:
        seat.deck_id = deck.id
    incomplete = _game(db, pg, [a, b], [1, None], 3)
    incomplete.seats[0].deck_id = deck.id
    unlinked = _game(db, None, [a, b], [1, 2], 4)
    unlinked.seats[0].deck_id = deck.id
    db.commit()
    r = svc.playgroup_ratings(db, pg.id, a.id, [a.id, b.id], rank_by="decks")
    assert r["rated_games"] == 1
    assert r["skipped_games"] == 2
    assert r["unattributed_games"] == 1
    assert r["unidentified_deck_seats"] == 4
    assert len(r["ratings"]) == 1
    assert r["ratings"][0]["label"] == f"Deck #{deck.id}"
    assert r["ratings"][0]["owner"] == c.player_label
    assert r["ratings"][0]["borrowed_games"] == 1
    assert svc.playgroup_ratings(db, pg.id, a.id, rank_by="bogus")["input_error"]
    assert svc.playgroup_ratings(db, pg.id, outsider.id, rank_by="decks") is None
    draft = svc.playgroup_ratings(db, pg.id, a.id, game_format="Draft", rank_by="decks")
    assert draft["rated_games"] == 0 and draft["unattributed_games"] == 1
    assert not draft["ratings"]
    response = client.get(f"/playgroups/{pg.id}/ratings?rank_by=decks&game_format=Draft")
    assert "1 game has no linked decks" in response.text
    assert unknown.seats[0].deck_id is None
