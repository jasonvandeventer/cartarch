"""Dashboard tile aggregates (v3.27.10, Decks tile added v3.27.11).

Three tiles surface data the app already computes — query-and-display, not
new subsystems. Each aggregate is a single SQL query against InventoryRow /
Deck / TransactionLog. All numbers reconcile against the Collection /
Drawers / Decks pages because the canonical unit is card-count
(``sum(quantity)``) on every surface; pending is split out as an explicit
sub-stat, never folded into the headline. The Decks tile reports a deck
COUNT — its own unit, unaffected by the card-count canon.

v3.27.11 — replaced the Sets Collected tile with a Decks tile. The Sets
aggregate carried no actionable signal: numerator was placed-row distinct
``set_code`` (190 on prod); denominator was distinct ``set_code`` in the
``cards`` table (320 on prod, NOT the v3.25.0 bulk Scryfall cache view as
v3.27.10's docs initially claimed — the ``cards`` table is the working
per-card subset populated as the app encounters cards via imports / token
references / deck pulls; ``scryfall_cards`` is the bulk cache, much
larger). On the dashboard the percentage moved as OTHER users' imports
populated ``cards`` — signal the current user didn't generate. The Decks
tile replaces it with a count + commander-tagged sub-line that points at
``/decks``.

Performance budget — measured on prod data shape:

- Collection Value (finish-aware ``SUM(quantity * price)`` via CASE):  ~2.6 ms
- Decks count + commander-tagged subquery:                             ~1 ms
- Activity feed top-8 (TransactionLog + LEFT JOIN cards):              ~1.1 ms

Total under 10 ms. Live aggregation on every dashboard render — no
in-process cache, no precomputed counts table. SQLite-until-v4 constraint
respected (no schema, no migration). If a future tile crosses the
per-request budget, the established pattern is the daemon-loop precompute
shape (``_price_refresh_loop`` / ``_bulk_data_loop`` etc.), NOT a new DB
table.

**Deliberate avoidance**: the Decks tile does NOT route through
``deck_service.list_decks`` — that path still carries the 3×N per-deck
N+1 (SUM(quantity) + commanders + all-rows queries) flagged in the
v3.27.9 diagnostic and folded into the Deck Analytics Rebuild's scope.
Pulling the full deck list just to display a count would reintroduce
that cost on every dashboard render. The Decks tile uses a direct
``COUNT(Deck.id)`` + commander-tagged subquery — single SQL pass.
"""

from __future__ import annotations

from sqlalchemy import Float, case, cast, func
from sqlalchemy.orm import Session

from app.models import Card, Deck, InventoryRow, TransactionLog


def _placed_value_expr() -> object:
    """Finish-aware unit price expression for SUM(quantity * price).

    Mirrors ``app.pricing.effective_price`` finish-fallback order in SQL so
    the dashboard tile reconciles against the Collection-page total to the
    cent. ``cast(..., Float)`` because Scryfall prices are stored as TEXT
    in SQLite (the v3.25.0 bulk cache preserves the wire format for
    byte-identical round-tripping); the multiplication with
    ``InventoryRow.quantity`` (Integer) needs a numeric type on both sides.
    Float maps to SQLite REAL, the same shape the prod-measured raw SQL
    used (``CAST(... AS REAL)``).
    """
    return cast(
        case(
            (
                InventoryRow.finish == "foil",
                func.coalesce(Card.price_usd_foil, Card.price_usd),
            ),
            (
                InventoryRow.finish == "etched",
                func.coalesce(Card.price_usd_etched, Card.price_usd_foil, Card.price_usd),
            ),
            else_=Card.price_usd,
        ),
        Float,
    )


def get_dashboard_tiles(session: Session, user_id: int) -> dict:
    """Build the three v3.27.10 dashboard tiles' data in one place.

    Returns a dict with three keys:

    - ``collection``: ``{placed_cards, placed_value, pending_cards,
      pending_value}`` — placed headline + pending sub-stat (canon).
    - ``decks``: ``{total, commander_format}`` — total decks for the
      user + count of those whose ``Deck.format`` is ``'Commander'``
      (case-insensitive — Deck.format is free-text, NOT normalized by
      v3.27.2). v3.27.11 — replaces Sets Collected. Avoids the
      v3.27.9-flagged ``list_decks`` 3×N N+1 by going direct against
      ``Deck`` with two simple aggregate queries.
    - ``activity``: list of recent ``TransactionLog`` rows (top 8), each
      with ``{event_type, created_at, quantity_delta, source_location,
      destination_location, note, card_name, set_code}``. Display-only —
      no new event types or logging.
    """
    # Tile 1 — Collection Value (placed headline + pending sub-stat).
    # One SUM aggregate per slice. Same canon as the Collection page so the
    # tile and the page show identical numbers.
    placed_cards = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(False),
        )
        .scalar()
    )
    placed_value = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity * _placed_value_expr()), 0))
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(False),
        )
        .scalar()
    )
    pending_cards = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(True),
        )
        .scalar()
    )
    pending_value = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity * _placed_value_expr()), 0))
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(True),
        )
        .scalar()
    )

    # Tile 2 — Decks Owned (v3.27.11; replaced Sets Collected). Total
    # Deck rows for the user + count of those whose Deck.format is
    # 'Commander' (case-insensitive — Deck.format is free-text and was
    # NOT normalized by v3.27.2, which only normalized Game.format).
    # Going direct against the Deck table avoids the v3.27.9-flagged
    # list_decks 3×N per-deck N+1 (folded into the Deck Analytics
    # Rebuild scope, not fixed here). Commander is the dominant format
    # on this install today (prod check: all 24 decks across 4 users
    # are 'Commander'); as users add other formats the headline
    # "Decks Owned: N" + sub-line "Commander: X" will gain useful
    # variance. If Deck.format ever drifts beyond a single canonical
    # 'Commander' string, this query stays correct via the lower()
    # case-fold — same robustness the v3.27.2 Game.format normalization
    # path uses on the way in.
    decks_total = (session.query(func.count(Deck.id)).filter(Deck.user_id == user_id).scalar()) or 0
    decks_commander_format = (
        session.query(func.count(Deck.id))
        .filter(
            Deck.user_id == user_id,
            func.lower(Deck.format) == "commander",
        )
        .scalar()
    ) or 0

    # Tile 3 — Recent Activity feed. TransactionLog + LEFT JOIN cards for
    # the display name (card may have been deleted; LEFT JOIN tolerates
    # the dangling ID — same defensive pattern as the audit page). Top 8
    # ordered by created_at DESC.
    activity_rows = (
        session.query(
            TransactionLog.id,
            TransactionLog.event_type,
            TransactionLog.created_at,
            TransactionLog.quantity_delta,
            TransactionLog.source_location,
            TransactionLog.destination_location,
            TransactionLog.note,
            Card.name.label("card_name"),
            Card.set_code,
        )
        .outerjoin(Card, TransactionLog.card_id == Card.id)
        .filter(TransactionLog.user_id == user_id)
        .order_by(TransactionLog.created_at.desc())
        .limit(8)
        .all()
    )
    activity = [
        {
            "id": r.id,
            "event_type": r.event_type,
            "created_at": r.created_at,
            "quantity_delta": r.quantity_delta,
            "source_location": r.source_location,
            "destination_location": r.destination_location,
            "note": r.note,
            "card_name": r.card_name,
            "set_code": r.set_code,
        }
        for r in activity_rows
    ]

    return {
        "collection": {
            "placed_cards": int(placed_cards or 0),
            "placed_value": float(placed_value or 0.0),
            "pending_cards": int(pending_cards or 0),
            "pending_value": float(pending_value or 0.0),
        },
        "decks": {
            "total": int(decks_total),
            "commander_format": int(decks_commander_format),
        },
        "activity": activity,
    }
