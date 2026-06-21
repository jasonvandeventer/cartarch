"""v4 cutover GATE — the step-11 GO/NO-GO validation (read-only, decision-surfacing).

Runs AFTER the load (migrate_sqlite_to_pg.py) AND the sweep (sweep_fk_orphans.py),
against the loaded green Postgres, with the source SQLite snapshot mounted read-only
for parity. This is the runbook's step-11 gate: it PRINTS a clearly-inspectable
report and exits 0 (all hard checks pass — human may GO) or 1 (a hard check failed —
ABORT). It writes NOTHING (the one probe insert is rolled back). The final GO/NO-GO
is the operator's: the row-count reconciliation is surfaced for a human to confirm
against the Stage-3 sweep counts; everything else hard-passes or hard-fails.

The four checks (mirrors the rehearsal / v4-cutover-runbook step 11):
  1. ZERO FK orphans on PG          — confirms the sweep took (belt-and-suspenders).
  2. Row-count parity per table     — source SQLite vs green PG; pg must never exceed
                                       source; any deficit must be in a CASCADE-FK
                                       table (a swept table) and the TOTAL deficit is
                                       printed for the operator to reconcile against
                                       the Stage-3 deleted count.
  3. Boolean landmine               — game_changer_cards.active round-trips (WHERE
                                       active == WHERE active = true, > 0) AND the
                                       REAL app path `_gather_deck_signals` executes
                                       that WHERE against PG's native boolean without
                                       error on a representative deck.
  4. Sequence sanity                — a real insert into cards gets MAX+1 (no
                                       collision), then rolled back.
  5. Cache round-trip               — the scryfall_cards seam is byte-identical across
                                       the backend swap: the SAME row read from SQLite
                                       and from PG, each through `_cached_row_to_payload`,
                                       yields equal payloads (full_art int->bool, cmc
                                       REAL, JSON/text passthrough, NULL->None).

Usage (in-cluster Job; DATABASE_URL -> green PG via the CNPG secret):
    DATABASE_URL=postgresql+psycopg://... \
        python -m scripts.validate_cutover --source /snapshot/mana_archive.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import app.legacy_tables  # noqa: E402,F401 — registers the 7 raw tables on Base.metadata
import app.models  # noqa: E402,F401 — registers the ORM tables on Base.metadata
from app.bracket_v2_service import _gather_deck_signals  # noqa: E402
from app.db import Base, engine  # noqa: E402 — engine binds DATABASE_URL (green PG)
from app.scryfall import _CACHE_COLUMNS, _cached_row_to_payload  # noqa: E402
from scripts.sweep_fk_orphans import find_orphans  # noqa: E402

EXPECTED_SKIP = {"schema_migrations", "alembic_version", "sqlite_sequence"}
CACHE_SAMPLE = 200  # scryfall_cards rows compared byte-for-byte (full table would be fine but slow)


def _load_tables():
    return [t for t in Base.metadata.sorted_tables if t.name not in EXPECTED_SKIP]


def make_source_engine(path: str) -> Engine:
    """Open the SQLite source strictly READ-ONLY (mode=ro URI) — never writes it."""
    abspath = os.path.abspath(path)
    if not os.path.exists(abspath):
        sys.exit(f"FATAL: source SQLite snapshot not found: {abspath}")

    def _connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{abspath}?mode=ro", uri=True)

    return create_engine("sqlite://", creator=_connect)


# --------------------------------------------------------------------------- #
def check_zero_orphans(tables) -> bool:
    print("\n[1] FK ORPHANS ON PG (expect ZERO — confirms the sweep took)")
    with Session(engine) as s:
        orphans = find_orphans(s)
    if orphans:
        for key, ids in orphans.items():
            print(f"    *** {key}: {len(ids)} orphan(s) REMAIN ***")
        print(f"    RESULT: FAIL — {sum(len(v) for v in orphans.values())} orphan(s) on PG")
        return False
    print("    0 orphans — the loaded schema is FK-clean. OK")
    return True


def check_row_parity(src_engine, tables) -> bool:
    """pg must never exceed source; a deficit is only legitimate in a CASCADE-FK table
    (one the sweep deletes from). Prints sqlite/pg/delta per table + the total deficit
    for the operator to reconcile against the Stage-3 deleted count."""
    print("\n[2] ROW-COUNT PARITY (source SQLite vs green PG)")
    cascade_tables = {
        t.name
        for t in tables
        for col in t.columns
        for fk in col.foreign_keys
        if (fk.ondelete or "").upper() == "CASCADE"
    }
    ok = True
    total_deficit = 0
    with src_engine.connect() as s:
        with engine.connect() as p:
            for t in tables:
                sc = s.execute(select(func.count()).select_from(t)).scalar()
                pc = p.execute(select(func.count()).select_from(t)).scalar()
                delta = sc - pc
                if pc > sc:
                    flag = "*** PG > SOURCE (phantom rows!) ***"
                    ok = False
                elif delta == 0:
                    flag = "OK"
                elif t.name in cascade_tables:
                    flag = f"-{delta} swept (verify vs Stage-3)"
                    total_deficit += delta
                else:
                    flag = f"*** -{delta} deficit in a NON-CASCADE table ***"
                    ok = False
                print(f"    {t.name:<28} sqlite={sc:<8} pg={pc:<8} {flag}")
    print(
        f"    total swept deficit = {total_deficit} "
        f"(OPERATOR: confirm this equals the Stage-3 'deleted' total before GO)"
    )
    print(f"    RESULT: {'OK' if ok else 'FAIL'}")
    return ok


def check_boolean_landmine() -> bool:
    print("\n[3] BOOLEAN LANDMINE — game_changer_cards.active via the real app path")
    ok = True
    with engine.connect() as p:
        total = p.execute(text("SELECT count(*) FROM game_changer_cards")).scalar()
        active = p.execute(text("SELECT count(*) FROM game_changer_cards WHERE active")).scalar()
        manual = p.execute(
            text("SELECT count(*) FROM game_changer_cards WHERE active = true")
        ).scalar()
        if active != manual or active == 0:
            ok = False
        print(
            f"    total={total}  WHERE active={active}  (active = true)={manual}  "
            f"{'OK' if (active == manual and active) else '*** boolean did not round-trip ***'}"
        )
        # Exercise the REAL code path that issues `WHERE active` against PG's boolean.
        deck = p.execute(
            text(
                "SELECT ir.storage_location_id AS loc, ir.user_id AS uid, count(*) AS c "
                "FROM inventory_rows ir JOIN storage_locations sl "
                "ON sl.id = ir.storage_location_id "
                "WHERE sl.type = 'deck' GROUP BY ir.storage_location_id, ir.user_id "
                "ORDER BY c DESC LIMIT 1"
            )
        ).first()
    if deck is None:
        print(
            "    (no deck with inventory rows in the snapshot — _gather_deck_signals path SKIPPED)"
        )
        return ok
    with Session(engine) as s:
        try:
            signals = _gather_deck_signals(s, deck.loc, deck.uid)
        except Exception as e:  # noqa: BLE001 — any error here is a hard gate failure
            print(f"    *** _gather_deck_signals RAISED on PG: {e!r} ***")
            return False
    gc = signals.get("game_changers", [])
    print(
        f"    _gather_deck_signals(deck_loc={deck.loc}, user={deck.uid}) ran OK — "
        f"{len(gc)} game-changer(s) on that deck (WHERE active executed on native boolean)"
    )
    return ok


def check_sequence_sanity() -> bool:
    print("\n[4] SEQUENCE SANITY — insert+rollback probe on cards (MAX+1, no collision)")
    from datetime import datetime

    cards = Base.metadata.tables["cards"]
    with engine.connect() as p:
        trans = p.begin()
        try:
            before = p.execute(select(func.max(cards.c.id))).scalar() or 0
            new_id = p.execute(
                cards.insert()
                .values(
                    scryfall_id="__cutover_seqprobe__",
                    name="__cutover_seqprobe__",
                    set_code="XXX",
                    collector_number="0",
                    updated_at=datetime(2026, 1, 1),
                )
                .returning(cards.c.id)
            ).scalar()
            good = new_id > before
            print(
                f"    prev max id={before}, probe insert id={new_id} "
                f"{'OK (no collision) — rolled back' if good else '*** COLLISION ***'}"
            )
            return good
        finally:
            trans.rollback()


def check_cache_roundtrip(src_engine) -> bool:
    print(f"\n[5] CACHE ROUND-TRIP — scryfall_cards seam byte-identical (sample {CACHE_SAMPLE})")
    sample_sql = text(
        f"SELECT {_CACHE_COLUMNS} FROM scryfall_cards "
        f"WHERE scryfall_id IS NOT NULL ORDER BY scryfall_id LIMIT :n"
    )
    one_sql = text(f"SELECT {_CACHE_COLUMNS} FROM scryfall_cards WHERE scryfall_id = :sid")
    with src_engine.connect() as s:
        src_rows = s.execute(sample_sql, {"n": CACHE_SAMPLE}).mappings().all()
    if not src_rows:
        print("    (no scryfall_cards rows in snapshot — SKIPPED)")
        return True
    mismatches = 0
    with engine.connect() as p:
        for sm in src_rows:
            sid = sm["scryfall_id"]
            pm = p.execute(one_sql, {"sid": sid}).mappings().first()
            if pm is None:
                print(f"    *** {sid}: present in SQLite, MISSING on PG ***")
                mismatches += 1
                continue
            if _cached_row_to_payload(sm) != _cached_row_to_payload(pm):
                print(f"    *** {sid}: cache payload differs SQLite vs PG ***")
                mismatches += 1
    if mismatches:
        print(f"    RESULT: FAIL — {mismatches}/{len(src_rows)} row(s) not byte-identical")
        return False
    print(f"    {len(src_rows)} rows byte-identical SQLite<->PG. OK")
    return True


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="v4 cutover GO/NO-GO validation gate (read-only).")
    ap.add_argument(
        "--source", required=True, help="path to the source SQLite snapshot (read-only)"
    )
    args = ap.parse_args()

    if engine.dialect.name != "postgresql":
        sys.exit(f"FATAL: DATABASE_URL must point at Postgres, got dialect {engine.dialect.name!r}")

    src = make_source_engine(args.source)
    tables = _load_tables()

    print("=== v4 CUTOVER VALIDATION GATE (step 11) ===")
    print(f"source : {os.path.abspath(args.source)} (read-only)")
    print(f"target : {engine.url.host}/{engine.url.database} (green PG)")
    print(f"tables : {len(tables)} (Base.metadata minus {sorted(EXPECTED_SKIP)})")

    results = {
        "zero-orphans": check_zero_orphans(tables),
        "row-parity": check_row_parity(src, tables),
        "boolean-landmine": check_boolean_landmine(),
        "sequence-sanity": check_sequence_sanity(),
        "cache-roundtrip": check_cache_roundtrip(src),
    }

    print("\n=== GATE SUMMARY ===")
    for name, ok in results.items():
        print(f"    {name:<20} {'PASS' if ok else '*** FAIL ***'}")
    overall = all(results.values())
    print(
        f"\n=== GATE RESULT: {'PASS — hard checks green; OPERATOR makes GO/NO-GO' if overall else 'FAIL — ABORT (Phase R)'} ==="
    )
    print(
        "    (A PASS clears the automated checks only. Reconcile the row-parity deficit "
        "against the Stage-3 sweep counts, then decide GO/NO-GO. A FAIL means ABORT.)"
    )
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
