"""playgroup.gg global commander priors (commander_global_stats).

Covers: name discovery from deck_commanders, upsert from a real-shaped payload,
404 handling (row recorded, no weekly retry hammering), staleness selection,
and network-failure batch abort.

    pytest tests/test_playgroup_stats.py
"""

from __future__ import annotations

import itertools
import json
from datetime import timedelta

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import deck_service, playgroup_stats
from app.db import Base
from app.models import Card, CommanderGlobalStat, DeckCommander, StorageLocation, User
from app.timeutil import utc_now

_seq = itertools.count(1)

PAYLOAD = {
    "id": 3468,
    "name": "Anti-Venom, Horrifying Healer",
    "cached_elo": 1511,
    "global_rank": 1754,
    "stats": {
        "games_won": 318,
        "games_lost": 904,
        "win_rate": 25,
        "average_wins_by_turn": 10,
        "decks_count": 376,
        "games_count": 1222,
    },
}


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _deck_with_commander(s, name):
    u = User(username=f"u{next(_seq)}", password_hash="x")
    s.add(u)
    s.flush()
    loc = StorageLocation(user_id=u.id, name=f"loc {next(_seq)}", type="deck")
    s.add(loc)
    s.flush()
    deck = deck_service.create_deck(s, user_id=u.id, name=f"Deck {next(_seq)}")
    card = Card(
        name=name,
        scryfall_id=f"sf-{next(_seq)}",
        set_code="tst",
        set_name="Test",
        collector_number=str(next(_seq)),
        rarity="rare",
    )
    s.add(card)
    s.flush()
    s.add(DeckCommander(deck_id=deck.id, card_id=card.id))
    s.flush()
    return deck


def test_names_and_upsert():
    s = _fresh_session()
    _deck_with_commander(s, "Anti-Venom, Horrifying Healer")
    assert playgroup_stats.deck_commander_names(s) == ["Anti-Venom, Horrifying Healer"]

    row = playgroup_stats.upsert_stat(s, "Anti-Venom, Horrifying Healer", PAYLOAD)
    s.commit()
    assert row.elo == 1511 and row.games_won == 318 and row.win_rate == 25
    assert json.loads(row.payload)["id"] == 3468

    # Upsert updates in place — one row per commander.
    playgroup_stats.upsert_stat(s, "Anti-Venom, Horrifying Healer", PAYLOAD)
    s.commit()
    assert s.query(CommanderGlobalStat).count() == 1


def test_404_recorded_and_not_retried():
    s = _fresh_session()
    _deck_with_commander(s, "Some Homebrew Commander")
    playgroup_stats.upsert_stat(s, "Some Homebrew Commander", None)
    s.commit()
    row = s.query(CommanderGlobalStat).one()
    assert row.payload is None and row.elo is None
    # Fresh fetched_at keeps it out of the stale list for a week.
    assert playgroup_stats.stale_names(s, 10) == []


def test_staleness_selection():
    s = _fresh_session()
    _deck_with_commander(s, "Old Commander")
    _deck_with_commander(s, "New Commander")
    old = playgroup_stats.upsert_stat(s, "Old Commander", None)
    old.fetched_at = utc_now() - timedelta(days=8)
    s.commit()
    assert playgroup_stats.stale_names(s, 10) == ["New Commander", "Old Commander"]
    assert playgroup_stats.stale_names(s, 1) == ["New Commander"]


def test_refresh_batch_aborts_on_network_error(monkeypatch):
    s = _fresh_session()
    _deck_with_commander(s, "A Commander")
    _deck_with_commander(s, "B Commander")

    calls = []

    def flaky(name):
        calls.append(name)
        if len(calls) == 2:
            raise requests.ConnectionError("down")
        return PAYLOAD

    monkeypatch.setattr(playgroup_stats, "fetch_commander", flaky)
    processed = playgroup_stats.refresh_batch(s, batch=5, sleep=lambda _s: None)
    assert processed == 1  # first upserted+committed, second aborted the batch
    assert s.query(CommanderGlobalStat).count() == 1
