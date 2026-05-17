"""Offline contract test for the v3.25.0 _bulk_data_loop daemon.

Standalone runner (no pytest — same pattern as tests/test_scryfall_cache.py).
Invoke via:

    DATA_DIR=dev-data DEV_MODE=true python -m tests.test_bulk_data_loop

Drives the REAL ``refresh_bulk_cache`` with zero network: ``_get_json`` and
``_session`` are monkeypatched to feed a BytesIO body, and ``engine`` is
pointed at a throwaway temp SQLite file. So the freshness guard, the real
``ijson.items(..., use_float=True)`` streaming parse, the real
``_BULK_UPSERT_SQL`` ON CONFLICT upsert, per-batch commit, and the
meta-only-after-success ordering are all exercised exactly as in production.

Pins four contract properties:
  1. streamed cards land byte-identical to _normalize_card_payload (reuses
     the step-2 contract via _cached_row_to_payload + the same fixtures);
  2. re-running the same export over a populated table is a no-op
     (ON CONFLICT idempotency) — and the freshness guard short-circuits a
     same-updated_at re-run with no streaming at all;
  3. an exception mid-population leaves scryfall_bulk_meta at its prior
     value (or unset) — never the new updated_at (partial-write recovery),
     while already-committed batches stay durable;
  4. batch flushing fires at the 1000-row boundary and never drops the
     trailing partial batch (1500 rows -> flushes of 1000 then 500).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import types

from sqlalchemy import create_engine, text

import app.scryfall as sc
from app.scryfall import _CACHE_COLUMNS, _cached_row_to_payload, _normalize_card_payload
from tests.test_scryfall_cache import (
    RAW_FULLART,
    RAW_LEGALITIES_COLORLESS,
    RAW_MULTIFACE,
    RAW_NORMAL,
)

# DDL mirrors scripts/migrate_v3_25_0_scryfall_cards.py. test_scryfall_cache
# already guards scryfall_cards columns vs the normalizer; meta is trivial.
_CARDS_DDL = """
CREATE TABLE scryfall_cards (
    scryfall_id      TEXT PRIMARY KEY,
    name             TEXT,
    set_code         TEXT,
    set_name         TEXT,
    collector_number TEXT,
    rarity           TEXT,
    image_url        TEXT,
    type_line        TEXT,
    oracle_text      TEXT,
    price_usd        TEXT,
    price_usd_foil   TEXT,
    price_usd_etched TEXT,
    colors           TEXT,
    color_identity   TEXT,
    mana_cost        TEXT,
    cmc              REAL,
    legalities       TEXT,
    full_art         INTEGER,
    frame_effects    TEXT,
    set_type         TEXT,
    layout           TEXT
)
"""
_META_DDL = "CREATE TABLE scryfall_bulk_meta (key TEXT PRIMARY KEY, value TEXT)"

_BASE_FIXTURES = [RAW_NORMAL, RAW_MULTIFACE, RAW_LEGALITIES_COLORLESS, RAW_FULLART]


# ---------------------------------------------------------------------------
# Offline harness
# ---------------------------------------------------------------------------


class _FakeRaw(io.BytesIO):
    """BytesIO subclass: ijson can .read() it AND the daemon can set
    .decode_content on it (plain io.BytesIO forbids attribute assignment).
    """


class _FakeResp:
    def __init__(self, body: bytes):
        self.raw = _FakeRaw(body)

    def raise_for_status(self) -> None:
        pass

    def close(self) -> None:
        self.raw.close()


def _json_array(cards: list[dict], *, terminated: bool = True) -> bytes:
    """Serialize cards as a JSON array. terminated=False omits the closing
    ``]`` so a real ijson stream raises IncompleteJSONError after yielding
    every complete object (the mid-population failure case).
    """
    body = b"[" + b",".join(json.dumps(c).encode() for c in cards)
    return body + b"]" if terminated else body


def _temp_engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as conn:
        conn.execute(text(_CARDS_DDL))
        conn.execute(text(_META_DDL))
    return eng, path


def _run_refresh(eng, body: bytes, updated_at: str, flush_sizes: list[int] | None = None):
    """Invoke the real sc.refresh_bulk_cache offline against ``eng``.

    Returns its int result. If flush_sizes is given, every real
    _flush_bulk_batch call appends its batch length (boundary spy).
    """
    saved_engine = sc.engine
    saved_get_json = sc._get_json
    saved_session = sc._session
    saved_throttle = sc._throttle
    saved_flush = sc._flush_bulk_batch
    try:
        sc.engine = eng
        sc._throttle = lambda: None
        sc._get_json = lambda url: {
            "data": [
                {
                    "type": "default_cards",
                    "updated_at": updated_at,
                    "download_uri": "http://offline/default-cards.json",
                }
            ]
        }
        sc._session = types.SimpleNamespace(get=lambda *a, **k: _FakeResp(body))
        if flush_sizes is not None:
            real_flush = sc._flush_bulk_batch

            def _spy(batch):
                if batch:
                    flush_sizes.append(len(batch))
                return real_flush(batch)

            sc._flush_bulk_batch = _spy
        return sc.refresh_bulk_cache()
    finally:
        sc.engine = saved_engine
        sc._get_json = saved_get_json
        sc._session = saved_session
        sc._throttle = saved_throttle
        sc._flush_bulk_batch = saved_flush


def _count(eng) -> int:
    with eng.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM scryfall_cards")).scalar_one()


def _meta(eng) -> str | None:
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM scryfall_bulk_meta WHERE key = 'default_cards_updated_at'")
        ).fetchone()
    return row[0] if row else None


def _row_payload(eng, scryfall_id: str) -> dict:
    with eng.connect() as conn:
        row = (
            conn.execute(
                text(f"SELECT {_CACHE_COLUMNS} FROM scryfall_cards WHERE scryfall_id = :i"),
                {"i": scryfall_id},
            )
            .mappings()
            .fetchone()
        )
    return _cached_row_to_payload(row)


def _clone(base: dict, n: int) -> list[dict]:
    """n unique cards cloned from base (unique id + collector_number)."""
    out = []
    for i in range(n):
        c = dict(base)
        c["id"] = f"{base['id']}-{i}"
        c["collector_number"] = f"{i}"
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stream_byte_identical() -> tuple[int, int]:
    """Streamed+upserted rows == _normalize_card_payload for each input."""
    passed = failed = 0
    eng, path = _temp_engine()
    try:
        n = _run_refresh(eng, _json_array(_BASE_FIXTURES), "2026-05-16T00:00:00Z")
        if n == len(_BASE_FIXTURES) and _count(eng) == len(_BASE_FIXTURES):
            print(f"  [OK] populated {n} cards")
            passed += 1
        else:
            print(f"  [FAIL] expected {len(_BASE_FIXTURES)} cards, got n={n} count={_count(eng)}")
            failed += 1

        all_match = True
        for raw in _BASE_FIXTURES:
            expected = _normalize_card_payload(raw)
            got = _row_payload(eng, expected["scryfall_id"])
            if got != expected:
                all_match = False
                diff = {k: (expected[k], got.get(k)) for k in expected if expected[k] != got.get(k)}
                print(f"  [FAIL] {raw['name']}: differs at {diff}")
        if all_match:
            print("  [OK] every streamed row byte-identical to _normalize_card_payload")
            passed += 1
        else:
            failed += 1

        if _meta(eng) == "2026-05-16T00:00:00Z":
            print("  [OK] meta advanced to updated_at after success")
            passed += 1
        else:
            print(f"  [FAIL] meta not advanced: {_meta(eng)!r}")
            failed += 1
    finally:
        eng.dispose()
        os.unlink(path)
    return passed, failed


def test_idempotent_rerun_and_freshness_skip() -> tuple[int, int]:
    passed = failed = 0
    eng, path = _temp_engine()
    try:
        body = _json_array(_BASE_FIXTURES)
        _run_refresh(eng, body, "A")
        count_a = _count(eng)
        snapshot = {
            r["id"]: _row_payload(eng, _normalize_card_payload(r)["scryfall_id"])
            for r in _BASE_FIXTURES
        }

        # Re-run with a CHANGED updated_at -> re-streams, upserts over the
        # same scryfall_ids. ON CONFLICT idempotency: counts/content stable.
        flushes: list[int] = []
        _run_refresh(eng, body, "B", flush_sizes=flushes)
        same_count = _count(eng) == count_a
        same_content = all(
            _row_payload(eng, _normalize_card_payload(r)["scryfall_id"]) == snapshot[r["id"]]
            for r in _BASE_FIXTURES
        )
        if same_count and same_content and len(flushes) > 0:
            print(f"  [OK] re-run idempotent: count stable ({count_a}), content unchanged")
            passed += 1
        else:
            print(
                f"  [FAIL] re-run not idempotent: count_stable={same_count} "
                f"content_stable={same_content} flushes={flushes}"
            )
            failed += 1
        if _meta(eng) == "B":
            print("  [OK] meta advanced on the changed-updated_at re-run")
            passed += 1
        else:
            print(f"  [FAIL] meta should be 'B', got {_meta(eng)!r}")
            failed += 1

        # Re-run with the SAME updated_at -> freshness guard skips entirely:
        # no streaming, no flush, returns 0.
        flushes2: list[int] = []
        ret = _run_refresh(eng, body, "B", flush_sizes=flushes2)
        if ret == 0 and flushes2 == []:
            print("  [OK] freshness guard short-circuits same-updated_at re-run (no stream)")
            passed += 1
        else:
            print(f"  [FAIL] freshness guard didn't skip: ret={ret} flushes={flushes2}")
            failed += 1
    finally:
        eng.dispose()
        os.unlink(path)
    return passed, failed


def test_partial_write_recovery() -> tuple[int, int]:
    """Exception mid-population => meta NEVER advances to the new value;
    already-committed batches stay durable.
    """
    passed = failed = 0

    # 1100 cards, UNterminated array -> real ijson yields 1100 then raises
    # IncompleteJSONError. batch=1000: one batch commits (1000 durable),
    # 100 buffered are lost, meta write never reached.
    cards = _clone(RAW_NORMAL, 1100)

    # (a) no prior meta -> stays unset
    eng, path = _temp_engine()
    try:
        raised = False
        try:
            _run_refresh(eng, _json_array(cards, terminated=False), "NEW")
        except Exception:
            raised = True
        if raised and _meta(eng) is None and _count(eng) == 1000:
            print("  [OK] mid-stream failure: meta stays unset, 1000 committed rows durable")
            passed += 1
        else:
            print(
                f"  [FAIL] raised={raised} meta={_meta(eng)!r} count={_count(eng)} "
                "(expected raised, meta=None, count=1000)"
            )
            failed += 1
    finally:
        eng.dispose()
        os.unlink(path)

    # (b) prior meta 'OLD' -> stays 'OLD', never 'NEW'
    eng, path = _temp_engine()
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO scryfall_bulk_meta (key, value) "
                    "VALUES ('default_cards_updated_at', 'OLD')"
                )
            )
        try:
            _run_refresh(eng, _json_array(cards, terminated=False), "NEW")
        except Exception:
            pass
        if _meta(eng) == "OLD":
            print("  [OK] mid-stream failure: prior meta 'OLD' preserved (never 'NEW')")
            passed += 1
        else:
            print(f"  [FAIL] meta should remain 'OLD', got {_meta(eng)!r}")
            failed += 1
    finally:
        eng.dispose()
        os.unlink(path)
    return passed, failed


def test_batch_boundary_no_trailing_loss() -> tuple[int, int]:
    """1500 rows, batch=1000 -> flushes of [1000, 500]; all 1500 present
    (trailing partial batch not dropped).
    """
    passed = failed = 0
    eng, path = _temp_engine()
    try:
        cards = _clone(RAW_NORMAL, 1500)
        flushes: list[int] = []
        n = _run_refresh(eng, _json_array(cards), "D", flush_sizes=flushes)
        if flushes == [1000, 500]:
            print("  [OK] flush boundary fired exactly at 1000 then 500")
            passed += 1
        else:
            print(f"  [FAIL] expected flushes [1000, 500], got {flushes}")
            failed += 1
        if n == 1500 and _count(eng) == 1500:
            print("  [OK] all 1500 rows present — trailing 500 not lost")
            passed += 1
        else:
            print(f"  [FAIL] expected 1500 rows, n={n} count={_count(eng)}")
            failed += 1
        if _meta(eng) == "D":
            print("  [OK] meta advanced after full 1500-row success")
            passed += 1
        else:
            print(f"  [FAIL] meta should be 'D', got {_meta(eng)!r}")
            failed += 1
    finally:
        eng.dispose()
        os.unlink(path)
    return passed, failed


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all() -> int:
    total_p = total_f = 0
    suites = [
        ("Test 1: streamed cards byte-identical to normalizer", test_stream_byte_identical),
        (
            "Test 2: idempotent re-run + freshness-guard skip",
            test_idempotent_rerun_and_freshness_skip,
        ),
        (
            "Test 3: partial-write recovery (meta never advances on failure)",
            test_partial_write_recovery,
        ),
        ("Test 4: batch boundary — no trailing-batch loss", test_batch_boundary_no_trailing_loss),
    ]
    for title, fn in suites:
        print(f"\n=== {title} ===")
        p, f = fn()
        total_p += p
        total_f += f
    print(f"\n{'=' * 60}")
    print(f"TOTAL: {total_p} passed, {total_f} failed")
    return total_f


if __name__ == "__main__":
    sys.exit(run_all())
