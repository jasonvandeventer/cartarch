"""Pairwise trading service layer (v3.29.2).

Per-domain service file following the established convention
(``share_service.py``, ``playgroup_service.py``, ``watchlist_service.py``,
``deck_service.py``). Implements the recording-only pairwise-trading
model settled in the v3.29.2 spec's decision register.

**Recording-only (B1).** A Trade reaching ``accepted`` records that two
parties agreed; the app NEVER mutates InventoryRow. Inventory execution
is deferred (v4-gated; v4-schema-design input). Parties resolve the
physical exchange manually via the app's existing Move / Adjust-
quantity affordances; the v3.29.2 UI shows a "Trade agreed" panel on
``accepted`` pointing at those affordances (B2).

**State machine.** Lifecycle: one non-terminal (``proposed``) and four
terminal (``accepted``, ``declined``, ``cancelled``, ``abandoned``).
Transitions are gated by the actor:

  - ``proposed → accepted``: recipient only
  - ``proposed → declined``: recipient only
  - ``proposed → cancelled``: proposer only
  - ``proposed → abandoned``: system only (the §10 cleanup hooks)
  - terminal → anything: rejected

There is no ``completed`` (execution deferred per B1) and no
``countered`` (counter-offers deferred per A3 — re-negotiation is
decline-and-re-propose). The only path that mutates ``Trade.status``
from a user action is :func:`transition_trade`.

**Hybrid identity (A4).** ``TradeItem`` carries live FKs
(``inventory_row_id``, ``card_id``, optional ``showcase_item_id``)
plus five ``*_at_trade`` snapshot columns. Snapshots are written on
every terminal transition by :func:`write_trade_terminal_snapshot` so
the historical record survives later inventory edits, card-table
updates, or party account deletions. Live FKs stay populated after
terminal — they are nulled only when the underlying row is deleted
(§10 cleanup helpers).

**Validation contracts.**

  - At least one item per side at proposal time (decision A6 — no
    one-sided gifts).
  - Every ``side='offered'`` item references an InventoryRow owned by
    the proposer.
  - Every ``side='requested'`` item carries a ``showcase_item_id``
    from a Showcase the recipient has shared to the trade's playgroup
    (decision C2). The column is nullable at the DB layer so offered
    items can leave it NULL, but the app-layer check is hard.
  - Both parties must be members of the trade's playgroup at proposal
    time (decision D1). Proposer ≠ recipient.

**Circular-import discipline.** This module imports
:mod:`app.playgroup_service` LAZILY (function-level, inside
:func:`create_trade` only) to avoid a module-load cycle: the §10
cleanup hooks in ``playgroup_service`` import trade_service at module
level and call the cleanup helpers below. With trade_service importing
playgroup_service only at call time, no module-load cycle exists. The
inventory_service + admin.py + share_service §10 hooks similarly
import trade_service at function level for safety, but their module-
level imports of trade_service would also be fine — only the
trade_service → playgroup_service direction needs the lazy guard.

**Sanitized projection reuse (§8).** Trade-item rendering reuses the
v3.29.1 ``_ReadOnlyCardProjection`` from :mod:`app.share_service`. The
privacy hard-flag — no private InventoryRow field surfaces in
rendered trade-detail HTML — applies in both directions (each party
sees the other's items; both sides need the same discipline).

**SQLite-until-v4 posture.** Two additive tables; service-layer enums
for ``status`` and ``side`` (no DB CHECK); naive-UTC datetimes per
project convention.
"""

from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from app.decklist_service import _build_full_location_label
from app.models import (
    Card,
    InventoryRow,
    Playgroup,
    PlaygroupMember,
    Share,
    Showcase,
    ShowcaseItem,
    StorageLocation,
    Trade,
    TradeItem,
    TradeRevision,
    User,
)
from app.pricing import effective_price
from app.share_service import _ReadOnlyCardProjection
from app.timeutil import utc_now

# ── Canonical enums ─────────────────────────────────────────────

CANONICAL_TRADE_STATUSES: tuple[str, ...] = (
    "proposed",
    "accepted",
    "declined",
    "cancelled",
    "abandoned",
)
DEFAULT_TRADE_STATUS = "proposed"

CANONICAL_TRADE_ITEM_SIDES: tuple[str, ...] = ("offered", "requested")

TERMINAL_TRADE_STATUSES: frozenset[str] = frozenset(
    {"accepted", "declined", "cancelled", "abandoned"}
)


def normalize_status(raw: str | None, default: str = DEFAULT_TRADE_STATUS) -> str:
    """Normalize a status string against the canonical set.

    Empty / whitespace / None → ``default``; unknown values also
    resolve to ``default`` (non-blocking, mirrors v3.27.2 / v3.29.0
    normalize_* posture).
    """
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in CANONICAL_TRADE_STATUSES:
        return value
    return default


# ── Internals ───────────────────────────────────────────────────


def _user_display(user: User | None) -> str | None:
    """Project-wide convention for the user's display string."""
    if user is None:
        return None
    name = user.player_label
    return name or None


def current_revision(trade: Trade) -> TradeRevision | None:
    """THE definition of "which version of this trade is live".

    The LAST revision that has not been declined. A declined counter stays on
    disk (it is the record of what was rejected) but stops being current, which
    is how "decline it and the trade returns to its original state" is a lookup
    rather than a replay.
    """
    live = [r for r in trade.revisions if r.declined_at is None]
    return live[-1] if live else None


def _current_author_id(trade: Trade) -> int | None:
    """Who issued the version now on the table. Falls back to the proposer when
    the revision's author row is gone (account deletion NULLs it) — the trade
    still has to be answerable by somebody."""
    rev = current_revision(trade)
    if rev is None or rev.author_user_id is None:
        return trade.proposer_user_id
    return rev.author_user_id


def previous_revision(trade: Trade) -> TradeRevision | None:
    """The revision the trade would fall back to if the current one were
    declined — the one before it, skipping revisions already declined."""
    live = [r for r in trade.revisions if r.declined_at is None]
    return live[-2] if len(live) > 1 else None


def _items_by_side(
    trade: Trade, side: str, revision: TradeRevision | None = None
) -> list[TradeItem]:
    """Items on one side of ONE revision — by default the current one.

    Every render path goes through here, so scoping it is what keeps a superseded
    version out of the detail page, the totals and the terminal snapshot.
    """
    rev = revision if revision is not None else current_revision(trade)
    if rev is None:
        # A trade written before counter-proposals existed cannot occur (the
        # migration backfilled revision 1), but a half-built object in a test
        # can — fall back to every item rather than rendering nothing.
        return [item for item in trade.items if item.side == side]
    return [item for item in trade.items if item.side == side and item.revision_id == rev.id]


def _share_visible_to_recipient_playgroup(
    session: Session,
    showcase_item: ShowcaseItem,
    recipient_user_id: int,
    playgroup_id: int,
) -> bool:
    """Verify a ShowcaseItem belongs to a Showcase the recipient owns
    AND that Showcase is currently shared to the given playgroup.

    Decision C2 — requested items must come from the recipient's
    shared Showcase. The Showcase ownership check uses the v3.29.1
    invariant that ShowcaseItem.showcase_id resolves to a Showcase
    owned by recipient_user_id; the Share check confirms an active
    Share row for that Showcase targeting playgroup_id.
    """
    showcase = (
        session.query(Showcase)
        .filter(
            Showcase.id == showcase_item.showcase_id,
            Showcase.user_id == recipient_user_id,
        )
        .first()
    )
    if showcase is None:
        return False
    share = (
        session.query(Share)
        .filter(
            Share.showcase_id == showcase.id,
            Share.playgroup_id == playgroup_id,
            Share.user_id == recipient_user_id,
        )
        .first()
    )
    return share is not None


def write_trade_terminal_snapshot(session: Session, trade: Trade) -> None:
    """Copy live values into the *_at_trade snapshot fields.

    Called by :func:`transition_trade` on every terminal transition and
    by the §10 cleanup helpers' ``abandon_*`` paths. The snapshots are
    the durable historical record — after this runs, the trade-detail
    page renders from snapshots regardless of later card-table or
    inventory-row changes.

    No-op when the trade is still proposed (defensive — callers
    should only invoke this on terminal transitions).
    """
    # Identity snapshots.
    trade.proposer_name_at_trade = _user_display(trade.proposer)
    trade.recipient_name_at_trade = _user_display(trade.recipient)
    # Per-item snapshots. Use the live InventoryRow + Card if still
    # resolvable; fall back to existing values for already-snapshotted
    # fields (defensive — if the inventory was deleted before the
    # cleanup hook ran, we still want to capture whatever we can).
    # ONLY the current revision (counter-proposals). A countered trade holds
    # every version's items, and snapshotting all of them would freeze a record
    # of a trade nobody agreed to — two Sol Rings where one was traded.
    rev = current_revision(trade)
    snapshot_items = (
        [i for i in trade.items if i.revision_id == rev.id] if rev is not None else trade.items
    )
    for item in snapshot_items:
        inv = item.inventory_row
        card = item.card
        if inv is not None and card is None:
            card = inv.card
        # Card identity (read from Card if available, else preserve any
        # already-set snapshot).
        if card is not None:
            item.card_name_at_trade = card.name
            item.card_set_code_at_trade = card.set_code
            item.card_collector_number_at_trade = card.collector_number
        # Finish + quantity — prefer live values, fall back to current snapshot.
        item.finish_at_trade = item.finish or (
            inv.finish if inv is not None else item.finish_at_trade
        )
        item.quantity_at_trade = (
            item.quantity if item.quantity is not None else item.quantity_at_trade
        )


# ── Creation ────────────────────────────────────────────────────


def create_trade(
    session: Session,
    proposer_user_id: int,
    recipient_user_id: int,
    playgroup_id: int,
    offered: list[dict],
    requested: list[dict],
    proposer_note: str | None = None,
) -> Trade:
    """Create a new proposed Trade with all items in one transaction.

    ``offered`` / ``requested`` are lists of dicts shaped like:

        {"inventory_row_id": int,
         "card_id": int,            # optional; resolved if absent
         "showcase_item_id": int,   # required for requested side
         "finish": str,             # optional
         "quantity": int}           # default 1

    Validations (all raise ``ValueError`` — the v3.4.6 handler turns
    these into clean 400s):

      - proposer != recipient
      - both members of the playgroup
      - recipient has an active Share of their Showcase to that
        playgroup (the source of any requested item)
      - >= 1 item on each side (decision A6)
      - every offered item references an InventoryRow owned by proposer
      - every requested item carries a showcase_item_id from one of
        the recipient's Shares to that playgroup (decision C2)

    Returns the persisted ``Trade`` in ``proposed`` status.
    """
    _require_trade_context(session, proposer_user_id, recipient_user_id, playgroup_id)

    if not offered:
        raise ValueError("At least one offered item is required.")
    if not requested:
        raise ValueError("At least one requested item is required.")

    resolved_offered = _resolve_offered_items(session, proposer_user_id, playgroup_id, offered)
    resolved_requested = _resolve_requested_items(
        session, recipient_user_id, playgroup_id, requested
    )

    now = utc_now()
    note = (proposer_note or "").strip() or None
    trade = Trade(
        proposer_user_id=proposer_user_id,
        recipient_user_id=recipient_user_id,
        playgroup_id=playgroup_id,
        status="proposed",
        proposer_note=note,
        created_at=now,
        updated_at=now,
    )
    # Revision 1 — written for EVERY trade, so "the current items" needs no
    # null-revision branch anywhere downstream.
    proposer = session.query(User).filter(User.id == proposer_user_id).first()
    revision = TradeRevision(
        author_user_id=proposer_user_id,
        author_name_at_revision=proposer.player_label if proposer else None,
        created_at=now,
    )
    trade.revisions.append(revision)
    for item in resolved_offered + resolved_requested:
        item.revision = revision
        trade.items.append(item)
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def _require_trade_context(
    session: Session,
    proposer_user_id: int,
    recipient_user_id: int,
    playgroup_id: int,
) -> None:
    """The preconditions BOTH a proposal and a counter share: two different
    users, both members, and the recipient sharing a Showcase to the playgroup
    (without it there is nothing for the requested side to point at).

    Extracted rather than copied — a counter that validated its context
    differently from a proposal is exactly how a rule grows a second, wrong
    implementation."""
    # Lazy import to avoid module-load cycle (see module docstring).
    from app import playgroup_service

    if proposer_user_id == recipient_user_id:
        raise ValueError("Proposer and recipient must be different users.")

    if (
        playgroup_service.require_membership(
            session, proposer_user_id, playgroup_id, min_role="member"
        )
        is None
    ):
        raise ValueError("Proposer is not a member of that playgroup.")
    if (
        playgroup_service.require_membership(
            session, recipient_user_id, playgroup_id, min_role="member"
        )
        is None
    ):
        raise ValueError("Recipient is not a member of that playgroup.")

    recipient_share = (
        session.query(Share)
        .filter(Share.user_id == recipient_user_id, Share.playgroup_id == playgroup_id)
        .first()
    )
    if recipient_share is None:
        raise ValueError("Recipient has not shared a Showcase with that playgroup.")

    proposer = session.query(User).filter(User.id == proposer_user_id).first()
    recipient = session.query(User).filter(User.id == recipient_user_id).first()
    if proposer is None or recipient is None:
        raise ValueError("Proposer or recipient not found.")


def _resolve_offered_items(
    session: Session,
    proposer_user_id: int,
    playgroup_id: int,
    offered: list[dict],
    current_offered: dict[int, TradeItem] | None = None,
) -> list[TradeItem]:
    """The OFFERED side is always the proposer's cards, whoever is authoring.

    THREE ways to name the same card, because the two parties see the proposer's
    collection differently:

    * ``inventory_row_id`` — the proposer picking from their own inventory.
    * ``showcase_item_id`` — a countering RECIPIENT adding something the
      proposer has SHARED with this playgroup. Refused unless that Showcase
      really is shared here: a counter must never reach into inventory its
      author cannot see.
    * ``trade_item_id`` — a line ALREADY on the trade's current revision, which
      is how a countering recipient keeps an offered card they can't otherwise
      name. Without it, re-sending the offered side would silently drop every
      card the proposer offered but does not publicly share.
    """
    from app.inventory_service import is_brew_placeholder_row

    resolved: list[TradeItem] = []
    for raw in offered:
        inv = None
        inv_row_id = int(raw.get("inventory_row_id") or 0)
        showcase_item_id = int(raw.get("showcase_item_id") or 0)
        trade_item_id = int(raw.get("trade_item_id") or 0)
        if trade_item_id > 0 and current_offered is not None:
            existing = current_offered.get(trade_item_id)
            if existing is None:
                raise ValueError("Offered item is not part of this trade.")
            inv = existing.inventory_row
            if inv is None or inv.user_id != proposer_user_id:
                raise ValueError("An offered card is no longer available.")
        elif inv_row_id > 0:
            inv = (
                session.query(InventoryRow)
                .filter(
                    InventoryRow.id == inv_row_id,
                    InventoryRow.user_id == proposer_user_id,
                )
                .first()
            )
            if inv is None:
                raise ValueError("Offered item references a card you don't own.")
        elif showcase_item_id > 0:
            # A countering recipient picks the proposer's cards out of the
            # proposer's OWN shared Showcase — same C2 check as the requested
            # side, pointed the other way.
            showcase_item = (
                session.query(ShowcaseItem).filter(ShowcaseItem.id == showcase_item_id).first()
            )
            if showcase_item is None or not _share_visible_to_recipient_playgroup(
                session, showcase_item, proposer_user_id, playgroup_id
            ):
                raise ValueError(
                    "Offered item is not in their Showcase shared with this playgroup."
                )
            inv = showcase_item.inventory_row
            if inv is None or inv.user_id != proposer_user_id:
                raise ValueError("Offered item's source row is unavailable.")
        else:
            raise ValueError("Offered item missing inventory_row_id.")

        # #140 — a brew placeholder is an unowned proxy that exists only in its
        # deck; it can't be offered in a trade.
        if is_brew_placeholder_row(session, inv):
            raise ValueError("You can't offer a brew placeholder in a trade.")
        qty = max(1, int(raw.get("quantity") or 1))
        if qty > inv.quantity:
            raise ValueError(f"Offered quantity {qty} exceeds your held quantity ({inv.quantity}).")
        resolved.append(
            TradeItem(
                side="offered",
                inventory_row_id=inv.id,
                card_id=inv.card_id,
                showcase_item_id=None,
                finish=inv.finish,
                quantity=qty,
            )
        )
    return resolved


def _mirrored_member_row(
    session: Session, recipient_user_id: int, playgroup_id: int, row_id: int
) -> InventoryRow | None:
    """The recipient's InventoryRow ``row_id``, but ONLY if it is currently a
    member of a Showcase they share with this playgroup.

    Resolved through ``resolve_showcase_rows`` so membership has one definition
    — the same one the share page renders from. A row id that names something
    they own but do not share resolves to None, so a hand-crafted request cannot
    reach into unshared inventory.
    """
    from app.share_service import resolve_showcase_rows

    shares = (
        session.query(Share)
        .filter(Share.user_id == recipient_user_id, Share.playgroup_id == playgroup_id)
        .all()
    )
    for share in shares:
        for entry in resolve_showcase_rows(session, share.showcase_id):
            inv = entry["row"]
            if inv is not None and inv.id == row_id and inv.user_id == recipient_user_id:
                return inv
    return None


def _resolve_requested_items(
    session: Session,
    recipient_user_id: int,
    playgroup_id: int,
    requested: list[dict],
) -> list[TradeItem]:
    """The REQUESTED side is always the recipient's cards, and always comes from
    their Showcase shared with this playgroup (decision C2) — unchanged by
    counter-proposals, including when the recipient is the one countering and
    adding their own cards to it."""
    resolved: list[TradeItem] = []
    for raw in requested:
        showcase_item_id = int(raw.get("showcase_item_id") or 0)
        row_id = int(raw.get("inventory_row_id") or 0)
        if showcase_item_id <= 0 and row_id > 0:
            # A MIRRORED card: it is in the showcase because its location is
            # mirrored, so it has no ShowcaseItem to point at. C2 is unchanged in
            # substance — the card must be in the Showcase the recipient shares
            # with this playgroup — only the membership test moves to the ONE
            # resolver (#135) instead of the items table. `showcase_item_id`
            # stays NULL on the TradeItem, which is a shape it already has: the
            # link is navigation-only (C1) and §10 cleanup nulls it anyway.
            inv = _mirrored_member_row(session, recipient_user_id, playgroup_id, row_id)
            if inv is None:
                raise ValueError(
                    "Requested item is not in the recipient's Showcase shared with this playgroup."
                )
            qty = max(1, int(raw.get("quantity") or 1))
            if qty > inv.quantity:
                qty = inv.quantity
            if qty <= 0:
                raise ValueError("Requested item has no available quantity.")
            resolved.append(
                TradeItem(
                    side="requested",
                    inventory_row_id=inv.id,
                    card_id=inv.card_id,
                    showcase_item_id=None,
                    finish=inv.finish,
                    quantity=qty,
                )
            )
            continue
        if showcase_item_id <= 0:
            raise ValueError(
                "Requested items must be selected from the "
                "recipient's shared Showcase (decision C2)."
            )
        showcase_item = (
            session.query(ShowcaseItem).filter(ShowcaseItem.id == showcase_item_id).first()
        )
        if showcase_item is None:
            raise ValueError("Requested ShowcaseItem not found.")
        if not _share_visible_to_recipient_playgroup(
            session, showcase_item, recipient_user_id, playgroup_id
        ):
            raise ValueError(
                "Requested item is not in the recipient's Showcase shared with this playgroup."
            )
        inv = showcase_item.inventory_row
        if inv is None or inv.user_id != recipient_user_id:
            raise ValueError("Requested item's source row is unavailable.")
        qty = max(1, int(raw.get("quantity") or showcase_item.quantity_offered or 1))
        # Cap at what's actually available (the v3.29.1 displayed-available).
        available = min(showcase_item.quantity_offered, inv.quantity)
        if available <= 0:
            raise ValueError("Requested item has no available quantity.")
        if qty > available:
            qty = available
        resolved.append(
            TradeItem(
                side="requested",
                inventory_row_id=inv.id,
                card_id=inv.card_id,
                showcase_item_id=showcase_item.id,
                finish=inv.finish,
                quantity=qty,
            )
        )
    return resolved


# ── Counter-proposals ───────────────────────────────────────────


def counter_trade(
    session: Session,
    trade_id: int,
    author_user_id: int,
    offered: list[dict],
    requested: list[dict],
    note: str | None = None,
) -> Trade:
    """Issue a counter-proposal: a NEW revision of both sides, by either party.

    The trade keeps its id, its status and its place in both inboxes — only
    which revision is current changes. Nothing about the previous version is
    destroyed, which is what makes the diff and the fall-back cheap.

    Rules (owner decisions, 2026-08-21):

    * **Either party may counter, without limit.** A counter answers a counter;
      the revision list is the negotiation.
    * **A side may be EMPTY on a counter** — "actually, just take it" is a real
      trade — while an opening proposal still requires both (decision A6 holds
      where it was made, so the wishlist flow's no-one-sided-gift rule is
      untouched).
    * **Only a live trade can be countered.** A terminal one is the record.
    * **Countering something identical is refused** — an empty diff would put a
      "they countered" badge on an unchanged trade.

    Sides keep their meaning regardless of who authors: OFFERED is always the
    proposer's cards, REQUESTED always the recipient's. What changes is how the
    author names them — see ``_resolve_offered_items``.
    """
    trade = session.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise ValueError("Trade not found.")
    if author_user_id not in (trade.proposer_user_id, trade.recipient_user_id):
        raise ValueError("Only a party to the trade can counter it.")
    if trade.status != "proposed":
        raise ValueError(f"A {trade.status} trade can no longer be countered.")

    _require_trade_context(
        session, trade.proposer_user_id, trade.recipient_user_id, trade.playgroup_id
    )
    current_offered = {it.id: it for it in _items_by_side(trade, "offered")}
    resolved_offered = _resolve_offered_items(
        session, trade.proposer_user_id, trade.playgroup_id, offered, current_offered
    )
    resolved_requested = _resolve_requested_items(
        session, trade.recipient_user_id, trade.playgroup_id, requested
    )
    if not resolved_offered and not resolved_requested:
        raise ValueError("A counter-proposal needs at least one card on one side.")
    if not _differs_from_current(trade, resolved_offered + resolved_requested):
        raise ValueError("That counter-proposal is identical to the current trade.")

    author = session.query(User).filter(User.id == author_user_id).first()
    now = utc_now()
    revision = TradeRevision(
        author_user_id=author_user_id,
        author_name_at_revision=author.player_label if author else None,
        note=(note or "").strip() or None,
        created_at=now,
    )
    trade.revisions.append(revision)
    for item in resolved_offered + resolved_requested:
        item.revision = revision
        trade.items.append(item)
    trade.updated_at = now
    session.commit()
    session.refresh(trade)
    return trade


def _item_signature(items: list[TradeItem]) -> set[tuple]:
    """Comparable identity for a set of trade items — side, row, printing,
    finish and quantity. Used to refuse a counter that changes nothing, and to
    build the diff."""
    return {(it.side, it.inventory_row_id, it.card_id, it.finish, it.quantity) for it in items}


def _differs_from_current(trade: Trade, proposed: list[TradeItem]) -> bool:
    rev = current_revision(trade)
    if rev is None:
        return True
    live = [it for it in trade.items if it.revision_id == rev.id]
    return _item_signature(live) != _item_signature(proposed)


def decline_counter(session: Session, trade_id: int, actor_user_id: int) -> Trade:
    """Reject the current counter — the trade returns to its previous state.

    The declined revision is KEPT and marked, so the record still shows what was
    proposed and refused; the previous revision simply becomes current again.

    **Only the party who did NOT write it may decline it** (declining your own
    counter is a withdrawal, and would need to be called that), and revision 1
    can never be declined — there is nothing behind it, and rejecting the
    ORIGINAL proposal is what ``transition_trade(..., "declined")`` is for.
    """
    trade = session.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise ValueError("Trade not found.")
    if actor_user_id not in (trade.proposer_user_id, trade.recipient_user_id):
        raise ValueError("Only a party to the trade can decline a counter-proposal.")
    if trade.status != "proposed":
        raise ValueError(f"A {trade.status} trade can no longer be changed.")
    rev = current_revision(trade)
    if rev is None or previous_revision(trade) is None:
        raise ValueError("There is no counter-proposal to decline.")
    if rev.author_user_id == actor_user_id:
        raise ValueError("You can't decline your own counter-proposal.")

    now = utc_now()
    rev.declined_at = now
    trade.updated_at = now
    session.commit()
    session.refresh(trade)
    return trade


def trade_revision_diff(trade: Trade) -> dict:
    """What the current revision changed, against the one before it.

    Returns ``{"has_diff", "author", "offered": [...], "requested": [...]}``
    where each entry is ``{"item", "change"}`` and change is ``added`` /
    ``removed`` / ``quantity`` / ``unchanged``. A removed entry carries the
    PREVIOUS revision's item, since that is the only copy of it.

    Matching is on the inventory row plus printing and finish, NOT the item id —
    every revision writes fresh TradeItem rows, so ids never match across two of
    them and an id-based diff would report every line as both added and removed.
    """
    rev = current_revision(trade)
    prev = previous_revision(trade)
    if rev is None or prev is None:
        return {"has_diff": False, "author": None, "offered": [], "requested": []}

    def key(it: TradeItem) -> tuple:
        return (it.inventory_row_id, it.card_id, it.finish)

    out: dict = {"has_diff": False, "author": rev.author, "offered": [], "requested": []}
    for side in ("offered", "requested"):
        now_items = {key(i): i for i in trade.items if i.revision_id == rev.id and i.side == side}
        was_items = {key(i): i for i in trade.items if i.revision_id == prev.id and i.side == side}
        rows = []
        for k, item in now_items.items():
            was = was_items.get(k)
            if was is None:
                rows.append({"item": item, "change": "added", "was_quantity": None})
            elif was.quantity != item.quantity:
                rows.append({"item": item, "change": "quantity", "was_quantity": was.quantity})
            else:
                rows.append({"item": item, "change": "unchanged", "was_quantity": None})
        for k, was in was_items.items():
            if k not in now_items:
                rows.append({"item": was, "change": "removed", "was_quantity": was.quantity})
        out[side] = rows
        if any(r["change"] != "unchanged" for r in rows):
            out["has_diff"] = True
    return out


# ── State-machine transition ────────────────────────────────────


def transition_trade(
    session: Session,
    trade_id: int,
    actor_user_id: int,
    new_status: str,
    recipient_note: str | None = None,
) -> Trade:
    """Move a Trade through its state machine. The ONE allowed status mutator.

    Validates the transition is legal AND the actor is permitted:

      - ``proposed → accepted``: recipient only
      - ``proposed → declined``: recipient only
      - ``proposed → cancelled``: proposer only
      - any terminal → anything: rejected (already closed)

    On any terminal transition: writes the *_at_trade snapshots,
    sets ``closed_at = utc_now()``, sets ``updated_at``. All in one
    transaction. ``recipient_note`` is captured on accept/decline
    transitions (a short note the recipient sends back; optional).

    Raises ``ValueError`` on illegal transitions, illegal actors, or
    unknown trades.
    """
    trade = (
        session.query(Trade)
        .filter(Trade.id == trade_id)
        .options(
            joinedload(Trade.items)
            .joinedload(TradeItem.inventory_row)
            .joinedload(InventoryRow.card),
            joinedload(Trade.items).joinedload(TradeItem.card),
            joinedload(Trade.proposer),
            joinedload(Trade.recipient),
        )
        .first()
    )
    if trade is None:
        raise ValueError("Trade not found.")
    if trade.status != "proposed":
        raise ValueError(f"Trade is already {trade.status}; no further transitions allowed.")

    new_status = normalize_status(new_status)
    if new_status not in TERMINAL_TRADE_STATUSES:
        raise ValueError(f"Cannot transition to {new_status!r}.")
    if new_status == "abandoned":
        # System-only — user code paths must not hit this branch.
        raise ValueError("Abandonment is system-only; use a cleanup helper.")

    # Actor gating. Counter-proposals generalise the rule without changing it
    # for a trade nobody has countered: **the party who did NOT write the
    # current version answers it, and the one who DID may withdraw it.** On
    # revision 1 the author is the proposer, so this reduces exactly to
    # "recipient accepts or declines, proposer cancels".
    author_id = _current_author_id(trade)
    responder_id = (
        trade.recipient_user_id if author_id == trade.proposer_user_id else trade.proposer_user_id
    )
    if new_status in ("accepted", "declined"):
        if actor_user_id != responder_id:
            raise ValueError("Only the recipient can accept or decline this trade.")
    elif new_status == "cancelled":
        if actor_user_id != author_id:
            raise ValueError("Only the proposer can cancel this trade.")

    # Optional recipient note (only meaningful on recipient-side
    # transitions, but we accept it regardless and ignore on cancel).
    if new_status in ("accepted", "declined") and recipient_note is not None:
        rn = recipient_note.strip()
        trade.recipient_note = rn or None

    now = utc_now()
    trade.status = new_status
    trade.closed_at = now
    trade.updated_at = now
    write_trade_terminal_snapshot(session, trade)
    session.commit()
    session.refresh(trade)
    return trade


# ── Read APIs ───────────────────────────────────────────────────


def get_trade_detail(
    session: Session,
    viewer_user_id: int,
    trade_id: int,
) -> dict | None:
    """Resolve a Trade for a viewer. Returns dict or None.

    Returns None when the viewer is neither proposer nor recipient
    (non-leakage discipline — the route layer renders a non-leaky
    redirect, never a 403; keeps the existence of a trade-id
    non-leaky for non-parties).

    The returned dict carries the trade plus two pre-built rendering
    lists (``offered_items``, ``requested_items``) each of which is
    a list of sanitized projection dicts (the v3.29.1
    ``_ReadOnlyCardProjection`` pattern — see §8 of the spec). The
    rendering lists pull from snapshots after terminal; from live
    InventoryRow/Card before terminal.
    """
    trade = (
        session.query(Trade)
        .filter(Trade.id == trade_id)
        .options(
            joinedload(Trade.items)
            .joinedload(TradeItem.inventory_row)
            .joinedload(InventoryRow.card),
            joinedload(Trade.items).joinedload(TradeItem.card),
            joinedload(Trade.items).joinedload(TradeItem.showcase_item),
            joinedload(Trade.proposer),
            joinedload(Trade.recipient),
            joinedload(Trade.playgroup),
        )
        .first()
    )
    if trade is None:
        return None
    if viewer_user_id not in (trade.proposer_user_id, trade.recipient_user_id):
        return None

    offered_items = [
        _build_trade_item_projection(it, trade) for it in _items_by_side(trade, "offered")
    ]
    requested_items = [
        _build_trade_item_projection(it, trade) for it in _items_by_side(trade, "requested")
    ]
    # Side totals (proxies already contribute $0 per item — ADR
    # proxy-valuation-2026-06-12). ``*_has_proxy`` drives the one-line notice.
    offered_total = sum(it["total_value"] for it in offered_items)
    requested_total = sum(it["total_value"] for it in requested_items)
    has_proxy = any(it["is_proxy"] for it in offered_items + requested_items)
    return {
        "trade": trade,
        "offered_items": offered_items,
        "requested_items": requested_items,
        "offered_total": offered_total,
        "requested_total": requested_total,
        "has_proxy": has_proxy,
        "viewer_is_proposer": viewer_user_id == trade.proposer_user_id,
        "viewer_is_recipient": viewer_user_id == trade.recipient_user_id,
    }


def _build_trade_item_projection(item: TradeItem, trade: Trade) -> dict:
    """Build the sanitized projection for one TradeItem (§8).

    PRIVATE-FIELD HARD-FLAG: the dict returned here MUST contain ONLY
    whitelisted card identity + finish + quantity. No InventoryRow
    private fields (notes, tags, role, is_pending, storage_location_id,
    drawer, slot, from_drawer, from_slot, created_at, updated_at,
    user_id) and no ShowcaseItem.notes ever land in this projection.

    Pre-terminal: read live InventoryRow + Card (the proposer and
    recipient both see each other's items during negotiation — the
    privacy discipline applies in both directions).

    On/after terminal: read snapshot columns. After a terminal
    transition the snapshot is the durable historical record; live
    inventory edits don't rewrite history.

    Returns a dict shaped like the v3.29.1 inventory_card projection so
    the macro renders unchanged (its ``item.card.X`` access goes through
    the ``_ReadOnlyCardProjection`` wrapper which raises AttributeError
    on any non-whitelisted field).
    """
    use_snapshot = trade.status in TERMINAL_TRADE_STATUSES

    if use_snapshot:
        # Snapshot path — render exactly what was captured at terminal.
        # ``_ReadOnlyCardProjection`` wants a Card-like; for the snapshot
        # path we build a lightweight stand-in carrying just the
        # whitelisted identity fields. Fall through to live render if a
        # snapshot is somehow missing (defensive).
        if item.card_name_at_trade or item.card_set_code_at_trade:
            finish = item.finish_at_trade or item.finish or "normal"
            qty = item.quantity_at_trade or item.quantity or 1
            # Finish-aware price from the live Card if still resolvable
            # (only a public Scryfall field; safe to surface). Snapshots
            # do NOT carry price (decision A4 — identity only); live Card
            # gives us a current dollar figure for display.
            price = (effective_price(item.card, finish) or 0.0) if item.card else 0.0
            return {
                "id": item.id,
                "card": _SnapshotCardProjection(
                    name=item.card_name_at_trade,
                    set_code=item.card_set_code_at_trade,
                    collector_number=item.card_collector_number_at_trade,
                    card_id=item.card_id,
                    live_card=item.card,
                ),
                "finish": finish,
                "language": "en",
                "is_proxy": False,
                "quantity": qty,
                "effective_price": price,
                "total_value": price * qty,
            }

    # Live path. If the inventory row was deleted (§10) the card FK
    # remains; render from Card alone.
    card = item.card
    if card is None and item.inventory_row is not None:
        card = item.inventory_row.card
    if card is None:
        # Defensive — totally unresolvable; emit a placeholder so the
        # template doesn't crash.
        return {
            "id": item.id,
            "card": _SnapshotCardProjection(
                name="(card record removed)",
                set_code=None,
                collector_number=None,
                card_id=item.card_id,
                live_card=None,
            ),
            "finish": item.finish or "normal",
            "language": "en",
            "is_proxy": False,
            "quantity": item.quantity or 1,
            "effective_price": 0.0,
            "total_value": 0.0,
        }
    inv = item.inventory_row
    finish = item.finish or (inv.finish if inv is not None else "normal")
    qty = item.quantity or 1
    # Proxies carry $0 market value on a live trade (ADR proxy-valuation-2026-06-12).
    # Terminal/snapshot trades can't know (no is_proxy_at_trade snapshot) — those
    # paths keep is_proxy=False; this matters only for the live trust surface.
    is_proxy = bool(inv.is_proxy) if inv is not None else False
    price = 0.0 if is_proxy else (effective_price(card, finish) or 0.0)
    return {
        "id": item.id,
        "card": _ReadOnlyCardProjection(card),
        "finish": finish,
        "language": "en",
        "is_proxy": is_proxy,
        # ``quantity`` is the trade quantity, NOT the underlying
        # InventoryRow.quantity (which is private to that user).
        "quantity": qty,
        "effective_price": price,
        "total_value": price * qty,
    }


class _SnapshotCardProjection:
    """Card-like attribute proxy for the post-terminal snapshot render path.

    Carries only the whitelisted identity fields captured in the
    ``*_at_trade`` columns. Exposes the same attribute surface as
    ``_ReadOnlyCardProjection`` so the inventory_card macro renders
    unchanged. ``id`` resolves to the live Card.id when one is still
    reachable (the post-terminal card-detail link works); falls back
    to None otherwise.

    No private InventoryRow attribute is reachable through this
    object — only ``name``, ``set_code``, ``collector_number``, ``id``,
    and the deliberately-None attributes that pose no privacy risk
    (image_url, prices, etc. — all of which return None and render
    cleanly via the template's ``{% if %}`` guards).
    """

    __slots__ = ("_name", "_set_code", "_collector_number", "_card_id", "_live_card")

    def __init__(
        self,
        name: str | None,
        set_code: str | None,
        collector_number: str | None,
        card_id: int | None,
        live_card: Card | None,
    ) -> None:
        self._name = name
        self._set_code = set_code
        self._collector_number = collector_number
        self._card_id = card_id
        # Preserve a reference to the live Card if still in the session
        # (post-terminal render can still show image_url / set_name etc.
        # when the card exists — the snapshot's role is to lock the
        # IDENTITY against later renames).
        self._live_card = live_card

    @property
    def id(self) -> int | None:
        return self._card_id

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def set_code(self) -> str | None:
        return self._set_code

    @property
    def collector_number(self) -> str | None:
        return self._collector_number

    @property
    def set_name(self) -> str | None:
        return getattr(self._live_card, "set_name", None) if self._live_card else None

    @property
    def image_url(self) -> str | None:
        return getattr(self._live_card, "image_url", None) if self._live_card else None

    @property
    def scryfall_id(self) -> str | None:
        # REQUIRED alongside image_url, never separately: the templates guard on
        # image_url but build the <img> src from mirror_image_url(scryfall_id).
        # Without this property the attribute raised AttributeError, which Jinja
        # silently turns into Undefined -> an empty id -> ".../normal.jpg" 404 ->
        # every card on a completed trade rendered as a blank frame.
        return getattr(self._live_card, "scryfall_id", None) if self._live_card else None

    @property
    def rarity(self) -> str | None:
        return getattr(self._live_card, "rarity", None) if self._live_card else None

    @property
    def type_line(self) -> str | None:
        return getattr(self._live_card, "type_line", None) if self._live_card else None

    @property
    def mana_cost(self) -> str | None:
        return getattr(self._live_card, "mana_cost", None) if self._live_card else None

    @property
    def cmc(self):
        return getattr(self._live_card, "cmc", None) if self._live_card else None

    @property
    def colors(self):
        return getattr(self._live_card, "colors", None) if self._live_card else None

    @property
    def color_identity(self):
        return getattr(self._live_card, "color_identity", None) if self._live_card else None

    @property
    def oracle_text(self) -> str | None:
        return getattr(self._live_card, "oracle_text", None) if self._live_card else None

    @property
    def price_usd(self) -> str | None:
        return getattr(self._live_card, "price_usd", None) if self._live_card else None

    @property
    def price_usd_foil(self) -> str | None:
        return getattr(self._live_card, "price_usd_foil", None) if self._live_card else None

    @property
    def price_usd_etched(self) -> str | None:
        return getattr(self._live_card, "price_usd_etched", None) if self._live_card else None

    @property
    def legalities(self) -> str | None:
        return getattr(self._live_card, "legalities", None) if self._live_card else None


# ── Inbox + badge ───────────────────────────────────────────────


def list_trades_for_user(session: Session, user_id: int) -> dict:
    """Inbox payload: incoming pending, sent pending, recent terminal.

    Returns ``{"incoming": [...], "sent": [...], "recent": [...]}``
    where each list is ordered most-recent first. The lists are
    deliberately separate so the template can render three sections
    without filtering in Jinja.

    ``recent`` is capped to the latest 20 terminal trades involving
    the user (proposer OR recipient) so the inbox stays compact.
    """
    incoming = (
        session.query(Trade)
        .filter(
            Trade.recipient_user_id == user_id,
            Trade.status == "proposed",
        )
        .options(
            joinedload(Trade.proposer),
            joinedload(Trade.recipient),
            joinedload(Trade.playgroup),
            joinedload(Trade.items),
        )
        .order_by(Trade.created_at.desc())
        .all()
    )
    sent = (
        session.query(Trade)
        .filter(
            Trade.proposer_user_id == user_id,
            Trade.status == "proposed",
        )
        .options(
            joinedload(Trade.proposer),
            joinedload(Trade.recipient),
            joinedload(Trade.playgroup),
            joinedload(Trade.items),
        )
        .order_by(Trade.created_at.desc())
        .all()
    )
    recent = (
        session.query(Trade)
        .filter(
            or_(
                Trade.proposer_user_id == user_id,
                Trade.recipient_user_id == user_id,
            ),
            Trade.status.in_(list(TERMINAL_TRADE_STATUSES)),
        )
        .options(
            joinedload(Trade.proposer),
            joinedload(Trade.recipient),
            joinedload(Trade.playgroup),
            joinedload(Trade.items),
        )
        .order_by(Trade.closed_at.desc().nulls_last(), Trade.updated_at.desc())
        .limit(20)
        .all()
    )
    return {"incoming": incoming, "sent": sent, "recent": recent}


def pending_action_count(session: Session, user_id: int) -> int:
    """Nav-badge count: proposed trades where the user is the recipient.

    Single indexed count(*) — the inbox aggregate that lives in the
    nav badge. ``proposed + recipient_user_id`` is covered by the
    ``ix_trades_recipient_user_id`` + ``ix_trades_status`` indexes.
    """
    if not user_id:
        return 0
    return (
        session.query(Trade)
        .filter(
            Trade.recipient_user_id == user_id,
            Trade.status == "proposed",
        )
        .count()
    )


def resolve_propose_from_showcase_item(
    session: Session,
    proposer_user_id: int,
    showcase_item_id: int,
) -> dict | None:
    """Propose-from-share entry: resolve a ShowcaseItem the proposer
    clicked on the v3.29.1 share view.

    Returns ``{recipient: User, playgroup: Playgroup, showcase_item:
    ShowcaseItem, requested_item: dict}`` if and only if:

      - the ShowcaseItem exists, AND
      - it belongs to a Showcase the recipient owns, AND
      - that Showcase is currently shared to a playgroup the
        proposer is also a member of (the C2 + D1 prereq).

    Returns None on any miss; the route layer renders a clean
    construction page on /trades/new without prefill.
    """
    from app import playgroup_service

    showcase_item = (
        session.query(ShowcaseItem)
        .filter(ShowcaseItem.id == showcase_item_id)
        .options(
            joinedload(ShowcaseItem.inventory_row).joinedload(InventoryRow.card),
            joinedload(ShowcaseItem.showcase).joinedload(Showcase.user),
        )
        .first()
    )
    if showcase_item is None:
        return None
    showcase = showcase_item.showcase
    if showcase is None or showcase.user is None:
        return None
    recipient = showcase.user
    if recipient.id == proposer_user_id:
        return None  # can't trade with yourself
    # Find a Share of this Showcase to a playgroup the proposer is in.
    candidate_shares = (
        session.query(Share, Playgroup)
        .join(Playgroup, Share.playgroup_id == Playgroup.id)
        .filter(Share.showcase_id == showcase.id, Share.user_id == recipient.id)
        .all()
    )
    for share, playgroup in candidate_shares:
        proposer_membership = playgroup_service.require_membership(
            session, proposer_user_id, playgroup.id, min_role="member"
        )
        if proposer_membership is not None:
            return {
                "recipient": recipient,
                "playgroup": playgroup,
                "showcase_item": showcase_item,
                "showcase": showcase,
                "share": share,
            }
    return None


def resolve_propose_from_share_row(
    session: Session,
    proposer_user_id: int,
    inventory_row_id: int | None,
) -> dict | None:
    """Propose-from-share for a MIRRORED card — the sibling of
    ``resolve_propose_from_showcase_item`` for a card with no ShowcaseItem.

    The share page used to point every card at ``?from_showcase_item={{ item.id
    }}``, and a mirrored card's id is None, so the link rendered
    ``from_showcase_item=None`` and answered **422**: a card the viewer could see,
    with a button that could only error.

    Same guards, one substitution — membership is resolved through
    ``resolve_showcase_rows`` (#135's one answer) rather than by loading an item
    that does not exist. Returns None on any miss, exactly like its sibling, so
    the route degrades to a plain construction page.
    """
    from app import playgroup_service
    from app.share_service import resolve_showcase_rows

    if not inventory_row_id:
        return None
    row = (
        session.query(InventoryRow)
        .filter(InventoryRow.id == inventory_row_id)
        .options(joinedload(InventoryRow.card))
        .first()
    )
    if row is None or row.card is None or row.user_id == proposer_user_id:
        return None  # missing, or your own card — you cannot trade with yourself

    candidate_shares = (
        session.query(Share, Playgroup, Showcase)
        .join(Playgroup, Share.playgroup_id == Playgroup.id)
        .join(Showcase, Share.showcase_id == Showcase.id)
        .filter(Share.user_id == row.user_id)
        .all()
    )
    for share, playgroup, showcase in candidate_shares:
        if (
            playgroup_service.require_membership(
                session, proposer_user_id, playgroup.id, min_role="member"
            )
            is None
        ):
            continue
        member_ids = {
            e["row"].id for e in resolve_showcase_rows(session, showcase.id) if e["row"] is not None
        }
        if row.id in member_ids:
            return {
                "recipient": showcase.user,
                "playgroup": playgroup,
                "showcase_item": None,
                "inventory_row": row,
                "showcase": showcase,
                "share": share,
            }
    return None


def shared_pick_items(session: Session, showcase_id: int, search: str = "") -> list[dict]:
    """What the other party is actually offering — curated AND mirrored.

    **Membership comes from `share_service.resolve_showcase_rows`, THE one
    answer (#135), not from a `ShowcaseItem` query.** The picker used to read
    the items table directly, so a card that is in a showcase because its
    LOCATION is mirrored — which has no ShowcaseItem at all — was visible to a
    viewer on the share page and then absent from the trade screen. Mirroring a
    box made its cards untradeable, silently, which is most of the point of
    putting them in a showcase.

    Each entry NAMES ITSELF: ``pick_kind`` is ``showcase_item_id`` for a curated
    card and ``inventory_row_id`` for a mirrored one, because those are two id
    spaces that would collide as bare integers. ``inventory_row_id`` is an
    opaque id, not one of §8's forbidden InventoryRow FIELDS (notes, tags,
    location, raw quantity) — the card itself still goes through the sanitized
    projection.
    """
    from app.share_service import resolve_showcase_rows

    # #184 — `search` is the app's boolean/Scryfall query language, applied
    # SERVER-SIDE by the resolver (the same parser the Collection bar uses), so
    # the picker no longer has to render a whole showcase for a filter to work.
    out: list[dict] = []
    for entry in resolve_showcase_rows(session, showcase_id, search):
        inv = entry["row"]
        if inv is None or inv.card is None:
            continue
        available = max(0, min(entry["offered"], inv.quantity))
        if available <= 0:
            continue
        item = entry["item"]
        out.append(
            {
                "showcase_item_id": item.id if item is not None else None,
                "inventory_row_id": inv.id,
                "pick_kind": "showcase_item_id" if item is not None else "inventory_row_id",
                "pick_id": item.id if item is not None else inv.id,
                # A curated card sends BOTH ids, so the trade keeps its
                # provenance link (C1) while the row stays the thing the picker
                # can always name.
                "pick_alt": f"inventory_row_id:{inv.id}" if item is not None else "",
                "mirrored": bool(entry["mirrored"]),
                "card": _ReadOnlyCardProjection(inv.card),
                "finish": inv.finish,
                "available": available,
                "is_proxy": bool(inv.is_proxy),
            }
        )
    out.sort(key=lambda d: ((d["card"].name or "").lower(), d["pick_id"]))
    return out


def offered_pick_items(session: Session, user_id: int, search: str = "") -> list[dict]:
    """The user's own pickable inventory — the OFFERED side's source.

    Extracted from ``get_construction_options`` when the picker became paged
    (#184) so the page and the search endpoint build the list one way. Search is
    server-side in the app's query language; a 1,600-row inventory no longer has
    to reach the browser for a filter to work.
    """
    # #140 — a brew placeholder is a card you do NOT own: an is_proxy row that
    # exists only inside a brew deck, standing in for something you plan to buy.
    # ``create_trade`` has always refused one, so listing them here offered cards
    # that could not be traded and answered the attempt with an error (reported
    # 2026-08-21). The ONE discriminator, same as the other enforcement points —
    # never a hand-rolled is_proxy check, which would also drop the real proxies
    # people legitimately trade.
    from app.inventory_service import apply_collection_search_filters, brew_placeholder_exclusion

    inv_q = (
        session.query(InventoryRow)
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(False),
            InventoryRow.quantity > 0,
            brew_placeholder_exclusion(user_id),
        )
        .options(joinedload(InventoryRow.card), joinedload(InventoryRow.storage_location))
    )
    # #184 — the offered side is searched SERVER-SIDE with the same query
    # language as the Collection bar, so a 1,600-row inventory no longer has to
    # reach the browser for a filter to work.
    if search and search.strip():
        inv_q = apply_collection_search_filters(inv_q, search)
    inv_q = inv_q.order_by(Card.name, InventoryRow.finish).all()
    # Where each offered copy currently lives, so a card that would have to come
    # out of a deck is obvious before it's offered. OFFERED SIDE ONLY — the
    # recipient's locations are private and never leave the sanitized card
    # projection. One batched parent fetch (locations are few even when the
    # inventory is thousands of rows), then the SHARED label builder the
    # decklist checker uses — not a second location-label format.
    parent_ids = {
        row.storage_location.parent_id
        for row in inv_q
        if row.storage_location is not None and row.storage_location.parent_id is not None
    }
    parent_chain: dict[int, StorageLocation] = {}
    if parent_ids:
        parent_chain = {
            p.id: p
            for p in session.query(StorageLocation).filter(StorageLocation.id.in_(parent_ids)).all()
        }

    proposer_inventory: list[dict] = []
    for row in inv_q:
        if row.card is None:
            continue
        loc = row.storage_location
        proposer_inventory.append(
            {
                "inventory_row_id": row.id,
                # Picker identity (#184): one id space on this side, so no alt.
                "pick_kind": "inventory_row_id",
                "pick_id": row.id,
                "pick_alt": "",
                "available": row.quantity,
                "card": _ReadOnlyCardProjection(row.card),
                "finish": row.finish,
                "quantity": row.quantity,
                "is_proxy": bool(row.is_proxy),
                "location_label": _build_full_location_label(loc, parent_chain),
                "location_type": (loc.type or "other").lower() if loc is not None else "",
            }
        )
    return proposer_inventory


def get_construction_options(
    session: Session,
    proposer_user_id: int,
    recipient_user_id: int | None,
    playgroup_id: int | None,
    requested_search: str = "",
    offered_search: str = "",
) -> dict:
    """Resolve construction-page options for a proposer.

    Returns ``{recipients, recipient_share_items, proposer_inventory}``
    where:

      - ``recipients``: list of {user, playgroup} pairs where the
        proposer + that user are co-members in a playgroup and the
        user has an active Share to that playgroup. (Each user may
        appear with multiple playgroups; the construction page lists
        all so the proposer picks the trade context explicitly.)
      - ``recipient_share_items``: when ``recipient_user_id`` +
        ``playgroup_id`` are set and the proposer + recipient are
        co-members of that playgroup AND the recipient has a Share
        there, returns the sanitized ShowcaseItem list. Empty
        otherwise.
      - ``proposer_inventory``: list of the proposer's placed
        InventoryRows (not pending), joined to Card, ordered by name.
        The offered-side picker source.
    """
    # Build candidate recipients across all shares targeting playgroups
    # the proposer is in.
    proposer_pg_ids = [
        row.playgroup_id
        for row in session.query(PlaygroupMember.playgroup_id)
        .filter(PlaygroupMember.user_id == proposer_user_id)
        .all()
    ]
    recipients: list[dict] = []
    if proposer_pg_ids:
        share_rows = (
            session.query(Share, User, Playgroup, Showcase)
            .join(User, Share.user_id == User.id)
            .join(Playgroup, Share.playgroup_id == Playgroup.id)
            .join(Showcase, Share.showcase_id == Showcase.id)
            .filter(
                Share.playgroup_id.in_(proposer_pg_ids),
                Share.user_id != proposer_user_id,
                User.is_active.is_(True),
            )
            .order_by(User.player_label_expr(), Playgroup.name)
            .all()
        )
        for share, user, playgroup, showcase in share_rows:
            recipients.append(
                {
                    "user": user,
                    "playgroup": playgroup,
                    "showcase": showcase,
                    "share": share,
                }
            )

    recipient_share_items: list[dict] = []
    if recipient_user_id and playgroup_id:
        # Confirm the (recipient, playgroup) pair is among the candidates.
        for cand in recipients:
            if cand["user"].id == recipient_user_id and cand["playgroup"].id == playgroup_id:
                recipient_share_items = shared_pick_items(
                    session, cand["showcase"].id, requested_search
                )
                break

    proposer_inventory = offered_pick_items(session, proposer_user_id, offered_search)
    return {
        "recipients": recipients,
        "recipient_share_items": recipient_share_items,
        "proposer_inventory": proposer_inventory,
    }


def counter_options(session: Session, trade: Trade, author_user_id: int) -> dict:
    """The two pick lists for a counter, oriented for whoever is authoring it.

    **Sides never swap.** OFFERED is the proposer's cards and REQUESTED the
    recipient's, whichever party is editing — otherwise "they offered" would
    mean something different on each screen. What changes is where the author's
    options come from:

    * OFFERED — the proposer picks from their own inventory (row ids). A
      countering RECIPIENT can only see what the proposer has SHARED with this
      playgroup (showcase item ids), and gets an empty list if they share
      nothing: they can still drop or reduce what was offered, just not reach
      into inventory they cannot see.
    * REQUESTED — always the recipient's Showcase shared with this playgroup
      (decision C2), which the recipient can also pick from when countering,
      since it is their own.

    Returns ``{"offered_rows", "offered_share_items", "requested_share_items",
    "author_is_proposer"}``.
    """
    author_is_proposer = author_user_id == trade.proposer_user_id
    forward = get_construction_options(
        session, trade.proposer_user_id, trade.recipient_user_id, trade.playgroup_id
    )
    offered_rows: list[dict] = []
    offered_share_items: list[dict] = []
    if author_is_proposer:
        offered_rows = forward["proposer_inventory"]
    else:
        reverse = get_construction_options(
            session, trade.recipient_user_id, trade.proposer_user_id, trade.playgroup_id
        )
        offered_share_items = reverse["recipient_share_items"]
    return {
        "offered_rows": offered_rows,
        "offered_share_items": offered_share_items,
        "requested_share_items": forward["recipient_share_items"],
        "author_is_proposer": author_is_proposer,
    }


def current_picks_for_restore(trade: Trade, author_is_proposer: bool) -> dict:
    """The current revision as the picker's restore blob, so opening the counter
    editor starts from the trade as it stands rather than from nothing.

    Same shape the rejected-submit path uses — one restore mechanism, not two.
    An OFFERED line restores by inventory row for the proposer and by showcase
    item for the recipient, matching whichever list that author is picking from.
    """
    if author_is_proposer:
        offered = [
            {"inventory_row_id": it.inventory_row_id, "quantity": it.quantity}
            for it in _items_by_side(trade, "offered")
            if it.inventory_row_id
        ]
    else:
        # The recipient's editor lists the offered side as the trade's OWN lines
        # (they cannot name the proposer's rows), so restore by trade item id.
        offered = [
            {"trade_item_id": it.id, "quantity": it.quantity}
            for it in _items_by_side(trade, "offered")
        ]
    requested = [
        {"showcase_item_id": it.showcase_item_id, "quantity": it.quantity}
        for it in _items_by_side(trade, "requested")
        if it.showcase_item_id
    ]
    return {"offered": offered, "requested": requested}


# ── §10 cleanup helpers ─────────────────────────────────────────


def _abandon_query(session: Session, q) -> int:
    """Shared finalize step: snapshot + status=abandoned + closed_at on every
    pending trade in the provided query. Returns the count abandoned.

    Each trade's items + relationships are eager-loaded before mutation
    so ``write_trade_terminal_snapshot`` can capture identity. Single
    flush at the end; the caller commits (matches the playgroup_service
    convention where ``handle_user_deletion`` doesn't commit and the
    admin route does the enclosing commit).
    """
    pending_trades = q.options(
        joinedload(Trade.items).joinedload(TradeItem.inventory_row).joinedload(InventoryRow.card),
        joinedload(Trade.items).joinedload(TradeItem.card),
        joinedload(Trade.proposer),
        joinedload(Trade.recipient),
    ).all()
    now = utc_now()
    for trade in pending_trades:
        trade.status = "abandoned"
        trade.closed_at = now
        trade.updated_at = now
        write_trade_terminal_snapshot(session, trade)
    session.flush()
    return len(pending_trades)


def abandon_pending_trades_for_playgroup(session: Session, playgroup_id: int) -> int:
    """Auto-abandon every proposed trade scoped to a playgroup. Used by
    playgroup-delete cleanup (§10).

    Terminal trades for the playgroup are NOT touched — they are the
    historical record. The playgroup-delete cascade SET-NULLs their
    ``playgroup_id`` (FK hygiene) but keeps the rows."""
    q = session.query(Trade).filter(
        Trade.playgroup_id == playgroup_id,
        Trade.status == "proposed",
    )
    return _abandon_query(session, q)


def abandon_pending_trades_for_member_in_playgroup(
    session: Session,
    user_id: int,
    playgroup_id: int,
) -> int:
    """Auto-abandon every proposed trade where the user is a party AND
    the trade is scoped to that playgroup. Used by playgroup
    leave/remove-member cleanup (§10).

    A user can be on either side (proposer or recipient) of trades in
    a playgroup; both directions abandon when they exit the audience."""
    q = session.query(Trade).filter(
        Trade.playgroup_id == playgroup_id,
        Trade.status == "proposed",
        or_(
            Trade.proposer_user_id == user_id,
            Trade.recipient_user_id == user_id,
        ),
    )
    return _abandon_query(session, q)


def abandon_pending_trades_involving_user(session: Session, user_id: int) -> int:
    """Auto-abandon every proposed trade involving a user across all
    playgroups. Used by admin user-deletion cascade BEFORE the trade
    rows are deleted (§10).

    Terminal trades are handled separately (SET-NULL the user FKs,
    leave snapshots intact) in the admin route."""
    q = session.query(Trade).filter(
        Trade.status == "proposed",
        or_(
            Trade.proposer_user_id == user_id,
            Trade.recipient_user_id == user_id,
        ),
    )
    return _abandon_query(session, q)


def abandon_pending_trades_for_inventory_rows(
    session: Session,
    inventory_row_ids: list[int],
) -> int:
    """Auto-abandon every proposed trade whose ``TradeItem.inventory_row_id``
    references one of the about-to-be-deleted InventoryRow ids. Used by
    inventory_service delete paths (§10).

    Also NULLs ``inventory_row_id`` on any remaining ``TradeItem`` (in
    terminal or now-abandoned trades) referencing the deleted rows —
    defensive hygiene so future renders fall back to snapshots cleanly.
    """
    if not inventory_row_ids:
        return 0
    affected_trade_ids = [
        tid
        for (tid,) in session.query(TradeItem.trade_id)
        .filter(TradeItem.inventory_row_id.in_(inventory_row_ids))
        .distinct()
        .all()
    ]
    if not affected_trade_ids:
        return 0
    q = session.query(Trade).filter(
        Trade.id.in_(affected_trade_ids),
        Trade.status == "proposed",
    )
    count = _abandon_query(session, q)
    # NULL the inventory_row_id on every TradeItem referencing the
    # deleted rows (across all trades — pending or terminal). The trade
    # carries snapshots; the link is no longer meaningful.
    session.query(TradeItem).filter(TradeItem.inventory_row_id.in_(inventory_row_ids)).update(
        {TradeItem.inventory_row_id: None},
        synchronize_session=False,
    )
    return count


def null_trade_item_showcase_links(session: Session, showcase_item_ids: list[int]) -> int:
    """NULL ``TradeItem.showcase_item_id`` for any TradeItem referencing
    a now-removed ShowcaseItem. Used by ``share_service.remove_showcase_item``
    (§10).

    Does NOT transition the parent Trade — the link is navigation
    metadata only (decision C1); the trade stays alive against its
    ``inventory_row_id`` (which is the actual identity). Returns the
    number of TradeItem rows updated.
    """
    if not showcase_item_ids:
        return 0
    result = (
        session.query(TradeItem)
        .filter(TradeItem.showcase_item_id.in_(showcase_item_ids))
        .update(
            {TradeItem.showcase_item_id: None},
            synchronize_session=False,
        )
    )
    session.flush()
    return result


__all__ = [
    "CANONICAL_TRADE_ITEM_SIDES",
    "CANONICAL_TRADE_STATUSES",
    "DEFAULT_TRADE_STATUS",
    "TERMINAL_TRADE_STATUSES",
    "abandon_pending_trades_for_inventory_rows",
    "abandon_pending_trades_for_member_in_playgroup",
    "abandon_pending_trades_for_playgroup",
    "abandon_pending_trades_involving_user",
    "create_trade",
    "get_construction_options",
    "get_trade_detail",
    "list_trades_for_user",
    "normalize_status",
    "null_trade_item_showcase_links",
    "pending_action_count",
    "resolve_propose_from_showcase_item",
    "transition_trade",
    "write_trade_terminal_snapshot",
]


# ---------------------------------------------------------------------------
# Wishlist tie-in (#146/#147 follow-up — Alex, Discord 2026-07-22).
# ---------------------------------------------------------------------------


def wishlist_propose_targets(session: Session, proposer_user_id: int) -> dict[int, Playgroup]:
    """owner_user_id -> Playgroup, for every co-member the proposer could trade with.

    A wishlist owner is a valid target ONLY if they have an active Showcase Share
    to a playgroup the proposer is also in — the C2 precheck ``create_trade``
    enforces. Reuses ``get_construction_options``' candidate walk (one query), so
    the button is hidden rather than 400ing on an owner with no Share (decision:
    keep A6, land on the construction page). First playgroup wins when several
    are shared.
    """
    targets: dict[int, Playgroup] = {}
    for cand in get_construction_options(session, proposer_user_id, None, None)["recipients"]:
        targets.setdefault(cand["user"].id, cand["playgroup"])
    return targets


def resolve_propose_from_wishlist(
    session: Session,
    proposer_user_id: int,
    owner_user_id: int,
) -> dict | None:
    """Wishlist → trade entry: seed the OFFERED side from wishlist ∩ own inventory.

    The mirror of :func:`resolve_propose_from_showcase_item` (which seeds the
    requested side). Returns ``{recipient, playgroup, offered_row_ids}`` or None
    when there is no shared playgroup in which the owner has an active Share.

    ``offered_row_ids`` is the proposer's own placed, non-proxy, **tradeable**
    (``mode in {managed, sink}``) rows whose card name the owner is watching —
    reusing the #147 ownership definition verbatim (``owned_inventory_for_names``,
    already tradeable-first sorted). Deck/display copies are deliberately NOT
    prefilled; they remain addable by hand from the offered grid.

    A6 is unchanged: the caller lands on the construction page with the offered
    side prefilled and must still pick >= 1 requested item.
    """
    from app.decklist_service import owned_inventory_for_names
    from app.watchlist_service import build_public_wishlist_view

    if proposer_user_id == owner_user_id:
        return None
    playgroup = wishlist_propose_targets(session, proposer_user_id).get(owner_user_id)
    if playgroup is None:
        return None
    names = [c["name"] for c in build_public_wishlist_view(session, owner_user_id)["cards"]]
    detail = owned_inventory_for_names(session, proposer_user_id, names, exclude_proxies=True)
    row_ids = [
        r["row_id"]
        for rows in detail.values()
        for r in rows
        if r["is_tradeable"] and not r["is_pending"]
    ]
    return {
        "recipient": session.query(User).filter(User.id == owner_user_id).first(),
        "playgroup": playgroup,
        "offered_row_ids": row_ids,
    }


def pending_offer_names_for_playgroup(
    session: Session,
    playgroup_id: int,
    owner_user_ids: list[int],
) -> dict[int, set[str]]:
    """owner_user_id -> lowercased card names already covered by an in-flight trade.

    "In-flight" = a trade in this playgroup TO that owner, status ``proposed`` OR
    ``accepted``, carrying that card on the OFFERED side. Accepted counts because
    trades are recording-only (B1) — an accepted trade doesn't clear the wishlist
    row, and that is exactly when "already being provided" matters most.

    **Privacy: aggregate only.** No proposer identity, no quantity — and this is
    the playgroup surface only; the anonymous ``/w/{token}`` page never gets it.
    One query for the whole page (never per entry). Name comes from the terminal
    snapshot when present, else the live Card (§10 cleanup can null ``card_id``).
    """
    out: dict[int, set[str]] = {}
    if not owner_user_ids:
        return out
    rows = (
        session.query(Trade.recipient_user_id, TradeItem.card_name_at_trade, Card.name)
        .join(TradeItem, TradeItem.trade_id == Trade.id)
        .outerjoin(Card, TradeItem.card_id == Card.id)
        .filter(
            Trade.playgroup_id == playgroup_id,
            Trade.recipient_user_id.in_(owner_user_ids),
            Trade.status.in_(("proposed", "accepted")),
            TradeItem.side == "offered",
        )
        .all()
    )
    for owner_id, snapshot_name, live_name in rows:
        name = snapshot_name or live_name
        if name:
            out.setdefault(owner_id, set()).add(name.lower())
    return out


# Reference imported for static-analysis cleanliness (and_ may be reintroduced
# in future query shapes). Keeping the import documented avoids ruff F401
# without a noqa comment.
_ = and_
