"""Oracle catalog ingest tests (Momir Sim #109).

The Scryfall bulk fetch is replaced with an in-memory list of oracle entries —
no live network. Covers: the is_momir_legal filter policy (creature-only, layout
/ vintage / set exclusions), multi-face FRONT-face extraction, upsert-by-oracle_id
idempotency, JSON keyword/color passthrough, valid-MV reporting, and the Scryfall
User-Agent contract.
"""

from __future__ import annotations

import json

from app import live_game_service
from app.jobs import oracle_ingest
from app.jobs.oracle_ingest import extract, run_ingest
from app.models import OracleCatalog


def _entry(oracle_id, name, cmc, type_line="Creature — Bear", **over):
    e = {
        "oracle_id": oracle_id,
        "id": f"sid-{oracle_id}",
        "name": name,
        "cmc": cmc,
        "type_line": type_line,
        "mana_cost": "{1}{G}",
        "oracle_text": "",
        "keywords": [],
        "power": "2",
        "toughness": "2",
        "colors": ["G"],
        "color_identity": ["G"],
        "layout": "normal",
        "legalities": {"vintage": "legal"},
        "set_type": "expansion",
    }
    e.update(over)
    return e


# ── filter policy ────────────────────────────────────────────────────────────


def test_extract_skips_non_creatures_and_missing_oracle_id():
    assert extract(_entry("o1", "Bolt", 1, type_line="Instant")) is None
    e = _entry("o2", "Nameless", 1)
    del e["oracle_id"]
    assert extract(e) is None


def test_is_momir_legal_policy():
    assert extract(_entry("o1", "Legal Bear", 2))["is_momir_legal"] is True
    # Excluded layout, banned-in-vintage, and memorabilia set are creatures but
    # NOT Momir-legal (still ingested, just flagged False).
    assert extract(_entry("o2", "Token Bear", 2, layout="token"))["is_momir_legal"] is False
    assert (
        extract(_entry("o3", "Acorn", 2, legalities={"vintage": "not_legal"}))["is_momir_legal"]
        is False
    )
    assert extract(_entry("o4", "Oversized", 2, set_type="memorabilia"))["is_momir_legal"] is False


def test_extract_multiface_uses_front_face_but_root_cmc_and_id():
    dfc = _entry(
        "o1",
        "Root // Back",  # root name is the joined name; front face wins
        4,
        type_line="Sorcery // Creature — Elemental",  # root type; front is the creature
        card_faces=[
            {
                "name": "Front Creature",
                "type_line": "Creature — Elemental",
                "oracle_text": "Trample",
                "power": "5",
                "toughness": "5",
                "mana_cost": "{3}{R}",
                "colors": ["R"],
            },
            {"name": "Back", "type_line": "Sorcery"},
        ],
    )
    v = extract(dfc)
    assert v["name"] == "Front Creature"
    assert v["power"] == "5" and v["toughness"] == "5"
    assert v["cmc"] == 4  # root-level
    assert v["scryfall_id"] == "sid-o1"  # root id
    assert v["is_momir_legal"] is True  # front type_line carries "Creature"


# ── ingest pipeline ──────────────────────────────────────────────────────────


def test_run_ingest_upserts_and_counts(db):
    cards = [
        _entry("o1", "Bear", 2, keywords=["Trample"]),
        _entry("o2", "Bolt", 1, type_line="Instant"),  # skipped (non-creature)
        _entry("o3", "Token", 0, layout="token"),  # ingested, not legal
    ]
    stats = run_ingest(db, cards)
    assert stats == {"inserted": 2, "updated": 0, "skipped": 1}

    bear = db.query(OracleCatalog).filter_by(oracle_id="o1").one()
    assert bear.name == "Bear"
    assert json.loads(bear.keywords) == ["Trample"]
    assert json.loads(bear.color_identity) == ["G"]
    assert bear.is_momir_legal is True

    # Re-ingest with a changed P/T → update in place, no duplicate row.
    stats = run_ingest(db, [_entry("o1", "Bear", 2, power="9")])
    assert stats == {"inserted": 0, "updated": 1, "skipped": 0}
    assert db.query(OracleCatalog).filter_by(oracle_id="o1").one().power == "9"
    assert db.query(OracleCatalog).count() == 2


def test_stream_sends_non_default_user_agent(monkeypatch):
    # Scryfall rejects the default requests/urllib User-Agent with HTTP 400
    # (generic_user_agent) — the whole ingest died on the first request in prod.
    # Pin that stream_oracle_cards sends a real, descriptive UA.
    seen = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": [
                    {
                        "type": "oracle_cards",
                        "jsonl_download_uri": "http://x/oracle.jsonl.gz",
                    }
                ]
            }

    def _fake_get(url, headers=None, **kw):
        seen.append(headers or {})
        return _Resp()

    monkeypatch.setattr(oracle_ingest.requests, "get", _fake_get)
    monkeypatch.setattr(oracle_ingest, "_stream_jsonl", lambda uri: iter(()))
    list(oracle_ingest.stream_oracle_cards())

    assert seen, "no HTTP request was made"
    ua = seen[0].get("User-Agent", "")
    assert ua and "python" not in ua.lower() and "requests" not in ua.lower()


def test_run_ingest_feeds_valid_mvs_live(db):
    run_ingest(db, [_entry("o1", "One", 1), _entry("o2", "Three", 3)])
    assert live_game_service.valid_momir_mvs(db) == {1, 3}
    # A later ingest adds a new MV; the helper (queried live) reflects it.
    run_ingest(db, [_entry("o3", "Five", 5)])
    assert live_game_service.valid_momir_mvs(db) == {1, 3, 5}
