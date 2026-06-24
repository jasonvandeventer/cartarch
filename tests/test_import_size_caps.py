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
    _count_lines,
    _enforce_import_size_limits,
)

# --- line counting (off-by-one on trailing newline) -----------------------------


def test_count_lines_no_trailing_newline():
    assert _count_lines("a\nb\nc") == 3
    assert _count_lines(b"a\nb\nc") == 3


def test_count_lines_trailing_newline_not_over_counted():
    # The bug the revision fixed: "a\nb\nc\n" is 3 lines, NOT 4. A valid file at
    # exactly the line cap that ends in "\n" must not be read as cap+1.
    assert _count_lines("a\nb\nc\n") == 3
    assert _count_lines(b"a\nb\nc\n") == 3


def test_count_lines_empty_is_zero():
    assert _count_lines("") == 0
    assert _count_lines(b"") == 0


def test_capped_file_with_trailing_newline_is_accepted():
    # Exactly MAX_IMPORT_LINES lines, terminated by a newline → still allowed.
    text = "".join(f"row{i}\n" for i in range(MAX_IMPORT_LINES))
    _enforce_import_size_limits(0, _count_lines(text))  # must not raise


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


def test_paste_multibyte_counts_actual_bytes_not_chars(client, monkeypatch):
    """A multi-byte UTF-8 paste must be measured by its true byte size, not its
    character count — a char can be up to 4 bytes, so a payload under the 2 MB
    CHARACTER count can still blow past the 2 MB BYTE cap. Guards the v4.0.x fix
    of the len(card_list)-as-byte-proxy bug."""

    def _boom(_text):
        raise AssertionError("parser must not run on an oversized import")

    monkeypatch.setattr(imports_routes, "parse_text_list", _boom)

    # '€' is 3 bytes in UTF-8. This string is well under MAX_IMPORT_BYTES *chars*
    # but ~3x over in bytes — the old len()-proxy would have wrongly accepted it.
    char_count = (MAX_IMPORT_BYTES // 3) + 1
    assert char_count < MAX_IMPORT_BYTES  # under the cap if measured as chars
    card_list = "€" * char_count
    assert len(card_list.encode("utf-8")) > MAX_IMPORT_BYTES  # over the cap in bytes
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


def test_csv_oversized_rejected_before_read(client, monkeypatch):
    # The DECLARED size (file.size) must trip the cap so .read() never loads the
    # whole body into RAM. Make .read() itself blow up to prove it isn't reached.
    async def _boom_read(*_a, **_k):
        raise AssertionError("file.read() must not run on an oversized upload")

    from starlette.datastructures import UploadFile as _UF

    monkeypatch.setattr(_UF, "read", _boom_read)
    monkeypatch.setattr(
        imports_routes,
        "parse_scanner_csv",
        lambda _b: (_ for _ in ()).throw(AssertionError("parser must not run")),
    )

    big = b"x" * (MAX_IMPORT_BYTES + 1)
    r = client.post(
        "/import/preview",
        data={"csrf_token": "x"},
        files={"file": ("big.csv", big, "text/csv")},
    )
    assert r.status_code == 400
    assert "bytes exceeds" in r.text


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
