"""Scryfall's bulk exports are gzipped JSON Lines, not a JSON array.

Scryfall removed ``download_uri`` from ``/bulk-data`` and replaced it with
``jsonl_download_uri``, changing the FORMAT at the same time. The bulk cache
guarded on the missing key and logged a skip, so it silently served a frozen
catalog from 2026-07-28 to 2026-08-07 (10 days); ``oracle_ingest`` indexed the
key directly and raised KeyError. This is the "grep for the question, not the
symptom" rule: the same upstream change broke THREE modules, and only one was
reported.

The third — ``scripts/scryfall_image_mirror.py`` — is why the consumer list
below is DISCOVERED rather than hardcoded. It lived outside the repo, so the
v4.13.14 sweep never reached it and this guard could not see it; it was still
dying on ``KeyError: 'download_uri'`` on 2026-08-15, weeks after the other two
were fixed. A hardcoded tuple only ever guards the copies you already knew
about, which is exactly the copy that is never the problem.
"""

import gzip
import json
import pathlib
import types

import app.legacy_tables  # noqa
from app.jobs import oracle_ingest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
# Every consumer of the Scryfall bulk-data contract, found by what it touches
# rather than by a list someone has to remember to update.
# Both markers are code-only on purpose: a bare "bulk-data" also matches English
# prose ("the bulk-data daemon" in deck_service/main), and a discovery rule that
# drags in commentary makes the guard fail on files that never touch Scryfall.
_MARKERS = ("api.scryfall.com/bulk-data", "download_uri")
_KNOWN = {"scryfall.py", "oracle_ingest.py", "scryfall_image_mirror.py"}


def _bulk_consumers() -> list[pathlib.Path]:
    return sorted(
        p
        for d in ("app", "scripts")
        for p in (_ROOT / d).rglob("*.py")
        if any(m in p.read_text() for m in _MARKERS)
    )


def test_discovery_finds_the_known_consumers():
    """Self-check: a discovery rule that silently matches nothing is a guard
    that passes forever. If a consumer is renamed or moved, fix the rule —
    do not let the set quietly shrink."""
    found = {p.name for p in _bulk_consumers()}
    assert _KNOWN <= found, f"bulk-data consumers went missing from discovery: {_KNOWN - found}"


def test_no_consumer_reads_the_removed_download_uri_key():
    """``download_uri`` no longer exists in the listing. Reading it is either a
    permanent skip or a KeyError, and both failed quietly enough to run for
    days — so the key must not reappear in any consumer."""
    for path in _bulk_consumers():
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
