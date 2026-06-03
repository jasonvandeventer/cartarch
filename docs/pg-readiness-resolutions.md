# SQLite → Postgres readiness — audit resolutions

Companion to **`docs/pg-readiness-2026-06-02.md`** (the source audit). That file
catalogues the findings; this file records what was *resolved*, what was
*deferred into the v4 Alembic baseline*, and what remains *open* — so the v4
re-plan starts from the current truth rather than re-deriving it from the audit.

Scope note: every code/CI fix below ships during the **SQLite period** and is
**dialect-aware, not converted** — the app still runs on SQLite today and keeps
running on SQLite, while also being correct on Postgres at cutover.

---

## Code-level breakers — RESOLVED

- **`app/db.py` — SQLite-only connection setup dialect-guarded.** The
  `check_same_thread=False` connect arg is now applied only when the configured
  backend is SQLite, and the per-connect PRAGMA listener (`journal_mode=WAL` /
  `synchronous=NORMAL` / `busy_timeout`) no-ops on any non-SQLite dialect rather
  than executing SQLite-only syntax. On SQLite the behavior is byte-identical to
  before. **Shipped v3.34.3.**

- **`scripts/orphan_audit.py` — read-only FK orphan-detection tool added.** It
  introspects **all** declared foreign keys from `Base.metadata` (so it can never
  drift from `app/models.py`), counts orphaned child rows per FK, samples up to 5
  ids each, and summarizes per-table totals. Strictly SELECT-only — no
  INSERT/UPDATE/DELETE, no DDL, no PRAGMA writes, no commits. Investigation tool,
  not a migration step. **Shipped v3.34.3.**

- **`app/bracket_v2_service.py` — `cursor.lastrowid` → `RETURNING id`.** The raw
  `text()` INSERT in `persist_estimate` now appends `RETURNING id` and reads it via
  `result.scalar_one()`, replacing the SQLite-only `lastrowid` (which psycopg does
  not populate). Kept as a raw `text()` statement (not converted to a Core
  `insert()`). `RETURNING` requires SQLite ≥ 3.35 and is native on Postgres;
  verified against **prod SQLite 3.46.1** (≥ 3.35 satisfied). **Shipped v3.34.5.**

- **`app/bracket_v2_service.py` — raw `active = 1` → `WHERE active`.** The
  `game_changer_cards` query in `_gather_deck_signals` now uses a bare boolean
  column reference, removing the `1`-vs-`true` type clash. Identical meaning on
  SQLite (integer-affinity boolean) and Postgres (native boolean); verified on real
  data (53 = 53 matching rows). No schema, column, or migration change.
  **Shipped v3.34.5.**

- **CI — black (retired) replaced with `ruff format --check .`.** The dead
  `black --check .` step (black was uninstalled → exit 127 on every tag push) was
  replaced with `ruff format --check .` in both `ci.yml` and `publish.yml`, and the
  `black .` call in `scripts/lint.sh` replaced with `ruff format .`. `ruff` pinned
  to **0.15.15** in `requirements-dev.txt` to match the `.pre-commit-config.yaml`
  hook so CI's formatter check agrees with the locally-formatted tree.
  **Shipped v3.34.4.**

---

## FK enforcement — orphan audit results & decisions

The orphan audit (run **2026-06-02** against prod-shape SQLite) found **5 orphan
rows total across 41 declared FKs**. The other **39 FKs are clean**, including
every "documentary" FK the audit flagged as theoretically dangling — `watchlist`,
`showcase_items`, and all four `trade_items` references.

**Conclusion: turning on FK enforcement at cutover is a switch-flip, not a
remediation project.** Only two FKs carry orphans, both `deck_id → decks.id`:

- **`game_seats.deck_id → decks.id` (4 orphans). DECIDED → `ON DELETE SET NULL`
  in the Alembic baseline.** This matches the existing `GameSeat.user_id` /
  `user_name_at_game` snapshot pattern: an orphaned `deck_id` is *by design* —
  `deck_name_at_game` already carries the durable historical record, so the live FK
  can be nulled without losing information. Clean the 4 existing orphans as a
  migration step.

- **`deck_token_requirements.deck_id → decks.id` (1 orphan). OPEN.** This one is a
  *missed cleanup*, not by-design. Resolution pends an open question: **fix the
  deck-deletion cascade path now** (during the SQLite period, to stop further
  accumulation) **vs. rely on `ON DELETE CASCADE` in the PG baseline** at cutover.
  Marked OPEN pending that decision.

---

## Deferred into the v4 Alembic baseline

These are deliberately **not** patched piecemeal now — they are correct as-is
against the existing SQLite database and only matter when the PG schema is created.

- **Boolean `server_default`.** `GameSeat.art_background_hidden` currently uses
  `server_default=text("0")`. This becomes `sa.false()` in the Alembic baseline.
  No model edit now: server defaults apply only on fresh table creation, so editing
  it today would be a no-op against the existing SQLite table and would only add
  drift between the model and the live schema.

- **`run_migrations()` — RETIRE, do not patch.** The hand-rolled, `PRAGMA
  table_info`/`sqlite_master`-based migration runner misfires on a fresh Postgres
  DB (the introspection guards don't translate). Alembic owns the schema baseline
  at v4. The open design task is the **handoff**: freeze the final SQLite schema →
  establish the Alembic baseline from it → verify the generated PG schema matches
  intent, explicitly including the `ondelete` policies and boolean defaults that
  SQLite never enforced (and so were never exercised).

---

## Explicitly NOT defects (intentional SQLite-era design — do not "fix")

These are deliberate decisions documented in the audit and `CLAUDE.md`. They are
listed here so a future pass doesn't "discover" and undo them.

- **`PRAGMA foreign_keys` OFF + explicit service-layer cascades.** FK integrity is
  maintained by explicit service/admin-path cleanup, not the DB. Enforcement turns
  on at the PG cutover (see the switch-flip conclusion above).
- **`AUTOINCREMENT`.** Maps to PG identity/serial in the Alembic baseline; no app
  change needed.
- **`client_token` rowid-reuse workaround.** Compensates for SQLite reusing a
  deleted row's id (which could resurface a deleted game's localStorage state).
  Becomes moot under Postgres sequences, but **must stay** — client localStorage
  keys already in the wild depend on it.
- **Service-layer enum enforcement (no DB `CHECK`).** Constrained-value columns are
  enforced with Python frozensets/mappings, deliberately not DB `CHECK` constraints.
- **`group_concat` in `dashboard_service.py` — the one remaining non-portable
  function. STILL OPEN, deferred.** Postgres has no `group_concat` (it needs
  `string_agg`); SQLAlchemy emits the name verbatim, so this errors on PG at
  runtime. It is the lone outstanding runtime-SQL portability item and needs an
  **ordering decision** (rewrite before vs. as part of the cutover) before the
  `group_concat → string_agg` change is made.

---

*Status as of the v3.34.5 release line. Update this file as the OPEN items
(`deck_token_requirements` cascade, `group_concat` ordering, the Alembic handoff)
are resolved.*
