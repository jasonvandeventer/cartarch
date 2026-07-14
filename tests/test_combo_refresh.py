"""#103 Phase A — combo-refresh daemon: fingerprint change detection, persist,
bracket recompute, and failure semantics (Spellbook down ≠ no combos).

Network is never touched: ``app.spellbook.fetch_deck_combos`` is monkeypatched
at its import site inside ``compute_deck_combos``.
"""

from __future__ import annotations

import itertools
import json

import app.legacy_tables  # noqa: F401 — registers the raw deck_bracket_* tables on Base.metadata
from app import combo_refresh_service as crs
from app.models import Card, Deck, DeckCombo, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _seed_bracket_rules(db):
    """Minimal commander_bracket_rules rows (the estimator loads them by
    rules_version) — the prod seed is scripts/migrate_v3_15_0_seed_bracket_rules."""
    from sqlalchemy import text

    from app.bracket_v2_service import RULES_VERSION

    for b in range(1, 6):
        db.execute(
            text(
                "INSERT INTO commander_bracket_rules (bracket, name, description,"
                " max_game_changers, allows_mass_land_denial, allows_extra_turn_chains,"
                " allows_two_card_combos, allows_combo_as_primary, competitive,"
                " rules_version, effective_date)"
                " VALUES (:b, :n, '', :gc, :x, :x, :x, :x, :x, :v, CURRENT_DATE)"
            ),
            {"b": b, "n": f"Tier {b}", "gc": 999 if b == 5 else b, "x": b == 5, "v": RULES_VERSION},
        )
    db.commit()


def _user(db) -> User:
    u = User(username=f"u{next(_seq)}@ex.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


def _deck(db, user_id, name="Deck") -> Deck:
    loc = StorageLocation(user_id=user_id, name=f"{name}-{next(_seq)}", type="deck", mode="managed")
    db.add(loc)
    db.flush()
    d = Deck(user_id=user_id, name=f"{name}-{next(_seq)}", storage_location_id=loc.id)
    db.add(d)
    db.flush()
    return d


def _add_card(db, user_id, deck, name, role=None) -> InventoryRow:
    c = Card(
        scryfall_id=f"sid-{next(_seq)}",
        name=name,
        set_code="tst",
        set_name="S",
        collector_number=str(next(_seq)),
        rarity="rare",
        type_line="Creature",
        oracle_text="x",
        color_identity="",
        set_type="expansion",
    )
    db.add(c)
    db.flush()
    r = InventoryRow(
        user_id=user_id,
        card_id=c.id,
        finish="normal",
        quantity=1,
        storage_location_id=deck.storage_location_id,
        role=role,
        is_pending=False,
    )
    db.add(r)
    db.flush()
    r.card = c
    return r


FAKE_COMBOS = {
    "included": [
        {
            "id": "1",
            "card_names": ["A", "B"],
            "owned": ["A", "B"],
            "missing": [],
            "description": "win",
            "results": ["Infinite"],
            "prerequisites": "",
            "mana_needed": "",
            "popularity": 1,
        }
    ]
}


def _patch_fetch(monkeypatch, result, calls=None):
    def fake(main_names, commander_names):
        if calls is not None:
            calls.append((sorted(main_names), sorted(commander_names)))
        return result

    monkeypatch.setattr("app.spellbook.fetch_deck_combos", fake)


def test_fingerprint_changes_on_card_add_stable_otherwise(db):
    u = _user(db)
    d = _deck(db, u.id)
    _add_card(db, u.id, d, "Sol Ring")
    rows = crs.resolved_deck_rows(db, d, u.id)
    fp1 = crs.deck_combo_fingerprint(rows)
    assert fp1 == crs.deck_combo_fingerprint(rows)  # stable
    _add_card(db, u.id, d, "Basalt Monolith")
    rows2 = crs.resolved_deck_rows(db, d, u.id)
    assert crs.deck_combo_fingerprint(rows2) != fp1  # changed


def test_refresh_persists_combos_and_bracket(db, monkeypatch):
    _seed_bracket_rules(db)
    u = _user(db)
    d = _deck(db, u.id)
    _add_card(db, u.id, d, "Kiki-Jiki, Mirror Breaker", role="commander")
    _add_card(db, u.id, d, "Zealous Conscripts")
    calls = []
    _patch_fetch(monkeypatch, FAKE_COMBOS, calls)

    assert crs.refresh_stale_deck_combos(db) == 1
    row = db.query(DeckCombo).filter(DeckCombo.deck_id == d.id).one()
    assert json.loads(row.payload) == FAKE_COMBOS
    assert calls == [(["Zealous Conscripts"], ["Kiki-Jiki, Mirror Breaker"])]
    # Bracket estimate persisted alongside (combo signal restored).
    from sqlalchemy import text

    est = db.execute(
        text("SELECT final_bracket FROM deck_bracket_estimates WHERE deck_id = :d"), {"d": d.id}
    ).first()
    assert est is not None
    # Reader helper round-trips.
    assert crs.load_deck_combos(db, d.id) == FAKE_COMBOS


def test_unchanged_deck_makes_no_network_call(db, monkeypatch):
    u = _user(db)
    d = _deck(db, u.id)
    _add_card(db, u.id, d, "Sol Ring")
    _patch_fetch(monkeypatch, {"included": []})
    assert crs.refresh_stale_deck_combos(db) == 1

    def boom(*a, **k):
        raise AssertionError("fetch called for a fresh deck")

    monkeypatch.setattr("app.spellbook.fetch_deck_combos", boom)
    assert crs.refresh_stale_deck_combos(db) == 0  # fresh → zero network


def test_fetch_failure_persists_nothing_and_retries(db, monkeypatch):
    u = _user(db)
    d = _deck(db, u.id)
    _add_card(db, u.id, d, "Sol Ring")
    _patch_fetch(monkeypatch, None)  # Spellbook down
    assert crs.refresh_stale_deck_combos(db) == 0
    assert db.query(DeckCombo).count() == 0  # nothing persisted

    _patch_fetch(monkeypatch, {"included": []})  # back up → retried next pass
    assert crs.refresh_stale_deck_combos(db) == 1
    assert db.query(DeckCombo).count() == 1


def test_card_change_triggers_refresh_and_limit_bounds_batch(db, monkeypatch):
    u = _user(db)
    decks = [_deck(db, u.id) for _ in range(3)]
    for d in decks:
        _add_card(db, u.id, d, f"Card {next(_seq)}")
    _patch_fetch(monkeypatch, {"included": []})
    # limit=2 → two per pass, third on the next.
    assert crs.refresh_stale_deck_combos(db, limit=2) == 2
    assert crs.refresh_stale_deck_combos(db, limit=2) == 1
    # Edit one deck → only it refreshes.
    _add_card(db, u.id, decks[0], "New Card")
    assert crs.refresh_stale_deck_combos(db) == 1


def test_empty_deck_persists_without_network(db, monkeypatch):
    u = _user(db)
    _deck(db, u.id)  # no cards

    def boom(*a, **k):
        raise AssertionError("network for an empty deck")

    monkeypatch.setattr("app.spellbook.fetch_deck_combos", boom)
    assert crs.refresh_stale_deck_combos(db) == 1  # persisted the empty result
    assert json.loads(db.query(DeckCombo).one().payload) == {"included": []}


# ── Phase B — surfaces read persisted-only, with staleness ───────────────────


def test_deck_combo_status_staleness(db, monkeypatch):
    u = _user(db)
    d = _deck(db, u.id)
    _add_card(db, u.id, d, "Sol Ring")
    rows = crs.resolved_deck_rows(db, d, u.id)
    # Never computed → combos None, stale.
    st = crs.deck_combo_status(db, d.id, rows)
    assert st["combos"] is None and st["stale"] is True
    # Computed → fresh.
    _patch_fetch(monkeypatch, FAKE_COMBOS)
    crs.refresh_stale_deck_combos(db)
    st = crs.deck_combo_status(db, d.id, crs.resolved_deck_rows(db, d, u.id))
    assert st["combos"] == FAKE_COMBOS and st["stale"] is False
    # Deck edited → stale until the daemon catches up.
    _add_card(db, u.id, d, "Mana Vault")
    st = crs.deck_combo_status(db, d.id, crs.resolved_deck_rows(db, d, u.id))
    assert st["combos"] == FAKE_COMBOS and st["stale"] is True


def test_panels_fragment_renders_win_conditions(client, db, user, monkeypatch):
    d = _deck(db, user.id)
    _add_card(db, user.id, d, "Kiki-Jiki, Mirror Breaker", role="commander")
    db.commit()
    # Before the daemon has computed: panel hidden.
    r = client.get(f"/decks/{d.id}/panels")
    assert r.status_code == 200 and "combos-panel" not in r.text
    # Daemon computes → panel renders from the persisted row.
    _patch_fetch(monkeypatch, FAKE_COMBOS)
    crs.refresh_stale_deck_combos(db)
    db.commit()
    r = client.get(f"/decks/{d.id}/panels")
    assert "combos-panel" in r.text
    assert "A + B" in r.text  # the combo's card names
    assert "deck changed" not in r.text  # fresh
    # Edit the deck → staleness chip appears (still renders the old combos).
    _add_card(db, user.id, d, "Zealous Conscripts")
    db.commit()
    r = client.get(f"/decks/{d.id}/panels")
    assert "combos-panel" in r.text and "deck changed" in r.text


def test_decks_list_bracket_chip(client, db, user, monkeypatch):
    _seed_bracket_rules(db)
    d = _deck(db, user.id)
    _add_card(db, user.id, d, "Sol Ring")
    _patch_fetch(monkeypatch, {"included": []})
    crs.refresh_stale_deck_combos(db)  # persists combos + a bracket estimate
    db.commit()
    r = client.get("/decks")
    assert r.status_code == 200
    assert "deck-bracket-chip" in r.text  # chip rendered from the persisted row


def test_deck_detail_hero_badge(client, db, user, monkeypatch):
    _seed_bracket_rules(db)
    d = _deck(db, user.id)
    _add_card(db, user.id, d, "Sol Ring")
    _patch_fetch(monkeypatch, {"included": []})
    crs.refresh_stale_deck_combos(db)
    db.commit()
    r = client.get(f"/decks/{d.id}")
    assert r.status_code == 200 and "deck-bracket-chip" in r.text


def test_delete_deck_cleans_combo_row(db, monkeypatch):
    from app.deck_service import delete_deck

    u = _user(db)
    d = _deck(db, u.id)
    _add_card(db, u.id, d, "Sol Ring")
    _patch_fetch(monkeypatch, {"included": []})
    crs.refresh_stale_deck_combos(db)
    assert db.query(DeckCombo).count() == 1
    assert delete_deck(db, d.id, u.id)
    assert db.query(DeckCombo).count() == 0
