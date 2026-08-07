"""Scryfall's bulk exports are gzipped JSON Lines, not a JSON array.

Scryfall removed ``download_uri`` from ``/bulk-data`` and replaced it with
``jsonl_download_uri``, changing the FORMAT at the same time. The bulk cache
guarded on the missing key and logged a skip, so it silently served a frozen
catalog from 2026-07-28 to 2026-08-07 (10 days); ``oracle_ingest`` indexed the
key directly and raised KeyError. Both call sites are pinned here — this is the
"grep for the question, not the symptom" rule: the same upstream change broke
two modules, and only one of them was reported.
"""

import gzip
import json
import pathlib
import types

import app.legacy_tables  # noqa
from app.jobs import oracle_ingest

_APP = pathlib.Path(__file__).resolve().parents[1] / "app"
_STREAMERS = (_APP / "scryfall.py", _APP / "jobs" / "oracle_ingest.py")


def test_no_streamer_reads_the_removed_download_uri_key():
    """``download_uri`` no longer exists in the listing. Reading it is either a
    permanent skip or a KeyError, and both failed quietly enough to run for
    days — so the key must not reappear in either streamer."""
    for path in _STREAMERS:
        src = path.read_text()
        for line in src.splitlines():
            code = line.split("#", 1)[0]
            assert '"download_uri"' not in code and "'download_uri'" not in code, (
                f"{path.name} reads the removed download_uri key: {line.strip()}"
            )
        assert "jsonl_download_uri" in src, f"{path.name} must resolve jsonl_download_uri"


def test_oracle_ingest_streams_gzipped_jsonl():
    """The export is served as ``content-type: application/gzip`` with NO
    ``Content-Encoding``, so ``decode_content`` does not inflate it — the gzip
    member has to be unwrapped explicitly. Feeding a real gzip body proves it."""
    cards = [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}]
    body = gzip.compress(b"\n".join(json.dumps(c).encode() for c in cards))

    class _Raw(__import__("io").BytesIO):
        pass

    resp = types.SimpleNamespace(
        raw=_Raw(body),
        raise_for_status=lambda: None,
        close=lambda: None,
    )
    original = oracle_ingest.requests.get
    oracle_ingest.requests.get = lambda *a, **k: resp
    try:
        got = list(oracle_ingest._stream_jsonl("http://offline/oracle.jsonl.gz"))
    finally:
        oracle_ingest.requests.get = original

    assert [c["name"] for c in got] == ["Alpha", "Beta"]


def test_a_blank_line_is_tolerated_not_fatal():
    """A trailing newline is normal in JSONL; it must not raise."""
    body = gzip.compress(b'{"id":"a","name":"Alpha"}\n\n')

    class _Raw(__import__("io").BytesIO):
        pass

    resp = types.SimpleNamespace(raw=_Raw(body), raise_for_status=lambda: None, close=lambda: None)
    original = oracle_ingest.requests.get
    oracle_ingest.requests.get = lambda *a, **k: resp
    try:
        got = list(oracle_ingest._stream_jsonl("http://offline/oracle.jsonl.gz"))
    finally:
        oracle_ingest.requests.get = original

    assert [c["name"] for c in got] == ["Alpha"]
