"""deck_sim_results — aggregated AI-simulation strength evidence.

Covers: seed insert + upsert-in-place, missing-deck tolerance, missing-file
no-op, the shipped seed file's validity, and delete_deck cleanup.

    pytest tests/test_deck_sim_results.py
"""

from __future__ import annotations

import itertools
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.legacy_tables  # noqa: F401 — registers the raw tables delete_deck cleans up
from app import deck_service
from app.db import Base
from app.models import DeckSimResult, StorageLocation, User

_seq = itertools.count(1)


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _deck(s):
    u = User(username=f"u{next(_seq)}", password_hash="x")
    s.add(u)
    s.flush()
    loc = StorageLocation(user_id=u.id, name=f"deck loc {next(_seq)}", type="deck")
    s.add(loc)
    s.flush()
    return deck_service.create_deck(s, user_id=u.id, name=f"Deck {next(_seq)}"), u


def test_seed_sim_results(tmp_path):
    s = _fresh_session()
    deck, _ = _deck(s)
    seed = [
        {"deck_id": deck.id, "run_label": "run-1", "strategy": "random", "wins": 3, "games": 12},
        {"deck_id": deck.id, "run_label": "run-1", "strategy": "core", "wins": 5, "games": 10},
        {"deck_id": 999999, "run_label": "run-1", "strategy": "random", "wins": 0, "games": 4},
    ]
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(seed), encoding="utf-8")

    stats = deck_service.seed_sim_results(s, seed_path=str(path))
    assert stats == {"seeded": 2, "missing_decks": 1}
    assert s.query(DeckSimResult).count() == 2

    # Re-seed with corrected numbers updates in place — no duplicate rows.
    seed[0]["wins"] = 4
    path.write_text(json.dumps(seed), encoding="utf-8")
    deck_service.seed_sim_results(s, seed_path=str(path))
    assert s.query(DeckSimResult).count() == 2
    row = (
        s.query(DeckSimResult)
        .filter_by(deck_id=deck.id, run_label="run-1", strategy="random")
        .one()
    )
    assert row.wins == 4

    # Missing file is a clean no-op.
    assert deck_service.seed_sim_results(s, seed_path=str(tmp_path / "absent.json")) == {
        "seeded": 0,
        "missing_decks": 0,
    }


def test_shipped_sim_seed_file_is_valid():
    with open(deck_service.SIM_RESULTS_SEED, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data, "shipped sim-results seed is empty"
    for entry in data:
        assert set(entry) == {"deck_id", "run_label", "strategy", "wins", "games"}
        assert 0 <= entry["wins"] <= entry["games"]
        assert len(entry["run_label"]) <= 64 and len(entry["strategy"]) <= 32


def test_delete_deck_removes_sim_results():
    s = _fresh_session()
    deck, user = _deck(s)
    s.add(DeckSimResult(deck_id=deck.id, run_label="r", strategy="random", wins=1, games=4))
    s.commit()
    assert s.query(DeckSimResult).count() == 1

    deck_service.delete_deck(s, deck.id, user.id)
    s.commit()
    assert s.query(DeckSimResult).count() == 0


def test_measured_strength_grouping():
    """Panel data: run labels grouped with core split, focus delta, hidden-when-empty."""
    s = _fresh_session()
    deck, _ = _deck(s)
    assert deck_service.measured_strength(s, deck) is None  # nothing measured -> hidden

    for strategy, wins, games in (("core", 3, 20), ("random", 7, 19)):
        s.add(
            DeckSimResult(
                deck_id=deck.id, run_label="gauntlet-x", strategy=strategy, wins=wins, games=games
            )
        )
    s.add(
        DeckSimResult(deck_id=deck.id, run_label="archenemy-y", strategy="core", wins=6, games=13)
    )
    s.commit()

    ms = deck_service.measured_strength(s, deck)
    assert [r["label"] for r in ms["sim_runs"]] == ["archenemy-y", "gauntlet-x"]
    g = ms["sim_runs"][1]
    assert g["wins"] == 10 and g["games"] == 39  # pooled across strategies
    assert g["core_wins"] == 3 and g["core_games"] == 20
    assert abs(ms["focus_delta"] - (6 / 13 - 3 / 20)) < 1e-9
    assert ms["global_stats"] == []  # no commander stats rows
