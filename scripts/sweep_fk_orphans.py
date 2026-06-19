"""FK-orphan sweep — FK-DEFINITION-DRIVEN remediation per the baseline ondelete intent.

When a parent row is deleted while ``PRAGMA foreign_keys`` is OFF (the SQLite
reality pre-v4), child rows are left pointing at a missing parent. Those orphans
are (a) silently wrong today and (b) **block enabling the FK at the Postgres
cutover** / load as violating rows. This sweep makes every FK enable-able by
remediating each orphan class according to the FK's OWN ``ondelete`` rule — the
exact rule encoded in the Gate #4 Alembic baseline:

  - ``ondelete=CASCADE``  → the child is meaningless without its parent → **DELETE**
    the orphan child rows.
  - ``ondelete=SET NULL`` → the child carries durable value (e.g. trade snapshot,
    game seat history) → **NULL** the dangling reference, keep the row.
  - ``ondelete`` NO ACTION / RESTRICT → there is no automatic intent; orphans are
    **reported (never auto-modified)** so a human decides.

It is **driven by ``Base.metadata`` FK definitions, not a hardcoded table list**,
so it cannot drift from the schema: add an FK and the sweep covers it. CRITICAL:
this imports ``app.legacy_tables`` so the 7 raw-SQL tables' FKs
(``deck_bracket_estimates/findings.deck_id`` etc.) are scanned too — the earlier
hardcoded sweep + the unfixed ``orphan_audit`` were blind to them (rehearsal
2026-06-18 surfaced 11 raw-table orphans the gate-#4 audit had missed).

Remediation loops to a fixpoint: a CASCADE delete can orphan a grandchild via a
second FK (e.g. deleting an orphaned ``deck_bracket_estimates`` row orphans its
``deck_bracket_findings`` via ``estimate_id``), which the next pass cleans. Works
identically whether FK enforcement is ON or OFF (purely explicit, not relying on
DB-level cascade).

Usage:
    DATABASE_URL=... python -m scripts.sweep_fk_orphans            # apply + report
    DATABASE_URL=... python -m scripts.sweep_fk_orphans --dry-run  # report only
"""

from __future__ import annotations

import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

import app.legacy_tables  # noqa: F401 — registers the 7 raw tables (and their FKs)
import app.models  # noqa: F401 — registers the ORM tables
from app.db import Base


def _fk_specs():
    """Every FK in the schema: (child_table, child_col, parent_table_name, parent_col, ondelete)."""
    specs = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            for fk in col.foreign_keys:
                ondelete = (fk.ondelete or "NO ACTION").upper()
                specs.append((table, col.name, fk.column.table.name, fk.column.name, ondelete))
    return specs


def _orphan_ids(session: Session, child, child_col, parent_name, parent_col) -> list:
    """PKs of child rows whose non-NULL FK has no matching parent row."""
    pk = next(iter(child.primary_key.columns)).name
    sql = text(
        f'SELECT c."{pk}" FROM "{child.name}" c '
        f'WHERE c."{child_col}" IS NOT NULL '
        f'AND NOT EXISTS (SELECT 1 FROM "{parent_name}" p WHERE p."{parent_col}" = c."{child_col}")'
    )
    return [r[0] for r in session.execute(sql).fetchall()]


def find_orphans(session: Session) -> dict[str, list]:
    """{ 'child.col->parent': [orphan child PKs] } for every FK that has orphans (read-only)."""
    out: dict[str, list] = {}
    for child, col, parent_name, pcol, _ in _fk_specs():
        ids = _orphan_ids(session, child, col, parent_name, pcol)
        if ids:
            out[f"{child.name}.{col}->{parent_name}"] = ids
    return out


def sweep_fk_orphans(session: Session, *, apply: bool = True) -> dict[str, dict[str, int]]:
    """Remediate FK orphans per ondelete intent. Returns counts by action and FK.

    Returns ``{"deleted": {fk: n}, "nulled": {fk: n}, "unhandled": {fk: n}}``.
    ``unhandled`` = orphans on a NO ACTION / RESTRICT FK (no automatic intent —
    surfaced, never modified). With ``apply=False`` nothing is written/committed
    and the counts are what *would* be remediated (single pass).
    """
    deleted: dict[str, int] = {}
    nulled: dict[str, int] = {}
    unhandled: dict[str, int] = {}

    max_passes = 25 if apply else 1
    for _ in range(max_passes):
        changed = False
        for child, col, parent_name, pcol, ondelete in _fk_specs():
            ids = _orphan_ids(session, child, col, parent_name, pcol)
            if not ids:
                continue
            key = f"{child.name}.{col}->{parent_name}"
            child_pk = next(iter(child.primary_key.columns))
            if ondelete == "CASCADE":
                if apply:
                    session.execute(child.delete().where(child_pk.in_(ids)))
                    changed = True
                deleted[key] = deleted.get(key, 0) + len(ids)
            elif ondelete == "SET NULL":
                if apply:
                    session.execute(child.update().where(child_pk.in_(ids)).values({col: None}))
                    changed = True
                nulled[key] = nulled.get(key, 0) + len(ids)
            else:  # NO ACTION / RESTRICT — no automatic intent
                unhandled[key] = len(ids)
        if not changed:
            break

    if apply:
        session.commit()
    return {"deleted": deleted, "nulled": nulled, "unhandled": unhandled}


def main(argv: list[str] | None = None) -> dict[str, dict[str, int]]:
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in argv

    from app.db import SessionLocal

    with SessionLocal() as session:
        result = sweep_fk_orphans(session, apply=not dry)

    mode = "DRY-RUN (no changes written)" if dry else "APPLIED"
    print(f"FK-orphan sweep [{mode}] — driven by Base.metadata FK ondelete intent")
    print(f"  CASCADE  -> deleted: {result['deleted'] or 'none'}")
    print(f"  SET NULL -> nulled:  {result['nulled'] or 'none'}")
    if result["unhandled"]:
        print(f"  *** NO ACTION/RESTRICT orphans (need manual decision): {result['unhandled']} ***")
    return result


if __name__ == "__main__":
    main()
