"""#166 — play sessions, the winning-deck-benched house rule, and session records.

The issue's own discipline is the shape of this suite: it asked that date
clustering be VERIFIED rather than assumed, and verifying it found the failure
case already present in prod. So the regression cases here are the real ones —
2026-06-28's two-events-on-one-day, and a game with no playgroup at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import session_service
from app.models import Deck, Game, GameSeat, GameSession, Playgroup, PlaygroupMember, User
from app.timeutil import utc_now

DAY = datetime(2026, 6, 28, tzinfo=utc_now().tzinfo)


@pytest.fixture
def playgroup(db, user):
    pg = Playgroup(name="Pod", created_by=user.id)
    db.add(pg)
    db.commit()
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=user.id, role="owner"))
    db.commit()
    return pg


def _deck(db, user, name):
    d = Deck(user_id=user.id, name=name, format="Commander")
    db.add(d)
    db.commit()
    return d


def _game(db, user, *, playgroup_id, played_at, status="finalized"):
    g = Game(
        user_id=user.id,
        playgroup_id=playgroup_id,
        played_at=played_at,
        status=status,
        format="Commander",
    )
    db.add(g)
    db.commit()
    return g


def _seat(db, game, deck, *, placement=None, user_id=None):
    s = GameSeat(
        game_id=game.id,
        deck_id=deck.id if deck else None,
        seat_number=1,
        player_name="P",
        placement=placement,
        user_id=user_id,
    )
    db.add(s)
    db.commit()
    return s


# --------------------------------------------------------------------------
# The session entity, and the one-open-session invariant.
# --------------------------------------------------------------------------


def test_open_session_finds_before_creating(db, playgroup):
    """FIND before CREATE — the #164 rule.

    `uq_game_sessions_open_per_playgroup` is UNIQUE over open sessions, so a
    blind insert raises on the SECOND game of an evening, which is the normal
    case rather than an edge one.
    """
    first = session_service.open_session(db, playgroup.id)
    second = session_service.open_session(db, playgroup.id)
    assert first.id == second.id
    assert db.query(GameSession).count() == 1


def test_a_closed_session_does_not_block_a_new_one(db, playgroup):
    """The partial predicate's other half: closed sessions may repeat freely.

    Without `WHERE ended_at IS NULL` a playgroup could hold exactly one session
    ever — the same trap `uq_decks_user_name` documents.
    """
    first = session_service.open_session(db, playgroup.id)
    session_service.close_session(db, first)
    second = session_service.open_session(db, playgroup.id)

    assert second.id != first.id
    assert db.query(GameSession).count() == 2
    assert session_service.get_open_session(db, playgroup.id).id == second.id


def test_closing_is_idempotent_and_keeps_the_first_end_time(db, playgroup):
    """Two people tapping "End session" as everyone packs up is the expected case."""
    s = session_service.open_session(db, playgroup.id)
    session_service.close_session(db, s)
    first_end = s.ended_at
    session_service.close_session(db, s)
    assert s.ended_at == first_end


# --------------------------------------------------------------------------
# THE regression case: two events on one day, and a game with no playgroup.
# --------------------------------------------------------------------------


def test_a_game_with_no_playgroup_gets_no_session(db, user, playgroup):
    """Game 64's case, and the reason the model is playgroup-scoped.

    2026-06-28 in prod holds four finalized games spanning 20.2 hours: game 64 at
    `00:00:00` with no playgroup (one member playing with non-members, a
    manual-log default stamp), then a real meetup 16:10–20:09. **Date grouping
    folds the foreign game into the playgroup's session.** Playgroup scoping
    excludes it for free — and no clock rule could have, since nothing about a
    midnight timestamp distinguishes a default from a real one.
    """
    foreign = _game(db, user, playgroup_id=None, played_at=DAY)
    theirs = _game(db, user, playgroup_id=playgroup.id, played_at=DAY + timedelta(hours=16))

    assert session_service.attach_game_to_session(db, foreign) is None
    attached = session_service.attach_game_to_session(db, theirs)

    assert foreign.session_id is None
    assert theirs.session_id == attached.id
    # The two games share a calendar date and are in different worlds.
    assert foreign.played_at.date() == theirs.played_at.date()


def test_attaching_is_idempotent(db, user, playgroup):
    g = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    first = session_service.attach_game_to_session(db, g)
    second = session_service.attach_game_to_session(db, g)
    assert first.id == second.id
    assert db.query(GameSession).count() == 1


# --------------------------------------------------------------------------
# The house rule — derived, never stored.
# --------------------------------------------------------------------------


def test_winning_benches_the_deck_for_the_rest_of_the_session(db, user, playgroup):
    sess = session_service.open_session(db, playgroup.id)
    winner = _deck(db, user, "Raph and Mikey")
    loser = _deck(db, user, "Fright Night")

    g1 = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    g1.session_id = sess.id
    _seat(db, g1, winner, placement=1)
    _seat(db, g1, loser, placement=2)
    db.commit()

    assert session_service.benched_deck_ids(db, sess.id) == {winner.id}


def test_bench_state_is_scoped_to_the_session_not_to_all_time(db, user, playgroup):
    """A deck benched last week is playable tonight — that is the whole rule."""
    old = session_service.open_session(db, playgroup.id)
    winner = _deck(db, user, "Raph and Mikey")
    g1 = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    g1.session_id = old.id
    _seat(db, g1, winner, placement=1)
    db.commit()
    session_service.close_session(db, old)

    tonight = session_service.open_session(db, playgroup.id)
    assert session_service.benched_deck_ids(db, tonight.id) == set()


def test_bench_is_derived_so_correcting_a_placement_corrects_the_bench(db, user, playgroup):
    """The argument for NOT storing a benched flag.

    A stored flag keeps asserting a bench the record no longer supports. A
    derived one cannot be wrong about the games it can see.
    """
    sess = session_service.open_session(db, playgroup.id)
    deck = _deck(db, user, "Contested")
    g = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    g.session_id = sess.id
    seat = _seat(db, g, deck, placement=1)
    db.commit()
    assert session_service.benched_deck_ids(db, sess.id) == {deck.id}

    seat.placement = 2  # the table corrects a mis-recorded result
    db.commit()
    assert session_service.benched_deck_ids(db, sess.id) == set()


def test_an_unfinalized_game_benches_nothing(db, user, playgroup):
    """A game in progress has no result, the same rule #152's record uses."""
    sess = session_service.open_session(db, playgroup.id)
    deck = _deck(db, user, "Still Playing")
    g = _game(db, user, playgroup_id=playgroup.id, played_at=DAY, status="in_progress")
    g.session_id = sess.id
    _seat(db, g, deck, placement=1)
    db.commit()

    assert session_service.benched_deck_ids(db, sess.id) == set()


def test_playing_a_benched_deck_is_recorded_not_prevented(db, user, playgroup):
    """The rule is the TABLE's, not the app's.

    A hard block is faithful right up until the table waives it, and an app that
    overrules the people at the table gets a game logged wrong or not at all.
    Measured on prod 2026-08-04: 0 violations across 66 deck-game rows — the rule
    is honoured perfectly, which is exactly why its statistical effect is real.
    """
    sess = session_service.open_session(db, playgroup.id)
    deck = _deck(db, user, "Raph and Mikey")

    g1 = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    g1.session_id = sess.id
    _seat(db, g1, deck, placement=1)
    g2 = _game(db, user, playgroup_id=playgroup.id, played_at=DAY + timedelta(hours=2))
    g2.session_id = sess.id
    _seat(db, g2, deck, placement=3)
    db.commit()

    violations = session_service.bench_violations(db, sess.id)
    assert len(violations) == 1
    assert violations[0]["deck_id"] == deck.id
    assert violations[0]["game_id"] == g2.id


def test_a_deck_that_wins_its_LAST_game_is_no_violation(db, user, playgroup):
    """Order is what makes the single pass correct.

    A win recorded after an appearance does not bench that appearance
    retroactively — the deck simply won later.
    """
    sess = session_service.open_session(db, playgroup.id)
    deck = _deck(db, user, "Slow Starter")

    g1 = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    g1.session_id = sess.id
    _seat(db, g1, deck, placement=3)
    g2 = _game(db, user, playgroup_id=playgroup.id, played_at=DAY + timedelta(hours=2))
    g2.session_id = sess.id
    _seat(db, g2, deck, placement=1)
    db.commit()

    assert session_service.bench_violations(db, sess.id) == []


# --------------------------------------------------------------------------
# The session-grained record — the metric the house rule makes necessary.
# --------------------------------------------------------------------------


def test_session_record_differs_from_the_game_record(db, user, playgroup):
    """Raph and Mikey's real shape: 5 of 5 sessions on 5 of 7 games.

    Reproduced in miniature — 2 sessions, won both, 3 games, 2 game wins. The
    game figure is a FLOOR: under the house rule the deck could not have won a
    third game, because winning the first benched it.
    """
    deck = _deck(db, user, "Raph and Mikey")
    for day, results in [(DAY, [1]), (DAY + timedelta(days=7), [2, 1])]:
        sess = session_service.open_session(db, playgroup.id)
        for i, placement in enumerate(results):
            g = _game(db, user, playgroup_id=playgroup.id, played_at=day + timedelta(hours=i))
            g.session_id = sess.id
            _seat(db, g, deck, placement=placement)
        db.commit()
        session_service.close_session(db, sess)

    stats = session_service.deck_session_stats(db, [deck.id])[deck.id]
    assert stats["sessions_played"] == 2
    assert stats["sessions_won"] == 2
    assert stats["session_win_rate"] == 100
    # ...while the game-level record over the same games is only 2 of 3.


def test_a_win_is_the_MINIMUM_placement_not_the_maximum(db, user, playgroup):
    """placement 1 is the BEST result, so a session win is min(placement) == 1.

    **This test needs a session where min and max DISAGREE**, or it proves
    nothing: a single 4th-place finish gives min == max == 4 and passes under
    either reading. So the deck places 4th, then wins — min 1, max 4. Reading
    the aggregate backwards reports the deck's WORST finish and calls it the
    result, which still produces a plausible number.
    """
    sess = session_service.open_session(db, playgroup.id)
    deck = _deck(db, user, "Slow Starter")

    g1 = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    g1.session_id = sess.id
    _seat(db, g1, deck, placement=4)
    g2 = _game(db, user, playgroup_id=playgroup.id, played_at=DAY + timedelta(hours=2))
    g2.session_id = sess.id
    _seat(db, g2, deck, placement=1)
    db.commit()

    stats = session_service.deck_session_stats(db, [deck.id])[deck.id]
    assert stats["sessions_played"] == 1, "two games in one session is ONE session"
    assert stats["sessions_won"] == 1


def test_a_session_the_deck_never_won_is_not_a_win(db, user, playgroup):
    """The other side of the same aggregate."""
    sess = session_service.open_session(db, playgroup.id)
    deck = _deck(db, user, "Never Wins")
    g = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    g.session_id = sess.id
    _seat(db, g, deck, placement=4)
    db.commit()

    stats = session_service.deck_session_stats(db, [deck.id])[deck.id]
    assert stats["sessions_played"] == 1
    assert stats["sessions_won"] == 0


def test_a_game_outside_any_session_is_not_counted(db, user, playgroup):
    """An unaffiliated game has no session, so it cannot move a session record."""
    deck = _deck(db, user, "Elsewhere")
    g = _game(db, user, playgroup_id=None, played_at=DAY)
    _seat(db, g, deck, placement=1)
    db.commit()

    assert session_service.deck_session_stats(db, [deck.id]) == {}


# --------------------------------------------------------------------------
# Wiring: new games join a session; deleting a playgroup takes its sessions.
# --------------------------------------------------------------------------


def test_linking_a_game_to_a_playgroup_attaches_a_session(db, user, playgroup):
    """set_game_playgroup is the SHARED mutator, so it is the seam — it also
    covers a game linked to a playgroup after the fact, which the create route
    could not."""
    from app.game_service import set_game_playgroup

    g = _game(db, user, playgroup_id=None, played_at=DAY)
    assert g.session_id is None

    assert set_game_playgroup(db, g.id, user.id, playgroup.id) is True
    db.refresh(g)
    assert g.session_id is not None


def test_unlinking_a_game_clears_its_session(db, user, playgroup):
    """A session belongs to a playgroup — a game no longer in one cannot sit in
    that playgroup's evening."""
    from app.game_service import set_game_playgroup

    g = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    set_game_playgroup(db, g.id, user.id, playgroup.id)
    db.refresh(g)
    assert g.session_id is not None

    set_game_playgroup(db, g.id, user.id, None)
    db.refresh(g)
    assert g.session_id is None


def test_deleting_a_playgroup_removes_its_sessions_and_unlinks_the_games(db, user, playgroup):
    """SQLite runs FKs OFF, so the CASCADE is NOT the mechanism in production.

    `delete_playgroup` does it explicitly — nulling `games.session_id` BEFORE
    deleting the sessions, which is the autoflush-ordering trap #148 hit on
    Postgres.
    """
    from app import playgroup_service

    g = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    session_service.attach_game_to_session(db, g)
    assert db.query(GameSession).count() == 1

    ok, err = playgroup_service.delete_playgroup(db, user.id, playgroup.id)
    assert ok, err
    db.commit()

    assert db.query(GameSession).count() == 0
    db.refresh(g)
    assert g.session_id is None
    assert g.playgroup_id is None


# --------------------------------------------------------------------------
# The picker warns.
# --------------------------------------------------------------------------


def test_the_new_game_picker_marks_benched_decks(client, db, user, playgroup):
    """Route-level: the context key has to reach the template (the #152 mode)."""
    sess = session_service.open_session(db, playgroup.id)
    deck = _deck(db, user, "Raph and Mikey")
    g = _game(db, user, playgroup_id=playgroup.id, played_at=DAY)
    g.session_id = sess.id
    _seat(db, g, deck, placement=1, user_id=user.id)
    db.commit()

    page = client.get("/games/new").text
    assert "benchedByPlaygroup" in page
    assert str(deck.id) in page.split("benchedByPlaygroup")[1][:200]


def test_no_open_session_means_nothing_is_benched(client, db, user, playgroup):
    """Control — the map is empty rather than absent, so the JS has no special case."""
    _deck(db, user, "Anything")
    page = client.get("/games/new").text
    assert "const benchedByPlaygroup = {}" in page


def test_ending_a_session_is_member_gated(client, db, user, playgroup):
    session_service.open_session(db, playgroup.id)

    other = User(username="stranger@example.com", password_hash="x")
    db.add(other)
    db.commit()

    resp = client.post(f"/playgroups/{playgroup.id}/end-session", follow_redirects=False)
    assert resp.status_code == 303
    assert session_service.get_open_session(db, playgroup.id) is None
