"""#166 — play sessions, the winning-deck-benched house rule, and session-grained
deck records.

**Why sessions have to be modelled at all.** The playgroup runs a table rule: win
a game and that deck is benched for the rest of the session. Measured on prod
2026-08-04 across 66 deck-game rows, it is honoured **perfectly — zero
violations**. But a rule that is obeyed still distorts every game-level statistic
built on top of it:

* It **caps deck win rate structurally.** A deck can win at most once per
  session, so a deck playing four games in a night has a ceiling of 25% that
  night — and the decks playing the most games per session are precisely the ones
  that keep losing. Game-level win rate therefore penalises exactly the decks it
  should reward.
* It **conditions every game after the first on failure.** A deck's second game
  only happens because it lost its first. Only game one of a session is an
  unconditioned sample.

*Raph and Mikey* is the case that makes it concrete: **5 sessions, 5 session
wins, 7 games, 5 game wins.** It has won every session it has ever been played
in. Under the house rule it could not have done better. Its 71% game-level figure
is a floor, not a measurement, and no game-grained surface can express that.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Deck, Game, GameSeat, GameSession
from app.timeutil import utc_now

# A game counts toward a session record only once it has a result. The same rule
# #152's playgroup record uses, and for the same reason: a game still in progress
# has no outcome, and counting it would break the wins + losses == played
# reconciliation the surface promises.
FINALIZED = "finalized"


def get_open_session(session: Session, playgroup_id: int) -> GameSession | None:
    """The playgroup's currently-open session, or None.

    At most one can exist — `uq_game_sessions_open_per_playgroup` is a PARTIAL
    unique index (`WHERE ended_at IS NULL`). Both halves are load-bearing: open
    sessions must be unique or "which session does this game join" is ambiguous,
    and closed ones must repeat or a playgroup could only ever hold one session.
    """
    return (
        session.query(GameSession)
        .filter(GameSession.playgroup_id == playgroup_id, GameSession.ended_at.is_(None))
        .first()
    )


def open_session(
    session: Session, playgroup_id: int, *, started_at: datetime | None = None, commit: bool = True
) -> GameSession:
    """Find-or-create the playgroup's open session.

    **FIND before CREATE**, the #164 rule: the partial unique index makes a blind
    insert raise the moment a second game is created in one evening, which is the
    normal case rather than an edge one.
    """
    existing = get_open_session(session, playgroup_id)
    if existing:
        return existing
    row = GameSession(playgroup_id=playgroup_id, started_at=started_at or utc_now())
    session.add(row)
    # flush, never commit, when the caller is running a dry run or composing a
    # larger transaction — the #164 dry-run lesson (a commit=False that commits
    # anyway wrote five decks to production).
    session.flush()
    if commit:
        session.commit()
    return row


def close_session(
    session: Session, game_session: GameSession, *, commit: bool = True
) -> GameSession:
    """End a session. Idempotent — closing a closed session keeps its first end time."""
    if game_session.ended_at is None:
        game_session.ended_at = utc_now()
        if commit:
            session.commit()
    return game_session


def attach_game_to_session(
    session: Session, game: Game, *, commit: bool = True
) -> GameSession | None:
    """Put a game in its playgroup's open session, opening one if needed.

    **A game with no playgroup gets NO session, permanently, and that is correct
    rather than a gap.** A session belongs to a playgroup; an unaffiliated game
    has nothing to belong to. Game 64 — one member playing with non-members, a
    `00:00:00` manual-log stamp — is the live instance, and it is exactly the
    game a date-clustered model would have wrongly folded into the playgroup's
    2026-06-28 meetup.
    """
    if game.playgroup_id is None:
        return None
    if game.session_id is not None:
        return session.get(GameSession, game.session_id)
    row = open_session(session, game.playgroup_id, commit=False)
    game.session_id = row.id
    if commit:
        session.commit()
    return row


def benched_deck_ids(
    session: Session, session_id: int, *, before: datetime | None = None
) -> set[int]:
    """Deck ids benched by the house rule in this session.

    A deck is benched once it has WON a finalized game in the session. `before`
    scopes it to games earlier than a given moment, which is what turns the same
    query into "was this deck benched when that game was played?".

    **Derived, never stored, and the absence of a column is deliberate.** A
    stored `benched` flag drifts the instant a game is un-finalized or a
    placement is corrected — it would keep asserting a bench the record no longer
    supports. Recomputing cannot be wrong about the games it can see. This is the
    same argument `routes/decks.py` makes with `if all_deck_rows:` over a stored
    flag, and the one #164 makes about `contents_tracked`.
    """
    q = (
        session.query(GameSeat.deck_id)
        .join(Game, Game.id == GameSeat.game_id)
        .filter(
            Game.session_id == session_id,
            Game.status == FINALIZED,
            GameSeat.deck_id.isnot(None),
            GameSeat.placement == 1,
        )
    )
    if before is not None:
        q = q.filter(Game.played_at < before)
    return {row[0] for row in q.all()}


def bench_violations(session: Session, session_id: int) -> list[dict]:
    """Games in this session where a benched deck was played anyway.

    The house rule is **tracked, not enforced** (see `routes/games.py`): the app
    warns and records, it does not overrule the people at the table, who may
    waive their own rule any evening they like. A waiver that leaves no trace is
    the thing worth avoiding, so this is how one is read back.

    Measured on prod 2026-08-04: **0 violations across 66 deck-game rows.** The
    rule is honoured perfectly, which is precisely why the statistical distortion
    it causes is real rather than theoretical.
    """
    rows = (
        session.query(Game.id, Game.played_at, GameSeat.deck_id, Deck.name, GameSeat.placement)
        .join(GameSeat, GameSeat.game_id == Game.id)
        .join(Deck, Deck.id == GameSeat.deck_id)
        .filter(
            Game.session_id == session_id,
            Game.status == FINALIZED,
            GameSeat.deck_id.isnot(None),
        )
        .order_by(Game.played_at, Game.id)
        .all()
    )
    # ONE pass in time order, and the placement rides along in the same query —
    # a deck's first win benches it, and any LATER appearance is the violation.
    # Order is what makes a single pass sufficient: a win recorded after an
    # appearance does not bench that appearance retroactively, it just means the
    # deck won later.
    out: list[dict] = []
    won_at: dict[int, datetime] = {}
    for game_id, played_at, deck_id, deck_name, placement in rows:
        prior_win = won_at.get(deck_id)
        if prior_win is not None and prior_win < played_at:
            out.append(
                {
                    "game_id": game_id,
                    "deck_id": deck_id,
                    "deck_name": deck_name,
                    "played_at": played_at,
                    "won_at": prior_win,
                }
            )
        if placement == 1 and deck_id not in won_at:
            won_at[deck_id] = played_at
    return out


def deck_session_stats(session: Session, deck_ids: list[int]) -> dict[int, dict]:
    """Per-deck sessions played / sessions won, keyed by deck id.

    **This is the metric the house rule makes necessary.** It sits ALONGSIDE the
    game-level record rather than replacing it — both are true, they answer
    different questions, and #156's lesson is that two surfaces disagreeing
    silently is worse than either number alone.

    Counts every finalized seat holding the deck, exactly as
    `deck_service.compute_deck_game_stats` does — including a **borrowed** deck
    someone else piloted (#156's Option C). A session counts as won if the deck
    took `placement == 1` in any of its games, and #114 permits duplicate
    placements for simultaneous eliminations, so a shared first place is a win
    for every deck holding it.
    """
    if not deck_ids:
        return {}
    # ONE query: min(placement) per (deck, session). A win is placement 1, which
    # is the BEST result, so it is the MINIMUM — reading max() here would report
    # the deck's worst finish and call it a win.
    rows = (
        session.query(
            GameSeat.deck_id,
            Game.session_id,
            func.min(GameSeat.placement).label("best"),
        )
        .join(Game, Game.id == GameSeat.game_id)
        .filter(
            GameSeat.deck_id.in_(deck_ids),
            Game.status == FINALIZED,
            Game.session_id.isnot(None),
        )
        .group_by(GameSeat.deck_id, Game.session_id)
        .all()
    )
    stats: dict[int, dict] = {}
    for deck_id, _sess_id, best in rows:
        s = stats.setdefault(deck_id, {"sessions_played": 0, "sessions_won": 0})
        s["sessions_played"] += 1
        if best == 1:
            s["sessions_won"] += 1
    for s in stats.values():
        s["session_win_rate"] = (
            round(s["sessions_won"] / s["sessions_played"] * 100) if s["sessions_played"] else 0
        )
    return stats


def session_date_key(moment: datetime) -> date:
    """The calendar date a session is filed under — backfill only.

    Deliberately NOT used at runtime. New games join their playgroup's OPEN
    session, which is a social boundary; this exists solely so historical games,
    recorded before sessions existed, can be grouped at all.
    """
    return moment.date()
