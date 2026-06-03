"""Read-only orphan-row audit for the v4 SQLite→Postgres FK-enforcement work.

The project runs with ``PRAGMA foreign_keys`` OFF (see CLAUDE.md / the
2026-06-02 pg-readiness audit), so every declared ``ForeignKey`` in
``app/models.py`` is currently *unenforced*. At the Postgres cutover those
constraints get turned on, and ``ALTER TABLE ... ADD CONSTRAINT`` will FAIL to
validate if any child row points at a parent id that no longer exists.

This script finds those orphans up front so the FK policy can be scoped. It is
an INVESTIGATION TOOL that lives in the repo — NOT a migration step.

STRICTLY READ ONLY:
  - It issues only SELECT statements.
  - No INSERT/UPDATE/DELETE, no DDL, no PRAGMA writes, no commits.
  - It discovers FKs by introspecting ``Base.metadata`` (populated by importing
    ``app.models``), so it can never drift from the model definitions.

Usage:
    python scripts/orphan_audit.py
"""

from __future__ import annotations

import os
import sys

# Support ``python scripts/orphan_audit.py`` from any working directory (not
# only ``python -m scripts.orphan_audit``): put the repo root on sys.path
# before importing the ``app`` package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

import app.models  # noqa: E402,F401  — registers every table on Base.metadata
from app.db import Base, engine  # noqa: E402


def _orphan_count_sql(child: str, child_col: str, parent: str, parent_col: str) -> str:
    """Count child rows whose non-NULL FK has no matching parent row.

    Identifiers come exclusively from our own SQLAlchemy metadata (never user
    input), so interpolating them is safe. Double-quoted identifiers and the
    correlated ``NOT EXISTS`` form are valid on both SQLite and Postgres; table
    aliases keep self-referential FKs (e.g. storage_locations.parent_id)
    unambiguous.
    """
    return (
        f'SELECT COUNT(*) FROM "{child}" AS c '
        f'WHERE c."{child_col}" IS NOT NULL '
        f'AND NOT EXISTS (SELECT 1 FROM "{parent}" AS p WHERE p."{parent_col}" = c."{child_col}")'
    )


def _orphan_sample_sql(
    child: str,
    child_col: str,
    parent: str,
    parent_col: str,
    child_pk: str,
    limit: int = 5,
) -> str:
    return (
        f'SELECT c."{child_pk}" FROM "{child}" AS c '
        f'WHERE c."{child_col}" IS NOT NULL '
        f'AND NOT EXISTS (SELECT 1 FROM "{parent}" AS p WHERE p."{parent_col}" = c."{child_col}") '
        f"LIMIT {limit}"
    )


def main() -> None:
    print(f"Orphan-row audit (READ ONLY) against {engine.url}")
    print("=" * 78)

    # Per-table running total of orphan rows across all of that table's FKs.
    per_table_total: dict[str, int] = {}
    grand_total = 0

    for table in Base.metadata.sorted_tables:
        # Single-column 'id' PK for every table in this schema; fall back to a
        # composite label if that ever changes.
        pk_cols = list(table.primary_key.columns)
        child_pk = pk_cols[0].name if pk_cols else "rowid"

        for col in table.columns:
            for fk in col.foreign_keys:
                parent_col = fk.column  # the referenced parent column
                parent_table = parent_col.table.name
                rel = f"{table.name}.{col.name} -> {parent_table}.{parent_col.name}"

                try:
                    with engine.connect() as conn:
                        count = conn.execute(
                            text(
                                _orphan_count_sql(
                                    table.name, col.name, parent_table, parent_col.name
                                )
                            )
                        ).scalar_one()

                        samples: list[object] = []
                        if count:
                            samples = [
                                r[0]
                                for r in conn.execute(
                                    text(
                                        _orphan_sample_sql(
                                            table.name,
                                            col.name,
                                            parent_table,
                                            parent_col.name,
                                            child_pk,
                                        )
                                    )
                                ).fetchall()
                            ]
                except Exception as exc:  # noqa: BLE001 — report, never abort the audit
                    print(f"  [error] {rel}: {exc}")
                    continue

                per_table_total[table.name] = per_table_total.get(table.name, 0) + count
                grand_total += count

                flag = "  <-- ORPHANS" if count else ""
                line = f"  {rel}: {count} orphan(s){flag}"
                if samples:
                    sample_str = ", ".join(str(s) for s in samples)
                    line += f"\n      sample {child_pk}s: {sample_str}"
                print(line)

    print("=" * 78)
    print("Per-table orphan totals:")
    if per_table_total:
        for tname in sorted(per_table_total):
            print(f"  {tname}: {per_table_total[tname]}")
    print("-" * 78)
    print(f"TOTAL orphan rows across all declared FKs: {grand_total}")


if __name__ == "__main__":
    main()
