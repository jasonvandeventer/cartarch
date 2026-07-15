"""#123 — GC list sync: date-real versions, delta detection, floor staleness.

History is never rewritten: adds are new rows stamped with a fresh dated
rules_version; removes flip active + date_removed on the existing row. A list
change makes persisted floors stale (estimate stamp != gc_list_version), and
the daemon re-floors them from persisted combos with zero network.
"""

from __future__ import annotations

import itertools

from sqlalchemy import text

import app.legacy_tables  # noqa: F401 — registers the raw bracket tables on Base.metadata
from app.bracket_v2_service import (
    GC_LIST_BASE_VERSION,
    RULES_VERSION,
    estimate_bracket_v2,
    gc_list_version,
    load_persisted_estimate,
    persist_estimate,
)
from app.jobs.gc_sync import apply_game_changer_sync, diff_game_changers
from app.models import Card, Deck, InventoryRow, StorageLocation, User

_seq = itertools.count(1)


def _gc(db, name, version="2026-02-09", active=True):
    db.execute(
        text(
            "INSERT INTO game_changer_cards (card_name, source, date_added, active, rules_version)"
            " VALUES (:n, 'seed', :d, :a, :v)"
        ),
        {"n": name, "d": version, "a": active, "v": version},
    )


def _gc_rows(db):
    return {
        r[0]: {"active": bool(r[1]), "version": r[2], "removed": r[3]}
        for r in db.execute(
            text(
                "SELECT card_name, active, rules_version, date_removed"
                " FROM game_changer_cards ORDER BY id"
            )
        )
    }


# --- version derivation ---------------------------------------------------------


def test_gc_list_version_empty_table_is_base(db):
    assert gc_list_version(db) == GC_LIST_BASE_VERSION


def test_gc_list_version_tracks_adds_and_removals(db):
    _gc(db, "Rhystic Study")
    db.commit()
    assert gc_list_version(db) == "2026-02-09"

    # an add stamps a newer version
    apply_game_changer_sync(db, ["Rhystic Study", "New Menace"], today="2026-06-10")
    assert gc_list_version(db) == "2026-06-10"

    # a removals-only sync still bumps (via date_removed)
    apply_game_changer_sync(db, ["Rhystic Study"], today="2026-07-01")
    assert gc_list_version(db) == "2026-07-01"


# --- diff + apply ----------------------------------------------------------------


def test_diff_detects_simulated_delta(db):
    _gc(db, "Rhystic Study")
    _gc(db, "Old Threat")
    _gc(db, "Long Gone", active=False)  # inactive rows are not "local list"
    db.commit()

    delta = diff_game_changers(db, ["Rhystic Study", "Brand New"])
    assert delta == {"adds": ["Brand New"], "removes": ["Old Threat"], "unchanged": 1}


def test_apply_stamps_adds_and_never_rewrites_history(db):
    _gc(db, "Rhystic Study")
    _gc(db, "Old Threat")
    db.commit()

    result = apply_game_changer_sync(db, ["Rhystic Study", "Brand New"], today="2026-06-10")
    assert result["applied"] is True
    assert result["rules_version"] == "2026-06-10"

    rows = _gc_rows(db)
    # add: new row, new dated stamp
    assert rows["Brand New"] == {"active": True, "version": "2026-06-10", "removed": None}
    # remove: deactivated with date_removed, ORIGINAL stamp preserved
    assert rows["Old Threat"]["active"] is False
    assert rows["Old Threat"]["version"] == "2026-02-09"
    assert str(rows["Old Threat"]["removed"]) == "2026-06-10"
    # unchanged rows untouched
    assert rows["Rhystic Study"] == {"active": True, "version": "2026-02-09", "removed": None}


def test_apply_no_delta_is_a_no_op(db):
    _gc(db, "Rhystic Study")
    db.commit()
    result = apply_game_changer_sync(db, ["Rhystic Study"], today="2026-06-10")
    assert result["applied"] is False
    assert result["rules_version"] is None
    assert gc_list_version(db) == "2026-02-09"  # no stamp minted


def test_readd_after_removal_is_a_new_row(db):
    _gc(db, "Comeback Kid")
    db.commit()
    apply_game_changer_sync(db, [], today="2026-06-10")  # removed
    apply_game_changer_sync(db, ["Comeback Kid"], today="2026-07-01")  # re-added
    rows = db.execute(
        text(
            "SELECT active, rules_version FROM game_changer_cards"
            " WHERE card_name = 'Comeback Kid' ORDER BY id"
        )
    ).fetchall()
    assert len(rows) == 2  # history preserved as two rows
    assert (bool(rows[0][0]), rows[0][1]) == (False, "2026-02-09")
    assert (bool(rows[1][0]), rows[1][1]) == (True, "2026-07-01")


# --- floor staleness end-to-end ---------------------------------------------------


def _seed_bracket_rules(db):
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


def _deck_with_card(db, name):
    u = User(username=f"u{next(_seq)}@ex.com", password_hash="x")
    db.add(u)
    db.flush()
    loc = StorageLocation(user_id=u.id, name=f"d{next(_seq)}", type="deck", mode="managed")
    db.add(loc)
    db.flush()
    deck = Deck(user_id=u.id, name=f"d{next(_seq)}", storage_location_id=loc.id)
    db.add(deck)
    db.flush()
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
    db.add(
        InventoryRow(
            user_id=u.id,
            card_id=c.id,
            finish="normal",
            quantity=1,
            storage_location_id=loc.id,
            is_pending=False,
        )
    )
    db.flush()
    return deck


def test_list_update_refloors_via_daemon_without_network(db, monkeypatch):
    from app import combo_refresh_service as crs
    from app.models import DeckCombo

    _seed_bracket_rules(db)
    deck = _deck_with_card(db, "Future Menace")
    db.commit()

    # evaluated today: no GCs -> floor 2, stamped with the current list version
    est = estimate_bracket_v2(db, deck, deck.user_id, combos={"included": []})
    persist_estimate(db, deck.id, est)
    got = load_persisted_estimate(db, deck.id)
    assert got["floor_bracket"] == 2
    assert got["rules_version"] == GC_LIST_BASE_VERSION

    # persisted combo row with a CURRENT fingerprint (decklist is fresh)
    rows = crs.resolved_deck_rows(db, deck, deck.user_id)
    db.add(
        DeckCombo(
            deck_id=deck.id,
            fingerprint=crs.deck_combo_fingerprint(rows),
            payload='{"included": []}',
        )
    )
    db.commit()

    # any Spellbook call would be a bug — the re-floor must be local-only
    def _boom(*a, **k):
        raise AssertionError("Spellbook must not be called for a GC re-floor")

    monkeypatch.setattr("app.spellbook.fetch_deck_combos", _boom)

    # fresh list + fresh fingerprint -> daemon does nothing
    assert crs.refresh_stale_deck_combos(db) == 0

    # the official list adds a card this deck plays -> floors go stale
    apply_game_changer_sync(db, ["Future Menace"], today="2026-06-10")
    assert crs.refresh_stale_deck_combos(db) == 1

    got = load_persisted_estimate(db, deck.id)
    assert got["rules_version"] == "2026-06-10"  # re-stamped
    assert got["floor_bracket"] == 3  # 1 GC -> floor 3
    gc = next(f for f in got["floor_findings"] if f["type"] == "game_changer_detected")
    assert "Future Menace" in gc["message"]

    # second pass: everything fresh again
    assert crs.refresh_stale_deck_combos(db) == 0
