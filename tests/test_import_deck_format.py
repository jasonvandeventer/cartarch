"""#150 item 2 — the importer round-trips ``Deck Format``.

The export half shipped in v4.11.33 (CSV ``Deck Format`` column + JSON
``deck_format``); the importer never read it back, so re-importing your own
export created the deck and dropped its format.

Scope matches ``is_brew`` exactly: consulted ONLY when a row's Location
auto-creates a NEW deck. An existing deck's own format is the authority — an
import must not rewrite it, or a stale backup would silently reformat a live deck.
"""

import app.legacy_tables  # noqa
from app import deck_service
from app.import_service import HEADER_ALIASES, auto_create_locations, normalize_header
from app.models import Deck


def test_the_export_header_is_recognised():
    """The exporter writes 'Deck Format'; normalize_header must map it."""
    assert normalize_header("Deck Format") == "deck_format"
    assert HEADER_ALIASES["deckformat"] == "deck_format"


def test_an_auto_created_deck_keeps_the_imported_format(db, user):
    created = auto_create_locations(
        db,
        user.id,
        ["Atraxa Superfriends"],
        name_to_type={"atraxa superfriends": "deck"},
        name_to_deck_format={"atraxa superfriends": "commander"},
    )
    db.commit()
    assert created
    deck = db.query(Deck).filter(Deck.name == "Atraxa Superfriends").one()
    assert deck.format == "commander"


def test_an_existing_deck_keeps_its_own_format(db, user):
    """The authority rule. A stale backup must not reformat a live deck."""
    deck_service.create_deck(db, user.id, "Whiplash", format_name="pauper")
    db.commit()

    auto_create_locations(
        db,
        user.id,
        ["Whiplash"],
        name_to_type={"whiplash": "deck"},
        name_to_deck_format={"whiplash": "commander"},
    )
    db.commit()

    assert db.query(Deck).filter(Deck.name == "Whiplash").one().format == "pauper"


def test_no_format_supplied_leaves_it_blank_not_broken(db, user):
    """A plain 6-column CSV carries no format column at all."""
    auto_create_locations(db, user.id, ["Plain Deck"], name_to_type={"plain deck": "deck"})
    db.commit()
    assert db.query(Deck).filter(Deck.name == "Plain Deck").one().format in ("", None)


def test_a_non_deck_location_ignores_the_format(db, user):
    """`Deck Format` on a binder row must not create a deck or raise."""
    created = auto_create_locations(
        db,
        user.id,
        ["Trade Binder"],
        name_to_type={"trade binder": "binder"},
        name_to_deck_format={"trade binder": "commander"},
    )
    db.commit()
    assert created
    assert db.query(Deck).filter(Deck.name == "Trade Binder").first() is None


def test_an_overlong_format_is_truncated_not_fatal(db, user):
    """`Deck.format` is String(64). A long value in a backup must not fail the
    import of the CARDS, which is what the row is actually for."""
    auto_create_locations(
        db,
        user.id,
        ["Long Format Deck"],
        name_to_type={"long format deck": "deck"},
        name_to_deck_format={"long format deck": ("x" * 200)[:64]},
    )
    db.commit()
    stored = db.query(Deck).filter(Deck.name == "Long Format Deck").one().format
    assert stored is not None and len(stored) <= 64


# --- the parse and form halves ---------------------------------------------------
#
# The service tests above prove `auto_create_locations` uses a format it is
# HANDED. They say nothing about whether the value survives the CSV parse or the
# preview form — the #152 failure mode, where a correct service sits behind a
# path that never delivers to it. These cover that gap.


def test_the_csv_parse_carries_the_column_into_the_row(monkeypatch):
    import app.import_service as import_service
    from app.scryfall import BulkFetchResult

    def _fake_ids(ids):
        return BulkFetchResult(
            cards={
                i: {
                    "scryfall_id": i,
                    "name": "Sol Ring",
                    "set_code": "c21",
                    "collector_number": "263",
                }
                for i in ids
            }
        )

    monkeypatch.setattr(import_service, "bulk_refresh_prices", _fake_ids)
    monkeypatch.setattr(import_service, "bulk_fetch_by_set_number", lambda pairs: BulkFetchResult())
    monkeypatch.setattr(import_service, "bulk_fetch_by_name", lambda names: BulkFetchResult())

    csv = (
        "Scryfall ID,Quantity,Finish,Location,Location Type,Deck Format\n"
        "sid-1,1,normal,Atraxa Superfriends,deck,commander\n"
    )
    result = import_service.parse_scanner_csv(csv.encode("utf-8"))

    assert result["invalid_rows"] == []
    assert result["valid_rows"][0]["deck_format"] == "commander"


def test_an_overlong_column_is_truncated_at_parse_time(monkeypatch):
    import app.import_service as import_service
    from app.scryfall import BulkFetchResult

    monkeypatch.setattr(
        import_service,
        "bulk_refresh_prices",
        lambda ids: BulkFetchResult(
            cards={i: {"scryfall_id": i, "name": "X", "set_code": "c21"} for i in ids}
        ),
    )
    monkeypatch.setattr(import_service, "bulk_fetch_by_set_number", lambda pairs: BulkFetchResult())
    monkeypatch.setattr(import_service, "bulk_fetch_by_name", lambda names: BulkFetchResult())

    csv = (
        "Scryfall ID,Quantity,Finish,Location,Location Type,Deck Format\n"
        f"sid-1,1,normal,Deck,deck,{'x' * 200}\n"
    )
    result = import_service.parse_scanner_csv(csv.encode("utf-8"))
    assert len(result["valid_rows"][0]["deck_format"]) == 64


def test_the_preview_form_field_reaches_the_parsed_row():
    """The hidden input is index-aligned with the other parallel arrays."""
    from app.routes.imports import _parsed_rows_from_form

    rows = _parsed_rows_from_form(
        ["1", "2"],
        ["A", "B"],
        ["sid-1", "sid-2"],
        ["c21", "c21"],
        ["1", "2"],
        ["normal", "normal"],
        ["1", "1"],
        ["My Deck", "My Deck"],
        deck_format=["commander", "commander"],
    )
    assert [r["deck_format"] for r in rows] == ["commander", "commander"]


def test_a_missing_array_means_no_format_never_a_clear():
    """An older preview page mid-deploy sends no deck_format array at all. That
    must read as 'nothing stated', not as an instruction (the #171 lesson)."""
    from app.routes.imports import _parsed_rows_from_form

    rows = _parsed_rows_from_form(
        ["1"], ["A"], ["sid-1"], ["c21"], ["1"], ["normal"], ["1"], ["My Deck"]
    )
    assert rows[0]["deck_format"] == ""
