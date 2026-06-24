"""Import size caps (S4) — oversized paste/CSV uploads are rejected with a clean
400 BEFORE any parsing begins, while normal-sized imports flow through unchanged.

invariant: CLAUDE.md → Import size caps (S4): cap bytes (2 MB) and lines (5,000)
on the two raw-input preview routes (/import/list/preview, /import/preview).
"""

from __future__ import annotations

import pytest

from app.routes import imports as imports_routes
from app.routes.imports import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_LINES,
    _enforce_import_size_limits,
)

# --- the predicate in isolation -------------------------------------------------


def test_enforce_under_limits_is_noop():
    # At the cap exactly → allowed (the cap is "exceeds", strictly greater).
    _enforce_import_size_limits(MAX_IMPORT_BYTES, MAX_IMPORT_LINES)


def test_enforce_rejects_oversized_bytes():
    with pytest.raises(ValueError, match="bytes exceeds"):
        _enforce_import_size_limits(MAX_IMPORT_BYTES + 1, 1)


def test_enforce_rejects_oversized_lines():
    with pytest.raises(ValueError, match="lines exceeds"):
        _enforce_import_size_limits(1, MAX_IMPORT_LINES + 1)


# --- the paste-text path --------------------------------------------------------


def test_paste_too_many_lines_returns_400_before_parsing(client, monkeypatch):
    # If the cap fires first, the parser is never reached.
    def _boom(_text):
        raise AssertionError("parser must not run on an oversized import")

    monkeypatch.setattr(imports_routes, "parse_text_list", _boom)

    card_list = "\n".join(f"1 Card {i}" for i in range(MAX_IMPORT_LINES + 5))
    r = client.post("/import/list/preview", data={"card_list": card_list, "csrf_token": "x"})
    assert r.status_code == 400
    assert "lines exceeds" in r.text


def test_paste_too_many_bytes_returns_400_before_parsing(client, monkeypatch):
    def _boom(_text):
        raise AssertionError("parser must not run on an oversized import")

    monkeypatch.setattr(imports_routes, "parse_text_list", _boom)

    # One huge line — trips the byte cap, not the line cap.
    card_list = "x" * (MAX_IMPORT_BYTES + 1)
    r = client.post("/import/list/preview", data={"card_list": card_list, "csrf_token": "x"})
    assert r.status_code == 400
    assert "bytes exceeds" in r.text


def test_paste_within_limits_still_parses(client, monkeypatch):
    seen = {}

    def _fake_parse(text):
        seen["text"] = text
        return {"valid_rows": [], "invalid_rows": [], "format_name": "Text List"}

    monkeypatch.setattr(imports_routes, "parse_text_list", _fake_parse)

    r = client.post(
        "/import/list/preview",
        data={"card_list": "1 Sol Ring\n2 Lightning Bolt", "csrf_token": "x"},
    )
    assert r.status_code == 200
    assert seen["text"] == "1 Sol Ring\n2 Lightning Bolt"


# --- the CSV upload path --------------------------------------------------------


def test_csv_too_many_bytes_returns_400_before_parsing(client, monkeypatch):
    def _boom(_b):
        raise AssertionError("parser must not run on an oversized import")

    monkeypatch.setattr(imports_routes, "parse_scanner_csv", _boom)

    big = b"x" * (MAX_IMPORT_BYTES + 1)
    r = client.post(
        "/import/preview",
        data={"csrf_token": "x"},
        files={"file": ("big.csv", big, "text/csv")},
    )
    assert r.status_code == 400
    assert "bytes exceeds" in r.text


def test_csv_too_many_lines_returns_400_before_parsing(client, monkeypatch):
    def _boom(_b):
        raise AssertionError("parser must not run on an oversized import")

    monkeypatch.setattr(imports_routes, "parse_scanner_csv", _boom)

    big = b"\n".join(b"row" for _ in range(MAX_IMPORT_LINES + 5))
    r = client.post(
        "/import/preview",
        data={"csrf_token": "x"},
        files={"file": ("many.csv", big, "text/csv")},
    )
    assert r.status_code == 400
    assert "lines exceeds" in r.text


def test_csv_within_limits_still_parses(client, monkeypatch):
    seen = {}

    def _fake_parse(file_bytes):
        seen["bytes"] = file_bytes
        return {"valid_rows": [], "invalid_rows": [], "format_name": "Scanner CSV"}

    monkeypatch.setattr(imports_routes, "parse_scanner_csv", _fake_parse)

    body = b"Name,Set,Number\nSol Ring,c21,263\n"
    r = client.post(
        "/import/preview",
        data={"csrf_token": "x"},
        files={"file": ("small.csv", body, "text/csv")},
    )
    assert r.status_code == 200
    assert seen["bytes"] == body
