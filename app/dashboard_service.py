"""Dashboard tile aggregates (v3.27.10).

Three tiles surface data the app already computes — query-and-display, not
new subsystems. Each aggregate is a single SQL query against InventoryRow /
TransactionLog. All numbers reconcile against the Collection / Drawers /
Decks pages because the canonical unit is card-count (``sum(quantity)``) on
every surface; pending is split out as an explicit sub-stat, never folded
into the headline.

Performance budget — measured on prod data shape (~4837 inventory rows,
190 owned sets, ~50 transaction-log rows/day):

- Collection Value (finish-aware ``SUM(quantity * price)`` via CASE):  ~2.6 ms
- Sets numerator (distinct ``set_code`` for placed rows):              ~2.6 ms
- Sets denominator (distinct ``set_code`` from cards):                 ~0.3 ms
- Activity feed top-8 (TransactionLog + LEFT JOIN cards):              ~1.1 ms

Total under 10 ms. Live aggregation on every dashboard render — no
in-process cache, no precomputed counts table. SQLite-until-v4 constraint
respected (no schema, no migration). If a future tile crosses the
per-request budget, the established pattern is the daemon-loop precompute
shape (``_price_refresh_loop`` / ``_bulk_data_loop`` etc.), NOT a new DB
table — but no tile in v3.27.10 needs it.
"""

from __future__ import annotations

from sqlalchemy import Float, case, cast, func
from sqlalchemy.orm import Session

from app.models import Card, InventoryRow, TransactionLog


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
    - ``sets``: ``{owned, total, completion_pct}`` — placed-only owned
      distinct ``set_code`` count, denominator is distinct ``set_code`` in
      the ``cards`` table (which is the v3.25.0 bulk Scryfall cache view).
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

    # Tile 2 — Sets Collected. Numerator: distinct set_codes the user has
    # at least one PLACED card from. Denominator: distinct set_codes in
    # the cards table (the v3.25.0 bulk Scryfall cache view; includes
    # paper + digital + promo + token sets — the denominator is a "tracked
    # universe" count, not a "paper sets the user could collect" filter;
    # captured as a known tradeoff in CLAUDE.md, not blocking).
    sets_owned = (
        session.query(func.count(func.distinct(Card.set_code)))
        .join(InventoryRow, InventoryRow.card_id == Card.id)
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(False),
        )
        .scalar()
    ) or 0
    sets_total = session.query(func.count(func.distinct(Card.set_code))).scalar() or 0
    completion_pct = round((sets_owned / sets_total) * 100, 1) if sets_total else 0.0

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
        "sets": {
            "owned": int(sets_owned),
            "total": int(sets_total),
            "completion_pct": completion_pct,
        },
        "activity": activity,
    }
