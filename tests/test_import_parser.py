"""Paste-list line parser tests (brew-buylist Defect B).

Pins two guarantees of the text-list import grammar:

1. **Broad grammar** — set codes may begin with a digit and vary in length
   (40K, 2X2, 2XM, PMEI, PLST); collector numbers may contain hyphens, letters,
   and unicode stars (2026-1, 353a, ★). The two exact lines the v3.37.1 brew
   import silently dropped — ``1 Reverberate (40K) 207`` and
   ``1 Command Tower (PMEI) 2026-1`` — are fixtures here, in BOTH the
   parenthesized form (the prompt's literal fixtures) and the bare
   ``SET COLLECTOR`` short form (the actual scanner-export regression: the old
   short-form regexes rejected ``40K`` as digit-leading and ``2026-1`` as
   hyphenated, so the line returned None and was ``continue``-dropped).

2. **No silent drops** — ``parse_text_list`` routes every non-empty,
   non-comment, non-section-header line it cannot parse into ``invalid_rows``
   with a reason, instead of dropping it. (Network batch is mocked so the test
   exercises the parser, not Scryfall.)
"""

from __future__ import annotations

import app.import_service as import_service
from app.import_service import _parse_list_line, parse_text_list
from app.scryfall import BulkFetchResult

# ---------------------------------------------------------------------------
# Grammar — the two exact Defect-B fixtures (parenthesized form)
# ---------------------------------------------------------------------------


def test_reverberate_40k_parens_parses():
    parsed = _parse_list_line("1 Reverberate (40K) 207")
    assert parsed is not None
    assert parsed["name"] == "Reverberate"
    assert parsed["set_code"] == "40k"
    assert parsed["collector_number"] == "207"
    assert parsed["quantity"] == 1


def test_command_tower_pmei_hyphen_collector_parses():
    parsed = _parse_list_line("1 Command Tower (PMEI) 2026-1")
    assert parsed is not None
    assert parsed["name"] == "Command Tower"
    assert parsed["set_code"] == "pmei"
    assert parsed["collector_number"] == "2026-1"
    assert parsed["quantity"] == 1


# ---------------------------------------------------------------------------
# Grammar — bare short form (the actual scanner-export regression)
# ---------------------------------------------------------------------------


def test_bare_digit_leading_set_parses():
    # Old _SHORT_SET_RE required a letter-leading set, so "40K" was rejected and
    # the line silently dropped.
    parsed = _parse_list_line("40K 207")
    assert parsed is not None
    assert parsed["set_code"] == "40k"
    assert parsed["collector_number"] == "207"
    assert parsed["quantity"] == 1


def test_bare_hyphen_collector_parses():
    # Old _SHORT_COLL_RE allowed only digits + one optional letter, so "2026-1"
    # was rejected and the line silently dropped.
    parsed = _parse_list_line("PMEI 2026-1")
    assert parsed is not None
    assert parsed["set_code"] == "pmei"
    assert parsed["collector_number"] == "2026-1"


def test_bare_short_form_with_quantity():
    parsed = _parse_list_line("3 40K 207")
    assert parsed is not None
    assert parsed["set_code"] == "40k"
    assert parsed["collector_number"] == "207"
    assert parsed["quantity"] == 3


# ---------------------------------------------------------------------------
# Grammar — the rest of the prompt's edge-case matrix
# ---------------------------------------------------------------------------


def test_varied_set_codes_parse():
    for raw, set_code in [
        ("1 Foo (2X2) 10", "2x2"),
        ("1 Foo (2XM) 10", "2xm"),
        ("1 Foo (PMEI) 10", "pmei"),
        ("1 Foo (PLST) 10", "plst"),
    ]:
        parsed = _parse_list_line(raw)
        assert parsed is not None, raw
        assert parsed["set_code"] == set_code


def test_varied_collector_numbers_parse():
    for raw, collector in [
        ("1 Foo (2X2) 353a", "353a"),
        ("1 Foo (PLST) 2026-1", "2026-1"),
        ("1 Foo (PLST) ★", "★"),
    ]:
        parsed = _parse_list_line(raw)
        assert parsed is not None, raw
        assert parsed["collector_number"] == collector


def test_foil_marker_still_parses_with_broadened_grammar():
    parsed = _parse_list_line("1 Cyclonic Rift (SOA) 14 *F*")
    assert parsed is not None
    assert parsed["set_code"] == "soa"
    assert parsed["collector_number"] == "14"
    assert parsed["finish"] == "foil"


def test_bare_card_name_is_not_a_short_form_match():
    # "Sol Ring" must NOT be misparsed as set=sol collector=ring (Ring fails the
    # collector grammar), so it falls through to the name-only path (None here;
    # parse_text_list then routes it to invalid_rows — see below).
    assert _parse_list_line("Sol Ring") is None


# ---------------------------------------------------------------------------
# No silent drops — parse_text_list routes unparseable lines to invalid_rows
# ---------------------------------------------------------------------------


def _no_network(monkeypatch):
    """Force BOTH batch lookups (set+collector and name) to miss (no network)."""
    monkeypatch.setattr(
        import_service,
        "bulk_fetch_by_set_number",
        lambda pairs: BulkFetchResult(),
    )
    monkeypatch.setattr(
        import_service,
        "bulk_fetch_by_name",
        lambda names: BulkFetchResult(),
    )


def test_unparseable_line_becomes_invalid_row_not_dropped(monkeypatch):
    _no_network(monkeypatch)
    # A bare multi-word name with no set/collector cannot parse at all.
    result = parse_text_list("Sol Ring\nLightning Bolt")
    invalid = result["invalid_rows"]
    invalid_names = {r["name"] for r in invalid}
    assert "Sol Ring" in invalid_names
    assert "Lightning Bolt" in invalid_names
    # Every reported invalid row carries a reason.
    assert all(r["reason"] for r in invalid)


def test_no_line_is_silently_dropped(monkeypatch):
    _no_network(monkeypatch)
    text = "\n".join(
        [
            "1 Reverberate (40K) 207",
            "1 Command Tower (PMEI) 2026-1",
            "Sol Ring",  # unparseable bare name
            "garblednonsense ?!",  # unparseable
            "# a comment",  # skipped, not a card line
            "Deck",  # section header, skipped
            "",  # blank, skipped
        ]
    )
    result = parse_text_list(text)
    accounted = len(result["valid_rows"]) + len(result["invalid_rows"])
    # The 4 card-bearing lines are all accounted for; comment/header/blank are
    # legitimately skipped (not silent drops of card data).
    assert accounted == 4


def test_section_headers_and_comments_are_not_invalid_rows(monkeypatch):
    _no_network(monkeypatch)
    result = parse_text_list("Deck\n# notes\n\nCommander\nSideboard")
    assert result["invalid_rows"] == []
    assert result["valid_rows"] == []


def test_decorated_headers_and_slash_comments_are_skipped(monkeypatch):
    _no_network(monkeypatch)
    # Decorated section headers (trailing count / colon) and // comment lines
    # are skipped like their bare forms, NOT surfaced as unparseable.
    result = parse_text_list("Sideboard (15)\nMaybeboard:\n// build notes\nDeck (99)\nCOMMANDER")
    assert result["invalid_rows"] == []
    assert result["valid_rows"] == []


def test_slash_prefix_does_not_swallow_dfc_card_names(monkeypatch):
    _no_network(monkeypatch)
    # "//" is a comment only as a line PREFIX — a DFC name with an internal
    # "//" must still parse as a card line.
    parsed = _parse_list_line("1 Expansion // Explosion (RVR) 243")
    assert parsed is not None
    assert parsed["name"] == "Expansion // Explosion"
    assert parsed["set_code"] == "rvr"
    assert parsed["collector_number"] == "243"


# ---------------------------------------------------------------------------
# Bare-name batch resolution (v3.39.x bulk_fetch_by_name) — the single-card
# "Import by name" matching, scaled to the batch paste importer.
# ---------------------------------------------------------------------------


def _card(name, set_code, collector, scryfall_id="sid-x"):
    return {
        "scryfall_id": scryfall_id,
        "name": name,
        "set_code": set_code,
        "collector_number": collector,
        "price_usd": "1.00",
        "price_usd_foil": "2.00",
        "price_usd_etched": None,
    }


def test_bare_name_line_resolves_via_name_batch(monkeypatch):
    # The set+collector batch misses; the name batch resolves "Mizzix of the
    # Izmagnus" (a bare name with a leading quantity) — the exact case the old
    # "can't be batch-resolved" message rejected.
    monkeypatch.setattr(import_service, "bulk_fetch_by_set_number", lambda pairs: BulkFetchResult())

    captured = {}

    def fake_name_batch(names):
        captured["names"] = names
        return BulkFetchResult(
            cards={("mizzix of the izmagnus", ""): _card("Mizzix of the Izmagnus", "c15", "39")}
        )

    monkeypatch.setattr(import_service, "bulk_fetch_by_name", fake_name_batch)

    result = parse_text_list("1 Mizzix of the Izmagnus")
    assert result["invalid_rows"] == []
    assert len(result["valid_rows"]) == 1
    row = result["valid_rows"][0]
    assert row["name"] == "Mizzix of the Izmagnus"
    assert row["scryfall_id"] == "sid-x"
    assert row["quantity"] == 1
    # The line was handed to the name batch with an empty set hint.
    assert captured["names"] == [("Mizzix of the Izmagnus", "")]


def test_name_with_set_hint_passes_set_to_name_batch(monkeypatch):
    # A "Name (SET)" line with no collector number is name-batchable WITH the
    # set as a hint (resolves that printing), not rejected.
    monkeypatch.setattr(import_service, "bulk_fetch_by_set_number", lambda pairs: BulkFetchResult())

    captured = {}

    def fake_name_batch(names):
        captured["names"] = names
        return BulkFetchResult(cards={("sol ring", "c21"): _card("Sol Ring", "c21", "263")})

    monkeypatch.setattr(import_service, "bulk_fetch_by_name", fake_name_batch)

    result = parse_text_list("1 Sol Ring (C21)")
    assert result["invalid_rows"] == []
    assert result["valid_rows"][0]["set_code"] == "c21"
    assert captured["names"] == [("Sol Ring", "c21")]


def test_set_plus_collector_line_is_not_routed_to_name_batch(monkeypatch):
    # Strictness: a line carrying BOTH set+collector is resolved only by the
    # set batch. If that printing doesn't resolve, the line is invalid — it is
    # NEVER silently substituted with a different printing via the name batch.
    monkeypatch.setattr(import_service, "bulk_fetch_by_set_number", lambda pairs: BulkFetchResult())

    captured = {"names": None}

    def fake_name_batch(names):
        captured["names"] = names
        return BulkFetchResult()

    monkeypatch.setattr(import_service, "bulk_fetch_by_name", fake_name_batch)

    result = parse_text_list("1 Sol Ring (C21) 263")
    # The name batch was never invoked — a set+collector line is not name-batchable.
    assert captured["names"] is None
    assert result["valid_rows"] == []
    assert len(result["invalid_rows"]) == 1


def test_unknown_bare_name_is_invalid_with_spelling_reason(monkeypatch):
    # Name batch ran but found nothing (typo / not a real card) → invalid row
    # whose reason points at spelling / adding a set+collector, NOT the old
    # "can't be batch-resolved" message.
    monkeypatch.setattr(import_service, "bulk_fetch_by_set_number", lambda pairs: BulkFetchResult())
    monkeypatch.setattr(
        import_service,
        "bulk_fetch_by_name",
        lambda names: BulkFetchResult(not_found=[("definitely not a card", "")]),
    )

    result = parse_text_list("1 Definitely Not A Card")
    assert result["valid_rows"] == []
    assert len(result["invalid_rows"]) == 1
    reason = result["invalid_rows"][0]["reason"]
    assert "not found" in reason.lower()
    assert "batch-resolved" not in reason


def test_transient_name_batch_failure_reports_retry(monkeypatch):
    # Name batch POST errored after retries (Scryfall down) → the line's reason
    # is the transient "re-import to retry", distinct from a genuine not-found.
    monkeypatch.setattr(import_service, "bulk_fetch_by_set_number", lambda pairs: BulkFetchResult())
    monkeypatch.setattr(
        import_service,
        "bulk_fetch_by_name",
        lambda names: BulkFetchResult(failed=[("sol ring", "")]),
    )

    result = parse_text_list("1 Sol Ring")
    assert result["valid_rows"] == []
    reason = result["invalid_rows"][0]["reason"]
    assert "temporarily failed" in reason.lower()
