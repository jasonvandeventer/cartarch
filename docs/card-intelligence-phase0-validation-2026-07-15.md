# Card Intelligence — Phase 0 Codebase Validation Report

**Issue:** #122 (program spec issue B-0) — *report-only, no broad implementation.*
**Date:** 2026-07-15
**Author:** AI Dev Team harness (run `122-20260715-1049`), evidence-grounded pass.
**Vault authority (source of truth for the program):**
`cartarch/card-intelligence-and-bracket-floor-program-spec-2026-07-14.md` +
`cartarch/card-intelligence-validation-spec-v0.1-2026-07-14.md`.

This report executes v0.1 §2–§4 and delivers the §29 A–J deliverable: repository
inspection, assumption audit, existing-implementation map with concrete
citations, reuse-vs-replace recommendations, a revised phase plan, and a **ranked**
first-increment recommendation. Per the approved-with-revisions spec, every
conclusion is stated as a hypothesis pinned to evidence, not a pre-determined
build order. The Chronicle entry is **deliberately not authored here** — it is
generated from the accepted report at the harness ship step (spec revision #12).

---

## 0. Reading guide — Evidence Hierarchy (spec revision #3)

Every finding below is tagged with the strongest evidence class that backs it, so
"Confirmed" is transparent and defensible:

| Tag | Class | Meaning |
|-----|-------|---------|
| `E1` | Code inspection | A file:line citation in the current tree. |
| `E2` | Runtime verification | Observed against a real database/artifact (this pass profiled a June-2026 snapshot — see §1 caveat). |
| `E3` | Test coverage | A committed test pins the behaviour. |
| `E4` | Production telemetry | Live prod query. **Not gathered in this pass** — flagged where it would change a conclusion. |

A finding is **Confirmed** only at `E1`+ with no contradicting evidence; anything
resting on assumption is marked **Hypothesis** and given a cheap test to settle it.

---

## 1. Deployment-State Reconciliation (spec revision #2)

Grounds the report in a concrete baseline so it can't describe a system nobody confirmed.

| Axis | Value | Evidence |
|------|-------|----------|
| Branch / base commit | `harness/122` off `6564df5` (`v4.11.11: proxy lifecycle …`, #134) | `E1` git |
| Alembic head (schema baseline) | **`d0e1f2a3b4c5`** (`issue_123_gc_list_date_versions`) — single head, no branches | `E1` `alembic/versions/` walk |
| Schema owner in prod | Alembic only; `Base.metadata.create_all` gated to SQLite/dev (`app/db.py`); PreSync `alembic upgrade head` runs before the app rolls | `E1` `app/main.py:200-210` |
| Key dep pins | `fastapi==0.136.3`, `starlette==0.52.1`, `uvicorn[standard]==0.49.0`, `sqlalchemy>=2.0,<3.0`, `prometheus-fastapi-instrumentator==7.1.0`, `ijson>=3.0` | `E1` `requirements.txt` |
| Runtime DB | PostgreSQL (CloudNativePG `cartarch-prod`); local dev may run SQLite via `DATABASE_URL` | `E1` `current-status.md`, `CLAUDE.md` |

**Confirmed doc-drift (not blocking, but flag before the report is trusted):** the
three in-repo version markers disagree — `README.md` says **v4.11.11**,
`CLAUDE.md` header says **v4.1.22**, `current-status.md` banner says **v4.1.15
(2026-07-01)**. `E1`. The git log (`v4.11.11`, #134) is the authority. Any Phase-1
PR should reconcile the `CLAUDE.md`/`current-status.md` headers as a side-fix; this
report treats **v4.11.11 / Alembic `d0e1f2a3b4c5`** as the baseline.

**E2/E4 gap — declared honestly.** No live prod query was run (`E4` absent). The
data-quality numbers in §4 come from a **local snapshot `snap-watch-20260619-1532.db`
dated 2026-06-19** (`E2`) — it is ~4 weeks stale and **predates `oracle_catalog`**
(table absent in it). Treat §4 numbers as *shape and order-of-magnitude*, not
current counts; re-run the same queries against prod to promote them to `E4`
before any migration sizing decision. The queries are given verbatim so that is a
copy-paste step, not a re-derivation.

---

## 2. Assumption Audit (v0.1 §3) — every program-spec claim, tested

The issue says to *start from* the program spec's known-substrate inventory rather
than discover it cold. Each substrate claim is re-checked against the tree:

| # | Program-spec claim | Verdict | Evidence |
|---|--------------------|---------|----------|
| A | `oracle_catalog` is a partial §8 canonical entity and the **sole** `oracle_id` holder | **Confirmed** | `oracle_id` appears only on `OracleCatalog` (`app/models.py:168`) and only in migration `b1c2d3e4f5a6`; grep across models + all migrations finds it nowhere else. `E1` |
| B | Printings carry **no** `oracle_id` (`cards`, `scryfall_cards`) — the confirmed gap | **Confirmed** | `Card` model `app/models.py:98-146` and baseline `create_table("cards")` `489afd0e62f9:66-94` have no `oracle_id`; `scryfall_cards` baseline `:136-163` keyed on `scryfall_id` PK, no `oracle_id`. `E1` |
| C | `scryfall_cards` semantic fields exist: `keywords`, `produced_mana`, `produced_tokens` | **Confirmed** | Seam keys 27, 28, 22 in `_normalize_card_payload` `app/scryfall.py:159-206`. `E1`/`E3` |
| D | `deck_strategy_profiles` (deckbuilder v2 #60) is a partial §12/§13 substrate | **Confirmed, and more complete than "partial" implies** | Full CRUD: seed/save/reset in `app/recommendation_service.py:1171-1207`, read routes `app/routes/recommendations.py:144-181`, tests `tests/test_recommendation_v2.py:684-791`. Lazily auto-seeded (no daemon). `E1`/`E3` |
| E | `deck_combos` + the #103 daemon/persist/**fingerprint** pattern is a working §20 Option C precedent | **Confirmed — this is the strongest reuse asset** | Table `b8c9d0e1f2a3` / model `app/models.py:397-415`; SHA-256 fingerprint `app/combo_refresh_service.py:33-40`; daemon `app/main.py:714-721`; tests `tests/test_combo_refresh.py`. See §7. `E1`/`E3` |
| F | `deck_bracket_findings` is an existing typed-findings-with-evidence pattern | **Confirmed** | Raw table `app/legacy_tables.py:130-149` (`finding_type` + `finding_value` evidence + `severity` + `weight` + `contributes_to_bracket`); `Finding` dataclass `app/bracket_v2_service.py:215-222`; persist/load `:1060-1165`. `E1` |
| G | The goldfish playtester is a §27-adjacent surface, **explicitly not a rules engine** | **Confirmed** | GET-only, read-only vs `InventoryRow` (`app/routes/goldfish.py:9-15,40-285`); in-code "not a rules engine" assertions `app/static/goldfish.js:124-125,175,499,1460`; region placement by `type_line` only. `E1` |

**Net:** the substrate inventory the program spec hands us is **accurate**. The one
naming nuance: item F's table is literally `deck_bracket_findings`
(`legacy_tables.py:130`), with parent `deck_bracket_estimates` (`:106-128`) — the
program spec's shorthand is correct; do not confuse the two.

---

## 3. Requirements-to-Evidence Matrix (spec revisions #4, #10)

Maps each capability the program implies onto its current implementation state, so
Phase 1 builds only what is genuinely missing. Status ∈ {Implemented, Partial,
Unimplemented}; Coverage ∈ {Tested `E3`, Code-only `E1`, Untested}.

| Requirement (implied by program §8/§12/§20/§27) | State | Backing artifact | Coverage |
|---|---|---|---|
| Canonical card identity (`oracle_id`) on **printings** | **Unimplemented** | — (only `oracle_catalog`, name-scoped) | Untested |
| Canonical oracle entity catalog | **Partial** | `oracle_catalog` (Momir pool only; read by `live_game_service` only) | `E1` |
| Name-as-oracle-proxy grouping | **Implemented** | autocomplete-grouped over `scryfall_cards` `app/routes/decks.py:1391-1392`; brew match-by-name `app/deck_service.py:2946` | `E1` |
| Semantic card fields (keywords / produced_mana / produced_tokens / P/T / loyalty / defense) | **Implemented** | 28-key seam `app/scryfall.py:62-207` | `E3` (`test_scryfall_cache.py`, `test_bulk_data_loop.py`) |
| LLM-parseable card serialization | **Implemented** | `card_metadata()` `app/pricing.py:14-51`; JSON export routes | `E3` (`test_collection_export.py:222`, `test_deck_export.py:193`) |
| Persisted deck analysis w/ recompute-on-change | **Implemented** | `deck_combos` fingerprint+daemon; `deck_bracket_estimates` persist | `E3` (`test_combo_refresh.py`) |
| Typed findings with cited evidence | **Implemented** | `deck_bracket_findings` | `E1` (`test_deck_bracket_page.py`, `test_bracket_floor.py`) |
| Deck strategy profile | **Implemented** | `deck_strategy_profiles` | `E3` (`test_recommendation_v2.py`) |
| Request-path network invariant (batched, off-path backfill) | **Implemented** | `bulk_fetch_by_name/_set_number` `app/scryfall.py:693-861`; 5 daemon loops | `E3` (`test_bulk_fetch_by_name.py`) |
| Bulk mirror → local-first reads | **Implemented** | `_bulk_data_loop` `app/scryfall.py:996` → `scryfall_cards` | `E3` |
| Goldfish read-only playtest surface | **Implemented** | `app/routes/goldfish.py` | **Untested** (see §8 gap) |

**First-increment implication:** the only **Unimplemented** row is `oracle_id` on
printings. Everything the program leans on already exists and is mostly tested — so
Phase 1's job is a *small identity bridge*, not a subsystem.

---

## 4. Data-Quality & Identity-Integrity Profiling (spec revision #5)

Goes beyond row counts. Queries run against the 2026-06-19 snapshot (`E2` — see §1
caveat; re-run on prod for `E4`). `cards` = owned/referenced subset; `scryfall_cards`
= full bulk mirror.

| Metric | `cards` | `scryfall_cards` |
|--------|---------|------------------|
| Rows | 13,700 | 115,817 |
| Null/blank `name` | **0** | **0** |
| Null `scryfall_id` | **0** | 0 (PK) |
| Distinct `scryfall_id` | 13,700 (fully unique) | 115,817 (PK) |
| Distinct `name` | 10,051 | 37,744 |

**Identity-integrity findings:**

1. **`scryfall_id` is a clean, non-null, unique key on both tables** — a sound join
   anchor. `E2`
2. **Name is a *consistent* oracle proxy but a 1:N one.** Where the tables share a
   `scryfall_id`, `cards.name` vs `scryfall_cards.name` disagree in **0** rows — so
   the codebase's "name grouping IS oracle grouping" assumption
   (`app/data/chronicle.json:42`) holds *for consistency*. But **1,723 of 10,051
   owned names carry >1 printing** (max **107** printings for a single name). So
   name-grouping conflates printings under a display string; an `oracle_id` would
   make that grouping *formal and stable across reprints/renames* rather than
   string-equality. `E2`
3. **Mirror coverage gap: 16 owned printings (0.12%) have a `scryfall_id` absent
   from `scryfall_cards`.** `E2`. Small but real — it means any oracle-id backfill
   that resolves *through the local mirror* would miss those 16 until the next bulk
   refresh catches them. This directly informs the Backfill Service Level decision
   (§11) and argues for backfill that tolerates gaps (last-known-value posture, like
   the price ingest) rather than assuming 100% mirror coverage.
4. **Same-name-different-oracle risk is real in MTG** (e.g. reversible/renamed cards)
   but not quantifiable from a name-only snapshot — this is precisely the ambiguity
   an `oracle_id` column removes and a name proxy cannot. **Hypothesis**; settle it
   with the oracle-ingest cross-check query in §12.

**Profiling queries (verbatim, to promote `E2`→`E4`):** see Appendix A.

---

## 5. Source of Truth & Null-Semantics (spec revisions #6, #7)

**Source of truth for identity — declared:** **Scryfall is authoritative for card
semantics and identity; the local catalog is a cache/projection of it.** Evidence:
`scryfall_cards` is a mirror of Scryfall's `default-cards` bulk export
(`app/scryfall.py:863-871`); `oracle_catalog` is populated from Scryfall's
`oracle_cards` bulk file (`app/jobs/oracle_ingest.py`); `card_metadata` and all
reads are projections of persisted Scryfall-derived columns. Gameplay data (games,
decks) never originates identity — it *references* it by `scryfall_id`/`name`. `E1`.
This matches the established price-path doctrine (MTGJSON is authoritative upstream;
`Card.price_usd*` is its cache) — the same "one authoritative upstream, denormalized
local read" shape should govern `oracle_id`.

**Null-semantics for a new `oracle_id` on printings — defined before any migration:**

- The column MUST be **nullable** at introduction. `NULL` means **"not yet
  backfilled"** — a *transient, benign* state, never "no oracle identity exists"
  (every real printing has one upstream) and never a disallowed state. `E1` rationale:
  this mirrors the exact posture that made `set_type`/`produced_mana` safe to add —
  additive nullable column, passive daemon backfill, forward-progress guarantee
  (`_trait_backfill_loop` writes `set_type=""` on a Scryfall miss to avoid re-selecting
  a poison row, `app/main.py:561-564`).
- A backfill MUST NOT block on 100% coverage (§4 finding 3: 16 rows have no mirror
  row *today*). `NULL` is tolerated indefinitely and simply excluded from oracle-keyed
  grouping until filled — exactly how NULL `color_identity` is excluded from the
  commander-legal subset filter (`CLAUDE.md`, facet-filter section).
- **Do not** add a `NOT NULL` or `CHECK` constraint (constrained values are enforced
  at the service layer in this codebase, never with a DB `CHECK` — `CLAUDE.md`
  invariant). `E1`.

---

## 6. Existing-Implementation Map (v0.1 §2 / §29 C) — cited

The reuse assets, one place, with the citation each recommendation in §9 leans on:

- **Identity today (fractured, no join key):** `oracle_id` lives only in
  `oracle_catalog` (`models.py:165-182`, Momir pool, **manual** ingest
  `app/jobs/oracle_ingest.py`, read only by `app/live_game_service.py`). Printings
  (`cards`, `scryfall_cards`) carry `scryfall_id` + a per-row `name`. Every identity
  op outside Momir falls back to name-string matching (`decks.py:1391`,
  `deck_service.py:2946`). `E1`.
- **The cache seam (the extension point for `oracle_id`):** `_normalize_card_payload`
  / `_CACHE_COLUMNS` / `_cached_row_to_payload` / `card_constructor_kwargs`,
  `app/scryfall.py:62-545`. **28 keys today** (produced_mana was the 28th, #100).
  Precedent migrations that added a semantic column to **both** `cards` and
  `scryfall_cards`: `c8d2e5f7a1b4` (power/toughness/keywords, #76) and
  `a7b8c9d0e1f2` (produced_mana, #100). Parity tests: `test_scryfall_cache.py`,
  `test_bulk_data_loop.py`. `E1`/`E3`.
- **Persistence precedent (§20 Option C):** `deck_combos` — SHA-256 **fingerprint**
  of the semantic inputs decides recompute-vs-reuse
  (`combo_refresh_service.py:33-40,68-70`); a bounded daemon (`limit=3`/pass,
  commit-per-deck) catches up in the background; request path reads the persisted
  row (`deck_service.py:1965-2068`). `E1`/`E3`. **The persistence question is
  effectively settled** — this pattern is the answer.
- **Typed-findings precedent:** `deck_bracket_findings` — `finding_type` +
  `finding_value` (evidence) + `severity` + `weight` + `contributes_to_bracket`;
  `persist_estimate` deletes-then-inserts per deck (`bracket_v2_service.py:1060-1131`).
  Any future "card intelligence findings" surface should adopt this shape verbatim
  rather than invent a new one. `E1`.
- **LLM/export surface:** `card_metadata()` (`pricing.py:14-51`) + `?format=json`
  on `/collection/export` (`collections.py:707-748`) and `/decks/{id}/export`
  (`decks.py:1175-1259`). `E1`/`E3`.

---

## 7. Operational Analysis of Background Jobs (spec revision #8)

Five in-process daemon threads, spawned in `lifespan` (`app/main.py:211-220`) as
`threading.Thread(daemon=True)`, stopped via `shutdown_event` + `join(timeout=10)` +
WAL checkpoint. `E1`.

| Loop | Def | Cadence (busy/idle) | Batch / commit | Bound |
|------|-----|---------------------|----------------|-------|
| `_price_refresh_loop` | `main.py:488` | — / 600s | oldest 75 cards, one commit | 75/pass |
| `_trait_backfill_loop` | `main.py:576` | 3s / 600s | `set_type IS NULL`, commit-per-batch, writes `""` on miss | 75/pass |
| `_bulk_data_loop` | `scryfall.py:996` | poll | full bulk mirror refresh, catch-all retry | 1/cycle |
| `_loyalty_defense_backfill_loop` | `main.py:672` | 3s, **one-shot** then exits | id-cursor paginated, commit-per-batch | 75/batch |
| `_combo_refresh_loop` | `main.py:714` | 15s / 900s | 3 stale decks, commit-per-deck | 3/pass |

- **Leader election: NONE.** No advisory lock / lease / leader gate anywhere. `E1`.
  The design is **explicitly single-replica** (`app/db.py:51`
  "*Single-replica scope (Gate #7); PgBouncer + worker-split tuning is deferred to
  v4.0.x*"; `app/live_game_events.py:8-10`; `password_reset_service.py:84`). At >1
  replica, every replica would run all five loops uncoordinated → duplicate
  Spellbook/bulk fetches and concurrent SQLite-writer contention. **Any new card-
  intelligence daemon inherits this constraint** — do not add one that assumes
  multi-replica safety without the deferred worker-split.
- **Crash recovery: idempotent re-selection, no job table.** Each loop re-derives
  its worklist from DB state every pass (oldest-price / `NULL`-column / fingerprint
  mismatch / id-cursor), commits per batch so partial progress survives a pod kill,
  and **advances past poison rows** to guarantee termination (`set_type=""` on miss;
  unconditional cursor advance `main.py:640-644`). `E1`.
- **Poison records / manual replay:** handled by forward-progress writes, not a
  dead-letter queue. Manual replay = null the column (or bump the fingerprint input)
  and the loop re-selects it. `E1`.
- **Observability:** per-loop `logger` on exception + "daemon must never die
  silently" catch-alls (`scryfall.py:1002-1006`). No metrics per-loop beyond the
  global Prometheus instrumentator. **Gap to note:** no per-daemon progress gauge —
  an oracle-id backfill's "% filled" would not be observable without adding one.

**Recommendation:** an `oracle_id` backfill needs **no new daemon** — it rides the
**existing `_trait_backfill_loop`** (same `NULL`-column-select, same 75/batch, same
forward-progress posture), or is filled passively on the next bulk rebuild via the
seam. That is the laziest correct answer and avoids the single-replica leader
question entirely.

---

## 8. Security & Tenant-Boundary Review — MCP / LLM (spec revision #9)

The LLM-facing surface splits cleanly into **in-repo (safe)** and **external
(unverified here)**:

- **In-repo JSON export routes are tenant-scoped and read-only.** `E1`. Both
  `?format=json` routes are GET handlers that only `session.query(...)` (no writes)
  and filter by `current_user.id`: `collections.py:712,720-722` via
  `_filtered_collection_query(session, current_user.id, ...)`; `decks.py:1180-1192`
  via `get_deck(..., user_id=current_user.id)` + `InventoryRow.user_id ==
  current_user.id`. Non-ownership does not leak existence (goldfish redirects rather
  than 404s; deck export 404s). `card_metadata` reads persisted columns only, no
  network (`pricing.py:15-22`). **No prompt-injection surface** — these emit data,
  they don't consume model output.
- **The MCP raw-SQL surface is NOT in this repo and is the real risk to escalate.**
  `E1`. `run_query` / `describe_schema` / `call_endpoint` / `recent_logs` are the
  **external `cartarch-mcp` deployment** (platform repo `k8s/apps/cartarch-mcp`);
  this repo contains only a `/health` probe (`main.py:335-350`). `call_endpoint`
  inherits per-route tenant scoping (it goes through FastAPI). **`run_query` bypasses
  FastAPI entirely** — no `user_id` filter and no read-only guarantee exist *in this
  codebase* to govern it; any such guarantee lives in the external service and could
  not be verified here.

**Action for the review (not this repo):** before any card-intelligence feature
exposes data via MCP, verify in `k8s/apps/cartarch-mcp` that `run_query` is (a)
operator-only / not end-user reachable, (b) SELECT/read-only enforced, and (c)
not a cross-tenant read path. **Schema is intentionally surfaced** to the operator
LLM via `describe_schema` — acceptable, but it means adding a table like
`oracle_id`-on-`cards` is immediately visible to that tool; no new secret is
introduced by the identity work, but the boundary owner should be told.

---

## 9. Reuse-vs-Replace with Credible Alternatives (spec revision #11 / §29 D)

For each identity decision, the recommendation **and at least one real alternative**:

**Decision 1 — Where does canonical `oracle_id` live for printings?**
- **Reuse (recommended): add `oracle_id` as the 29th cache-seam column** on `cards`
  AND `scryfall_cards`, via the exact #76/#100 precedent (migration on both tables,
  extend `_normalize_card_payload`/`_CACHE_COLUMNS`, parity tests, passive backfill).
  *Pros:* smallest diff; one proven pattern; request-path-safe; keeps the "one
  authoritative upstream, denormalized local read" doctrine. *Cons:* denormalized
  copy on 2 tables (parity discipline needed — the seam tests already enforce it).
- **Alt A — join to `oracle_catalog` at read time** (no new column). *Pros:* zero
  schema change; single home for oracle data. *Cons:* `oracle_catalog` is
  name/oracle-scoped and **manually** ingested (stale between runs), covers only the
  Momir pool today, and has no `scryfall_id`→`oracle_id` printing map — so joining
  needs a name bridge, reintroducing the 1:N ambiguity §4 is trying to kill. Rejected
  as the primary.
- **Alt B — dedicated `printing_oracle_map(scryfall_id, oracle_id)` table.**
  *Pros:* normalized, no seam churn. *Cons:* a new join on every identity read
  (N+1 risk on the request path the whole codebase is architected to avoid), and a
  second thing to keep in sync. Reasonable if the seam parity burden ever bites, but
  heavier than the reuse option today.

**Decision 2 — How is persisted card intelligence recomputed on change?**
- **Reuse (recommended): the `deck_combos` fingerprint+daemon pattern** verbatim
  (§6/§7). The program spec itself flags this as "largely settled" — confirmed.
- **Alt — event-driven / write-path hooks** (recompute on deck/inventory mutation).
  *Pros:* fresher, no polling lag. *Cons:* couples every mutation path to the
  analysis; the codebase deliberately chose the *opposite* (fingerprint invalidation,
  no write-path hooks — `models.py:400-404`) because it's crash-safe and decoupled.
  Do not relitigate without a reason the fingerprint model can't meet.

**Decision 3 — Backfill mechanism for `oracle_id`.**
- **Reuse (recommended): ride `_trait_backfill_loop`** (NULL-column select, 75/batch,
  forward-progress) — no new daemon, no leader-election question (§7).
- **Alt — a bespoke one-shot backfill daemon** (like `_loyalty_defense_backfill_loop`).
  *Pros:* isolated cadence. *Cons:* another single-replica thread to reason about;
  unnecessary when an existing NULL-column loop already fits.

---

## 10. Ranked First-Increment Recommendation (spec revisions #1, #10 / §29 J)

Replaces the program spec's *mandatory* `oracle_id` PR with a ranked recommendation.
**Recommended order: A, then B in parallel, C only if reconciliation surfaces a
surprise.**

**Candidate A — `oracle_id` as the 29th cache-seam column (RECOMMENDED first PR).**
- *What:* additive nullable `oracle_id` on `cards` + `scryfall_cards`; extend
  `_normalize_card_payload` / `_CACHE_COLUMNS` / `_cached_row_to_payload`; bump the
  seam parity tests to 29; passive backfill via `_trait_backfill_loop` + next bulk
  rebuild.
- *Rationale:* it is the **only Unimplemented requirement** (§3), it unblocks formal
  identity grouping (§4 finding 2), and it rides two proven precedents (#76, #100)
  with a known-small diff and existing test guardrails.
- *Risks / dependencies:* seam-parity discipline (mitigated by the pin tests); the
  16-row mirror gap (§4.3) means backfill must tolerate NULL indefinitely (§5);
  Scryfall bulk export must carry `oracle_id` per printing (it does — settle with
  Appendix-A query at `E4`).
- *Evidence level to green-light:* `E1` complete; promote the §4 profiling to `E4`
  on prod first to size the backfill.

**Candidate B — Characterization-test PR for the name-as-oracle-proxy paths.**
- *What:* pin current behaviour of `decks.py:1391` (autocomplete-grouped) and the
  brew match-by-name path (`deck_service.py:2946`) *before* `oracle_id` changes
  anything, since these are the code paths an oracle column will eventually touch.
- *Rationale:* §3 shows these are `E1` code-only (untested at the grouping level);
  characterizing them de-risks A. Cheap, parallelizable, no schema.
- *When to prefer over A:* if the human sets Required Confidence Level to "tests
  before schema" (§11).

**Candidate C — Deployment-state reconciliation PR.**
- *What:* fix the version-marker drift (§1) and run the §4 queries against prod to
  establish `E4` counts.
- *Rationale:* lowest-value as a *feature* but zero-risk; do it only if A's sizing
  needs the prod numbers first, or fold the doc-fix into A.

---

## 11. Human Decisions Required Before Phase 1 (spec "Human Decisions" table)

Carried forward unresolved — these gate the build, not this report:

| Decision | Recommended default (this report) |
|----------|-----------------------------------|
| Authoritative baseline | Prod = `main`/v4.11.11 @ Alembic `d0e1f2a3b4c5` (§1) |
| Decision authority | Author recommends first PR + follow-up issues; **human approves before any schema change** |
| Required confidence level | `E1`+`E3` to build; `E4` (prod profiling) to *size* the backfill |
| Data authority for identity | **Scryfall** (§5); local catalog is cache |
| Catalog scope | Paper Scryfall printings (as `scryfall_cards` already mirrors); custom/tokens out of oracle scope |
| Backfill service level | **Lazy, gap-tolerant, NULL-until-filled** (§5); add a progress gauge (§7 gap) |
| Report acceptance | Human review; **#122 stays open until explicitly accepted** (spec revision #13) |

---

## 12. Stopping Conditions & Closure (spec revisions #13, #14)

**This report is complete** by the spec's own definition: all seven substrate
claims tested (§2), all requirements mapped (§3), identity profiled with real
numbers (§4), a ranked first increment provided (§10). **It intentionally stops
here** — no code, no schema, no chronicle text (§0), per the report-only scope.

**Closure of #122 is contingent on human review:** publish → attach the §11
unresolved decisions → request review → close only after acceptance/rejection is
explicitly recorded. Do **not** auto-close.

---

## Appendix A — Profiling queries (run on prod to promote §4 to `E4`)

```sql
-- Identity integrity on the two printing tables
SELECT count(*) FROM cards WHERE name IS NULL OR name = '';
SELECT count(*) FROM cards WHERE scryfall_id IS NULL;
SELECT count(DISTINCT name) AS distinct_names, count(*) AS rows FROM cards;
-- Names carrying >1 printing (what an oracle_id would formalize)
SELECT count(*) FROM (SELECT name FROM cards GROUP BY name HAVING count(*) > 1) t;
SELECT max(c) FROM (SELECT count(*) c FROM cards GROUP BY name) t;
-- Bulk-mirror coverage gap
SELECT count(*) FROM cards ca
  LEFT JOIN scryfall_cards s ON ca.scryfall_id = s.scryfall_id
  WHERE s.scryfall_id IS NULL;
-- Name consistency where the join key matches (expect 0)
SELECT count(*) FROM cards ca
  JOIN scryfall_cards s ON ca.scryfall_id = s.scryfall_id
  WHERE ca.name IS DISTINCT FROM s.name;
-- Same-name-different-oracle check (needs oracle_catalog populated on prod)
-- Cross oracle_catalog.name against cards.name to quantify §4 finding 4.
```

## Appendix B — Open questions / test gaps

- **Goldfish has no route-level test** (`E1`/§3) — only `test_image_mirror.py` grazes
  it. If Phase 1 touches the goldfish payload, add a read-only-guarantee test first.
- **No per-daemon progress metric** (§7) — an oracle-id backfill's coverage is not
  observable without one.
- **§4 finding 4 (same-name/different-oracle count) is a Hypothesis** until the
  Appendix-A oracle_catalog cross-check runs on prod (`E4`).
- **MCP `run_query` read-only/tenant enforcement is unverifiable from this repo**
  (§8) — must be checked in the platform repo.
- **Version-marker drift** (`CLAUDE.md` v4.1.22 / `current-status.md` v4.1.15 vs
  README v4.11.11, §1) — reconcile in the first PR.
