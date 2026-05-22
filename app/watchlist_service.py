"""Watchlist service (v3.27.12).

A user watchlist of cards. Two identity modes per the v3.27.12 spec
decision:

- **Printing-specific** (``card_id`` set): one watch row references a
  single ``cards.id``. Tracks a particular Scryfall printing.
- **Printing-agnostic** (``card_name`` set): one watch row stores a
  card name string. Matches any printing whose ``Card.name`` equals
  the stored canonical name.

Exactly one of the two identity columns is populated per row. This
``XOR`` shape is enforced **here** at the service layer — SQLite
``CHECK`` constraints stay out of the schema to preserve the
SQLite-until-v4 no-rebuild constraint, and the project convention
(v3.10.6 ``VALID_LOCATION_TYPES``, v3.27.2 ``CANONICAL_GAME_FORMATS``)
puts free-text / shape validation in the service layer. The two
partial-unique indexes from the v3.27.12 migration handle
"one row per identity per user" at the DB level; this module handles
"exactly one identity per row" and the read-side aggregation.

Ownership data: ``list_watchlist`` returns each item enriched with
the user's placed + pending card counts for the watched card.
Card-id watches join on ``card_id``; name watches aggregate across
all printings whose ``Card.name`` matches. The aggregates are cheap
on prod data shape (the same finish-aware patterns the v3.27.10
dashboard tiles already use). No request-path network calls.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import Card, InventoryRow, WatchlistItem


def _normalize_card_name(name: str | None) -> str | None:
    """Trim + collapse whitespace. Returns None for empty input.

    Callers should pass either an already-canonical Scryfall card name
    (preferred — from the autocomplete-driven add form) or a free-text
    value. This helper doesn't try to canonicalize against the cards
    table — that lookup belongs in the route handler (where a found
    Card lets us upgrade the watch from name-only to card_id if the
    user wants).
    """
    if name is None:
        return None
    stripped = name.strip()
    return stripped or None


def _ownership_for_card_id(session: Session, user_id: int, card_id: int) -> tuple[int, int]:
    """Return (placed_count, pending_count) for one card_id."""
    placed = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.card_id == card_id,
            InventoryRow.is_pending.is_(False),
        )
        .scalar()
    )
    pending = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.card_id == card_id,
            InventoryRow.is_pending.is_(True),
        )
        .scalar()
    )
    return int(placed or 0), int(pending or 0)


def _ownership_for_card_name(session: Session, user_id: int, card_name: str) -> tuple[int, int]:
    """Return (placed_count, pending_count) summed across all printings."""
    base = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(
            InventoryRow.user_id == user_id,
            Card.name == card_name,
        )
    )
    placed = base.filter(InventoryRow.is_pending.is_(False)).scalar()
    pending = base.filter(InventoryRow.is_pending.is_(True)).scalar()
    return int(placed or 0), int(pending or 0)


def list_watchlist(session: Session, user_id: int) -> list[dict]:
    """Return all watchlist items for one user, with ownership counts.

    Each dict carries: ``id, identity_mode, card, card_name,
    display_name, added_at, note, placed_count, pending_count``.

    - ``identity_mode``: ``"card"`` (printing-specific) or ``"name"``
      (printing-agnostic). Lets templates branch without re-checking
      column nullability.
    - ``card``: the joined ``Card`` ORM object for card-id watches;
      ``None`` for name watches.
    - ``display_name``: the canonical name to render (``card.name``
      for card watches; the stored ``card_name`` for name watches).
    """
    items = (
        session.query(WatchlistItem)
        .options(joinedload(WatchlistItem.card))
        .filter(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.added_at.desc())
        .all()
    )
    out: list[dict] = []
    for item in items:
        if item.card_id is not None and item.card is not None:
            placed, pending = _ownership_for_card_id(session, user_id, item.card_id)
            out.append(
                {
                    "id": item.id,
                    "identity_mode": "card",
                    "card": item.card,
                    "card_name": None,
                    "display_name": item.card.name,
                    "added_at": item.added_at,
                    "note": item.note,
                    "placed_count": placed,
                    "pending_count": pending,
                }
            )
        elif item.card_name is not None:
            placed, pending = _ownership_for_card_name(session, user_id, item.card_name)
            out.append(
                {
                    "id": item.id,
                    "identity_mode": "name",
                    "card": None,
                    "card_name": item.card_name,
                    "display_name": item.card_name,
                    "added_at": item.added_at,
                    "note": item.note,
                    "placed_count": placed,
                    "pending_count": pending,
                }
            )
        # Defensive: a row with neither column populated would be invalid
        # (the service-layer XOR contract prevents writes that violate
        # this). If one ever lands, skip silently rather than crash the
        # whole page — the row will surface in the audit/diagnostic next.
    return out


def add_to_watchlist(
    session: Session,
    user_id: int,
    *,
    card_id: int | None = None,
    card_name: str | None = None,
    note: str | None = None,
) -> WatchlistItem:
    """Create a watchlist row. XOR-validates the identity inputs.

    Exactly one of ``card_id`` / ``card_name`` must be set. Raises
    ``ValueError`` otherwise (the FastAPI ``ValueError`` handler from
    v3.4.6 turns these into a clean 400 instead of a 500 stack trace).
    Returns the persisted row. Caller is responsible for commit.

    Duplicate-watch (same user_id + same identity) raises IntegrityError
    via the partial-unique indexes from the v3.27.12 migration — the
    route handler converts this into a redirect with the existing row
    surfaced rather than treating it as an error. Pre-check here is
    informational; the index is the authority.
    """
    name = _normalize_card_name(card_name)
    if (card_id is None and name is None) or (card_id is not None and name is not None):
        raise ValueError(
            "Watchlist add requires exactly one of card_id (printing-specific) "
            "or card_name (printing-agnostic), never both, never neither."
        )

    item = WatchlistItem(
        user_id=user_id,
        card_id=card_id,
        card_name=name,
        note=(note.strip() if note and note.strip() else None),
        added_at=datetime.utcnow(),
    )
    session.add(item)
    session.flush()
    return item


def remove_from_watchlist(session: Session, user_id: int, watchlist_id: int) -> bool:
    """Delete one watchlist row. Returns True if a row was deleted.

    Per-user scoping enforced via the user_id filter — a user cannot
    delete another user's watchlist row even with a tampered id. Caller
    is responsible for commit.
    """
    item = (
        session.query(WatchlistItem)
        .filter(WatchlistItem.id == watchlist_id, WatchlistItem.user_id == user_id)
        .first()
    )
    if item is None:
        return False
    session.delete(item)
    return True


def update_note(session: Session, user_id: int, watchlist_id: int, note: str | None) -> bool:
    """Update the note field on one watchlist row. Returns True on hit.

    Empty / whitespace-only notes are stored as NULL (clears the note).
    Per-user scoping via user_id filter. Caller responsible for commit.
    """
    item = (
        session.query(WatchlistItem)
        .filter(WatchlistItem.id == watchlist_id, WatchlistItem.user_id == user_id)
        .first()
    )
    if item is None:
        return False
    cleaned = note.strip() if note else None
    item.note = cleaned or None
    return True


def is_card_watched(session: Session, user_id: int, card_id: int) -> bool:
    """True if the user has a printing-specific watch for this card_id."""
    return (
        session.query(WatchlistItem.id)
        .filter(WatchlistItem.user_id == user_id, WatchlistItem.card_id == card_id)
        .first()
        is not None
    )


def get_watch_ids_for_card(session: Session, user_id: int, card_id: int, card_name: str) -> dict:
    """Return existing watchlist row ids for both identity modes on this card.

    Used by the card detail page to render the watch-toggle UI:
    knowing the row id lets the template build the right "Stop
    watching" remove URL when one of the two watch types is active.
    Returns ``{"printing_id": int | None, "name_id": int | None}``.
    """
    printing_row = (
        session.query(WatchlistItem.id)
        .filter(WatchlistItem.user_id == user_id, WatchlistItem.card_id == card_id)
        .first()
    )
    normalized = _normalize_card_name(card_name)
    name_row = None
    if normalized is not None:
        name_row = (
            session.query(WatchlistItem.id)
            .filter(
                WatchlistItem.user_id == user_id,
                WatchlistItem.card_name == normalized,
            )
            .first()
        )
    return {
        "printing_id": printing_row[0] if printing_row else None,
        "name_id": name_row[0] if name_row else None,
    }


def is_name_watched(session: Session, user_id: int, card_name: str) -> bool:
    """True if the user has a printing-agnostic watch for this card name.

    Uses exact string match — caller should pass the canonical name
    (typically the joined Card.name from the page being rendered).
    """
    normalized = _normalize_card_name(card_name)
    if normalized is None:
        return False
    return (
        session.query(WatchlistItem.id)
        .filter(WatchlistItem.user_id == user_id, WatchlistItem.card_name == normalized)
        .first()
        is not None
    )
