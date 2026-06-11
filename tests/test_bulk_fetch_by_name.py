"""Unit tests for scryfall.bulk_fetch_by_name (v3.39.x).

The batched, request-path-safe analogue of the single-card "Import by name"
flow: it resolves bare card names (optionally hinted with a set) via the
/cards/collection ``{"name": ...}`` identifier in ceil(N/75) POSTs, never
one-per-name. ``_post_json`` is monkeypatched so these tests exercise the
batching / matching / result-bucketing logic, not the network.

Key invariants pinned here:
- API-only: it does NOT touch the local scryfall_cards cache (no released_at
  column → can't replicate Scryfall's preferred-printing choice for a name).
- Result key shape is (name_lower, set_lower), mirroring the request, so the
  caller looks up by exactly what it asked.
- A name with no match in a SUCCESSFUL batch → .not_found (permanent).
- A batch whose POST returns None (Scryfall down after retries) → .failed
  (transient), kept distinct from not_found.
- A double-faced card matches on either its full "A // B" name or front face.
"""

from __future__ import annotations

import app.scryfall as scryfall
from app.scryfall import bulk_fetch_by_name


def _raw(name, set_code, collector, card_id="sid"):
    """A minimal raw Scryfall card payload (the shape _normalize reads)."""
    return {
        "id": card_id,
        "name": name,
        "set": set_code,
        "collector_number": collector,
    }


def test_bare_name_resolves_and_keys_by_request(monkeypatch):
    captured = {}

    def fake_post(url, payload):
        captured["payload"] = payload
        return {"data": [_raw("Mizzix of the Izmagnus", "c15", "39", "sid-1")]}

    monkeypatch.setattr(scryfall, "_post_json", fake_post)

    result = bulk_fetch_by_name([("Mizzix of the Izmagnus", "")])
    # One name identifier, no set hint.
    assert captured["payload"] == {"identifiers": [{"name": "mizzix of the izmagnus"}]}
    assert result.cards[("mizzix of the izmagnus", "")]["scryfall_id"] == "sid-1"
    assert result.not_found == []
    assert result.failed == []


def test_set_hint_is_sent_and_keyed(monkeypatch):
    def fake_post(url, payload):
        assert payload == {"identifiers": [{"name": "sol ring", "set": "c21"}]}
        return {"data": [_raw("Sol Ring", "c21", "263", "sid-2")]}

    monkeypatch.setattr(scryfall, "_post_json", fake_post)

    result = bulk_fetch_by_name([("Sol Ring", "C21")])
    assert result.cards[("sol ring", "c21")]["set_code"] == "c21"


def test_set_hint_mismatch_is_not_found(monkeypatch):
    # Scryfall returned a printing in a different set than the hint → no match.
    monkeypatch.setattr(
        scryfall, "_post_json", lambda url, payload: {"data": [_raw("Sol Ring", "lea", "1")]}
    )
    result = bulk_fetch_by_name([("Sol Ring", "c21")])
    assert result.cards == {}
    assert result.not_found == [("sol ring", "c21")]


def test_unknown_name_goes_to_not_found(monkeypatch):
    # Successful POST, but the requested name isn't in the response.
    monkeypatch.setattr(scryfall, "_post_json", lambda url, payload: {"data": []})
    result = bulk_fetch_by_name([("Definitely Not A Card", "")])
    assert result.cards == {}
    assert result.not_found == [("definitely not a card", "")]
    assert result.failed == []


def test_post_failure_is_transient_failed(monkeypatch):
    # POST returned None (retry adapter exhausted) → the whole batch is .failed,
    # NOT .not_found — the caller distinguishes "Scryfall down" from "no card".
    monkeypatch.setattr(scryfall, "_post_json", lambda url, payload: None)
    result = bulk_fetch_by_name([("Sol Ring", "")])
    assert result.cards == {}
    assert result.not_found == []
    assert result.failed == [("sol ring", "")]


def test_dfc_front_face_name_matches(monkeypatch):
    # A paste line using only the front-face name resolves against a card whose
    # full name is "Front // Back".
    monkeypatch.setattr(
        scryfall,
        "_post_json",
        lambda url, payload: {
            "data": [_raw("Fable of the Mirror-Breaker // Reflection of Kiki-Jiki", "neo", "141")]
        },
    )
    result = bulk_fetch_by_name([("Fable of the Mirror-Breaker", "")])
    assert ("fable of the mirror-breaker", "") in result.cards


def test_duplicate_and_blank_names_deduped(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, payload):
        calls["n"] += 1
        # Only one unique identifier should reach the wire.
        assert payload["identifiers"] == [{"name": "sol ring"}]
        return {"data": [_raw("Sol Ring", "c21", "263")]}

    monkeypatch.setattr(scryfall, "_post_json", fake_post)

    result = bulk_fetch_by_name([("Sol Ring", ""), ("sol ring", ""), ("", "")])
    assert calls["n"] == 1
    assert ("sol ring", "") in result.cards


def test_no_cache_touch_means_no_db_dependency(monkeypatch):
    # The name path is API-only: with _post_json stubbed it resolves without
    # any scryfall_cards access. (If it consulted the cache it would need the
    # engine; this test runs with no DB fixture at all.)
    monkeypatch.setattr(
        scryfall, "_post_json", lambda url, payload: {"data": [_raw("Sol Ring", "c21", "263")]}
    )
    result = bulk_fetch_by_name([("Sol Ring", "")])
    assert ("sol ring", "") in result.cards
