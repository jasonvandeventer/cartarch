"""Foundation tests: init preserves identity, resolver reuses the mirror,
repository round-trips atomically, refresh preserves decisions.

Run: `python -m pytest deckbooks/tests` (kept OUT of the app's tests/ tree so the
prototype is not part of the app's CI gate).
"""

from __future__ import annotations

import sqlite3

import pytest

from deckbooks import image_resolver, repository
from deckbooks.init_deck import BELLO_DECK_COPY, BELLO_MUSEUM, initialize
from deckbooks.models import curation_complete, deck_copy_complete


def test_init_seeds_bello_and_preserves_exact_printings(tmp_path, monkeypatch):
    # Point the repo + reader at throwaway locations so the real seed isn't touched.
    _isolate(tmp_path, monkeypatch)

    summary = initialize("osha-violation", refresh=False)
    assert summary["action"] == "created"
    assert summary["finalized"] == 1

    cards = repository.load_cards("osha-violation")
    bello = next(c for c in cards if c["card_name"].startswith("Bello"))

    # Exact Scryfall UUIDs + finish are preserved (Section 8) — not invented.
    assert bello["decision"]["selected_printing"] == {
        "scryfall_id": BELLO_DECK_COPY,
        "finish": "foil",
    }
    assert bello["decision"]["museum_printing"]["scryfall_id"] == BELLO_MUSEUM
    assert curation_complete(bello) and deck_copy_complete(bello)

    # Every other deck card is pending / not finalized (no false finalization).
    others = [c for c in cards if not c["card_name"].startswith("Bello")]
    assert others and all(not curation_complete(c) for c in others)


def test_refresh_preserves_a_finalized_decision(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    initialize("osha-violation", refresh=False)

    # Simulate a curator finalizing a second card, then a deck re-sync.
    cards = repository.load_cards("osha-violation")
    victim = next(c for c in cards if c["card_name"] == "Sol Ring")
    victim["decision"]["status"] = "keep"
    victim["decision"]["finalized"] = True
    repository.save_cards("osha-violation", cards)

    initialize("osha-violation", refresh=True)
    after = {c["card_name"]: c for c in repository.load_cards("osha-violation")}
    assert after["Sol Ring"]["decision"]["finalized"] is True  # survived the refresh


def test_resolver_builds_mirror_url_and_reads_metadata_offline(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # The image URL is a pure contract — no network, no download.
    url = image_resolver.mirror_image_url(BELLO_DECK_COPY, size="large")
    assert url.endswith(f"/{BELLO_DECK_COPY}/large.jpg")
    assert image_resolver.mirror_image_url(BELLO_DECK_COPY, face="back").find("/back/") != -1

    # Metadata comes from the local scryfall_cards cache (offline).
    meta = image_resolver.get_printing(BELLO_DECK_COPY)
    assert meta is not None and meta.set_code == "blc" and meta.collector_number == "1"


def test_repository_rejects_a_path_escaping_id():
    with pytest.raises(ValueError, match="escapes"):
        repository.load_deckbook("../../etc")


def _isolate(tmp_path, monkeypatch):
    """Redirect the prototype's data dir to tmp, and build a tiny fixture
    Cartarch DB (the deck + the four printings the seed needs)."""
    from deckbooks import config

    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(repository, "DATA_DIR", data_dir)
    monkeypatch.setattr(repository, "deckbook_dir", lambda i=config.DECKBOOK_ID: data_dir / i)

    db = tmp_path / "cartarch.db"
    monkeypatch.setattr(config, "CARTARCH_DB", db)
    monkeypatch.setattr(image_resolver, "CARTARCH_DB", db)
    _seed_fixture_db(db)


def _seed_fixture_db(db):
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE decks(id INTEGER PRIMARY KEY, name TEXT, storage_location_id INTEGER);
        CREATE TABLE cards(id INTEGER PRIMARY KEY, name TEXT, scryfall_id TEXT);
        CREATE TABLE inventory_rows(id INTEGER PRIMARY KEY, card_id INTEGER,
            storage_location_id INTEGER, finish TEXT, quantity INTEGER, is_proxy INTEGER);
        CREATE TABLE scryfall_cards(scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT,
            set_name TEXT, collector_number TEXT, rarity TEXT, type_line TEXT,
            image_url TEXT, layout TEXT, price_usd TEXT, price_usd_foil TEXT,
            price_usd_etched TEXT, frame_effects TEXT, full_art INTEGER, set_type TEXT,
            border_color TEXT, promo_types TEXT);
        INSERT INTO decks VALUES (11, 'Bello, Bard of the Brambles', 24);
        """
    )
    seed = [
        (
            "Bello, Bard of the Brambles",
            "31e4b7a1-b377-49d2-a92e-4bcb0db35f16",
            "foil",
            "blc",
            "1",
            "Legendary Creature — Raccoon Bard",
        ),
        ("Sol Ring", "e5ba8c01-b6f5-486d-b300-cbae2c2b5edf", "foil", "pf19", "7", "Artifact"),
        (
            "Arcane Signet",
            "28180667-cc1e-4f64-9a69-00425ef85ba0",
            "normal",
            "blc",
            "127",
            "Artifact",
        ),
        (
            "Forest",
            "43b3be4a-973d-4aeb-a94e-37e2710ac178",
            "normal",
            "blb",
            "377",
            "Basic Land — Forest",
        ),
    ]
    for name, sid, finish, setc, coll, tline in seed:
        conn.execute("INSERT INTO cards(name, scryfall_id) VALUES (?, ?)", (name, sid))
        cid = conn.execute("SELECT id FROM cards WHERE scryfall_id=?", (sid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO inventory_rows(card_id, storage_location_id, finish, quantity, is_proxy)"
            " VALUES (?, 24, ?, 1, 0)",
            (cid, finish),
        )
        conn.execute(
            "INSERT INTO scryfall_cards(scryfall_id, name, set_code, set_name, collector_number,"
            " rarity, type_line, image_url, layout) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, name, setc, setc.upper(), coll, "rare", tline, "http://x/y.jpg", "normal"),
        )
    # The museum printing must resolve too (metadata lookup on the card detail page).
    conn.execute(
        "INSERT INTO scryfall_cards(scryfall_id, name, set_code, set_name, collector_number,"
        " rarity, type_line, image_url, layout) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            BELLO_MUSEUM,
            "Bello, Bard of the Brambles",
            "blc",
            "BLC",
            "101",
            "mythic",
            "Legendary Creature — Raccoon Bard",
            "http://x/z.jpg",
            "normal",
        ),
    )
    conn.commit()
    conn.close()
