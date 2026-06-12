# SQLite → PostgreSQL cutover readiness audit — 2026-06-03

**Scope:** full re-baseline of SQLite couplings ahead of the v4 CloudNativePG cutover.
**Method:** read `CLAUDE.md` + `architecture.md` (binding invariants); grep/read `app/models.py`,
`app/`, `app/routes/`, `scripts/migrate_*.py`, `app/db.py`; deterministic transpile of raw SQL via
`sqlglot 30.8.0` (`read='sqlite', write='postgres'`).
**REPORT ONLY — no source was edited.** SQLite-until-v4 holds; nothing below is a defect to "fix" now.

Surface size: **21 ORM tables, 42 ForeignKeys, 55 `migrate_*.py` scripts, ~230 `text()` occurrences.**

## Verdict

**Cutover-ready in shape, with a short, well-understood punch list.** The dialect-hardening done in
v3.34.x (db.py guard, `WHERE active`, `RETURNING id`) plus this pass leaves **exactly one runtime SQL
break** (`group_concat`) and a small set of **behavioral divergences to verify** (strict CAST on TEXT
prices, FK enforcement, NULL ordering). Everything else is either already portable or an intentional
SQLite-era decision to be handled structurally at cutover (Alembic baseline, FK-on).

---

## Intentional SQLite-era decisions — handle at cutover, NOT defects

Catalogued per the invariants; do not "fix" these in SQLite-era code:

- **`PRAGMA foreign_keys` OFF + explicit-UPDATE/DELETE cascades.** `db.py` sets no `foreign_keys`
  pragma (SQLite default OFF). App-level cleanup compensates (`admin.delete_user`,
  `playgroup_service.delete_playgroup`, `deck_service.delete_variant_group`). → Turn FK enforcement
  ON at cutover (Category 1).
- **Service-layer enums, no DB `CHECK`** (`VALID_LOCATION_TYPES/MODES`, `CANONICAL_GAME_FORMATS` +
  `normalize_game_format`). Postgres could add CHECK/enum types, but the service-layer contract is
  deliberate and portable as-is. Optional hardening only.
- **`client_token` rowid-reuse workaround** (`migrate_v3_27_0_client_token`, `games.py:169`). SQLite
  reuses `games.id` rowids after delete; the client token disambiguates. Postgres sequences never
  reuse ids → the workaround becomes unnecessary but harmless. Leave in place.
- **Hand-rolled idempotent `migrate_*.py` + `schema_migrations` runner** (55 scripts, 32
  `pragma_table_info` guards, `run_migrations.py`). → Replaced by an Alembic baseline at cutover
  (Category 7); decided in the v4 scope.

---

## Findings by category

### 1. FK enforcement & cascades  *(highest cutover attention)*
- **42 FKs; only 3 declare `ondelete`** — all `SET NULL`, documented as v4 intent and inert under
  SQLite: `Deck.variant_group_id` (`models.py:204`), `Game.playgroup_id` (`models.py:312`),
  `GameSeat.user_id` (`models.py:346`). The other 39 rely on app-level cleanup.
- **Current (SQLite, FK OFF):** deleting a parent silently orphans children unless app code cleans up.
  **Postgres (FK ON):** the 3 `SET NULL` clauses fire automatically; every *other* parent delete
  **RESTRICT-errors** unless the app deletes children first.
- `admin.delete_user` (`app/routes/admin.py:163+`) already does an **ordered** explicit cascade
  (Share → Showcase(+items) → TransactionLog → InventoryRow → ImportBatch → Deck → VariantGroup →
  StorageLocation → WatchlistItem → PlaygroupMember → PasswordResetToken; SET NULL GameSeat/Trade).
  That order is children-before-parents and should satisfy RESTRICT.
- The v3.34.3 orphan audit (`scripts/orphan_audit.py`, `docs/pg-readiness-2026-06-02.md`) found
  **5 orphans across the declared FKs** (`game_seats.deck_id` ×4 by-design; `deck_token_requirements.deck_id` ×1).
- **NEW orphan class — `showcase_items` / `trade_items` → `inventory_rows` (added 2026-06-12).** The
  `collection-delete-investigation.md` cascade study found the **merge path**
  (`move_inventory_row_to_location` / placement-merge) and the **import-undo paths** were deleting
  inventory rows **without** cleaning their referencing `showcase_items` (NOT NULL FK) /
  `trade_items` (nullable FK) — silently orphaning under SQLite, and a **hard FK error under
  Postgres**. The **demo account alone carried 8 real orphaned `showcase_items`** from normal merge
  use (0 trade_items). The code paths are **fixed in v3.39.x** (all `session.delete(row)` sites now
  call the shared `clean_inventory_row_references`), so no NEW orphans accrue — but **existing prod
  rows from before the fix still violate the FK** and must be swept before constraints are enabled.
  These were NOT in the v3.34.3 "5 known orphans" count; that number is understated for this class.
- **Remediation — `scripts/sweep_fk_orphans.py`** (idempotent): deletes orphaned `showcase_items`
  (meaningless pointers; NOT NULL FK can't be NULLed) and **NULLs** the dangling
  `trade_items.inventory_row_id` (nullable FK; the `*_at_trade` snapshot is the durable trade record
  — decision A4, so the trade row is kept, matching `abandon_pending_trades_for_inventory_rows`).
  Reports counts by type; a second run reports zero. Dev run (2026-06-12): **8 showcase_items deleted,
  0 trade_items nulled**, second run 0/0.
- **Cutover action:** clean the 5 known declared-FK orphans **AND run the orphan sweep on the
  prod snapshot** (see checklist step below); enable FK enforcement; verify *every* parent-delete
  path (user, playgroup, variant group, deck, showcase) either runs explicit cleanup or has an
  `ondelete` — RESTRICT failures only surface at delete time, which the orphan audit (existing-data)
  does not catch. Adopt FK `ondelete` in the Alembic baseline rather than relying solely on app code.

### 2. rowid / AUTOINCREMENT
- `AUTOINCREMENT` appears only in **migration DDL** (`migrations.py:12`, 50+ `migrate_*.py` CREATE
  TABLEs) — not runtime. sqlglot maps it to `INT GENERATED BY DEFAULT AS IDENTITY`. The Alembic
  baseline (regenerated from ORM `Integer` PKs) emits portable identities; do **not** carry
  `AUTOINCREMENT` forward.
- `result.lastrowid` in `migrate_v3_4_decks_as_locations.py:71` — one-time migration already applied;
  not runtime. The runtime `lastrowid` site was already fixed in v3.34.5 (`bracket_v2_service.py` →
  `RETURNING id`).
- `client_token` rowid-reuse — intentional (above).

### 3. Type affinity  *(verify before cutover)*
- **TEXT prices cast to FLOAT.** `_effective_price_expr()` (`inventory_service.py:852`) wraps a
  `case(...)` over `Card.price_usd[_foil/_etched]` (all TEXT) in `cast(..., Float)`; used by the
  `price:`/`usd:` boolean-search keyword and the facet price range. **SQLite** coerces leniently
  (`CAST('' AS FLOAT)` → 0.0, non-numeric → 0.0). **Postgres** is strict: `CAST('' AS DOUBLE
  PRECISION)` and `CAST('abc' …)` **raise** (`invalid input syntax`). Prices normally arrive
  null-or-numeric from Scryfall (`prices.get('usd')`), so the risk is a stray empty/non-numeric value.
  **Cutover action:** audit `cards.price_usd*` for non-numeric/empty strings; if any exist, the cutover
  needs a safe cast (`NULLIF(price,'')` / numeric-validated) — otherwise the price filter 500s in PG.
- **JSON stored as TEXT** (`legalities`, `frame_effects`, `produced_tokens`) — portable (TEXT↔TEXT);
  not native JSON, so no jsonb concerns. Leave as TEXT (changing to jsonb is optional v4 polish).
- `cmc` REAL/`Float`, quantities `Integer` — clean.

### 4. Case sensitivity
- `.ilike(...)` (token_service, set_service, inventory_service search, import_service) — **portable**:
  SQLAlchemy emits native `ILIKE` on PG, `lower() LIKE lower()` on SQLite. No action.
- `func.lower(col) == value` (auth, dashboard, decklist, import, token) — explicit, portable. No action.
- **`.like()` (case-insensitive reliance):** only two sites, both the v3.36.x backfill —
  `Card.type_line.like("%Planeswalker%")` / `("%Battle%")` (`main.py:382-383`). SQLite `LIKE` is
  case-insensitive (ASCII); **Postgres `LIKE` is case-sensitive.** Works under PG anyway because
  `type_line` is canonical title-case from Scryfall, but **`.ilike()` is the portable-intent form**.
  Low severity — switch to `ilike` at cutover for consistency.

### 5. Boolean & datetime storage
- **Boolean** columns (38 Boolean/DateTime cols) go through SQLAlchemy `Boolean` (0/1 ↔ native bool) —
  portable. The one raw boolean predicate (`active = 1`) was fixed in v3.34.5 (`WHERE active`); grep
  found **no remaining raw `= 1/= 0` boolean SQL.**
- **Datetime:** all Python-side `default=datetime.utcnow` (naive UTC); no `server_default`. Portable;
  pgloader maps `DATETIME` → `timestamp without time zone`, preserving naive UTC. `CURRENT_TIMESTAMP`
  in the `schema_migrations` DDL is portable (and that table is replaced by Alembic anyway).
- **NULL ordering divergence:** `ORDER BY x ASC` puts NULLs **first** in SQLite, **last** in Postgres.
  `main.py:201` (`order_by(Card.updated_at.asc())`, price-refresh) is the one unqualified case —
  low risk (`updated_at` has a default, rarely NULL). `trade_service.py:784` already uses
  `.nulls_last()` explicitly. **Cutover action:** add explicit `nulls_first()/nulls_last()` where NULL
  position is semantically load-bearing.

### 6. PRAGMA usage & connection setup  *(already largely cutover-ready)*
- `app/db.py` is **dialect-guarded (v3.34.3):** `check_same_thread` applied only when
  `DATABASE_URL.startswith("sqlite")`; the `connect` PRAGMA listener returns early when
  `engine.dialect.name != "sqlite"`. WAL / `synchronous=NORMAL` / `busy_timeout` are correctly
  SQLite-only. `checkpoint_and_dispose()` runs `PRAGMA wal_checkpoint` but is best-effort/try-except
  (harmless no-op risk on PG; ideally also dialect-gate at cutover).
- **Cutover adds:** asyncpg/psycopg driver URL, connection pooling config, and the daemon worker-split
  (`RUN_WORKERS`) per the v4 scope. `busy_timeout`'s whole reason for being — single-writer contention
  — disappears under Postgres (real MVCC concurrency). This session hit that exact wall: the v3.36.3
  forced bulk re-stream died with `sqlite3.OperationalError: database is locked` (bulk daemon vs.
  other write daemons). **That class is eliminated by Postgres** — a concrete cutover payoff, not a fix.

### 7. Migrations (hand-rolled → Alembic)
- 55 idempotent `migrate_*.py` + `schema_migrations` + `run_migrations.py` (called from `on_startup`),
  32 `pragma_table_info`-guarded adds (SQLite has no `ADD COLUMN IF NOT EXISTS`). Intentional pattern.
- **Cutover action (decided):** generate an Alembic baseline from the current schema, retire
  `run_migrations.py`. Baseline must use IDENTITY (not AUTOINCREMENT), declare FK `ondelete` per
  Category 1, and adopt native `boolean`/`timestamp`. Keep migrations as historical artifacts.

### 8. Raw SQL / `text()` blocks  *(deterministic sqlglot results)*

| Construct | Site | sqlite→postgres | Verdict |
|---|---|---|---|
| `group_concat(col, ' ')` | `dashboard_service.py:276` (via `func.group_concat`) | → `STRING_AGG` | **BREAK** — see #1 below |
| `INSERT … ON CONFLICT(col) DO UPDATE SET x = excluded.x` | `scryfall.py:732/737` (`_BULK_UPSERT_SQL`), `bracket_v2_service.py:194` | round-trips (`excluded` valid both) | ✅ portable |
| `INSERT … RETURNING id` | `bracket_v2_service.py:936` | round-trips | ✅ portable (v3.34.5) |
| `WHERE active` (bare boolean) | `bracket_v2_service.py:297` | round-trips | ✅ portable (v3.34.5) |
| `CREATE TABLE … AUTOINCREMENT` | `migrations.py:12`, migrate DDL | → `IDENTITY` | baseline-only (Cat 2/7) |
| `:param` bind style | all `text()` | SQLAlchemy rebinds per driver (`%(name)s`) | ✅ portable |

`text()` SQL is overwhelmingly simple parameterized CRUD that round-trips. No `GLOB`/`REGEXP`/`substr`/
SQLite date-funcs in SQL; `strftime` appears only in Python date formatting.

---

## #1 actionable — the only runtime SQL break

**`func.group_concat(Card.color_identity, " ")` — `dashboard_service.py:276`.** Postgres has no
`group_concat`; `func.group_concat` emits the literal name and errors `function group_concat does not
exist`. The surrounding query is otherwise PG-clean (`GROUP BY Deck.id` covers the only non-aggregated
SELECT column). Recommended cutover change: SQLAlchemy **`aggregate_strings(Card.color_identity, " ")`**
(compiles to `group_concat` on SQLite, `string_agg` on Postgres) — a one-line, dialect-portable swap
that also works on today's SQLite, so it can land as the first v4-prep commit. (A `@compiles` custom
construct is the fallback if the SQLAlchemy version lacks `aggregate_strings`.)

---

## Prioritized cutover checklist

1. **[code, do first — safe on SQLite]** Swap `func.group_concat` → portable string-agg in
   `dashboard_service.py:276`. The only runtime SQL break; verify the commander color signature still
   renders on the dashboard.
2. **[data]** Clean the 5 known declared-FK orphan rows (v3.34.3 audit) so FK enforcement is a switch-flip.
2a. **[data, cutover-day]** **Run the `showcase_items`/`trade_items` orphan sweep on the prod
    snapshot** before enabling FK constraints — these orphans (from pre-v3.39.x merge/undo deletes)
    block `ALTER TABLE ... ADD CONSTRAINT`. Command (idempotent; safe to re-run):
    `DATA_DIR=<prod-data-dir> python -m scripts.sweep_fk_orphans` (or `--dry-run` first to preview
    counts). It must report **0/0 on a second run** before proceeding; a non-zero first run is a
    remediation step, not a formality.
3. **[data, verify]** Audit `cards.price_usd*` for empty/non-numeric strings (Category 3); if present,
   plan a safe-cast wrapper or data cleanup before enabling the `price:` filter on PG.
4. **[baseline]** Generate the Alembic baseline from current schema: IDENTITY PKs, FK `ondelete`
   declared per Category 1, native `boolean`/`timestamp`. Retire `run_migrations.py`.
5. **[cutover]** Enable FK enforcement; re-verify every parent-delete path (user/playgroup/variant
   group/deck/showcase) cleans up or cascades — RESTRICT errors surface only at delete time.
6. **[code, low severity]** `.like()` → `.ilike()` at `main.py:382-383`; add explicit
   `nulls_first/last()` where NULL ordering is load-bearing (`main.py:201`).
7. **[infra]** asyncpg/psycopg URL + pooling + daemon worker-split (`RUN_WORKERS`); dialect-gate
   `checkpoint_and_dispose`'s PRAGMA. Drop `busy_timeout`-era single-writer assumptions — PG MVCC
   removes the `database is locked` contention class (seen live this session).
8. **[rehearse]** Stand up CloudNativePG, **prove a restore**, run a full `pgloader` rehearsal in a
   sandbox; spot-check the price filter, dashboard color signatures, cascade deletes, and ILIKE search.

---

## Non-cutover note: documentation drift

`architecture.md:23,27` still states the `scryfall_cards` seam is **"22 keys"** and `Card` has
**"21 fields"** (pre-v3.36.1). The seam is now **24 keys** and `Card` has **23** payload-mapped
fields (`loyalty`, `defense` appended in v3.36.1; `CLAUDE.md:208-209` is current). Not a cutover
issue — flagged for a docs refresh. (Report-only; not edited here.)
