"""#152 — playgroup win-loss record, and the picker default that keeps it complete.

Baseline for the aggregation rules is prod data measured 2026-07-26: grouping by
`player_name` would split one real account (`Alex` / `SaintWacko`) into two rows, which
is why the grouping key is `user_id`.
"""

from __future__ import annotations

import itertools

import pytest

from app import playgroup_service as svc
from app.models import Game, GameSeat, Playgroup, PlaygroupMember, User


@pytest.fixture
def seq():
    return itertools.count(1)


def _user(db, seq, name=None) -> User:
    n = next(seq)
    u = User(username=f"pr{n}@ex.com", password_hash="x", display_name=name or f"U{n}")
    db.add(u)
    db.flush()
    return u


def _playgroup(db, owner, *members) -> Playgroup:
    pg = Playgroup(name="Pod", created_by=owner.id)
    db.add(pg)
    db.flush()
    db.add(PlaygroupMember(playgroup_id=pg.id, user_id=owner.id, role="owner"))
    for m in members:
        db.add(PlaygroupMember(playgroup_id=pg.id, user_id=m.id))
    db.flush()
    return pg


def _game(db, owner, playgroup, seats):
    """seats: list of (user_or_None, placement, player_name)."""
    g = Game(
        user_id=owner.id,
        format="Commander",
        status="finalized",
        playgroup_id=playgroup.id if playgroup else None,
    )
    db.add(g)
    db.flush()
    for i, (u, placement, name) in enumerate(seats, start=1):
        db.add(
            GameSeat(
                game_id=g.id,
                seat_number=i,
                player_name=name,
                user_id=u.id if u else None,
                placement=placement,
                starting_life=40,
            )
        )
    db.flush()
    return g


def _by_label(record):
    return {r["label"]: r for r in record}


def test_record_reports_played_wins_losses_and_rate(db, seq):
    a, b = _user(db, seq, "Alpha"), _user(db, seq, "Beta")
    pg = _playgroup(db, a, b)
    _game(db, a, pg, [(a, 1, "Alpha"), (b, 2, "Beta")])
    _game(db, a, pg, [(a, 2, "Alpha"), (b, 1, "Beta")])
    _game(db, a, pg, [(a, 1, "Alpha"), (b, 2, "Beta")])

    rec = _by_label(svc.playgroup_record(db, pg.id))
    assert (rec["Alpha"]["played"], rec["Alpha"]["wins"], rec["Alpha"]["losses"]) == (3, 2, 1)
    assert (rec["Beta"]["played"], rec["Beta"]["wins"], rec["Beta"]["losses"]) == (3, 1, 2)
    assert rec["Alpha"]["win_rate"] == pytest.approx(2 / 3)


def test_wins_plus_losses_always_reconcile_to_played(db, seq):
    a, b = _user(db, seq), _user(db, seq)
    pg = _playgroup(db, a, b)
    for placement in (1, 2, 3, 1):
        _game(db, a, pg, [(a, placement, "A"), (b, 2, "B")])
    for r in svc.playgroup_record(db, pg.id):
        assert r["wins"] + r["losses"] == r["played"], r


def test_grouping_is_by_user_id_not_player_name(db, seq):
    """One account that played under two names is ONE row — the live `Alex` /
    `SaintWacko` case."""
    a, b = _user(db, seq, "SaintWacko"), _user(db, seq, "Other")
    pg = _playgroup(db, a, b)
    _game(db, a, pg, [(a, 1, "Alex"), (b, 2, "Other")])
    _game(db, a, pg, [(a, 2, "SaintWacko"), (b, 1, "Other")])

    rec = svc.playgroup_record(db, pg.id)
    assert len(rec) == 2
    assert _by_label(rec)["SaintWacko"]["played"] == 2


def test_every_unattributed_seat_collapses_into_one_guests_row(db, seq):
    a = _user(db, seq, "Alpha")
    pg = _playgroup(db, a)
    _game(db, a, pg, [(a, 1, "Alpha"), (None, 2, "Brett"), (None, 3, "Opp 1")])
    _game(db, a, pg, [(a, 1, "Alpha"), (None, 2, "Someone else")])

    rec = _by_label(svc.playgroup_record(db, pg.id))
    assert set(rec) == {"Alpha", svc.GUESTS_LABEL}
    assert rec[svc.GUESTS_LABEL]["played"] == 3  # one row, not three
    assert rec[svc.GUESTS_LABEL]["user_id"] is None


def test_a_tie_for_first_is_a_win_for_every_tied_seat(db, seq):
    """#114 permits duplicate placements for simultaneous eliminations."""
    a, b, c = _user(db, seq, "A"), _user(db, seq, "B"), _user(db, seq, "C")
    pg = _playgroup(db, a, b, c)
    _game(db, a, pg, [(a, 1, "A"), (b, 1, "B"), (c, 3, "C")])

    rec = _by_label(svc.playgroup_record(db, pg.id))
    assert rec["A"]["wins"] == 1 and rec["B"]["wins"] == 1
    assert rec["C"]["wins"] == 0 and rec["C"]["losses"] == 1


def test_a_tie_below_first_is_a_loss_for_every_tied_seat(db, seq):
    a, b, c = _user(db, seq, "A"), _user(db, seq, "B"), _user(db, seq, "C")
    pg = _playgroup(db, a, b, c)
    _game(db, a, pg, [(a, 1, "A"), (b, 2, "B"), (c, 2, "C")])

    rec = _by_label(svc.playgroup_record(db, pg.id))
    assert (rec["B"]["wins"], rec["B"]["losses"]) == (0, 1)
    assert (rec["C"]["wins"], rec["C"]["losses"]) == (0, 1)


def test_only_games_carrying_this_playgroup_id_are_counted(db, seq):
    """An unlinked private game must never surface on a shared page."""
    a, b = _user(db, seq, "Alpha"), _user(db, seq, "Beta")
    pg = _playgroup(db, a, b)
    other = _playgroup(db, b)
    _game(db, a, pg, [(a, 1, "Alpha"), (b, 2, "Beta")])
    _game(db, a, None, [(a, 1, "Alpha"), (b, 2, "Beta")])  # unlinked
    _game(db, b, other, [(a, 1, "Alpha"), (b, 2, "Beta")])  # a DIFFERENT playgroup

    assert _by_label(svc.playgroup_record(db, pg.id))["Alpha"]["played"] == 1


def test_a_linked_game_still_in_progress_does_not_break_reconciliation(db, seq):
    """No placement recorded yet = no result yet. Counting it would add a game that is
    neither a win nor a loss and break `wins + losses == played`."""
    a, b = _user(db, seq, "Alpha"), _user(db, seq, "Beta")
    pg = _playgroup(db, a, b)
    _game(db, a, pg, [(a, 1, "Alpha"), (b, 2, "Beta")])
    _game(db, a, pg, [(a, None, "Alpha"), (b, None, "Beta")])  # in progress

    rec = _by_label(svc.playgroup_record(db, pg.id))
    assert rec["Alpha"]["played"] == 1
    assert rec["Alpha"]["wins"] + rec["Alpha"]["losses"] == 1


def test_sorted_by_wins_then_win_rate(db, seq):
    a, b, c = _user(db, seq, "A"), _user(db, seq, "B"), _user(db, seq, "C")
    pg = _playgroup(db, a, b, c)
    # A: 2 wins of 4. B: 2 wins of 2 (same wins, better rate). C: 0 of 1.
    for placement in (1, 1, 2, 2):
        _game(db, a, pg, [(a, placement, "A")])
    for _ in range(2):
        _game(db, a, pg, [(b, 1, "B")])
    _game(db, a, pg, [(c, 2, "C")])

    assert [r["label"] for r in svc.playgroup_record(db, pg.id)] == ["B", "A", "C"]


def test_a_playgroup_with_no_linked_games_returns_empty_not_an_error(db, seq):
    a = _user(db, seq)
    pg = _playgroup(db, a)
    assert svc.playgroup_record(db, pg.id) == []
    detail = svc.get_playgroup_detail(db, pg.id, a.id)
    assert detail["record"] == []  # the page renders its empty state


def test_the_record_rides_on_get_playgroup_detail(db, seq):
    a = _user(db, seq, "Alpha")
    pg = _playgroup(db, a)
    _game(db, a, pg, [(a, 1, "Alpha")])
    detail = svc.get_playgroup_detail(db, pg.id, a.id)
    assert [r["label"] for r in detail["record"]] == ["Alpha"]
    # ...and a non-member still gets nothing at all.
    outsider = _user(db, seq)
    assert svc.get_playgroup_detail(db, pg.id, outsider.id) is None


# ── the recurrence fix: preselect the playgroup on game creation ─────────────
# A game nobody links is invisible to the record above, and the blank default made
# that the outcome of simply forgetting. Both creation paths must preselect when there
# is exactly one playgroup, and neither may guess when there are several.
#
# Driven through the REAL routes rather than by slicing the template: the picker only
# behaves correctly if the route also supplies `user_playgroups`, and a block extracted
# by regex would pass even if the route stopped passing it.

import re  # noqa: E402

_PICKER_PAGES = ("/games/new", "/games/manual-log")


def _picker(html: str) -> str:
    """The playgroup <select> only."""
    m = re.search(r'<select name="playgroup_id".*?</select>', html, re.S)
    return m.group(0) if m else ""


@pytest.mark.parametrize("path", _PICKER_PAGES)
def test_exactly_one_playgroup_is_preselected(client, db, user, path, seq):
    _playgroup(db, user)
    db.commit()
    sel = _picker(client.get(path).text)
    assert sel, f"no playgroup picker on {path}"
    assert re.search(r'<option value="\d+"[^>]*\sselected', sel), sel
    assert not re.search(r'<option value=""[^>]*\sselected', sel), sel


@pytest.mark.parametrize("path", _PICKER_PAGES)
def test_several_playgroups_preserve_the_blank_default(client, db, user, path, seq):
    """With a choice to make, the app must not guess one."""
    for _ in range(3):
        _playgroup(db, user)
    db.commit()
    sel = _picker(client.get(path).text)
    assert re.search(r'<option value=""[^>]*\sselected', sel), sel
    assert not re.search(r'<option value="\d+"[^>]*\sselected', sel), sel


@pytest.mark.parametrize("path", _PICKER_PAGES)
def test_blank_stays_selectable_when_one_playgroup_is_preselected(client, db, user, path, seq):
    """Preselected is not the same as forced — "not shared" must remain reachable."""
    _playgroup(db, user)
    db.commit()
    assert '<option value=""' in _picker(client.get(path).text)


@pytest.mark.parametrize("path", _PICKER_PAGES)
def test_no_playgroups_means_no_picker_at_all(client, db, user, path, seq):
    """Unchanged pre-existing behaviour: the whole block is omitted."""
    assert _picker(client.get(path).text) == ""


def test_the_record_table_actually_renders_on_the_page(client, db, user, seq):
    """Route-level, deliberately: the context is enumerated key-by-key in
    `playgroups_detail`, so a service that returns the record is not enough — the first
    cut passed every service test above while the page still showed the empty state."""
    other = _user(db, seq, "Rival")
    pg = _playgroup(db, user, other)
    _game(db, user, pg, [(user, 1, "Me"), (other, 2, "Rival"), (None, 3, "Guest")])
    _game(db, user, pg, [(user, 2, "Me"), (other, 1, "Rival")])
    db.commit()

    html = client.get(f"/playgroups/{pg.id}").text
    table = re.search(r"playgroup-record-table.*?</tbody>", html, re.S)
    assert table, "record table missing — is `record` in the route context?"
    cells = [
        " ".join(re.sub(r"<[^>]+>", " ", c).split())
        for c in re.findall(r"<td.*?</td>", table.group(0), re.S)
    ]
    joined = " | ".join(cells)
    assert "Rival" in joined and svc.GUESTS_LABEL in joined
    assert "50%" in joined  # 1 of 2 for both players
    assert "Each member links their own games" in html  # the incompleteness caveat


def test_the_empty_state_renders_rather_than_a_broken_table(client, db, user, seq):
    pg = _playgroup(db, user)
    db.commit()
    html = client.get(f"/playgroups/{pg.id}").text
    assert "No games are linked to this playgroup yet" in html
    assert "playgroup-record-table" not in html
