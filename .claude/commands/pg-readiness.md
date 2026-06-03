---
description: Audit SQLite→Postgres cutover readiness; report only, never edit source
argument-hint: [optional focus area, e.g. "models" or "migrations"]
allowed-tools: Read, Grep, Glob, Bash(python3:*)
---

You are auditing this repository for SQLite→Postgres (v4 / CloudNativePG) cutover
readiness. You produce a REPORT ONLY. You must not edit, refactor, or delete any
source file. Optional focus: $ARGUMENTS

## Step 0 — Load the invariants FIRST (non-negotiable)
Read @CLAUDE.md and @architecture.md and treat them as binding. If the obsidian MCP
is connected, also read vault notes cartarch/architecture.md and cartarch/CLAUDE.md.
These document deliberate SQLite-era decisions. CATALOGUE the behaviors that change at
cutover — do NOT "fix" them. The following are intentional and must be reported as
"handle at cutover", never as defects: PRAGMA foreign_keys OFF with explicit-UPDATE
cascades; service-layer enum enforcement (no DB CHECK); the client_token rowid-reuse
workaround; hand-rolled idempotent migrate_*.py scripts. Propose NO schema change or
Postgres-ism in app code (SQLite until v4).

## Step 1 — Inventory the data surface
Grep/read app/models.py, app/routes/, main.py, migrate_*.py. Catalogue SQLite couplings
by category: (1) FK enforcement & cascades (turn ON in Postgres); (2) rowid/AUTOINCREMENT
assumptions; (3) type-affinity reliance (loose SQLite vs strict Postgres); (4) case
sensitivity (LIKE vs ILIKE); (5) boolean/datetime storage; (6) PRAGMA usage and
connection setup (sync today; asyncpg/pooling at v4); (7) hand-rolled migrations vs
Alembic; (8) raw SQL / SQLAlchemy text() blocks.

## Step 2 — Deterministic SQL check
For any raw SQL or text() string, transpile sqlite→postgres with sqlglot and flag what
doesn't round-trip:
python3 -c "import sqlglot; print(sqlglot.transpile(SQL, read='sqlite', write='postgres'))"

## Step 3 — Report
Write findings to a dated note: cartarch/pg-readiness-<YYYY-MM-DD>.md in the vault if the
obsidian MCP is available, else docs/pg-readiness-<YYYY-MM-DD>.md in the repo. For each
finding: file:line, category, current SQLite behavior, what changes under Postgres, and a
recommended cutover action. End with a prioritized checklist. Make NO code changes.
