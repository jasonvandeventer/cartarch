"""Universal import adapter tests (issue #81).

Covers content-based format detection, the two normalizers (MTGO text shared by
Moxfield/ManaBox, and Archidekt's headerless positional CSV), and one full
detect -> normalize -> resolve pipeline pass through parse_scanner_csv with the
Scryfall batch mocked (the established import-test pattern — exercises the
adapter wiring, not the network).
"""

from __future__ import annotations

import os

import app.import_service as import_service
from app.import_adapters import (
    detect_import_format,
    normalize_archidekt_csv,
    normalize_mtgo_text,
)
from app.scryfall import BulkFetchResult

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "imports")


def _read(name: str) -> str:
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def _card(name, set_code, collector, scryfall_id="sid-x"):
    return {
        "scryfall_id": scryfall_id,
        "name": name,
        "set_code": set_code,
        "collector_number": collector,
        "price_usd": "1.00",
        "price_usd_foil": "2.00",
        "price_usd_etched": "3.00",
    }


# --- 5a: detection --------------------------------------------------------------


def test_detect_moxfield_is_mtgo_text():
    assert detect_import_format(_read("moxfield_export.txt")) == "mtgo_text"


def test_detect_manabox_is_mtgo_text():
    assert detect_import_format(_read("manabox_export.txt")) == "mtgo_text"


def test_detect_archidekt_is_archidekt_csv():
    assert detect_import_format(_read("archidekt_export.csv")) == "archidekt_csv"


def test_detect_helvault_csv_falls_through():
    # A header-based CSV (Helvault carries an "Extras" column) is NOT an adapter
    # format — it returns None so the existing detect_csv_format handles it.
    helvault = "Name,Edition,Extras,Quantity\nSol Ring,C21,foil,1\n"
    assert detect_import_format(helvault) is None


def test_detect_empty_content_is_none():
    assert detect_import_format("") is None
    assert detect_import_format("\n  \n") is None


# --- 5b: normalize_mtgo_text ----------------------------------------------------


def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_mtgo_parses_all_moxfield_rows():
    rows = normalize_mtgo_text(_read("moxfield_export.txt"))
    assert len(rows) == 8
    by = _by_name(rows)
    anje = by["Anje Falkenrath"]
    assert anje == {
        "name": "Anje Falkenrath",
        "set_code": "C19",
        "collector_number": "37",
        "quantity": 1,
        "finish": "foil",
    }


def test_mtgo_parses_all_manabox_rows():
    rows = normalize_mtgo_text(_read("manabox_export.txt"))
    assert len(rows) == 9
    # ManaBox uses different printings than Moxfield for the shared cards.
    by = _by_name(rows)
    assert by["Forest"]["collector_number"] == "280"
    assert by["The One Ring"]["collector_number"] == "246"


def test_mtgo_finish_markers():
    rows = _by_name(normalize_mtgo_text(_read("moxfield_export.txt")))
    assert rows["Anje Falkenrath"]["finish"] == "foil"  # F
    assert rows["Arid Mesa"]["finish"] == "etched"  # E
    assert rows["Deadly Dispute"]["finish"] == "normal"  # absent


def test_mtgo_asterisk_wrapped_finish():
    # Real MTGO exports wrap the marker in asterisks (*F*); both forms parse.
    rows = normalize_mtgo_text("1 Sol Ring (FDC) 2 *F*\n1 Arid Mesa (MH2) 436 *E*")
    by = _by_name(rows)
    assert by["Sol Ring"]["finish"] == "foil"
    assert by["Arid Mesa"]["finish"] == "etched"


def test_mtgo_quantity_greater_than_one():
    rows = _by_name(normalize_mtgo_text(_read("moxfield_export.txt")))
    assert rows["Forest"]["quantity"] == 3


def test_mtgo_unicode_name():
    rows = _by_name(normalize_mtgo_text(_read("manabox_export.txt")))
    assert "Lim-Dûl's Vault" in rows
    assert rows["Lim-Dûl's Vault"]["set_code"] == "C13"
    assert rows["Lim-Dûl's Vault"]["collector_number"] == "197"


def test_mtgo_the_prefix_name_not_truncated():
    rows = _by_name(normalize_mtgo_text(_read("moxfield_export.txt")))
    assert "The One Ring" in rows
    assert rows["The One Ring"]["collector_number"] == "451"


# --- 5c: normalize_archidekt_csv ------------------------------------------------


def test_archidekt_parses_all_rows():
    rows = normalize_archidekt_csv(_read("archidekt_export.csv"))
    assert len(rows) == 8
    by = _by_name(rows)
    assert by["Anje Falkenrath"]["set_code"] == "C19"
    assert by["Anje Falkenrath"]["collector_number"] == "37"


def test_archidekt_quoted_oracle_text_with_commas_and_quotes():
    # Deadly Dispute's oracle_text contains commas; Anje's contains embedded
    # double-quotes. csv.reader must not let either bleed into the positional
    # fields we read (name / set / collector / scryfall_id).
    by = _by_name(normalize_archidekt_csv(_read("archidekt_export.csv")))
    assert by["Deadly Dispute"]["collector_number"] == "124"
    assert by["Deadly Dispute"]["set_code"] == "CLB"
    assert by["Anje Falkenrath"]["collector_number"] == "37"


def test_archidekt_finish_mapping():
    by = _by_name(normalize_archidekt_csv(_read("archidekt_export.csv")))
    assert by["Anje Falkenrath"]["finish"] == "foil"  # Foil
    assert by["Arid Mesa"]["finish"] == "etched"  # Etched
    assert by["Deadly Dispute"]["finish"] == "normal"  # Normal


def test_archidekt_preserves_scryfall_id():
    by = _by_name(normalize_archidekt_csv(_read("archidekt_export.csv")))
    assert by["Sol Ring"]["scryfall_id"] == "a1b2c3d4-0006-4000-8000-000000000002"


def test_archidekt_quantity_greater_than_one():
    by = _by_name(normalize_archidekt_csv(_read("archidekt_export.csv")))
    assert by["Forest"]["quantity"] == 3


# --- 5d: full pipeline integration ----------------------------------------------


def test_pipeline_resolves_moxfield_fixture(monkeypatch):
    """detect -> normalize_mtgo_text -> pre_rows -> pass 2/3 resolution, driven
    through parse_scanner_csv (the /import/preview route's parser). Scryfall is
    mocked to echo a card for each requested (set, collector) pair."""

    def fake_set_batch(pairs):
        return BulkFetchResult(
            cards={(s, c): _card("Resolved", s, c, f"sid-{s}-{c}") for (s, c) in pairs}
        )

    monkeypatch.setattr(import_service, "bulk_fetch_by_set_number", fake_set_batch)
    monkeypatch.setattr(import_service, "bulk_refresh_prices", lambda ids: BulkFetchResult())
    monkeypatch.setattr(import_service, "bulk_fetch_by_name", lambda names: BulkFetchResult())

    result = import_service.parse_scanner_csv(_read("moxfield_export.txt").encode("utf-8"))

    assert result["format_name"] == "Moxfield / ManaBox (text)"
    assert result["invalid_rows"] == []
    assert len(result["valid_rows"]) == 8
    by = {r["set_code"] + r["collector_number"]: r for r in result["valid_rows"]}
    # set+collector resolved (lower-cased on the resolution path) and finish is
    # carried through from the adapter, per card.
    assert by["c1937"]["finish"] == "foil"  # Anje Falkenrath F
    assert by["mh2436"]["finish"] == "etched"  # Arid Mesa E
    assert by["clb124"]["finish"] == "normal"  # Deadly Dispute (no marker)
    assert by["fdn281"]["quantity"] == 3  # Forest x3
