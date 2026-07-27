"""#158 — cross-game pod dynamics and pace.

Split from #96 because the gate differs: per-deck surfaces need repeat samples of the
same deck, these are player-level and the roster is stable.

The tests build event streams directly rather than driving the live service, because the
cases that matter (a revive that changes elimination ORDER, two seats out in the same
round) are fiddly to produce through the API and trivial to state as events. The event
SHAPES are copied from what the service writes — see `test_life_chart_resolution.py` for
the service-driven equivalents.
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import timedelta

from app import playgroup_service
from app.game_analytics_service import build_cross_game_analytics
from app.models import Game, GameEvent, GameSeat, User
from app.timeutil import utc_now

_seq = itertools.count(1)


def _user(db, name=None) -> User:
    n = next(_seq)
    u = User(username=f"cg{n}@ex.com", password_hash="x", display_name=name or f"U{n}")
    db.add(u)
    db.flush()
    return u


class _Builder:
    """Assemble one recorded live game's event stream at 1-second resolution."""

    def __init__(self, db, owner, players, *, minutes=10):
        self.db, self.t, self.n = db, utc_now(), 0
        self.game = Game(
            user_id=owner.id,
            format="Commander",
            status="finalized",
            client_token="T",
            played_at=self.t,
        )
        db.add(self.game)
        db.flush()
        self.seats = {}
        for i, (label, user) in enumerate(players, start=1):
            s = GameSeat(
                game_id=self.game.id,
                seat_number=i,
                player_name=label,
                user_id=(user.id if user else None),
                starting_life=40,
            )
            db.add(s)
            db.flush()
            self.seats[label] = s
        self.minutes = minutes
        self.eliminated: dict[str, bool] = {}
        self._ev(
            "live_started",
            None,
            json.dumps(
                {
                    "lives": {str(s.id): 40 for s in self.seats.values()},
                    "eliminated": {},
                    "turn": 1,
                    "currentTurnId": next(iter(self.seats.values())).id,
                }
            ),
            secs=0,  # the stream starts at t+0, so total length is exactly `minutes`
        )

    def _ev(self, atype, seat, payload, *, secs=1):
        self.n += secs
        self.db.add(
            GameEvent(
                game_id=self.game.id,
                action_type=atype,
                seat_id=(seat.id if seat is not None else None),
                turn=1,
                actor_kind="table",
                payload=payload,
                created_at=self.t + timedelta(seconds=self.n),
            )
        )

    def turn(self, secs=1):
        self._ev("turn", None, json.dumps({}), secs=secs)
        return self

    def out(self, label, secs=1):
        s = self.seats[label]
        self._ev("eliminate", s, json.dumps({"seat_id": s.id, "eliminated": True}), secs=secs)
        self.eliminated[str(s.id)] = True
        return self

    def revive(self, label, secs=1):
        s = self.seats[label]
        self._ev("eliminate", s, json.dumps({"seat_id": s.id, "eliminated": False}), secs=secs)
        self.eliminated.pop(str(s.id), None)
        return self

    def finish(self, total_minutes=None):
        self.n = (total_minutes or self.minutes) * 60
        # The finalized blob mirrors what the service writes: an `eliminated` map and a
        # round-grained `eliminatedAtTurn`. Every event here is turn 1, so all seats TIE
        # on that key — which is exactly the case where blob ordering falls back to seat
        # id and gets the order wrong.
        self._ev(
            "finalized",
            None,
            json.dumps(
                {
                    "lives": {str(s.id): 0 for s in self.seats.values()},
                    "eliminated": dict(self.eliminated),
                    "eliminatedAtTurn": dict.fromkeys(self.eliminated, 1),
                }
            ),
            secs=0,
        )
        self.db.flush()
        return self.game


def _players(a):
    return {r["label"]: r for r in a["players"]}


# ── the surfaces ─────────────────────────────────────────────────────────────


def test_first_out_frequency_per_player(db):
    owner, b, c = _user(db, "Ann"), _user(db, "Bo"), _user(db, "Cy")
    for first in ("Bo", "Bo", "Cy"):
        g = _Builder(db, owner, [("Ann", owner), ("Bo", b), ("Cy", c)])
        g.out(first).out("Cy" if first == "Bo" else "Bo").finish()

    a = build_cross_game_analytics(db, owner.id)
    p = _players(a)
    assert a["games"] == 3
    assert (p["Bo"]["first_out"], p["Bo"]["games"]) == (2, 3)
    assert (p["Cy"]["first_out"], p["Cy"]["games"]) == (1, 3)
    assert p["Ann"]["first_out"] == 0
    assert p["Ann"]["survived"] == 3  # never eliminated in any game
    # Percentage is of THIS player's games, not of all games.
    assert p["Bo"]["first_out_pct"] == 67


def test_elimination_order_is_surfaced_per_game_and_aggregated(db):
    owner, b, c = _user(db, "Ann"), _user(db, "Bo"), _user(db, "Cy")
    g = _Builder(db, owner, [("Ann", owner), ("Bo", b), ("Cy", c)])
    g.out("Cy").out("Bo").finish()

    a = build_cross_game_analytics(db, owner.id)
    assert a["per_game"][0]["order"] == ["Cy", "Bo"]
    assert a["per_game"][0]["survivors"] == ["Ann"]
    p = _players(a)
    assert p["Cy"]["positions"][0] == 1  # 1st out once
    assert p["Bo"]["positions"][1] == 1  # 2nd out once


def test_a_revived_seat_is_ordered_by_its_FINAL_elimination(db):
    """The load-bearing case. Bo is eliminated first, revived, and dies last — so Cy is
    first out, not Bo. Reading the finalized blob instead would get this wrong."""
    owner, b, c = _user(db, "Ann"), _user(db, "Bo"), _user(db, "Cy")
    g = _Builder(db, owner, [("Ann", owner), ("Bo", b), ("Cy", c)])
    g.out("Bo").revive("Bo").out("Cy").out("Bo").finish()

    a = build_cross_game_analytics(db, owner.id)
    assert a["per_game"][0]["order"] == ["Cy", "Bo"]
    p = _players(a)
    assert p["Cy"]["first_out"] == 1
    assert p["Bo"]["first_out"] == 0


def test_a_seat_revived_and_never_re_eliminated_counts_as_survived(db):
    owner, b = _user(db, "Ann"), _user(db, "Bo")
    g = _Builder(db, owner, [("Ann", owner), ("Bo", b)])
    g.out("Bo").revive("Bo").finish()

    a = build_cross_game_analytics(db, owner.id)
    assert a["per_game"][0]["order"] == []
    assert _players(a)["Bo"]["survived"] == 1


def test_average_length_by_pod_size_carries_its_sample_size(db):
    owner = _user(db, "Ann")
    others = [_user(db) for _ in range(4)]
    names = ["Ann", "B", "C", "D", "E"]
    for mins in (10, 20):  # two 4-player games
        _Builder(
            db, owner, [(names[i], (owner if i == 0 else others[i - 1])) for i in range(4)]
        ).finish(mins)
    _Builder(
        db, owner, [(names[i], (owner if i == 0 else others[i - 1])) for i in range(5)]
    ).finish(30)  # one 5-player

    pods = {p["pod_size"]: p for p in build_cross_game_analytics(db, owner.id)["pace_by_pod"]}
    assert pods[4]["games"] == 2 and pods[4]["avg_label"] == "15m 00s"
    assert pods[5]["games"] == 1 and pods[5]["avg_label"] == "30m 00s"


def test_average_turn_duration_is_attributed_via_segment_owners(db):
    """No recorded game carries `active_seat_id` (0 of 324 in prod), so ownership comes
    from the rotation replay. Turns rotate Ann → Bo → Ann, so each gets one segment."""
    owner, b = _user(db, "Ann"), _user(db, "Bo")
    g = _Builder(db, owner, [("Ann", owner), ("Bo", b)])
    g.turn(secs=10).turn(secs=30).finish()

    p = _players(build_cross_game_analytics(db, owner.id))
    assert p["Ann"]["turns"] == 1 and p["Ann"]["avg_turn_label"] == "10s"
    assert p["Bo"]["turns"] == 1 and p["Bo"]["avg_turn_label"] == "30s"


# ── exclusions, grouping and thin states ─────────────────────────────────────


def test_games_with_no_event_stream_are_excluded_not_counted_as_zero(db):
    owner = _user(db, "Ann")
    _Builder(db, owner, [("Ann", owner)]).finish(10)
    bare = Game(user_id=owner.id, format="Commander", status="finalized", played_at=utc_now())
    db.add(bare)
    db.flush()
    db.add(
        GameSeat(
            game_id=bare.id, seat_number=1, player_name="Ann", user_id=owner.id, starting_life=40
        )
    )
    db.flush()

    a = build_cross_game_analytics(db, owner.id)
    assert a["games"] == 1  # the hand-logged game contributes nothing, not a zero
    assert _players(a)["Ann"]["games"] == 1


def test_players_group_by_user_id_and_guests_collapse_to_one_row(db):
    """#152's rule, same reason: `player_name` is a per-seat snapshot and one account
    really has played under several spellings."""
    owner, b = _user(db, "Ann"), _user(db, "Bo")
    _Builder(db, owner, [("Ann", owner), ("Bo", b), ("Brett", None)]).out("Brett").finish()
    _Builder(db, owner, [("Ann", owner), ("Bobby", b), ("Someone", None)]).out("Someone").finish()

    p = _players(build_cross_game_analytics(db, owner.id))
    # One account, ONE row — carrying the most recent spelling, since games are
    # ordered newest-first. "Bo" and "Bobby" must not both appear.
    assert "Bo" not in p and p["Bobby"]["games"] == 2
    guests = p[playgroup_service.GUESTS_LABEL]
    assert guests["games"] == 2 and guests["first_out"] == 2  # one row, not two


def test_no_qualifying_games_returns_none_so_the_section_hides(db):
    owner = _user(db, "Ann")
    assert build_cross_game_analytics(db, owner.id) is None
    bare = Game(user_id=owner.id, format="Commander", status="finalized", played_at=utc_now())
    db.add(bare)
    db.flush()
    assert build_cross_game_analytics(db, owner.id) is None


def test_a_game_with_no_eliminations_renders_without_dividing_by_zero(db):
    owner, b = _user(db, "Ann"), _user(db, "Bo")
    _Builder(db, owner, [("Ann", owner), ("Bo", b)]).turn().finish()

    a = build_cross_game_analytics(db, owner.id)
    assert a["max_place"] == 0
    for r in a["players"]:
        assert r["first_out"] == 0 and r["positions"] == [] and r["survived"] == 1
    assert a["per_game"][0]["order"] == []


def test_scope_is_the_viewers_participant_set(db):
    """Owned + played-in, the same definition Recent Games uses. Someone else's game
    that this viewer never sat in must not appear."""
    owner, other = _user(db, "Ann"), _user(db, "Zed")
    _Builder(db, other, [("Zed", other)]).finish()
    assert build_cross_game_analytics(db, owner.id) is None

    _Builder(db, other, [("Zed", other), ("Ann", owner)]).finish()
    a = build_cross_game_analytics(db, owner.id)
    assert a["games"] == 1  # only the one Ann played in


def test_the_page_renders_and_is_not_swallowed_by_the_game_id_route(db, client, user):
    """`/games/analytics` must match before `/games/{game_id}`, whose int converter would
    422 on the literal."""
    _Builder(db, user, [("Me", user), ("Them", _user(db))]).out("Them").finish()
    db.commit()
    r = client.get("/games/analytics")
    assert r.status_code == 200, r.status_code
    assert "Elimination order" in r.text and "Them" in r.text
    assert "wall clock" in r.text  # the turn-duration caveat is stated


def test_the_empty_state_renders_rather_than_zeros(db, client, user):
    r = client.get("/games/analytics")
    assert r.status_code == 200
    assert "No recorded live games yet" in r.text


# ── the per-game timeline now shares the same ordering authority ─────────────


def test_same_round_eliminations_are_ordered_by_EVENT_not_seat_id(db):
    """`game_summary.html`'s ELIMINATION ORDER block used to read the finalized blob,
    whose `eliminatedAtTurn` is round-grained — so two seats out in one round tied and
    broke by seat id. Live case: game 67 reported Phil before Alex although Alex died
    first. Seats here are created in id order Ann < Bo, and Bo dies FIRST, so a seat-id
    tiebreak would report Ann first."""
    from app.game_analytics_service import build_game_analytics

    owner, b = _user(db, "Ann"), _user(db, "Bo")
    g = _Builder(db, owner, [("Ann", owner), ("Bo", b)])
    g.out("Bo").out("Ann").finish()

    tl = build_game_analytics(db, g.game.id)["timeline"]
    assert [t["label"] for t in tl] == ["Bo", "Ann"]
    assert [t["remaining"] for t in tl] == [1, 0]


def test_the_timeline_still_excludes_a_revived_seat(db):
    from app.game_analytics_service import build_game_analytics

    owner, b = _user(db, "Ann"), _user(db, "Bo")
    g = _Builder(db, owner, [("Ann", owner), ("Bo", b)])
    g.out("Bo").revive("Bo").out("Ann").finish()

    tl = build_game_analytics(db, g.game.id)["timeline"]
    assert [t["label"] for t in tl] == ["Ann"]  # Bo came back; not in the order


def test_the_analytics_link_is_in_the_sidebar_and_marks_itself_active(db, client, user):
    """Both nav rows must never highlight at once: `/games` uses a startswith match, so
    it has to exclude the analytics path explicitly."""
    _Builder(db, user, [("Me", user)]).finish()
    db.commit()

    def nav_classes(html):
        """{href: class} for the two sidebar rows under Play."""
        out = {}
        for href, cls in re.findall(
            r'<a href="(/games(?:/analytics)?)"\s+class="(nav-item[^"]*)"', html
        ):
            out[href] = cls
        return out

    on_list = nav_classes(client.get("/games").text)
    assert "active" in on_list["/games"] and "active" not in on_list["/games/analytics"]

    on_analytics = nav_classes(client.get("/games/analytics").text)
    assert "active" in on_analytics["/games/analytics"]
    assert "active" not in on_analytics["/games"]
