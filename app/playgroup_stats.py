"""Harvest worldwide per-commander priors from playgroup.gg's public API.

Only the OPEN endpoints are used (``/commanders/by_name/{name}`` — no auth, no
API key). Game-level and user-level playgroup.gg data is deliberately out of
scope: it is playgroup-scoped, and this playgroup's real games live in
Cartarch's own ``games`` tables.

Runs off the request path via a daemon loop in ``app.main`` (the price-ingest
pattern): small batches, one polite request per second, weekly staleness. A
commander playgroup.gg doesn't know (404) still gets a row with a null payload
and a fresh ``fetched_at`` so the loop doesn't retry it every pass.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Card, CommanderGlobalStat, DeckCommander
from app.timeutil import utc_now

API_BASE = "https://playgroup.gg/api/public/v1"
FRESH_FOR = timedelta(days=7)
REQUEST_GAP_SECONDS = 1.0
TIMEOUT_SECONDS = 20


def deck_commander_names(session: Session) -> list[str]:
    """Distinct commander card names across all decks (partners included)."""
    rows = session.execute(
        select(Card.name).join(DeckCommander, DeckCommander.card_id == Card.id).distinct()
    )
    return sorted({name for (name,) in rows})


def stale_names(session: Session, limit: int) -> list[str]:
    cutoff = utc_now() - FRESH_FOR
    fresh = {
        row.commander_name
        for row in session.query(CommanderGlobalStat)
        .filter(CommanderGlobalStat.fetched_at >= cutoff)
        .all()
    }
    return [n for n in deck_commander_names(session) if n not in fresh][:limit]


def fetch_commander(name: str) -> dict | None:
    """One open-API lookup. Returns the parsed payload, or None on 404."""
    resp = requests.get(
        f"{API_BASE}/commanders/by_name/{requests.utils.quote(name)}",
        timeout=TIMEOUT_SECONDS,
        headers={"User-Agent": "cartarch (deck stats; contact: cartarch.com)"},
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def upsert_stat(session: Session, name: str, payload: dict | None) -> CommanderGlobalStat:
    row = (
        session.query(CommanderGlobalStat)
        .filter(CommanderGlobalStat.commander_name == name)
        .first()
    )
    if row is None:
        row = CommanderGlobalStat(commander_name=name)
        session.add(row)
    row.fetched_at = utc_now()
    if payload is None:
        row.payload = None
        return row
    stats = payload.get("stats") or {}
    row.pg_commander_id = payload.get("id")
    row.elo = payload.get("cached_elo")
    row.global_rank = payload.get("global_rank")
    row.games_won = stats.get("games_won")
    row.games_lost = stats.get("games_lost")
    row.win_rate = stats.get("win_rate")
    row.average_wins_by_turn = stats.get("average_wins_by_turn")
    row.decks_count = stats.get("decks_count")
    row.games_count = stats.get("games_count")
    row.payload = json.dumps(payload, ensure_ascii=False)
    return row


def refresh_batch(session: Session, batch: int = 5, sleep=time.sleep) -> int:
    """Fetch up to ``batch`` stale commanders. Returns how many were processed.

    One failed fetch aborts the batch quietly (network trouble shouldn't spin
    the loop); whatever was upserted before the failure is committed.
    """
    names = stale_names(session, batch)
    processed = 0
    for name in names:
        try:
            payload = fetch_commander(name)
        except requests.RequestException:
            break
        upsert_stat(session, name, payload)
        session.commit()
        processed += 1
        if processed < len(names):
            sleep(REQUEST_GAP_SECONDS)
    return processed
