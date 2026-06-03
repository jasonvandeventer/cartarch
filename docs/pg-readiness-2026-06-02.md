# SQLite → Postgres cutover-readiness audit — 2026-06-02

**Scope:** v4 / CloudNativePG cutover. **Report only — no source changed.** Audited against the binding invariants in `CLAUDE.md` + `architecture.md` (SQLite-until-v4; service-layer enums, no DB CHECK; `PRAGMA foreign_keys` OFF; hand-rolled idempotent migrations; the `client_token` rowid-reuse workaround). Those are **deliberate SQLite-era decisions** — catalogued below as "handle at cutover", never as defects.

App version at audit: **v3.34.1**. Engine: single sync SQLite engine (`app/db.py`), WAL mode, three background writer daemons + request path sharing one writer.

> Note: the obsidian vault (`cartarch/`) is mounted read-only, so this report was written to the repo `docs/` per the skill's fallback path.

---

## TL;DR — what actually breaks vs. what is intentional

**Two genuine code-level breakers** (fail at runtime on Postgres, independent of the schema rebuild):

1. **`func.group_concat(...)` in `app/dashboard_service.py:276`** — Postgres has no `group_concat`; SQLAlchemy emits the name verbatim → `function group_concat(...) does not exist`. Must become `string_agg`.
2. **Connection setup in `app/db.py`** — `connect_args={"check_same_thread": False}` and the per-connect `PRAGMA` event listener are SQLite-only; `check_same_thread` raises on a psycopg/asyncpg connect. Must be dialect-gated/removed at cutover.

**Smaller code-level items:** `result.lastrowid` (×2), `server_default=text("0")` on a Boolean column, raw `active = 1` boolean compares, the SQLite-only migration runner running on startup against a PG DB.

**Everything else is the deliberate SQLite-era design** (FK-off + explicit cascades, AUTOINCREMENT, type-affinity for prices/cmc/bools, the `client_token` workaround, the hand-rolled migrations). These are **schema-baseline + data-copy concerns for the v4 rebuild**, not app-code fixes, and are documented as such.

---

## Category 1 — FK enforcement & cascades (turn ON in Postgres) — *intentional, handle at cutover*

`PRAGMA foreign_keys` is **OFF project-wide** — `app/db.py` sets only `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout`; it never enables FK enforcement. This is documented in 8+ places (`models.py:70,195,302,334,442,500,628,868`, `routes/admin.py:226,251`, every recent migration header). Postgres **always enforces declared FKs** — this is the single largest behavior shift at cutover.

Two sub-cases:

**(a) FKs declared with `ondelete="SET NULL"` but enforced manually today**
- `Deck.variant_group_id` → `variant_groups.id` SET NULL (`models.py:197`)
- `Game.playgroup_id` → `playgroups.id` SET NULL (`models.py:305`)
- `GameSeat.user_id` → `users.id` SET NULL (`models.py:339`)
- Trade FKs nullable for SET-NULL (`models.py:785-795`, `864-877`)

Today the SET NULL is performed by explicit service-layer UPDATEs: `deck_service.delete_variant_group` (`deck_service.py:2281`), `playgroup_service.delete_playgroup`, and the admin user-deletion cascade in `routes/admin.py:delete_user`. **Under Postgres with FK on, the DB does this automatically.** The explicit UPDATEs become redundant but remain **harmless** (they SET NULL a column that's already being SET NULL).
- *Cutover action:* keep the explicit cascades; verify ordering doesn't fight the DB cascade; leave as belt-and-suspenders. No urgent change.

**(b) Plain FKs with NO `ondelete` — the risk case**
Most FKs (`inventory_rows.user_id/card_id/storage_location_id`, `decks.user_id`, `game_seats.game_id/deck_id`, `showcase_items.*`, `watchlist.card_id`, etc.) declare no `ondelete`. In SQLite (FK off) deletes of parents always succeed. **In Postgres these default to `NO ACTION`** → a parent delete is *blocked* if children exist.
- The "documentary FK" rows are explicitly called out as possibly-dangling today: `watchlist.card_id` (`models.py:441-448`), `showcase_items.inventory_row_id` (`models.py:627-632`), `trade_items.card_id/inventory_row_id/showcase_item_id` (`models.py:864-877`). If any dangling rows exist at copy time, **adding the FK constraint will fail validation**.
- *Cutover action:* (1) run an orphan-row audit before adding constraints; (2) decide per-FK between `ON DELETE CASCADE`, `SET NULL`, or keeping the manual cascade and adding `NO ACTION`; (3) ensure `routes/admin.py:delete_user` ordering matches whatever the DB now enforces. **This is the #1 schema-design task for v4.**

ORM-level `cascade="all, delete-orphan"` (Game.seats, Playgroup.members, Showcase.items, Trade.items) operates in the SQLAlchemy unit-of-work and is **DB-agnostic** — fine on Postgres.

---

## Category 2 — rowid / AUTOINCREMENT assumptions — *intentional + one real fix*

- **AUTOINCREMENT** in `app/migrations.py:12` (`schema_migrations`) and ~10 migration `CREATE TABLE`s (games, token_inventory, playgroups, showcases, watchlist, bracket_v2 tables, …). ORM models use plain `Integer primary_key=True` (SQLite rowid alias). sqlglot maps `INTEGER PRIMARY KEY AUTOINCREMENT` → `INT GENERATED BY DEFAULT AS IDENTITY` on PG. The v4 schema rebuild handles this; the historical migration DDL never re-runs on PG.
  - *Cutover action:* schema baseline uses `Base.metadata.create_all` against PG (or an Alembic baseline). No per-migration porting.
- **`Game.client_token` rowid-reuse workaround** (`models.py:281-287`, `routes/games.py:167-173`, `migrate_v3_27_0_client_token.py:25`). **INTENTIONAL.** Compensates for SQLite **reusing** a deleted row's `id`, which could resurface a deleted game's localStorage tracker state. **Postgres sequences never reuse ids**, so the root problem disappears — but the token is harmless, already baked into client localStorage keys in the wild, and must **stay**. Catalogue, do not remove.
- **`result.lastrowid`** — `bracket_v2_service.py:952` and `scripts/migrate_v3_4_decks_as_locations.py:71`. `lastrowid` works on SQLite and MySQL but **psycopg2/psycopg3 do not populate it reliably** — returns `None`/0 on Postgres. The migration script (`v3_4`) is frozen history (won't run on PG), but **`bracket_v2_service.py:952` is live code** (`estimate_id = result.lastrowid`).
  - *Cutover action:* change the `bracket_v2` INSERT to `... RETURNING id` and read `result.scalar()`, or use the ORM. **Code-level fix.**

---

## Category 3 — type-affinity reliance (loose SQLite → strict Postgres) — *data-copy concern*

SQLite's loose affinity lets the bulk cache store wire-format strings/ints freely; the byte-identical seam (`architecture.md` §"byte-identical seam") depends on this. All columns are ORM-typed, so SQLAlchemy binds correctly on both dialects — the concern is the **one-time data copy**, not runtime.

- **Prices as TEXT** — `Card.price_usd/_foil/_etched` are `String(32)` and `scryfall_cards` prices are TEXT (`architecture.md`: "Prices stored as TEXT … no float-precision loss"). **INTENTIONAL.** PG `text`/`varchar` preserves this exactly. No change; keep TEXT in the PG baseline.
- **`cmc` REAL** (`Card.cmc` Float; `scryfall_cards.cmc` REAL, `ijson use_float=True` for byte-identical round-trip). → PG `double precision`. Copy must preserve float repr.
- **Bools as INTEGER 0/1** — `art_background_hidden` ("Stored as INTEGER 0/1 … SQLite's idiomatic boolean shape", `models.py:354-359`), plus raw-SQL tables `game_changer_cards.active` and `commander_bracket_rules.*` flags. See Category 5.
- **`colors=NULL` vs `color_identity=""` distinction** (`architecture.md`: all `scryfall_cards` columns nullable, no DEFAULT). **Load-bearing** — colorless (`colors=NULL`) vs explicit-empty identity (`""`). The data copy and PG baseline **must preserve NULL-vs-empty-string**; do not let a copy tool coerce `''`→NULL or vice-versa. The pip-filter subset logic (`inventory_service.py:697`) and `produced_tokens` `"[]"`-vs-NULL (`architecture.md` §Token data model) both depend on it.

---

## Category 4 — case sensitivity (LIKE vs ILIKE) — *mostly already portable*

- **`.ilike()` everywhere** (token_service, set_service, inventory_service 651-1069, decklist) — SQLAlchemy emits native `ILIKE` on Postgres and `lower() LIKE lower()` on SQLite. **Portable, no change.** No bare `.like()` calls exist in `app/`.
- **`.contains(letter)` on `Card.colors` / `Card.color_identity`** (`inventory_service.py:684,697,943,1049-1053`) compiles to `col LIKE '%x%'`. SQLite `LIKE` is **case-insensitive (ASCII)**; Postgres `LIKE` is **case-sensitive**. Color-identity values are canonical uppercase `WUBRG`, so **behavior is identical in practice** — but the latent difference is worth recording in case any non-canonical-case value ever lands.
  - *Cutover action:* low risk; spot-check there are no lowercase color letters in `cards.colors/color_identity` post-copy. No code change expected.
- **`func.lower(x) == y.lower()`** for case-insensitive matching (auth.py:57, import_service 993/1138/1162, decklist 326/331/337/439, token_service 39/61, dashboard 561) — identical on both dialects. **Already the portable pattern**; the email-canonicalization hardening (v3.33.1) relies on it and survives the move cleanly.

---

## Category 5 — boolean & datetime storage

**Booleans**
- ORM `Boolean` columns → SQLite INTEGER 0/1, PG native `boolean`; SQLAlchemy handles binding both ways. Fine **except**:
- **`server_default=text("0")` on `GameSeat.art_background_hidden`** (`models.py:357-359`). A PG `boolean` column rejects `DEFAULT 0` (`0` is integer, not boolean) — needs `false`/`'0'::boolean`. If the PG baseline is built from this model definition, the literal `text("0")` server_default is wrong for PG.
  - *Cutover action:* change to `server_default=text("false")` (or `sa.false()`) when building the PG schema. **Code/DDL-level fix.**
- **Raw-SQL boolean compares** — `bracket_v2_service.py:297` `WHERE active = 1`, and `_load_rules` reads `bool(r[3..7])` from `commander_bracket_rules` (`bracket_v2_service.py:246-250`). `game_changer_cards.active` and the bracket-rules flags are **raw-SQL INTEGER tables** (not ORM). If the v4 rebuild types them as PG `boolean`, `active = 1` fails (`operator does not exist: boolean = integer`).
  - *Cutover action:* either keep these columns as `integer` in the PG baseline (zero code change) **or** convert to `boolean` and change the raw SQL to `WHERE active` / `WHERE active = TRUE`. Recommend keeping `integer` to avoid touching the raw SQL.

**Datetimes**
- ORM `DateTime` with Python-side `default=datetime.utcnow` (naive UTC) throughout. PG `timestamp without time zone` stores naive values fine. `app/dependencies.py:116` documents the "naive UTC, rendered labeled UTC" assumption — unchanged by the move.
- **Mixed timestamp sourcing:** most timestamps come from Python `datetime.utcnow`, but `card_tags` upsert uses SQL **`CURRENT_TIMESTAMP`** (`bracket_v2_service.py:193,197`). SQLite `CURRENT_TIMESTAMP` = UTC string `'YYYY-MM-DD HH:MM:SS'`; PG `CURRENT_TIMESTAMP` = `timestamptz` (with tz, local-ish) and is **transaction-start**, not statement-time. Minor semantic drift on `card_tags.last_reviewed` only.
  - *Cutover action:* low priority; if exact parity matters, use `now() at time zone 'utc'` or move to Python `utcnow`. `card_tags` is internal tagging metadata, so cosmetic.

---

## Category 6 — PRAGMA usage & connection setup (sync today → asyncpg/pooling at v4)

`app/db.py` is entirely SQLite-shaped:
- **`create_engine(DATABASE_URL, connect_args={"check_same_thread": False})`** — `check_same_thread` is a SQLite-only DBAPI arg; **psycopg/asyncpg will error on it.** Must be removed/replaced at cutover.
- **`@event.listens_for(engine, "connect") _set_sqlite_pragmas`** — sets `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` on every connection. **All SQLite-only.** On PG these PRAGMAs are no-ops at best, errors at worst. The whole listener must be removed or dialect-guarded.
- **`checkpoint_and_dispose()`** runs `PRAGMA wal_checkpoint(TRUNCATE)` on shutdown — SQLite-only; becomes a no-op/removed on PG.
- **Single-writer rationale dissolves.** The `busy_timeout` comment ("three background writer daemons + request path contend for the single writer") describes SQLite's one-writer constraint. Postgres MVCC has no single-writer bottleneck — the daemon design (bounded batch, commit-per-batch) stays correct, but the lock-contention motivation is gone. Aligns with the planned `RUN_WORKERS` daemon/worker split (per v4 scope).
- **`DATABASE_URL = f"sqlite:///{DB_PATH}"`** + `DB_PATH.exists()` boot check in `init_db()` — both SQLite-file assumptions; replace with a PG DSN + a connectivity check.
- *Cutover action:* rewrite `app/db.py` for `postgresql+psycopg`/`asyncpg`, configure pool sizing for the daemon threads + request workers, drop all PRAGMA/WAL/checkpoint code, replace the file-exists check. This is the core of the planned v4 engine work; **deferred to the cutover, not now.**

---

## Category 7 — hand-rolled migrations vs Alembic — *intentional, needs a cutover guard*

All `scripts/migrate_*.py` are idempotent and **SQLite-coupled**: they guard with `PRAGMA table_info(...)` (every migration), `SELECT … FROM sqlite_master` (`migrate_v3_13_games.py:10`, `migrate_v3_5_drop_deck_items.py:10`), and `ALTER TABLE … ADD COLUMN` (SQLite has no `ADD COLUMN IF NOT EXISTS`, hence the pragma guard pattern — `architecture.md` §Migrations). Tracked by name in `schema_migrations`; **`run_migrations()` runs from `on_startup()` on every deploy** (`main.py`).

The risk: **`run_migrations()` will execute on first boot against a fresh Postgres DB.** `PRAGMA table_info(...)` returns an empty/error result on PG (it parses as `PRAGMA table_info = games` per sqlglot, i.e. a GUC set, not a table introspection) → the `column_exists` guards misbehave and the migrations either no-op incorrectly or raise.
- *Cutover action:* (1) build the v4 schema baseline directly (create_all or Alembic baseline); (2) **seed `schema_migrations` with every historical migration name marked applied** so `run_migrations()` skips them; (3) gate the SQLite migration runner to `if engine.dialect.name == "sqlite"`, or retire it in favor of Alembic for v4-forward. The frozen historical migrations are **not** ported. (Per locked v4 scope: schema baseline decision already settled — this is the mechanical follow-through.)

---

## Category 8 — raw SQL / `text()` blocks (deterministic sqlglot check)

Transpiled representative statements `read='sqlite', write='postgres'`:

| Statement (location) | Round-trips? | Note |
|---|---|---|
| `INSERT … ON CONFLICT(scryfall_id) DO UPDATE SET … = excluded.…` (`scryfall.py:668-677` bulk + meta upsert) | OK | `ON CONFLICT` + `excluded.` are identical in PG. Bind-param style handled by SQLAlchemy. |
| `card_tags` upsert w/ `CURRENT_TIMESTAMP` (`bracket_v2_service.py:189-201`) | OK syntactically | `ON CONFLICT (card_id, tag)` fine; `CURRENT_TIMESTAMP` semantics differ (Cat 5). |
| **`group_concat(color_identity, ' ')` (`dashboard_service.py:276`, via `func.group_concat`)** | **NO** | PG has **no `group_concat`** → must be `string_agg`. SQLAlchemy `func.group_concat` emits the name verbatim, so this **errors at runtime on PG**. **Top code-level breaker.** |
| `SELECT … FROM scryfall_cards WHERE scryfall_id IN :ids` expanding bindparam (`scryfall.py:460-499`) | OK | SQLAlchemy expands per-dialect; fine. |
| `PRAGMA table_info(...)` (all migrations) | NO | SQLite-only (Cat 7); sqlglot mis-parses to a GUC set. |
| `SELECT … FROM sqlite_master` (migrations) | NO | SQLite-only catalog (Cat 7). Use `information_schema` on PG. |
| `INTEGER PRIMARY KEY AUTOINCREMENT` (migrations, `migrations.py:12`) | warn | → `GENERATED … AS IDENTITY`; baseline handles (Cat 2). |
| `… DEFAULT 0 NOT NULL` on bool col (migration DDL) | warn | Fine as INTEGER; invalid if column becomes PG `boolean` (Cat 5). |

`distinct`, `count`, `sum`, `case`, `func.lower` — all portable. **`group_concat` is the only non-portable runtime SQL function in live code.**

---

## Prioritized cutover checklist

**P0 — code-level breakers (fix in the cutover PR; live request/daemon paths):**
1. `app/dashboard_service.py:276` — replace `func.group_concat(Card.color_identity, " ")` with `func.string_agg(...)` (or a dialect-conditional). Errors on every dashboard load otherwise.
2. `app/db.py` — drop `connect_args={"check_same_thread": False}`, remove/dialect-gate the `_set_sqlite_pragmas` listener and `checkpoint_and_dispose()` WAL checkpoint, swap `DATABASE_URL` to a PG DSN, replace the `DB_PATH.exists()` boot check, configure pooling for daemon threads + workers.
3. `app/bracket_v2_service.py:952` — replace `result.lastrowid` with `INSERT … RETURNING id` (psycopg doesn't populate `lastrowid`).
4. Gate `run_migrations()` to SQLite (`engine.dialect.name == "sqlite"`) **and** seed `schema_migrations` as all-applied on the PG baseline, so the SQLite/`PRAGMA`-based migration runner never executes against Postgres.

**P1 — schema-baseline / DDL decisions (the v4 rebuild):**
5. **FK policy per relationship** — decide CASCADE / SET NULL / NO ACTION for every FK (esp. the no-`ondelete` plain FKs and the "documentary" ones); reconcile with the explicit cascades in `routes/admin.py:delete_user`, `playgroup_service.delete_playgroup`, `deck_service.delete_variant_group`.
6. **Orphan-row audit before enabling FKs** — `watchlist.card_id`, `showcase_items.inventory_row_id`, `trade_items.{card_id,inventory_row_id,showcase_item_id}`, any dangling `game_seats.user_id`/`games.playgroup_id`. Adding the constraint fails if orphans exist.
7. `GameSeat.art_background_hidden` — change `server_default=text("0")` → `text("false")` for the PG boolean column (or keep the column `integer`).
8. Decide `game_changer_cards.active` / `commander_bracket_rules.*` flag column types — keep `integer` (zero code change, recommended) or convert to `boolean` and update the `active = 1` raw SQL (`bracket_v2_service.py:297`).

**P2 — data-copy fidelity (one-time migration):**
9. Preserve **`colors=NULL` vs `color_identity=''`** and **`produced_tokens` `'[]'` vs NULL** distinctions exactly; verify no copy-tool coercion.
10. Keep prices as `text` and `cmc` as `double precision`; verify float repr round-trips (the `ijson use_float=True` byte-identical contract).
11. Per the locked v4 scope, `scryfall_cards` is rebuilt from the daily bulk export post-cutover rather than copied — confirm the daemon repopulates and the 22-key seam is intact after the move.

**Intentional — catalogue only, do NOT "fix":**
- `client_token` rowid-reuse workaround (becomes moot under PG sequences but stays — client localStorage keys depend on it).
- Service-layer enum enforcement (no DB CHECK) — keep; do not add CHECK constraints at v4 unless deliberately chosen.
- The explicit service-layer cascades — keep as belt-and-suspenders even after FK enforcement turns on.
- `.ilike()` / `func.lower()` patterns — already portable; no change.

---

*No source files were modified. This is a report only.*
