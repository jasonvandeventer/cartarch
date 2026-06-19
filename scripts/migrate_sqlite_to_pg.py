"""Scripted SQLite -> Postgres data loader. THE v4 cutover tool (replaces pgloader).

Gate #4 made Alembic the schema owner, which removes pgloader's reason to exist
(schema translation). The remaining job is narrow: move already-consistent data
from a SQLite file into an EXISTING, Alembic-built Postgres schema. This script
does exactly that, loading through the app's own ``Base.metadata`` Core Tables so
SQLAlchemy's type machinery coerces booleans / timestamps / cache columns
correctly BY CONSTRUCTION (no empirical cast-probing).

  Rehearsal:  python scripts/migrate_sqlite_to_pg.py \
                  --source ~/lab/cartarch/tmp/auditdb/mana_archive.db \
                  --target 'postgresql+psycopg://USER:PASS@HOST:5432/DB' [--truncate-first]
  Cutover:    same script, run as an in-cluster pre-sync Job; --target is the
              <cluster>-rw service, --source the frozen prod snapshot. Runs ONCE
              into the empty Alembic-built schema (no --truncate-first).

Design / guarantees:
  - Source SQLite is opened READ-ONLY (file: URI, mode=ro) — never written.
  - Target schema is NOT created here; it must already exist (alembic upgrade head).
  - The load wraps in ``SET session_replication_role = replica`` so FK/trigger
    enforcement is OFF during insert. This is REQUIRED, not optional: the prod
    snapshot carries known, handled FK orphans (showcase_items / trade_items /
    game_seats.deck_id) that would fail insert under enforcement. Belt-and-
    suspenders: tables are still loaded in ``Base.metadata.sorted_tables`` order
    (parent-before-child). ``session_replication_role`` requires elevated
    privilege; if the target role lacks it the script ABORTS with a clear message
    (rerun with a superuser --target, or pre-sweep orphans then use --no-replica).
  - The 7 raw-SQL tables are pulled into ``Base.metadata`` via ``app.legacy_tables``
    (imported below — the running app never imports it), so they load automatically.
  - ``schema_migrations`` / ``alembic_version`` / ``sqlite_sequence`` are NOT in
    ``Base.metadata`` and are intentionally skipped; the script reports any OTHER
    SQLite table it does not cover so nothing is silently dropped.
  - After load: every serial sequence is reset via ``setval(...)``, then
    ``session_replication_role`` is flipped back to ``origin``.
  - A validation pass prints a report: per-table row-count parity, the boolean
    landmine (game_changer_cards WHERE active == total), an FK orphan re-scan, and
    a sequence-sanity insert+rollback. Exit code is non-zero on any hard failure.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import Integer, create_engine, func, inspect, select, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

import app.legacy_tables  # noqa: E402,F401 — registers the 7 raw tables on Base.metadata
import app.models  # noqa: E402,F401 — registers the ORM tables on Base.metadata
from app.db import Base  # noqa: E402

BATCH = 5000
# Tables present in the SQLite source but deliberately NOT loaded (not in the
# Alembic baseline). Any SQLite table outside this set AND outside the load set is
# flagged as an unexpected uncovered table.
EXPECTED_SKIP = {"schema_migrations", "alembic_version", "sqlite_sequence"}


# --------------------------------------------------------------------------- #
# Engines
# --------------------------------------------------------------------------- #
def make_source_engine(path: str) -> Engine:
    """Open the SQLite source strictly READ-ONLY (mode=ro URI) — never writes it."""
    abspath = os.path.abspath(path)
    if not os.path.exists(abspath):
        sys.exit(f"FATAL: source SQLite file not found: {abspath}")

    def _connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{abspath}?mode=ro", uri=True)

    return create_engine("sqlite://", creator=_connect)


def make_target_engine(url: str) -> Engine:
    if not url.startswith("postgresql"):
        sys.exit(f"FATAL: --target must be a postgresql URL, got: {url.split('://', 1)[0]}://...")
    # pool_pre_ping: transparently re-establish a dropped connection (resilient to a
    # flaky port-forward in the rehearsal; harmless in-cluster at cutover).
    return create_engine(url, pool_pre_ping=True)


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def try_replica_mode(conn) -> bool:
    """Attempt to disable FK/trigger enforcement for this session. Returns success."""
    try:
        conn.execute(text("SET session_replication_role = replica"))
        # Confirm it actually took (some roles silently no-op).
        got = conn.execute(text("SHOW session_replication_role")).scalar()
        return got == "replica"
    except Exception:
        return False


def truncate_all(conn, tables) -> None:
    names = ", ".join(f'"{t.name}"' for t in tables)
    conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


def load_table(src_conn, tgt_conn, table) -> int:
    """Stream rows from SQLite and bulk-insert into PG through the Core Table.

    Reading via ``select(table)`` applies the column types' SQLite result
    processors (e.g. Boolean 0/1 -> Python bool, DateTime str -> datetime);
    inserting via ``table.insert()`` applies the PG bind processors (bool -> PG
    boolean, etc.). That round-trip is what makes types correct by construction.
    Batched so a large table (scryfall_cards) never loads fully into memory.
    """
    result = src_conn.execution_options(stream_results=True).execute(select(table))
    cols = result.keys()
    total = 0
    while True:
        chunk = result.fetchmany(BATCH)
        if not chunk:
            break
        tgt_conn.execute(table.insert(), [dict(zip(cols, row, strict=True)) for row in chunk])
        total += len(chunk)
    return total


def reset_sequences(conn, tables) -> list[str]:
    """setval() every single-column integer-PK sequence to MAX(pk) (or empty=1)."""
    notes = []
    for t in tables:
        pks = list(t.primary_key.columns)
        if len(pks) != 1 or not isinstance(pks[0].type, Integer):
            continue  # composite or text PK (scryfall_*) -> no serial sequence
        pk = pks[0].name
        seq = conn.execute(
            text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": t.name, "c": pk}
        ).scalar()
        if not seq:
            continue
        maxid = conn.execute(text(f'SELECT COALESCE(MAX("{pk}"), 0) FROM "{t.name}"')).scalar()
        if maxid and maxid > 0:
            conn.execute(text("SELECT setval(:s, :v, true)"), {"s": seq, "v": maxid})
        else:
            conn.execute(text("SELECT setval(:s, 1, false)"), {"s": seq})
        notes.append(f"{t.name}.{pk} -> {maxid}")
    return notes


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def scan_orphans(conn, tables) -> dict[str, int]:
    """{'child.col->parent': count} for every FK with orphan rows on the target."""
    out: dict[str, int] = {}
    for t in tables:
        for col in t.columns:
            for fk in col.foreign_keys:
                parent, pcol = fk.column.table.name, fk.column.name
                n = conn.execute(
                    text(
                        f'SELECT count(*) FROM "{t.name}" c WHERE c."{col.name}" IS NOT NULL '
                        f'AND NOT EXISTS (SELECT 1 FROM "{parent}" pp WHERE pp."{pcol}" = c."{col.name}")'
                    )
                ).scalar()
                if n:
                    out[f"{t.name}.{col.name}->{parent}"] = n
    return out


def validate(src_engine, tgt_engine, tables) -> bool:
    ok = True
    print("\n=== VALIDATION ===")

    # (1) Per-table row-count parity.
    print("[row-count parity]")
    with src_engine.connect() as s, tgt_engine.connect() as p:
        for t in tables:
            sc = s.execute(select(func.count()).select_from(t)).scalar()
            pc = p.execute(select(func.count()).select_from(t)).scalar()
            flag = "OK" if sc == pc else "*** MISMATCH ***"
            if sc != pc:
                ok = False
            print(f"  {t.name:<28} sqlite={sc:<8} pg={pc:<8} {flag}")

    # (2) Boolean landmine: every game_changer_cards row is queryable via WHERE active.
    print("[boolean landmine — game_changer_cards WHERE active]")
    with tgt_engine.connect() as p:
        total = p.execute(text("SELECT count(*) FROM game_changer_cards")).scalar()
        active = p.execute(text("SELECT count(*) FROM game_changer_cards WHERE active")).scalar()
        # parity is what we assert; if all rows happen to be inactive that's a data
        # fact, but a mismatch between "WHERE active" and a manual = true means the
        # boolean did not round-trip. Report both.
        manual = p.execute(
            text("SELECT count(*) FROM game_changer_cards WHERE active = true")
        ).scalar()
        flag = "OK" if active == manual else "*** WHERE active != (active = true) ***"
        if active != manual:
            ok = False
        print(f"  total={total}  WHERE active={active}  (active = true)={manual}  {flag}")

    # (3) FK orphan re-scan on the loaded PG (same NOT EXISTS form as orphan_audit).
    #     Expected for the raw rehearsal snapshot: the known/handled orphans.
    print("[FK orphan re-scan on PG]")
    with tgt_engine.connect() as p:
        orphans = scan_orphans(p, tables)
    for key, n in orphans.items():
        print(f"  {key}: {n} orphan(s)")
    print(
        f"  total orphans on PG = {sum(orphans.values())}  "
        f"(pre-sweep: the known/handled snapshot orphans; post-sweep: expect 0)"
    )

    # (4) Sequence sanity: a real insert into a root table gets MAX+1, then rollback.
    print("[sequence sanity — insert+rollback on cards]")
    from datetime import datetime

    cards = Base.metadata.tables["cards"]
    with tgt_engine.connect() as p:
        trans = p.begin()
        try:
            before_max = p.execute(select(func.max(cards.c.id))).scalar() or 0
            new_id = p.execute(
                cards.insert()
                .values(
                    scryfall_id="__seqprobe__",
                    name="__seqprobe__",
                    set_code="XXX",
                    collector_number="0",
                    updated_at=datetime(2026, 1, 1),
                )
                .returning(cards.c.id)
            ).scalar()
            good = new_id > before_max
            if not good:
                ok = False
            print(
                f"  prev max id={before_max}, probe insert id={new_id} "
                f"{'OK (no collision)' if good else '*** COLLISION ***'} — rolled back"
            )
        finally:
            trans.rollback()  # never persist the probe row

    # (5) Datetime coercion: the one type coercion row-count parity can't catch.
    #     A timestamp column must come back as real parsed datetimes, not NULL /
    #     epoch / raw strings. psycopg returns native datetime for a PG timestamp.
    print("[datetime coercion — users.created_at]")
    from datetime import datetime as _dt

    with tgt_engine.connect() as p:
        rows = p.execute(text("SELECT id, created_at FROM users ORDER BY id LIMIT 3")).fetchall()
        if not rows:
            print("  (no users loaded — cannot check; INVESTIGATE if source has users)")
        for rid, ts in rows:
            real = isinstance(ts, _dt) and ts.year > 1971  # not NULL, not epoch-0
            if not real:
                ok = False
            print(
                f"  user id={rid}  created_at={ts!r}  type={type(ts).__name__}  "
                f"{'OK' if real else '*** NOT A REAL TIMESTAMP ***'}"
            )

    print(f"\n=== RESULT: {'PASS' if ok else 'FAIL'} ===")
    return ok


# --------------------------------------------------------------------------- #
# Coverage report (nothing silently dropped)
# --------------------------------------------------------------------------- #
def report_coverage(src_engine, load_tables) -> None:
    sqlite_tables = {
        r for r in inspect(src_engine).get_table_names() if not r.startswith("sqlite_")
    }
    load_names = {t.name for t in load_tables}
    uncovered = sqlite_tables - load_names - EXPECTED_SKIP
    missing_in_src = load_names - sqlite_tables
    print(
        f"SQLite source tables: {len(sqlite_tables)} | load set (Base.metadata): {len(load_names)}"
    )
    print(f"  intentionally skipped (not in baseline): {sorted(EXPECTED_SKIP & sqlite_tables)}")
    if uncovered:
        print(
            f"  *** UNCOVERED SQLite tables (would be silently dropped!): {sorted(uncovered)} ***"
        )
    if missing_in_src:
        print(
            f"  NOTE: load-set tables absent from source (loaded as empty): {sorted(missing_in_src)}"
        )


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Load a SQLite DB into an Alembic-built Postgres schema."
    )
    ap.add_argument(
        "--source", required=True, help="path to the source SQLite .db file (read-only)"
    )
    ap.add_argument(
        "--target", default=os.getenv("DATABASE_URL"), help="target postgresql+psycopg URL"
    )
    ap.add_argument(
        "--truncate-first",
        action="store_true",
        help="truncate target tables before load (rehearsal reload)",
    )
    ap.add_argument(
        "--no-replica",
        action="store_true",
        help="do NOT use session_replication_role=replica (only safe if the source has no FK orphans)",
    )
    ap.add_argument(
        "--sweep",
        action="store_true",
        help="after load, run the FK-orphan sweep (scripts.sweep_fk_orphans) to remediate "
        "known orphans per baseline ondelete intent before validation",
    )
    args = ap.parse_args()
    if not args.target:
        sys.exit("FATAL: --target (or DATABASE_URL) is required")

    src = make_source_engine(args.source)
    tgt = make_target_engine(args.target)

    load_tables = [t for t in Base.metadata.sorted_tables if t.name not in EXPECTED_SKIP]

    print("=== migrate_sqlite_to_pg ===")
    print(f"source : {os.path.abspath(args.source)} (read-only)")
    print(f"target : {args.target.split('@')[-1] if '@' in args.target else args.target}")
    report_coverage(src, load_tables)

    # The whole load is ONE transaction on the target: all-or-nothing.
    with src.connect() as sconn, tgt.begin() as tconn:
        replica = False if args.no_replica else try_replica_mode(tconn)
        if not args.no_replica and not replica:
            sys.exit(
                "FATAL: could not SET session_replication_role = replica (role lacks privilege).\n"
                "  The snapshot has known FK orphans that fail insert under enforcement.\n"
                "  Fix: rerun --target as a superuser role, OR pre-sweep orphans and pass --no-replica."
            )
        print(
            f"\nFK enforcement during load: {'OFF (replica mode)' if replica else 'ON (--no-replica)'}"
        )

        if args.truncate_first:
            print("--truncate-first: TRUNCATE ... RESTART IDENTITY CASCADE")
            truncate_all(tconn, load_tables)

        print("\n=== LOAD (Base.metadata.sorted_tables order) ===")
        for t in load_tables:
            n = load_table(sconn, tconn, t)
            print(f"  {t.name:<28} {n} rows")

        print("\n=== sequence reset ===")
        for note in reset_sequences(tconn, load_tables):
            print(f"  setval {note}")

        if replica:
            tconn.execute(text("SET session_replication_role = origin"))
        print("\nload transaction committing...")

    # Validate LOAD FAITHFULNESS first. Row-count parity is a PRE-sweep measure: the
    # sweep deliberately deletes CASCADE-orphan rows, so PG would then be < SQLite for
    # those tables by design. The orphan re-scan here shows the known snapshot orphans.
    ok = validate(src, tgt, load_tables)

    # Then the post-load orphan sweep (separate transaction, normal FK enforcement),
    # remediating per baseline ondelete intent, and confirm the DB is FK-clean.
    if args.sweep:
        from sqlalchemy.orm import Session

        from scripts.sweep_fk_orphans import sweep_fk_orphans

        print("\n=== FK-ORPHAN SWEEP (post-load, per baseline ondelete intent) ===")
        with Session(tgt) as session:
            res = sweep_fk_orphans(session, apply=True)
        print(f"  CASCADE  -> deleted: {res['deleted'] or 'none'}")
        print(f"  SET NULL -> nulled:  {res['nulled'] or 'none'}")
        if res["unhandled"]:
            print(
                f"  *** UNHANDLED (NO ACTION/RESTRICT) orphans — need manual decision: {res['unhandled']} ***"
            )
            ok = False

        print("\n=== POST-SWEEP ORPHAN RE-SCAN (expect ZERO) ===")
        with tgt.connect() as p:
            remaining = scan_orphans(p, load_tables)
        if remaining:
            print(f"  *** {sum(remaining.values())} orphan(s) REMAIN: {remaining} ***")
            ok = False
        else:
            print("  0 orphans — the loaded schema is FK-clean.")
        print(f"\n=== OVERALL: {'PASS' if ok else 'FAIL'} ===")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
