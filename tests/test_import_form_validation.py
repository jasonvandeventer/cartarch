"""Reject malformed parallel form arrays before any import can be written."""

import pytest

from app.models import ImportBatch, InventoryRow, TransactionLog
from app.routes.imports import _parsed_rows_from_form


@pytest.mark.parametrize("path", ["/import/commit", "/import/reconcile-preview"])
@pytest.mark.parametrize(
    "field", ["scryfall_id", "set_code", "collector_number", "finish", "quantity", "location"]
)
@pytest.mark.parametrize("surplus", [False, True])
def test_mismatched_import_fields_return_400_without_writes(client, db, path, field, surplus):
    data = dict(
        line_number=["1"],
        scryfall_id=["unknown"],
        set_code=["tst"],
        collector_number=["1"],
        finish=["normal"],
        quantity=["2"],
        location=[""],
    )
    if surplus:
        data[field] = data[field] * 2
    else:
        del data[field]
    response = client.post(path, data=data)
    assert response.status_code == 400
    assert "Please preview the import again" in response.text
    assert db.query(InventoryRow).count() == 0
    assert db.query(ImportBatch).count() == 0
    assert db.query(TransactionLog).count() == 0


def test_short_optional_arrays_keep_legacy_defaults():
    rows = _parsed_rows_from_form(
        line_number=["1", "2"],
        name=["First"],
        scryfall_id=["a", "b"],
        set_code=["tst", "tst"],
        collector_number=["1", "2"],
        finish=["normal", "foil"],
        quantity=["1", "2"],
        location=["", ""],
        language=["fr"],
        role=["commander"],
    )
    assert len(rows) == 2
    assert rows[0]["language"] == "fr" and rows[0]["role"] == "commander"
    assert rows[1]["name"] == "" and rows[1]["language"] == "en" and rows[1]["role"] == ""


@pytest.mark.parametrize("path", ["/import/commit", "/import/reconcile-preview"])
@pytest.mark.parametrize("field", ["is_proxy", "is_brew"])
def test_invalid_boolean_flags_rejected_without_writes(client, db, path, field):
    data = dict(
        line_number="1",
        scryfall_id="unknown",
        set_code="tst",
        collector_number="1",
        finish="normal",
        quantity="2",
        location="",
    )
    data[field] = "not-a-boolean"
    response = client.post(path, data=data)
    assert response.status_code == 400
    assert "Invalid proxy or brew flag" in response.text
    assert db.query(InventoryRow).count() == 0
    assert db.query(ImportBatch).count() == 0


@pytest.mark.parametrize("raw, expected", [(" TRUE ", True), ("false", False), ("", False)])
def test_valid_boolean_flags_keep_csv_semantics(raw, expected):
    rows = _parsed_rows_from_form(
        line_number=["1"],
        name=[],
        scryfall_id=["x"],
        set_code=["tst"],
        collector_number=["1"],
        finish=["normal"],
        quantity=["1"],
        location=[""],
        is_proxy=[raw],
        is_brew=[raw],
    )
    assert rows[0]["is_proxy"] is expected and rows[0]["is_brew"] is expected
