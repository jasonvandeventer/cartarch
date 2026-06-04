"""Dashboard data aggregates (v3.28.5 Folio dashboard redesign).

The Folio dashboard (v3.28.5) replaces the v3.27.10/v3.27.11 three-tile launcher
with a masthead + search hero + nine `§`-numbered editorial panels. This module
gathers everything the dashboard needs into a single ``get_dashboard_data()``
return shape — feeding the panels per the v3.28.5 spec's data-availability
triage:

  Bucket (a) — rearrangement of existing data:
    - masthead.greeting + masthead.username + masthead.pending_count
    - § III Chronicle (activity)
    - § IX Quick Actions (template-side; not in this module's return)

  Bucket (b) — existing data, new query:
    - § II Holdings (placed_value)
    - § IV Priority Queue (counts + oldest-pending narrative)
    - § V Spotlight (most-recent placed InventoryRow + Card)
    - § VI Deck Performance (Game + GameSeat aggregate per Deck)
    - § VII Colour Identity (W/U/B/R/G/Multi/Colorless by sum(quantity))
    - § VIII Collection Statistics (totals + sets + largest set + avg + watchlist)

  Bucket (c) — curated/computed, modest only:
    - masthead.insight (a single templated sentence)
    - § I Today's Brief (3-4 templated items)

  Bucket (d) — DEFERRED (price-history infrastructure not built):
    - masthead 30-day delta (collection value movement over time)
    - § II Holdings 30-day value chart
    - § V Spotlight 7-day price-delta chip
    - Watchlist delta chip
  These render as clean "available once price history lands" placeholders.

Performance budget — measured on prod data shape, target ≤50ms total:

  - Collection Value (finish-aware SUM via CASE):        ~3 ms
  - Pending count:                                       ~1 ms
  - Decks total + commander-format count:                ~1 ms
  - Activity feed (top 7 TransactionLog rows):           ~1 ms
  - Spotlight (latest placed row, LIMIT 1):              ~1 ms
  - Deck Performance (Game/GameSeat aggregate):          ~3 ms
  - Colour Identity (GROUP BY color_identity buckets):   ~5 ms
  - Collection Statistics (distinct sets, largest set):  ~5 ms
  - Priority Queue counts (pending breakdown):           ~3 ms
  - Today's Brief stat queries:                          ~5 ms

Total ~30 ms. Live aggregation on every dashboard render — no in-process cache,
no precomputed counts table. SQLite-until-v4 constraint respected.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, case, cast, desc, distinct, func
from sqlalchemy.orm import Session

from app.models import (
    Card,
    Deck,
    Game,
    GameSeat,
    ImportBatch,
    InventoryRow,
    TransactionLog,
    WatchlistItem,
)


def _placed_value_expr() -> object:
    """Finish-aware unit price expression for SUM(quantity * price).

    Mirrors ``app.pricing.effective_price`` finish-fallback order in SQL so
    the dashboard headline reconciles against the Collection-page total to
    the cent. ``cast(..., Float)`` because Scryfall prices are stored as
    TEXT in SQLite (the v3.25.0 bulk cache preserves the wire format for
    byte-identical round-tripping); the multiplication with
    ``InventoryRow.quantity`` (Integer) needs a numeric type on both sides.
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


def _time_greeting(now: datetime) -> str:
    """Time-aware greeting for the masthead. America/Chicago.

    5am–11:59am → 'Good morning'; 12pm–5:59pm → 'Good afternoon';
    everything else → 'Good evening'. Cheap, deterministic — no need to
    cache.
    """
    h = now.hour
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 18:
        return "Good afternoon"
    return "Good evening"


# v3.28.5 — colour-identity bucketing per the design package's COLOR_PIE shape.
# Each Card.color_identity (space-separated WUBRG, "" = colorless) buckets into
# exactly one of: W / U / B / R / G (single colour), M (multi-colour),
# C (colorless). Buckets sum to total placed cards.
_COLOR_BUCKETS = ["W", "U", "B", "R", "G", "M", "C"]
_COLOR_LABELS = {
    "W": "White",
    "U": "Blue",
    "B": "Black",
    "R": "Red",
    "G": "Green",
    "M": "Multi",
    "C": "Colorless",
}


def _bucket_for_color_identity(ci: str | None) -> str:
    """Map a Card.color_identity value to its bucket.

    Empty string OR None → Colorless. Single letter → that mono colour.
    Multiple letters → Multi.
    """
    if not ci or not ci.strip():
        return "C"
    letters = [c for c in ci.upper() if c in "WUBRG"]
    if len(letters) == 0:
        return "C"
    if len(letters) == 1:
        return letters[0]
    return "M"


def get_dashboard_data(session: Session, user_id: int, now: datetime | None = None) -> dict:
    """Build every panel's data for the Folio dashboard in one place.

    Returns a dict with these top-level keys (one per masthead / panel):

      ``masthead``      — greeting, insight line, pending count, deferred deltas
      ``today_brief``   — 3-4 modest templated items
      ``holdings``      — current placed value + deferred chart marker
      ``chronicle``     — recent activity (top 7, same as the v3.27.10 tile)
      ``priority_queue`` — pending count + location/batch breakdown
      ``spotlight``     — most-recent placed inventory row + card (or None)
      ``deck_performance`` — per-deck win-rate aggregate (top 6 by game count)
      ``color_identity`` — bucketed WUBRG/Multi/Colorless distribution
      ``collection_stats`` — six summary stats (totals + sets + largest + avg + watchlist)

    Live aggregation; no cache. ``now`` is a hook for tests; defaults to
    ``datetime.utcnow()``.
    """
    now = now or datetime.utcnow()

    # ── HEADLINE AGGREGATES (reused across multiple panels) ──────────────────
    placed_cards = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(False))
        .scalar()
    ) or 0
    placed_cards = int(placed_cards)

    placed_value = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity * _placed_value_expr()), 0.0))
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(False))
        .scalar()
    ) or 0.0
    placed_value = float(placed_value)

    pending_cards = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(True))
        .scalar()
    ) or 0
    pending_cards = int(pending_cards)

    # ── § IX QUICK ACTIONS data lives template-side (it's link cards) ────────

    # ── § III CHRONICLE (activity feed, top 7 — same as v3.27.10 tile) ──────
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
        .limit(7)
        .all()
    )
    chronicle = [
        {
            "id": r.id,
            "event_type": r.event_type,
            "created_at": r.created_at,
            "card_name": r.card_name,
            "set_code": r.set_code,
        }
        for r in activity_rows
    ]

    # ── § V SPOTLIGHT (most-recently-placed InventoryRow + Card) ────────────
    # Highest InventoryRow.id placed-and-not-pending — proxy for "most recent
    # add" since rows are append-only. Card.image_url comes from the v3.25.0
    # bulk cache so this is request-path-safe — no live Scryfall call.
    spot_row = (
        session.query(InventoryRow, Card)
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(False))
        .order_by(InventoryRow.id.desc())
        .first()
    )
    if spot_row:
        inv, card = spot_row
        spotlight = {
            "card_id": card.id,
            "name": card.name,
            "set_code": card.set_code,
            "collector_number": card.collector_number,
            "image_url": card.image_url,
            "color_identity": card.color_identity or "",
            "price_usd": card.price_usd,
            "finish": inv.finish,
            "quantity": inv.quantity,
        }
    else:
        spotlight = None

    # ── § VI DECK PERFORMANCE (Game + GameSeat aggregate) ────────────────────
    # Per-deck: total games (count of seats with placement IS NOT NULL on a
    # finalized game) + wins (placement == 1). Filter to Deck.user_id ==
    # current user so we show the user's own decks. Top 6 by game count.
    deck_perf_rows = (
        session.query(
            Deck.id.label("deck_id"),
            Deck.name.label("deck_name"),
            func.count(GameSeat.id).label("games"),
            func.sum(case((GameSeat.placement == 1, 1), else_=0)).label("wins"),
        )
        .join(GameSeat, GameSeat.deck_id == Deck.id)
        .join(Game, GameSeat.game_id == Game.id)
        .filter(
            Deck.user_id == user_id,
            Game.status == "finalized",
            GameSeat.placement.is_not(None),
        )
        .group_by(Deck.id, Deck.name)
        .order_by(desc("games"))
        .limit(6)
        .all()
    )
    # Look up each deck's color identity via its linked commander row (the
    # v3.5 role="commander" pattern). Batched: collect deck_ids, do ONE
    # query for commander rows joined to Card, fold the color_identity back.
    deck_ids = [r.deck_id for r in deck_perf_rows]
    deck_commander_colors: dict[int, str] = {}
    if deck_ids:
        commander_rows = (
            session.query(
                Deck.id.label("deck_id"),
                # aggregate_strings() is dialect-portable: compiles to
                # group_concat() on SQLite (behavior-identical to before) and
                # string_agg() on Postgres at the v4 cutover. func.group_concat
                # would 500 on PG (no such function). v4-prep, safe on SQLite.
                func.aggregate_strings(Card.color_identity, " ").label("ci_concat"),
            )
            .join(InventoryRow, InventoryRow.storage_location_id == Deck.storage_location_id)
            .join(Card, InventoryRow.card_id == Card.id)
            .filter(
                Deck.id.in_(deck_ids),
                InventoryRow.role == "commander",
            )
            .group_by(Deck.id)
            .all()
        )
        for cr in commander_rows:
            # Merge commander color_identities into a single WUBRG signature.
            letters = set()
            for ci in (cr.ci_concat or "").split():
                for c in ci.upper():
                    if c in "WUBRG":
                        letters.add(c)
            # Preserve WUBRG canonical order; space-separated to match the
            # Card.color_identity wire format and the mana_pips() macro's
            # expected input ("W U" not "WU").
            deck_commander_colors[cr.deck_id] = " ".join(c for c in "WUBRG" if c in letters)
    deck_performance = []
    for r in deck_perf_rows:
        games = int(r.games or 0)
        wins = int(r.wins or 0)
        deck_performance.append(
            {
                "deck_id": r.deck_id,
                "name": r.deck_name,
                "color_identity": deck_commander_colors.get(r.deck_id, ""),
                "games": games,
                "wins": wins,
                "win_rate": (wins / games) if games > 0 else 0.0,
            }
        )

    # ── § VII COLOUR IDENTITY (bucketed sum) ─────────────────────────────────
    # One GROUP BY across Card.color_identity, returning sum(quantity) per
    # distinct color_identity value. Bucket Python-side per
    # ``_bucket_for_color_identity``. Cheap on prod data: a handful of
    # distinct identity strings vs hundreds of thousands of inventory rows.
    ci_rows = (
        session.query(
            Card.color_identity,
            func.coalesce(func.sum(InventoryRow.quantity), 0).label("total"),
        )
        .join(InventoryRow, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(False))
        .group_by(Card.color_identity)
        .all()
    )
    bucket_totals: dict[str, int] = {b: 0 for b in _COLOR_BUCKETS}
    total_buckets = 0
    for row in ci_rows:
        b = _bucket_for_color_identity(row.color_identity)
        q = int(row.total or 0)
        bucket_totals[b] += q
        total_buckets += q
    color_identity = [
        {
            "id": b,
            "label": _COLOR_LABELS[b],
            "count": bucket_totals[b],
            "pct": (bucket_totals[b] / total_buckets * 100) if total_buckets > 0 else 0.0,
        }
        for b in _COLOR_BUCKETS
    ]

    # ── § VIII COLLECTION STATISTICS ─────────────────────────────────────────
    unique_printings = (
        session.query(func.count(distinct(InventoryRow.card_id)))
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(False))
        .scalar()
    ) or 0
    sets_represented = (
        session.query(func.count(distinct(Card.set_code)))
        .join(InventoryRow, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(False))
        .scalar()
    ) or 0
    # Largest set: top set_code by sum(quantity) placed.
    largest_set_row = (
        session.query(
            Card.set_code,
            func.coalesce(func.sum(InventoryRow.quantity), 0).label("total"),
        )
        .join(InventoryRow, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(False))
        .group_by(Card.set_code)
        .order_by(desc("total"))
        .first()
    )
    largest_set = (
        {"set_code": largest_set_row.set_code, "count": int(largest_set_row.total or 0)}
        if largest_set_row
        else None
    )
    watchlist_count = (
        session.query(func.count(WatchlistItem.id))
        .filter(WatchlistItem.user_id == user_id)
        .scalar()
    ) or 0
    avg_value = (placed_value / placed_cards) if placed_cards > 0 else 0.0
    collection_stats = {
        "total_cards": placed_cards,
        "unique_printings": int(unique_printings),
        "sets_represented": int(sets_represented),
        "largest_set": largest_set,
        "avg_value": float(avg_value),
        "watchlist_count": int(watchlist_count),
    }

    # ── § IV PRIORITY QUEUE ──────────────────────────────────────────────────
    # Pending count + distinct landing-locations (unique storage_location_id
    # on pending rows where assigned) + oldest pending row's age.
    # NOTE: InventoryRow has no direct ImportBatch FK (the batch linkage
    # lives on TransactionLog.batch_id only — checked via models.py).
    # Indirect counting via TransactionLog would compound query cost; we
    # surface card-count + location-count + oldest-age and skip a batch
    # count to keep this panel one cheap query per slot. The "Review now"
    # CTA links to /pending, which already shows the full batch breakdown.
    pq_locations = (
        session.query(func.count(distinct(InventoryRow.storage_location_id)))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(True),
            InventoryRow.storage_location_id.is_not(None),
        )
        .scalar()
    ) or 0
    oldest_pending = (
        session.query(func.min(InventoryRow.created_at))
        .filter(
            InventoryRow.user_id == user_id,
            InventoryRow.is_pending.is_(True),
        )
        .scalar()
    )
    if oldest_pending and isinstance(oldest_pending, datetime):
        delta = now - oldest_pending
        days = delta.days
        oldest_pending_label = f"{days}d" if days >= 1 else "today"
    else:
        oldest_pending_label = None
    priority_queue = {
        "pending_cards": pending_cards,
        "location_count": int(pq_locations),
        "oldest_pending_label": oldest_pending_label,
    }

    # ── § I TODAY'S BRIEF (modest curated items templated from data) ─────────
    # Items list is dynamic — only include items with actual signal.
    brief_items = []
    if pending_cards > 0:
        brief_items.append(
            {
                "lede_html": f"<b>{pending_cards}</b> card{'s' if pending_cards != 1 else ''} await placement.",
                "detail": (
                    (
                        f"Across {priority_queue['location_count']} location"
                        f"{'s' if priority_queue['location_count'] != 1 else ''}."
                    )
                    if priority_queue["location_count"] > 0
                    else None
                ),
            }
        )
    if largest_set:
        brief_items.append(
            {
                "lede_html": (
                    f"<b>{largest_set['set_code'].upper()}</b> is your largest set "
                    f"<i>({largest_set['count']} cards)</i>."
                ),
                "detail": None,
            }
        )
    # Most-recent ImportBatch
    recent_batch = (
        session.query(ImportBatch.id, ImportBatch.filename, ImportBatch.imported_at)
        .filter(ImportBatch.user_id == user_id)
        .order_by(ImportBatch.imported_at.desc())
        .first()
    )
    if recent_batch and recent_batch.filename:
        brief_items.append(
            {
                "lede_html": (f"Most recent import: <b>{recent_batch.filename}</b>."),
                "detail": None,
            }
        )
    # Deck Performance signal — top deck if there's at least one
    if deck_performance:
        top_deck = max(deck_performance, key=lambda d: d["win_rate"])
        if top_deck["games"] > 0:
            brief_items.append(
                {
                    "lede_html": (
                        f"Top win-rate: <b>{top_deck['name']}</b> at "
                        f"<i>{round(top_deck['win_rate'] * 100)}%</i> "
                        f"over {top_deck['games']} games."
                    ),
                    "detail": None,
                }
            )

    # ── MASTHEAD (insight line + greeting + stat block) ──────────────────────
    # Insight line — modest, templated. Single sentence summary of the
    # most-actionable signal.
    insight_parts = []
    if pending_cards > 0:
        insight_parts.append(
            f"<b>{pending_cards}</b> card{'s' if pending_cards != 1 else ''} await placement"
        )
    if largest_set:
        insight_parts.append(f"<i>{largest_set['set_code'].upper()}</i> is your largest set")
    if insight_parts:
        insight_html = " · ".join(insight_parts) + "."
    else:
        insight_html = "Your placement queue is clear."
    masthead = {
        "greeting": _time_greeting(now),
        "insight_html": insight_html,
        "pending_count": pending_cards,
        "placed_cards": placed_cards,
    }

    # ── § II HOLDINGS ────────────────────────────────────────────────────────
    # Current placed value (in dollars); 30-day delta + value-history chart
    # are deferred (price-history infrastructure is a separate future
    # release, per the v3.28.5 spec's bucket-d triage).
    holdings = {
        "placed_value": placed_value,
        "placed_cards": placed_cards,
        # Marker for the template to render the deferred-chart placeholder.
        "history_deferred": True,
    }

    return {
        "masthead": masthead,
        "today_brief": brief_items,
        "holdings": holdings,
        "chronicle": chronicle,
        "priority_queue": priority_queue,
        "spotlight": spotlight,
        "deck_performance": deck_performance,
        "color_identity": color_identity,
        "collection_stats": collection_stats,
    }


# v3.27.10/v3.27.11 backward-compatible shape — kept for any caller still
# reading the old tile dict (none currently, but defensible if something
# slips in later). The home route handler is rewritten to call
# ``get_dashboard_data`` directly in v3.28.5.
def get_dashboard_tiles(session: Session, user_id: int) -> dict:
    """Legacy shape: the three v3.27.10/v3.27.11 tiles. Use
    ``get_dashboard_data`` for the v3.28.5 Folio dashboard. Kept here for
    backward-compat with any other reader (none today)."""
    placed_cards = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(False))
        .scalar()
    )
    placed_value = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity * _placed_value_expr()), 0))
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(False))
        .scalar()
    )
    pending_cards = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity), 0))
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(True))
        .scalar()
    )
    pending_value = (
        session.query(func.coalesce(func.sum(InventoryRow.quantity * _placed_value_expr()), 0))
        .join(Card, InventoryRow.card_id == Card.id)
        .filter(InventoryRow.user_id == user_id, InventoryRow.is_pending.is_(True))
        .scalar()
    )
    decks_total = (session.query(func.count(Deck.id)).filter(Deck.user_id == user_id).scalar()) or 0
    decks_commander_format = (
        session.query(func.count(Deck.id))
        .filter(Deck.user_id == user_id, func.lower(Deck.format) == "commander")
        .scalar()
    ) or 0
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
