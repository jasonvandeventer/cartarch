"""Playgroup service layer (v3.29.0).

Per-domain service file following the established convention
(``game_service.py``, ``watchlist_service.py``, ``location_service.py``).

**Authority rule.** ``Playgroup.created_by`` is immutable audit; the
live authority is always ``PlaygroupMember.role == 'owner'`` looked up
via :func:`require_membership`. After an ownership transfer the two
diverge. Every permission check reads ``role``, never ``created_by``.

**The shared primitive** is :func:`co_members_of` (decision E2 in the
recon). It performs a single indexed query: "users sharing >= 1
playgroup with me, plus optionally me". v3.29.1 collection sharing
and v3.29.2 pairwise trading consume this directly, scoped to
playgroup membership. They MUST NOT fall back to "everyone" — only
the game-creation people-picker carries that single-tenant transition
fallback, via :func:`get_pickable_users`.

**SQLite-until-v4 posture.** Service-layer canonical enum for
``role`` (no DB CHECK); naive-UTC datetimes per project convention.
"""

from __future__ import annotations

import json
import secrets
from collections import defaultdict

from openskill.models import PlackettLuce
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Deck,
    Game,
    GameEvent,
    GameSeat,
    GameVariant,
    Playgroup,
    PlaygroupMember,
    Share,
    Trade,
    User,
    WishlistShare,
)

# Label for the single row that aggregates every unattributed seat (#152).
GUESTS_LABEL = "Guests"

# Service-layer canonical role enum (the v3.27.2 / v3.27.3 pattern, no
# DB CHECK constraint). v3.29.0 ships two roles; the enum can widen
# additively later (e.g. ``admin``) with no schema change.
CANONICAL_PLAYGROUP_ROLES: tuple[str, ...] = ("owner", "member")
DEFAULT_PLAYGROUP_ROLE = "member"


def normalize_role(raw: str | None, default: str = DEFAULT_PLAYGROUP_ROLE) -> str:
    """Normalize a role string against the canonical set.

    Empty/whitespace/None → ``default``. Unknown values also resolve
    to ``default`` (non-blocking; mirrors the v3.27.2
    ``normalize_game_format`` posture — bad input never raises).
    """
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in CANONICAL_PLAYGROUP_ROLES:
        return value
    return default


# ── Join codes ───────────────────────────────────────────────────


def _generate_join_code(session: Session) -> str:
    """Generate a unique opaque join code.

    8 bytes of CSPRNG via ``secrets.token_urlsafe(8)`` (~11 chars
    base64). The same primitive v3.27.0 ``Game.client_token`` uses.
    Retry on the rare collision against an existing ``join_code``.
    """
    for _ in range(8):
        code = secrets.token_urlsafe(8)
        exists = session.query(Playgroup.id).filter(Playgroup.join_code == code).first()
        if exists is None:
            return code
    # Astronomically unlikely; defensive fallback widens entropy.
    return secrets.token_urlsafe(16)


# ── Membership lookups ──────────────────────────────────────────


def require_membership(
    session: Session,
    user_id: int,
    playgroup_id: int,
    min_role: str = "member",
) -> PlaygroupMember | None:
    """Return the user's membership row, or ``None`` if not satisfied.

    ``min_role="member"`` accepts owner OR member; ``min_role="owner"``
    accepts owner only. Returns ``None`` if the user has no membership
    row in the playgroup, or has one with insufficient role. Route
    handlers translate ``None`` into a 403 / redirect.
    """
    member = (
        session.query(PlaygroupMember)
        .filter(
            PlaygroupMember.user_id == user_id,
            PlaygroupMember.playgroup_id == playgroup_id,
        )
        .first()
    )
    if member is None:
        return None
    if min_role == "owner" and member.role != "owner":
        return None
    return member


def find_playgroup_by_code(session: Session, code: str | None) -> Playgroup | None:
    """Resolve a join code to its playgroup, or None.

    Whitespace-trimmed; empty string / None / unmatched / disabled
    (NULL ``join_code``) all return None. Disabled codes are
    explicitly excluded — there is no row whose ``join_code`` is
    NULL that this lookup can match, because the SQL filter
    ``join_code == code`` is itself NULL-rejecting.
    """
    if code is None:
        return None
    trimmed = code.strip()
    if not trimmed:
        return None
    return session.query(Playgroup).filter(Playgroup.join_code == trimmed).first()


# ── The shared primitive (E2) ──────────────────────────────────


def co_members_of(
    session: Session,
    user_id: int,
    include_self: bool = True,
) -> list[User]:
    """Users sharing >= 1 playgroup with ``user_id``. Pure primitive.

    Returns ACTIVE users (``User.is_active = True``), deduplicated,
    ordered by ``display_name, username`` to match the v3.27.5
    picker's existing ordering.

    No fallback to "all active users". This is the primitive that
    v3.29.1 collection sharing and v3.29.2 pairwise trading
    consume — they MUST scope to actual playgroup co-members; a
    fallback would silently widen their visibility scope. The
    transition fallback for the game-creation picker lives in
    :func:`get_pickable_users`, NOT here.

    The query is a single indexed subselect: the user's own
    playgroup ids, then every membership row in those playgroups
    joined to the user. ``ix_playgroup_members_user_id`` covers
    the subselect; ``ix_playgroup_members_playgroup_id`` covers
    the outer JOIN.
    """
    pg_subq = (
        select(PlaygroupMember.playgroup_id)
        .where(PlaygroupMember.user_id == user_id)
        .scalar_subquery()
    )
    # Membership as an EXISTS, NOT a join + DISTINCT.
    #
    # `ORDER BY <expression>` on a SELECT DISTINCT is invalid on Postgres —
    # "ORDER BY expressions must appear in select list" — and ordering by
    # `player_label_expr()` (a coalesce) is exactly that. SQLite accepts it, so
    # the SQLite suite stays green while every people-picker 500s. This is the
    # SECOND time in two days: v4.13.28 fixed the same shape in the audit card
    # search. The join here only ever proved membership and its duplicate rows
    # were the only reason for the DISTINCT, so EXISTS removes both at once.
    member_of_a_shared_playgroup = (
        select(PlaygroupMember.id)
        .where(
            PlaygroupMember.user_id == User.id,
            PlaygroupMember.playgroup_id.in_(pg_subq),
        )
        .exists()
    )
    stmt = (
        select(User)
        .where(User.is_active.is_(True), member_of_a_shared_playgroup)
        .order_by(User.player_label_expr())
    )
    if not include_self:
        stmt = stmt.where(User.id != user_id)
    return list(session.scalars(stmt).all())


def get_pickable_users(session: Session, current_user_id: int) -> list[User]:
    """People-picker scope. C2 transition fallback included.

    Compute ``co_members_of(current_user_id, include_self=True)``.
    If the result contains only the current user (or is empty),
    return the global active-user list — the C2 transition fallback,
    generalized so a solo playgroup does not strand the picker.
    Otherwise return the co-member list.

    Only the game-creation picker uses this; v3.29.1 sharing and
    v3.29.2 trading consume :func:`co_members_of` directly.
    """
    scoped = co_members_of(session, current_user_id, include_self=True)
    # Fallback when the scoped list provides nothing beyond the user
    # themselves — preserves pre-v3.29.0 picker behavior for users who
    # haven't joined any playgroup yet, and for users alone in a solo
    # playgroup (the generalized C2 predicate).
    if len(scoped) <= 1:
        return list(
            session.query(User)
            # #172 — the fallback is "everyone", which now includes accounts minted
            # by guests claiming a seat. A host with no playgroup would otherwise
            # watch this picker fill with strangers from other people's tables. A
            # guest who is deliberately added to a playgroup still shows, because
            # `scoped` (co_members_of) is not filtered — only the blunt fallback is.
            .filter(User.is_active.is_(True), ~User.is_guest)
            .order_by(User.player_label_expr())
            .all()
        )
    return scoped


# ── Playgroup queries for UI ────────────────────────────────────


def list_playgroups_for_user(session: Session, user_id: int) -> list[dict]:
    """The user's playgroups for the /playgroups index page.

    Returns ``[{"playgroup": Playgroup, "role": str, "member_count":
    int}]`` ordered by playgroup name. Empty list when the user is
    in no playgroups — the template renders the empty-state panel.

    Two queries: one for the user's memberships joined to their
    playgroups; one for the per-playgroup member counts grouped by
    playgroup_id. Folded together in Python. Cheap; the user is
    typically in <10 playgroups.
    """
    rows = (
        session.query(PlaygroupMember, Playgroup)
        .join(Playgroup, PlaygroupMember.playgroup_id == Playgroup.id)
        .filter(PlaygroupMember.user_id == user_id)
        .order_by(Playgroup.name)
        .all()
    )
    if not rows:
        return []
    pg_ids = [pg.id for _, pg in rows]
    counts = dict(
        session.query(PlaygroupMember.playgroup_id, func.count(PlaygroupMember.id))
        .filter(PlaygroupMember.playgroup_id.in_(pg_ids))
        .group_by(PlaygroupMember.playgroup_id)
        .all()
    )
    return [
        {
            "playgroup": pg,
            "role": pm.role,
            "member_count": counts.get(pg.id, 0),
        }
        for pm, pg in rows
    ]


def get_playgroup_detail(session: Session, playgroup_id: int, viewer_user_id: int) -> dict | None:
    """Playgroup + members for the detail page, or None if viewer is not a member.

    Returns ``{"playgroup": Playgroup, "viewer_role": str, "members":
    [{"member": PlaygroupMember, "user": User}, ...]}``. The members
    list is ordered by role (owner first), then ``joined_at``
    (longest-tenured first) — matches the implicit "owner +
    longest-tenured" surfacing the management UI cares about.

    Non-members get ``None`` (route handler renders a redirect to
    ``/playgroups`` with an error, NOT a 403 — keeps the existence of
    a playgroup with the given id non-leaky).
    """
    viewer_membership = require_membership(session, viewer_user_id, playgroup_id, min_role="member")
    if viewer_membership is None:
        return None
    playgroup = session.query(Playgroup).filter(Playgroup.id == playgroup_id).first()
    if playgroup is None:
        return None
    members = (
        session.query(PlaygroupMember, User)
        .join(User, PlaygroupMember.user_id == User.id)
        .filter(PlaygroupMember.playgroup_id == playgroup_id)
        # Owner first (alphabetical 'owner' < 'member' in ASCII; reverse-sort
        # to put owner ahead of member explicitly), then longest-tenured.
        .order_by(
            (PlaygroupMember.role == "owner").desc(),
            PlaygroupMember.joined_at.asc(),
        )
        .all()
    )
    return {
        "playgroup": playgroup,
        "viewer_role": viewer_membership.role,
        "members": [{"member": pm, "user": u} for pm, u in members],
        "record": playgroup_record(session, playgroup_id),
    }


def playgroup_record(session: Session, playgroup_id: int) -> list[dict]:
    """Win-loss record per PLAYER over the games linked to this playgroup (#152).

    Returns ``[{"user_id", "label", "played", "wins", "losses", "win_rate"}, ...]``
    sorted by wins descending, then win rate — highest first.

    Three rules, decided in #152 and not to be re-litigated:

    * **Group by ``GameSeat.user_id``, never ``player_name``.** ``player_name`` is a
      per-seat free-text snapshot and the live data really does split one person across
      several spellings (``Alex`` / ``SaintWacko`` is one account). Name grouping
      produces a wrong leaderboard on first render.
    * **Every null-``user_id`` seat collapses into ONE ``Guests`` row**, not one row per
      placeholder name. (If distinct named guests ever appear it stops being a
      per-player row — noted in #152, not pre-solved here.)
    * **``placement == 1`` is a win for EVERY seat holding it.** #114 permits duplicate
      placements for simultaneous eliminations, so a tie for first wins for all tied
      seats; any other placement is a loss, including a tie below first.

    Scope is games carrying this ``playgroup_id`` — deliberately NOT every game among
    the members, or an unlinked private game would surface on a shared page.

    Only seats with a **recorded placement** count. A linked game still in progress has
    no result yet, and counting it would break the reconciliation this record promises
    (``wins + losses == played``) by adding a game that is neither.
    """
    rows = (
        session.query(
            GameSeat.user_id,
            User.player_label_expr().label("label"),
            func.count().label("played"),
            func.sum(case((GameSeat.placement == 1, 1), else_=0)).label("wins"),
        )
        .join(Game, Game.id == GameSeat.game_id)
        .outerjoin(User, User.id == GameSeat.user_id)
        .filter(Game.playgroup_id == playgroup_id, GameSeat.placement.isnot(None))
        .group_by(GameSeat.user_id, User.real_name, User.display_name, User.username)
        .all()
    )

    record = []
    for user_id, label, played, wins in rows:
        played, wins = int(played), int(wins or 0)
        record.append(
            {
                "user_id": user_id,
                # Same name convention as the Members table on this page — the one
                # `player_label` definition. A guest has no account, so it can only
                # ever be the GUESTS_LABEL.
                "label": label if user_id is not None else GUESTS_LABEL,
                "played": played,
                "wins": wins,
                "losses": played - wins,
                # `played` is >= 1 for any row GROUP BY produced, so this cannot divide
                # by zero; the guard is for callers that build a row by hand.
                "win_rate": (wins / played) if played else 0.0,
            }
        )
    # Wins first, then rate. `played` and label break remaining ties so the order is
    # stable across requests rather than dependent on row order.
    record.sort(key=lambda r: (-r["wins"], -r["win_rate"], -r["played"], r["label"].lower()))
    return record


def _rating_placements(game: Game) -> tuple[dict[int, int], bool]:
    """Resolve same-turn ties for ratings only; explicit result edits take precedence."""
    ranks = {seat.id: seat.placement for seat in game.seats}
    from app.game_analytics_service import _final_elimination_events

    elimination_events = []
    edited_seats = set()
    for event in sorted(game.events, key=lambda e: (e.created_at, e.id)):
        try:
            payload = json.loads(event.payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if event.action_type == "eliminate":
            elimination_events.append(event)
        elif event.action_type == "result_edit":
            # A person's explicit tie must survive the inferred timestamp ordering.
            edited_seats.update(str(sid) for sid in (payload.get("placements") or {}))
    eliminated_at = {
        sid: event.created_at for sid, event in _final_elimination_events(elimination_events)
    }
    tied = defaultdict(list)
    for sid, rank in ranks.items():
        tied[rank].append(sid)
    adjusted = False
    for rank, seat_ids in tied.items():
        if rank == 1 or len(seat_ids) < 2:
            continue
        if any(str(sid) not in eliminated_at or str(sid) in edited_seats for sid in seat_ids):
            continue
        bands = []
        for sid in sorted(seat_ids, key=lambda sid: (eliminated_at[str(sid)], sid)):
            if (
                not bands
                or (eliminated_at[str(sid)] - eliminated_at[str(bands[-1][-1])]).total_seconds()
                > 60
            ):
                bands.append([])
            bands[-1].append(sid)
        if len(bands) > 1:
            adjusted = True
            for band in reversed(bands):
                for sid in band:
                    ranks[sid] = rank
                rank += len(band)
    return ranks, adjusted


def playgroup_ratings(
    session: Session,
    playgroup_id: int,
    viewer_user_id: int,
    player_ids: list[int] | None = None,
    game_format: str = "all",
    rank_by: str = "players",
) -> dict | None:
    """Replay shared games with >=2 selected members; retain every opponent in each pod."""
    from app.game_service import CANONICAL_GAME_FORMATS

    detail = get_playgroup_detail(session, playgroup_id, viewer_user_id)
    if detail is None:
        return None
    members = {row["user"].id: row["user"] for row in detail["members"]}
    selected = set(members if player_ids is None else player_ids)
    result = {
        **detail,
        "selected_players": selected & members.keys(),
        "game_format": game_format,
        "rank_by": rank_by,
        "format_choices": CANONICAL_GAME_FORMATS,
        "ratings": [],
        "rated_games": 0,
        "adjusted_games": 0,
        "skipped_games": 0,
        "planechase_games": 0,
        "unidentified_deck_seats": 0,
        "unattributed_games": 0,
        "input_error": None,
    }
    if len(selected) < 2 or not selected <= members.keys():
        result["input_error"] = "Select at least two current playgroup members."
        return result
    if game_format != "all" and game_format not in CANONICAL_GAME_FORMATS:
        result["input_error"] = "Select a listed game format."
        return result
    if rank_by not in {"players", "decks"}:
        result["input_error"] = "Choose player or deck ratings."
        return result
    qualifying = (
        select(GameSeat.game_id)
        .where(GameSeat.user_id.in_(selected))
        .group_by(GameSeat.game_id)
        .having(func.count(func.distinct(GameSeat.user_id)) >= 2)
    )
    query = (
        session.query(Game)
        .filter(
            Game.playgroup_id == playgroup_id,
            Game.status == "finalized",
            Game.id.in_(qualifying),
        )
        .options(
            selectinload(Game.seats),
            selectinload(Game.events.and_(GameEvent.action_type.in_(["eliminate", "result_edit"]))),
        )
        .order_by(Game.played_at, Game.id)
    )
    if game_format != "all":
        query = query.filter(Game.format == game_format)
    games = query.all()
    deck_owners = {}
    if rank_by == "decks":
        deck_ids = {seat.deck_id for game in games for seat in game.seats if seat.deck_id}
        deck_owners = {
            did: (uid, label)
            for did, uid, label in session.query(Deck.id, Deck.user_id, User.player_label_expr())
            .join(User, User.id == Deck.user_id)
            .filter(Deck.id.in_(deck_ids))
        }
    planechase_ids = set(
        session.scalars(
            select(GameVariant.game_id).where(
                GameVariant.game_id.in_([game.id for game in games]),
                GameVariant.variant == "planechase",
            )
        )
    )
    # Explicit parameters + pinned version keep historical replays reproducible.
    model = PlackettLuce(mu=25, sigma=25 / 3, beta=25 / 6, tau=25 / 300)
    ratings = {}
    played = defaultdict(int)
    wins = defaultdict(int)
    borrowed = defaultdict(int)
    deck_labels = {}
    for game in games:
        keys = [
            ("user", seat.user_id)
            if seat.user_id is not None
            else ("guest", game.played_at.date().isoformat(), seat.player_name)
            for seat in game.seats
        ]
        if rank_by == "decks":
            keys = [
                ("deck", seat.deck_id) if seat.deck_id else ("unknown", seat.id)
                for seat in game.seats
            ]
        # Partial results or duplicate identities are not a complete ranked contest.
        if len(set(keys)) != len(keys) or any(
            seat.placement is None or seat.placement < 1 for seat in game.seats
        ):
            result["skipped_games"] += 1
            continue
        if rank_by == "decks":
            result["unidentified_deck_seats"] += sum(not seat.deck_id for seat in game.seats)
            if not any(seat.deck_id for seat in game.seats):
                result["unattributed_games"] += 1
                continue
        ranks, adjusted = _rating_placements(game)
        before = [[ratings.get(key, model.rating())] for key in keys]
        after = model.rate(before, ranks=[ranks[seat.id] for seat in game.seats])
        for key, seat, (rating,) in zip(keys, game.seats, after, strict=True):
            # Unknown decks are fresh opponents each game, never an invented deck lineage.
            if key[0] == "unknown":
                continue
            ratings[key] = rating
            played[key] += 1
            wins[key] += ranks[seat.id] == 1
            if rank_by == "decks":
                borrowed[key] += seat.user_id != deck_owners[seat.deck_id][0]
                # Only expose names recorded in these games, not a private deck's current metadata.
                if seat.deck_name_at_game:
                    deck_labels[seat.deck_id] = seat.deck_name_at_game
        result["rated_games"] += 1
        result["adjusted_games"] += adjusted
        result["planechase_games"] += game.id in planechase_ids
    display_keys = [("user", uid) for uid in selected] if rank_by == "players" else list(ratings)
    for key in display_keys:
        identity = key[1]
        rating = ratings.get(key)
        result["ratings"].append(
            {
                "user_id": identity if rank_by == "players" else None,
                "deck_id": identity if rank_by == "decks" else None,
                "label": members[identity].player_label
                if rank_by == "players"
                else deck_labels.get(identity, f"Deck #{identity}"),
                "owner": deck_owners[identity][1] if rank_by == "decks" else None,
                "borrowed_games": borrowed[key],
                "rating": rating.mu * 60 if rating else None,
                "uncertainty": rating.sigma * 60 if rating else None,
                "played": played[key],
                "wins": wins[key],
            }
        )
    result["ratings"].sort(
        key=lambda row: (
            row["rating"] is None,
            -(row["rating"] or 0),
            row["label"].casefold(),
            row["user_id"] or row["deck_id"],
        )
    )
    return result


# ── Mutations ───────────────────────────────────────────────────


def create_playgroup(
    session: Session,
    creator_user_id: int,
    name: str,
    notes: str | None = None,
) -> Playgroup:
    """Create a playgroup AND the creator's owner-membership row, atomically.

    A playgroup never exists without its owner-membership row — the
    single transaction enforces this. A fresh ``join_code`` is
    generated at creation; the owner can later regenerate or disable.

    ``name`` is required (trimmed; empty raises ``ValueError`` which
    the v3.4.6 handler renders as a clean 400). ``notes`` is
    optional free-text.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Playgroup name is required.")
    notes_trimmed = notes.strip() if notes else None
    playgroup = Playgroup(
        name=name,
        created_by=creator_user_id,
        notes=notes_trimmed or None,
        join_code=_generate_join_code(session),
    )
    session.add(playgroup)
    session.flush()  # need playgroup.id for the membership row
    session.add(
        PlaygroupMember(
            playgroup_id=playgroup.id,
            user_id=creator_user_id,
            role="owner",
        )
    )
    session.commit()
    session.refresh(playgroup)
    return playgroup


def join_by_code(session: Session, user_id: int, code: str | None) -> Playgroup | None:
    """Resolve the code and add ``user_id`` as a member. Idempotent.

    Returns the joined ``Playgroup`` on success; ``None`` when the
    code doesn't match a playgroup or matches one that's disabled.
    Already a member → no-op, returns the playgroup (the user
    re-following a join link does not error or duplicate).

    The race where two requests insert the same membership row
    concurrently is caught by the ``uq_playgroup_members_pg_user``
    unique index — the IntegrityError is swallowed and the existing
    row is returned.
    """
    pg = find_playgroup_by_code(session, code)
    if pg is None:
        return None
    existing = require_membership(session, user_id, pg.id, min_role="member")
    if existing is not None:
        return pg
    try:
        session.add(PlaygroupMember(playgroup_id=pg.id, user_id=user_id, role="member"))
        session.commit()
    except IntegrityError:
        # Concurrent insert won the race; we're a member already.
        session.rollback()
    return pg


def leave_playgroup(session: Session, user_id: int, playgroup_id: int) -> tuple[bool, str | None]:
    """Remove user's membership; sole-owner blocked; zero-member auto-delete.

    Returns ``(success, error_message)``. ``error_message`` is None on
    success; populated when the sole owner attempts to leave (must
    transfer or delete first — decision D1) or when the user isn't a
    member.

    If removal drops the playgroup to zero members, hard-deletes the
    playgroup (D1).
    """
    membership = require_membership(session, user_id, playgroup_id, min_role="member")
    if membership is None:
        return False, "You are not a member of that playgroup."

    if membership.role == "owner":
        other_count = (
            session.query(func.count(PlaygroupMember.id))
            .filter(
                PlaygroupMember.playgroup_id == playgroup_id,
                PlaygroupMember.user_id != user_id,
            )
            .scalar()
            or 0
        )
        if other_count > 0:
            return (
                False,
                "You are the sole owner — transfer ownership or "
                "delete the playgroup before leaving.",
            )

    # v3.29.1 — cleanup the departing user's shares targeting this
    # playgroup. The sharer is leaving the audience; hard-delete the
    # Share row (decision B2 — no soft-revoke). Direct DELETE rather
    # than importing ``share_service`` to avoid the circular import the
    # spec called out (§9). The Showcase itself is untouched.
    session.query(Share).filter(
        Share.user_id == user_id,
        Share.playgroup_id == playgroup_id,
    ).delete(synchronize_session=False)
    # #146 — same for the leaving user's wishlist share to this playgroup.
    session.query(WishlistShare).filter(
        WishlistShare.user_id == user_id,
        WishlistShare.playgroup_id == playgroup_id,
    ).delete(synchronize_session=False)
    # v3.29.2 — auto-abandon pending trades involving the leaving user
    # scoped to this playgroup (§10). The user is leaving the audience;
    # any in-flight proposal where they are a party in this playgroup
    # closes with ``status='abandoned'``. Terminal trades are
    # untouched — they're the historical record.
    from app import trade_service

    trade_service.abandon_pending_trades_for_member_in_playgroup(session, user_id, playgroup_id)
    session.delete(membership)
    session.flush()
    # If the playgroup is now empty, hard-delete it (D1 auto-delete).
    remaining = (
        session.query(func.count(PlaygroupMember.id))
        .filter(PlaygroupMember.playgroup_id == playgroup_id)
        .scalar()
        or 0
    )
    if remaining == 0:
        # v3.29.1 — playgroup gone, drop every share targeting it
        # (the case where leave_playgroup auto-deletes the playgroup).
        session.query(Share).filter(Share.playgroup_id == playgroup_id).delete(
            synchronize_session=False
        )
        session.query(WishlistShare).filter(WishlistShare.playgroup_id == playgroup_id).delete(
            synchronize_session=False
        )  # #146
        # v3.29.2 — auto-abandon pending trades scoped to this playgroup
        # (any party — the playgroup itself is going away). Terminal
        # trades keep their snapshots but lose the live playgroup_id.
        trade_service.abandon_pending_trades_for_playgroup(session, playgroup_id)
        session.query(Trade).filter(Trade.playgroup_id == playgroup_id).update(
            {Trade.playgroup_id: None},
            synchronize_session=False,
        )
        pg = session.query(Playgroup).filter(Playgroup.id == playgroup_id).first()
        if pg is not None:
            session.delete(pg)
    session.commit()
    return True, None


def remove_member(
    session: Session,
    actor_user_id: int,
    playgroup_id: int,
    target_user_id: int,
) -> tuple[bool, str | None]:
    """Owner removes a member. Cannot remove the owner via this path.

    The owner's own membership is removed via ``leave_playgroup`` (with
    sole-owner guard) or ``transfer_ownership`` first.
    """
    actor = require_membership(session, actor_user_id, playgroup_id, min_role="owner")
    if actor is None:
        return False, "Only the owner can remove members."
    target = require_membership(session, target_user_id, playgroup_id, min_role="member")
    if target is None:
        return False, "That user is not a member."
    if target.role == "owner":
        return False, "Use Transfer Ownership or Leave to change the owner."
    # v3.29.1 — cleanup the removed member's shares targeting this
    # playgroup. They are no longer in the audience; hard-delete the
    # Share rows (decision B2). Showcase itself untouched.
    session.query(Share).filter(
        Share.user_id == target_user_id,
        Share.playgroup_id == playgroup_id,
    ).delete(synchronize_session=False)
    # #146 — same for the removed member's wishlist share to this playgroup.
    session.query(WishlistShare).filter(
        WishlistShare.user_id == target_user_id,
        WishlistShare.playgroup_id == playgroup_id,
    ).delete(synchronize_session=False)
    # v3.29.2 — auto-abandon pending trades involving the removed
    # member scoped to this playgroup (§10). They've been kicked from
    # the audience; their in-flight proposals close as abandoned.
    from app import trade_service

    trade_service.abandon_pending_trades_for_member_in_playgroup(
        session, target_user_id, playgroup_id
    )
    session.delete(target)
    session.commit()
    return True, None


def transfer_ownership(
    session: Session,
    actor_user_id: int,
    playgroup_id: int,
    new_owner_user_id: int,
) -> tuple[bool, str | None]:
    """Demote actor to member; promote target to owner. One transaction.

    Owner-gated. Target must already be a member of the playgroup.
    Transferring to yourself is a no-op (returns success).
    """
    actor = require_membership(session, actor_user_id, playgroup_id, min_role="owner")
    if actor is None:
        return False, "Only the owner can transfer ownership."
    if actor.user_id == new_owner_user_id:
        return True, None
    target = require_membership(session, new_owner_user_id, playgroup_id, min_role="member")
    if target is None:
        return False, "Target must be a member of the playgroup."
    actor.role = "member"
    target.role = "owner"
    session.commit()
    return True, None


def rename_playgroup(
    session: Session,
    actor_user_id: int,
    playgroup_id: int,
    new_name: str,
) -> tuple[bool, str | None]:
    """Owner renames the playgroup."""
    actor = require_membership(session, actor_user_id, playgroup_id, min_role="owner")
    if actor is None:
        return False, "Only the owner can rename the playgroup."
    name = (new_name or "").strip()
    if not name:
        return False, "Name is required."
    pg = session.query(Playgroup).filter(Playgroup.id == playgroup_id).first()
    if pg is None:
        return False, "Playgroup not found."
    pg.name = name
    session.commit()
    return True, None


def update_notes(
    session: Session,
    actor_user_id: int,
    playgroup_id: int,
    notes: str | None,
) -> tuple[bool, str | None]:
    """Owner updates the playgroup notes (or clears them with empty/None)."""
    actor = require_membership(session, actor_user_id, playgroup_id, min_role="owner")
    if actor is None:
        return False, "Only the owner can edit notes."
    pg = session.query(Playgroup).filter(Playgroup.id == playgroup_id).first()
    if pg is None:
        return False, "Playgroup not found."
    trimmed = notes.strip() if notes else None
    pg.notes = trimmed or None
    session.commit()
    return True, None


def regenerate_join_code(
    session: Session,
    actor_user_id: int,
    playgroup_id: int,
) -> tuple[bool, str | None]:
    """Owner generates a fresh join code (invalidates the previous one)."""
    actor = require_membership(session, actor_user_id, playgroup_id, min_role="owner")
    if actor is None:
        return False, "Only the owner can regenerate the join code."
    pg = session.query(Playgroup).filter(Playgroup.id == playgroup_id).first()
    if pg is None:
        return False, "Playgroup not found."
    pg.join_code = _generate_join_code(session)
    session.commit()
    return True, None


def set_join_code_enabled(
    session: Session,
    actor_user_id: int,
    playgroup_id: int,
    enabled: bool,
) -> tuple[bool, str | None]:
    """Owner enables (generates a fresh code) or disables (NULLs the code)."""
    actor = require_membership(session, actor_user_id, playgroup_id, min_role="owner")
    if actor is None:
        return False, "Only the owner can toggle the join code."
    pg = session.query(Playgroup).filter(Playgroup.id == playgroup_id).first()
    if pg is None:
        return False, "Playgroup not found."
    if enabled:
        if pg.join_code is None:
            pg.join_code = _generate_join_code(session)
    else:
        pg.join_code = None
    session.commit()
    return True, None


def delete_playgroup(
    session: Session,
    actor_user_id: int,
    playgroup_id: int,
) -> tuple[bool, str | None]:
    """Owner hard-deletes the playgroup. Cascade clears member rows."""
    actor = require_membership(session, actor_user_id, playgroup_id, min_role="owner")
    if actor is None:
        return False, "Only the owner can delete the playgroup."
    pg = session.query(Playgroup).filter(Playgroup.id == playgroup_id).first()
    if pg is None:
        return False, "Playgroup not found."
    # v3.29.1 — every Share targeting this playgroup goes too. The
    # audience no longer exists; hard-delete (decision B2). Showcases
    # the shares pointed at are untouched — those belong to other
    # users (they may still be shared elsewhere via other Share rows).
    session.query(Share).filter(Share.playgroup_id == playgroup_id).delete(
        synchronize_session=False
    )
    session.query(WishlistShare).filter(WishlistShare.playgroup_id == playgroup_id).delete(
        synchronize_session=False
    )  # #146
    # v3.29.2 — auto-abandon pending trades scoped to this playgroup;
    # SET-NULL ``playgroup_id`` on terminal trades to preserve the
    # historical record (the *_name_at_trade snapshots survive). The
    # Trade row stays even after the playgroup is gone.
    from app import trade_service

    trade_service.abandon_pending_trades_for_playgroup(session, playgroup_id)
    session.query(Trade).filter(Trade.playgroup_id == playgroup_id).update(
        {Trade.playgroup_id: None},
        synchronize_session=False,
    )
    # v3.32.0 — unlink any game pointing at this playgroup (SQLite doesn't
    # enforce ON DELETE SET NULL). A dangling id is access-safe — the
    # membership visibility check finds nobody once the member rows are gone
    # — but clearing it keeps game-detail rendering correct (no orphan join).
    session.query(Game).filter(Game.playgroup_id == playgroup_id).update(
        {Game.playgroup_id: None},
        synchronize_session=False,
    )
    # #166 — and its sessions with it. A session BELONGS to a playgroup, so it
    # cannot outlive one. Done EXPLICITLY for the same reason the line above is:
    # SQLite runs with FKs OFF, so neither the `game_sessions.playgroup_id`
    # CASCADE nor the `games.session_id` SET NULL fires in production today —
    # the DB clauses are Postgres defence-in-depth, not the mechanism.
    #
    # Null the games FIRST: deleting the sessions while games still reference
    # them is exactly the autoflush-ordering trap #148 hit on Postgres, where a
    # pending parent delete flushed while a child still pointed at it.
    from app.models import GameSession

    doomed = [
        row.id
        for row in session.query(GameSession.id).filter(GameSession.playgroup_id == playgroup_id)
    ]
    if doomed:
        session.query(Game).filter(Game.session_id.in_(doomed)).update(
            {Game.session_id: None}, synchronize_session=False
        )
        session.query(GameSession).filter(GameSession.id.in_(doomed)).delete(
            synchronize_session=False
        )
    session.delete(pg)
    session.commit()
    return True, None


# ── Admin user-deletion integration ─────────────────────────────


def handle_user_deletion(session: Session, user_id: int) -> None:
    """Pre-clean playgroups before the admin user-deletion cascade.

    For each playgroup the user owns:
    - If other members remain: transfer ownership to the
      longest-tenured remaining member (earliest ``joined_at``).
    - If the user is the sole member: hard-delete the playgroup.

    After this returns, ``user_id`` owns no playgroup, and the
    cascade's plain ``PlaygroupMember.user_id == user_id`` DELETE is
    safe (it just removes the now-demoted membership rows).

    No commit here — caller (the admin cascade route) commits the
    enclosing transaction.
    """
    owned_pg_ids = [
        row.playgroup_id
        for row in session.query(PlaygroupMember.playgroup_id)
        .filter(
            PlaygroupMember.user_id == user_id,
            PlaygroupMember.role == "owner",
        )
        .all()
    ]
    for pg_id in owned_pg_ids:
        # Longest-tenured remaining member = earliest joined_at among
        # members other than the user being deleted.
        successor = (
            session.query(PlaygroupMember)
            .filter(
                PlaygroupMember.playgroup_id == pg_id,
                PlaygroupMember.user_id != user_id,
            )
            .order_by(PlaygroupMember.joined_at.asc())
            .first()
        )
        if successor is None:
            # Sole member; hard-delete the playgroup. Cascade clears
            # the about-to-be-deleted owner's membership row.
            # v3.29.1 — drop every Share targeting this playgroup
            # before deletion (the audience is going away). Shares
            # OWNED by the user being deleted are cleaned up
            # separately in the admin cascade in routes/admin.py.
            session.query(Share).filter(Share.playgroup_id == pg_id).delete(
                synchronize_session=False
            )
            session.query(WishlistShare).filter(WishlistShare.playgroup_id == pg_id).delete(
                synchronize_session=False
            )  # #146
            # v3.29.2 — auto-abandon pending trades scoped to this
            # playgroup; SET-NULL terminal trades' ``playgroup_id`` to
            # preserve the historical record. Mirrors the
            # ``delete_playgroup`` cleanup path so admin-cascade behaves
            # identically to a user-initiated playgroup delete.
            from app import trade_service

            trade_service.abandon_pending_trades_for_playgroup(session, pg_id)
            session.query(Trade).filter(Trade.playgroup_id == pg_id).update(
                {Trade.playgroup_id: None},
                synchronize_session=False,
            )
            # v3.32.0 — unlink games pointing at this playgroup (mirrors the
            # user-initiated delete_playgroup cleanup so admin-cascade behaves
            # identically).
            session.query(Game).filter(Game.playgroup_id == pg_id).update(
                {Game.playgroup_id: None},
                synchronize_session=False,
            )
            pg = session.query(Playgroup).filter(Playgroup.id == pg_id).first()
            if pg is not None:
                session.delete(pg)
        else:
            # Auto-transfer: demote the leaving user, promote the
            # longest-tenured remaining member.
            leaving = (
                session.query(PlaygroupMember)
                .filter(
                    PlaygroupMember.playgroup_id == pg_id,
                    PlaygroupMember.user_id == user_id,
                )
                .first()
            )
            if leaving is not None:
                leaving.role = "member"
            successor.role = "owner"
    session.flush()


__all__ = [
    "CANONICAL_PLAYGROUP_ROLES",
    "DEFAULT_PLAYGROUP_ROLE",
    "co_members_of",
    "create_playgroup",
    "delete_playgroup",
    "find_playgroup_by_code",
    "get_pickable_users",
    "get_playgroup_detail",
    "handle_user_deletion",
    "join_by_code",
    "leave_playgroup",
    "list_playgroups_for_user",
    "normalize_role",
    "regenerate_join_code",
    "remove_member",
    "rename_playgroup",
    "require_membership",
    "set_join_code_enabled",
    "transfer_ownership",
    "update_notes",
]
