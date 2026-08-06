# APP PERFORMANCE AUDIT — cc#869

Read-only. This document is the only artifact; no application code was changed, no DDL was run,
no VACUUM / ANALYZE / REINDEX / CREATE INDEX / DROP INDEX was executed. Every remediation below is
a **proposal** for the Railway console, weekend, outside market hours, with founder approval
(MAINTENANCE_LOCK_RULE cc#351).

Every finding is labelled **MEASURED** (a number produced against the live Railway DB, with the
query text so it can be re-run) or **STRUCTURAL** (established by reading the call path, because
this container cannot reach scorr.in and cannot open a socket to the DB).

Generated 06-Aug-2026.

---

## TWO P0s, STATED FIRST

The card's stop condition asks for immediate escalation when a finding is live breakage rather than
slowness — a path that can hang forever, or a lock that can wedge the database. Both exist. They are
stated here at the top rather than at their rank position. Nothing was remediated; the rest of the
audit is read-only and was completed so the report would be whole.

**P0-A — a read request takes an ACCESS EXCLUSIVE lock.** 16 GET handlers run `ALTER TABLE` on
every call (finding 3). `ADD COLUMN IF NOT EXISTS` still takes the lock even when it is a no-op. If
one of those tables is under a slow read, the ALTER queues behind it and *every subsequent access to
that table queues behind the ALTER* — including the writer's. That is the shape of the 10-Jul
incident that produced MAINTENANCE_LOCK_RULE, except here it fires from a page load rather than from
a console.

**P0-B — 129 of 134 `fetch()` call sites in the served front end have no timeout** (finding 7). A
request that never returns leaves a spinner or shimmer animating forever, which reads as "still
loading" and never as "failed". cc#858 established the principle on the R card; it was never applied
anywhere else.

---

## RANKED FINDINGS

| # | Finding | Evidence | Impact | Fix size |
|---|---|---|---|---|
| 1 | Auth check opens a fresh DB connection on the event loop, every protected page load | MEASURED + STRUCTURAL | Every page, every user | Small |
| 2 | 20 `async def` handlers block the event loop on psycopg | STRUCTURAL (AST, full coverage) | Whole service stalls | Small each |
| 3 | 61 handlers run DDL per request; 16 are GET + ALTER TABLE | STRUCTURAL (AST, full coverage) | Lock contention | Medium |
| 4 | `gvm_history` has no index on `segment` — 2 seq scans of 198 MB per GVM report | MEASURED 93.7 ms × 2 | GVM report page | Small |
| 5 | 65% of all DB connection time is idle-in-transaction | MEASURED | Connection pressure | Medium |
| 6 | `auth_gate` buffers the whole HTML body and rewrites it on the event loop | MEASURED (369 KB) | Dashboard load | Medium |
| 7 | 129 of 134 front-end `fetch()` sites have no AbortController | MEASURED (count) | "Stuck" symptom | Medium |
| 8 | `intraday_prices` carries 892 MB of indexes on a 517 MB heap | MEASURED | Write path, all day | Small (proposal) |
| 9 | No connection pooling anywhere — 117 modules, 158 connect sites | MEASURED | Compounds 1, 2, 5 | Large |
| 10 | `gvm_history` never autoanalyzed, never autovacuumed | MEASURED | Planner risk | Small (proposal) |

---

## 1 · THE AUTH CHECK IS A DB CONNECT ON THE EVENT LOOP — *every protected page load*

**MEASURED + STRUCTURAL. This is the single highest-value finding and it is on the hottest path in
the app.**

`main.py:311` — the request middleware is `async def`:

```python
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    _is_preview = request.url.path == '/preview' or request.url.path.startswith('/preview/')
    if (request.url.path in PROTECTED or _is_preview) and not _is_authed(request):
```

`scorr_auth.py:_is_authed` is a plain blocking function that opens a **brand-new connection**:

```python
def _is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token: return False
    with _conn() as conn, conn.cursor() as cur:              # psycopg.connect(DATABASE_URL)
        cur.execute("SELECT 1 FROM auth_sessions WHERE token=%s AND expires_at > now()", (token,))
```

So every request to `/`, `/dashboard`, `/check`, `/news`, `/holdings`, `/scanners`, `/sector`,
`/fpc`, `/filters`, `/cio`, `/cio2`, `/ask`, `/v9`–`/v15`, `/digest`, `/result-corner`,
`/screeners`, `/scheduler-master`, `/adaptive`, and every `/preview/*` performs a full TCP + TLS +
Postgres auth handshake **on the event loop**, before the route is even reached. While that
handshake is in flight, no other request on the service can progress.

**The query is not the cost.** MEASURED, reproducible:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT 1 FROM auth_sessions WHERE token='probe-nonexistent' AND expires_at > now();
-- Index Scan using auth_sessions_pkey ... Execution Time: 0.044 ms
```

0.044 ms of work. Everything else is connect and teardown. This container cannot open a socket to
Railway Postgres, so the handshake itself is **not measured here** and no number is invented for it
— but the ratio is the point: the fix removes an entire connection lifecycle to save a 0.044 ms
lookup, and it does so on the path every single page takes.

`auth_sessions` is also 3 live rows against 10 dead, never autovacuumed.

**Proposed fix — SMALL.** Either (a) an in-process dict cache of `{token: expiry}` with a short TTL,
so a valid token costs zero DB round-trips for the next N seconds, or (b) change `auth_gate` to a
non-async middleware so at least the block lands in a threadpool. (a) is strictly better and is
about fifteen lines. Real revocation still works if the TTL is short (60 s) — `/logout` already
DELETEs the row and the cache expires behind it.

---

## 2 · ITEM 1 — ASYNC-OVER-PSYCOPG2 AUDIT (the cc#857 class)

**STRUCTURAL. Coverage is complete, not sampled.**

Method: every `.py` file in the repo is parsed with `ast`. Every function carrying a route decorator
(`@x.get/post/put/delete/patch/head/options/websocket/api_route`) is classified. For each, a
per-module call graph is walked to see whether any reachable local function contains a blocking DB
marker (`psycopg`, `connect(`, `.cursor(`, `cur.execute`, `conn.commit`, `read_sql`). Handlers that
offload via `run_in_executor` / `to_thread` are excluded.

```
python files parsed            : 167
files declaring route handlers : 85
route handlers checked         : 471
  async def handlers           : 55
  async + reaches psycopg      : 20    <-- OFFENDERS
  async, no DB in path         : 35
  def (threadpool) + DB        : 295   <-- the correct pattern, already the majority
```

295 handlers already do the right thing. 20 do not. **One is enough to stall the service**, which is
why single-user testing never predicted it (cc#857's own conclusion).

### The 20 offenders

| File:line | Handler | Route | Path to DB |
|---|---|---|---|
| `mcp_dispatch.py:396` | `mcp_endpoint` | POST `/mcp` | `mcp_endpoint -> _call_tool` |
| `scorr_auth.py:210` | `login_get` | GET `/login` | `login_get -> _is_authed` |
| `scorr_auth.py:217` | `login_post` | POST `/login` | `login_post -> _new_session_token` |
| `scorr_auth.py:253` | `logout` | GET `/logout` | `logout -> _revoke_token` |
| `sector_brief_endpoints.py:159` | `sector_brief` | GET `/api/sector/brief` | direct |
| `sector_brief_endpoints.py:186` | `sector_brief_batch` | POST `/api/admin/sector/brief/batch` | direct |
| `fundamentals_scraper.py:870` | `fundamentals_scrape_status` | GET `/fundamentals_scrape_status` | direct |
| `anthropic_endpoints.py:53` | `anthropic_chat` | POST `/chat` | `-> get_db_conn` |
| `anthropic_endpoints.py:128` | `get_usage` | GET `/usage` | `-> get_db_conn` |
| `main.py:1356` | `v8_run` | POST `/api/v8/run` | `-> get_conn` |
| `main.py:1410` | `backfill_intraday` | POST `/api/admin/backfill_intraday` | `-> _get_futures_symbols` |
| `qb_endpoints.py:85` | `qb_mark_intraday_now` | POST `/mark_intraday` | `-> _conn` |
| `scorr_endpoints.py:123` | `scorr_query` | POST `/api/scorr/query` | direct |
| `v8_futures.py:45` | `upload_futures` | POST `/upload` | direct |
| `v8_futures.py:86` | `add_futures` | POST `/add` | direct |
| `v8_futures.py:132` | `remove_futures` | POST `/remove` | direct |
| `admin_data.py:73` | `load_input` | POST `/api/admin/load_input_from_drive` | direct |
| `admin_data.py:370` | `load_earnings_from_screener` | POST `/api/admin/…` | `-> refresh_earnings_calendar` |
| `gvm_nightly.py:805` | `load_screener_json` | POST `/api/admin/load_screener_json` | `-> _sql_clean_replace_screener` |
| `hr_endpoints.py:271` | `health_upload` | POST `/api/health/upload` | direct |

### The three that matter most

- **`mcp_dispatch.py:396 POST /mcp`.** Every MCP tool call from Claude — every `run_sql`, every
  `get_v8_metrics`, every backfill trigger — enters here on the event loop and `_call_tool` both
  queries the DB and makes outbound HTTP. A single slow tool call stalls the whole service for its
  duration. **This is very likely why the app feels stuck precisely while Claude is working on it.**
- **`scorr_auth.py` login/logout.** Compounds finding 1 — the auth path is blocking at both the
  middleware and the route.
- **`sector_brief_endpoints.py:159 GET /api/sector/brief`.** A user-facing read, `async def`, direct
  psycopg, *and* it runs `CREATE TABLE` per request (finding 3).

**Proposed fix — SMALL, per handler.** Drop the `async` keyword. FastAPI then runs the handler in a
threadpool. That is exactly what cc#857 did to `results_card`, and it is a one-word change wherever
the handler does not itself `await` anything. Handlers that *do* `await` (e.g. `mcp_endpoint`,
`admin_data.load_input`) need their blocking section moved into `await asyncio.to_thread(...)`
instead — still small, but not a one-word change.

**Binding on cc#874:** every new handler in `mobile_endpoints.py` must be `def`, not `async def`.
This audit is the check that keeps the new mobile surface out of this table.

---

## 3 · ITEM 2 — PER-REQUEST DDL (P0-A)

**STRUCTURAL. Same AST method, full coverage.**

```
route handlers whose call path reaches DDL : 61
  of which GET (user read paths)           : 34
  of which reach ALTER TABLE               : 26   (ACCESS EXCLUSIVE lock)
  GET *and* ALTER TABLE                    : 16   <-- the P0 class
```

cc#857 found and fixed **one** instance of this (`_ensure_cols()` on the R card). It is not unique
to that file; it is the house pattern. 61 handlers call some `ensure_tables()` / `_ensure_*()` helper
on every request instead of at startup.

### GET handlers that run ALTER TABLE on every call

```
intraday_scanner_endpoints.py:551   GET /api/scanners/intraday
intraday_scanner_endpoints.py:595   GET /api/scanners/intraday/short
intraday_scanner_endpoints.py:675   GET /api/scanners/intraday/watchlist
intraday_scanner_endpoints.py:824   GET /api/scanners/orb_ag
mf_pipeline.py:1153                 GET /api/v15/search
mf_pipeline.py:1222                 GET /api/v15/fund/{scheme_code}
mf_pipeline.py:1364                 GET /api/v15/stats
mf_pipeline.py:1378                 GET /api/v15/screener
mf_pipeline.py:1476                 GET /api/v15/screener/selftest
mf_pipeline.py:1482                 GET /api/v15/mf/search
mf_pipeline.py:2279                 GET /api/v15/mf/coverage_report
mf_pipeline.py:2306                 GET /api/v15/mf/curated
mf_pipeline.py:2319                 GET /api/v15/mf/fund/{scheme_code}
mf_pipeline.py:3003                 GET /api/v15/mf/mc_discover
v13_presets_endpoints.py:88         GET /api/v13/presets          (+ CREATE UNIQUE INDEX)
v13_presets_endpoints.py:314        GET /api/v13/theme/list       (+ CREATE UNIQUE INDEX)
```

`/scanners` is a page the founder opens. Loading it fires four of these.

### GET handlers that run CREATE TABLE / CREATE INDEX on every call

```
deriv_metrics.py:1231               GET /api/deriv-metrics/{symbol}
gvm_report_endpoints.py:259         GET /api/gvm/company/{symbol}      <- the GVM report page
gvm_report_endpoints.py:683         GET /api/gvm/recent-searches
intraday_scanner_endpoints.py:989   GET /api/scanners/result_radar
intraday_scanner_endpoints.py:1139  GET /api/scanners/result_radar/accuracy
ops_control_plane.py:223            GET /api/ops/control-plane/diagnosis
ops_control_plane.py:229            GET /api/ops/control-plane/findings
ops_metrics_pipeline.py:1888        GET /api/ops_metrics/registry
ops_metrics_pipeline.py:1903        GET /api/ops_metrics/company/{symbol}
ops_metrics_pipeline.py:2005        GET /api/ops_metrics/guidance/{symbol}
ops_metrics_pipeline.py:2021        GET /api/ops_metrics/sector_trend
ops_metrics_pipeline.py:2032        GET /api/ops_metrics/segment_trends
ops_metrics_pipeline.py:2055        GET /api/ops_metrics/concall/{symbol}
ops_metrics_pipeline.py:2117        GET /api/admin/ops_metrics/status
scheduler_master.py:481             GET /api/scheduler/master
sector_brief_endpoints.py:159       GET /api/sector/brief
sector_brief_endpoints.py:202       GET /api/admin/sector/brief/status
tc_scanner_endpoints.py:340         GET /api/scanners/tc/holds
```

**Why this is the wrong reading of cc#351.** The rule says lock-taking maintenance is console-only,
weekends, propose-first. These handlers take an ACCESS EXCLUSIVE lock on a Thursday at 11am from a
phone. The `IF NOT EXISTS` makes it a no-op *logically*, not *lock-wise*.

**Proposed fix — MEDIUM.** Move every `ensure_tables()` / `_ensure_*()` call from the request path to
a `@router.on_event("startup")` hook, which is exactly the shape cc#857 used. 61 call sites, but
they collapse to roughly a dozen distinct `ensure_*` functions — the edit is mechanical and each
module can move independently, so it can be split across several small cards rather than one big one.

---

## 4 · ITEM 4 — MEASURED QUERY TIMINGS

`pg_stat_statements` is **not installed**, so there is no query history to mine. Everything below was
measured by running `EXPLAIN (ANALYZE, BUFFERS)` against the live Railway DB. Query text is included
so each number is reproducible.

Threshold per the card: **>500 ms is a finding, >2000 ms is a P1 in its own right.**

| Surface | Query | Measured |
|---|---|---|
| Every protected page | auth session lookup | **0.044 ms** |
| Intel (`/news`) | polished feed, 20 rows | **9.3 ms** |
| GVM report | trajectory point (`_traj_point`, ×2 per load) | **93.7 ms each** |
| Results card (R) | post-cc#857 | 18 ms (cc#857, unchanged) |

Nothing measured crossed 500 ms *as the application calls it*. The GVM trajectory block is the
worst at ~190 ms per report load, and it is worst for a structural reason worth fixing (below).

### 4a · GVM report trajectory — 93.7 ms, twice, for 23 rows

`gvm_market_endpoints.py:153 _traj_point`, called twice by `_trajectory` (d-21 and d-126) from the
GVM report page.

```sql
EXPLAIN (ANALYZE, BUFFERS)
WITH latest AS (
    SELECT DISTINCT ON (symbol) symbol, gvm_score FROM gvm_history
    WHERE segment='FMCG - Large' AND score_date <= CURRENT_DATE - 21
    ORDER BY symbol, score_date DESC)
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY gvm_score) AS med,
       COUNT(*) FILTER (WHERE gvm_score IS NOT NULL) AS n,
       (SELECT COUNT(*) FROM latest l2 WHERE l2.gvm_score >
            (SELECT gvm_score FROM latest WHERE symbol='ITC')) + 1 AS rnk
FROM latest;
```

```
Parallel Seq Scan on gvm_history
  Filter: ((segment = 'FMCG - Large') AND (score_date <= (CURRENT_DATE - 21)))
  Rows Removed by Filter: 498142     (× 3 workers ≈ 1.52 M rows scanned)
  Buffers: shared hit=13703 read=11681
Execution Time: 93.734 ms
```

**1.52 million rows scanned to return 23.** The code comment calls it a "cheap indexed lookup"; it is
a full parallel sequential scan of a 198 MB table, because **`gvm_history` has no index on
`segment`**. Its four indexes are on `(symbol, score_date)` twice, `(score_date)`, and the pkey.

*Side note, clearly labelled synthetic:* the same `DISTINCT ON` shape with the `segment` filter
removed takes **11,195 ms** and walks 1,484,863 rows. That is not a query the app runs — it is
recorded only to show how badly this shape degrades once the filter stops being selective.

**Proposed fix — SMALL:** `CREATE INDEX CONCURRENTLY idx_gvm_history_segment_date ON gvm_history
(segment, score_date DESC)`. **PROPOSAL ONLY — console, weekend, founder approval.**

### 4b · Intel feed — 9.3 ms, healthy

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT polished_id AS id, raw_news_id, headline AS headline_clean, summary, full_summary,
       category, sentiment, impact, mentioned_symbols, source_name AS source,
       display_time AS published_time, polished_at
FROM v_polished_articles ORDER BY display_time DESC NULLS LAST, polished_id DESC LIMIT 20 OFFSET 0;
-- Execution Time: 9.293 ms
```

Worth recording because cc#870 added a suppression anti-join to this view **today**: the
`Hash Anti Join` against `news_suppressed` costs 0.085 ms on an empty table. No regression.

### Coverage gap, stated

Home, V8 dashboard and Trade Check were **not** individually EXPLAIN-measured. Their heavy paths
(`/api/paper/status`, `/api/v8/qualified/{basket}`, `/api/trade-check/v4`) assemble their results in
Python across many small queries rather than in one statement, so a single EXPLAIN would not
represent them and the honest measurement is per-request wall clock — which this container cannot
take (no route to scorr.in). Their dominant cost is almost certainly finding 1 plus finding 9, not
any one query. Flagging this rather than reporting a number that does not mean what it looks like.

---

## 5 · ITEM 5 — THE DB FINDINGS, RE-CHECKED

All of Claude.ai's 05-Aug measurements still hold on 06-Aug. Two got slightly worse.

| Table | Heap | Indexes | Live | Dead | Dead % | last_autovacuum | last_autoanalyze |
|---|---|---|---|---|---|---|---|
| `intraday_prices` | 517 MB | 892 MB | 4,443,949 | 633,911 | **14.3%** | **NULL** | 2026-08-05 |
| `gvm_history` | 198 MB | 185 MB | 33,986 | 7,125 | **21.0%** | **NULL** | **NULL** |
| `raw_prices` | 182 MB | 293 MB | 1,769,311 | 345,652 | **19.5%** | **NULL** | 2026-08-05 |
| `option_chain` | 10 MB | 124 MB | 70,780 | 3,761 | 5.3% | 2026-08-05 | 2026-08-05 |
| `v8_metrics` | 16 MB | 8.5 MB | 35,349 | 5,707 | 16.1% | 2026-08-04 | 2026-08-05 |
| `auth_sessions` | 8 kB | 16 kB | 3 | 10 | — | **NULL** | **NULL** |

- **`gvm_history` still has NO statistics at all** — `last_autoanalyze` and `last_analyze` both NULL
  on a 383 MB table. CONFIRMED. What it implies for the six surfaces: the GVM report is the only one
  of them that reads this table, and its worst query (4a) turned out to be a *shape* problem the
  planner estimated correctly (11,280 est vs 8,729 actual per worker), not a statistics blowout. So
  the missing statistics have **not** yet produced a visibly bad plan — but they are a live risk on
  every future query against it, and the fix is one `ANALYZE`.
- `intraday_prices` dead tuples rose 633,911 (14.3%, was 12.5% on 05-Aug) and it has **never** been
  autovacuumed. `raw_prices` 19.5% (was 16.2%). Both trending up.
- `auth_sessions`: 3 live, 10 dead, never autovacuumed — the table finding 1 hits on every page load.
- A second schema `harness` holds empty shadow copies of `intraday_prices` (BT7 sandbox). Harmless,
  recorded so nobody reads the duplicate `pg_stat_user_tables` rows as corruption.

**Proposed — PROPOSAL ONLY, console, weekend, founder approval:**
`ANALYZE gvm_history;` first (cheap, no lock of consequence, immediate planner benefit), then
`VACUUM (ANALYZE) intraday_prices, raw_prices, gvm_history;`. Plain `VACUUM`, **never** `VACUUM
FULL` — full rewrites the table under an ACCESS EXCLUSIVE lock and is precisely what cc#351 forbids.

---

## 6 · ITEM 6 — INDEX WEIGHT ON THE WRITE PATH

**MEASURED.** `intraday_prices` carries **892 MB of indexes against a 517 MB heap** — 1.7× the data —
and the 5-minute signal writer inserts into it all day. Every index is maintained on every insert.

| Table | Index | Size | idx_scan | Verdict |
|---|---|---|---|---|
| `intraday_prices` | `uq_intraday_sym_ts_tf_src` (unique) | 422 MB | 2,365,301 | **KEEP** — earns it, and the upsert needs it |
| | `idx_intraday_symbol_ts` | 272 MB | 1,079,517 | **REDUNDANT-PREFIX candidate** — `(symbol, ts)` is the leading prefix of the 422 MB unique above |
| | `intraday_prices_pkey` | 106 MB | **0** | **NOT A DROP CANDIDATE — constraint** |
| | `idx_intraday_prices_date` | 59 MB | 4,131,790 | **KEEP** — most-used index on the table |
| | `idx_intraday_timeframe` | 33 MB | **23** | **DROP CANDIDATE** — 23 scans in the table's lifetime, pure write tax |
| `raw_prices` | `idx_raw_prices_symbol_date` | 85 MB | 26,720 | **REDUNDANT-PREFIX candidate** — covered by the unique key below |
| | `raw_prices_symbol_price_date_key` (unique) | 63 MB | 4,268,847 | **KEEP** |
| | `idx_raw_prices_recent` | 54 MB | 200,346 | **KEEP** |
| | `raw_prices_pkey` | 40 MB | **0** | **NOT A DROP CANDIDATE — constraint** |
| | `idx_raw_prices_symbol` | 30 MB | 682,902 | KEEP (prefix, but heavily chosen) |
| | `idx_raw_prices_date` | 20 MB | 6,479 | Marginal |
| `option_chain` | `option_chain_symbol_ts_key` (unique) | 66 MB | 9,676,092 | **KEEP** |
| | `option_chain_pkey` | 34 MB | **0** | **NOT A DROP CANDIDATE — constraint** |
| | `idx_option_chain_underlying` | 15 MB | 148,208 | KEEP |
| | `idx_option_chain_ts` | 9.6 MB | 48,657 | KEEP |
| `gvm_history` | `gvm_history_symbol_score_date_key` (unique) | 75 MB | 41,126 | **KEEP** |
| | `idx_gvm_history_symbol_date` | 67 MB | 722,475 | **REDUNDANT-PREFIX candidate** — same columns as the unique above |
| | `gvm_history_pkey` | 33 MB | **0** | **NOT A DROP CANDIDATE — constraint** |
| | `idx_gvm_history_date` | 11 MB | 821 | Marginal |

**Flagged explicitly, as the card requires: the four primary keys reporting `idx_scan = 0`
(`intraday_prices_pkey` 106 MB, `raw_prices_pkey` 40 MB, `option_chain_pkey` 34 MB,
`gvm_history_pkey` 33 MB — 213 MB together) are CONSTRAINTS. Zero scans means nothing queries by
surrogate id; it does not mean they are unused. They must not be dropped.**

The honest candidates are the redundant secondaries:

| Proposal | Reclaims | Confidence |
|---|---|---|
| `DROP INDEX idx_intraday_timeframe` | 33 MB + write tax | **High** — 23 scans ever |
| `DROP INDEX idx_gvm_history_symbol_date` | 67 MB | Medium — same columns as the unique key; verify with the unique dropped from consideration first |
| `DROP INDEX idx_intraday_symbol_ts` | 272 MB + write tax | Medium — a prefix of the unique, but the planner picks it 1.08 M times because it is narrower. Measure before dropping |
| `DROP INDEX idx_raw_prices_symbol_date` | 85 MB | Medium — same reasoning |

**ALL PROPOSALS. Nothing was dropped.** Each should be tested by first setting the index invisible
(or measuring the equivalent query with `enable_indexscan` tricks) rather than dropping blind — a
272 MB index takes a long time to rebuild if the drop turns out to be wrong.

`option_chain` deserves a note of its own: **10 MB of heap carrying 124 MB of indexes**, a 12× ratio.
Nothing there is unused, so this is not a drop list — it is a schema question for a later card.

---

## 7 · ITEM 3 — CONNECTION LIFECYCLE. **Pooling is now the dominant remaining cost.**

**MEASURED.**

```
modules opening a fresh psycopg connection : 117
connect() call sites                       : 158
connection pools in the codebase           : 0     (no ConnectionPool, no psycopg_pool, nothing)
max_connections                            : 100
```

From `pg_stat_database` (stats never reset):

```
sessions                  : 681,186
session_time              : 1,429,775,827 ms   (16.5 days of connection time)
active_time               :    53,647,447 ms   ( 3.75% — actually executing SQL)
idle_in_transaction_time  :   931,425,842 ms   (65.1% — connected, inside a transaction, doing nothing)
numbackends (now)         : 1
```

Two things fall out of this and both are new:

1. **681,186 connections opened, and `numbackends` is 1.** Essentially every one of those was a
   connect-run-one-query-disconnect cycle. That is the `_conn()` pattern, 158 times over.
2. **Only 3.75% of all connection time is spent executing SQL. 65% is idle *inside an open
   transaction*.** That is the `with conn: ... with conn.cursor():` idiom holding a transaction open
   across Python work and outbound HTTP. It is also why `idle_in_transaction_session_timeout` had to
   be set to 5 minutes — that setting is treating the symptom.

**Answering the card's question directly: yes, pooling is now the dominant remaining cost, not a
minor one.** cc#857 deferred it and said so; the numbers now say it should not stay deferred. The
argument is not "connections are slow" in the abstract — it is that finding 1 puts one full connect
handshake on the event loop before every page, and 158 call sites put another one inside every
handler that runs.

**Recommendation — LARGE, and deliberately NOT implemented here.** A single `psycopg_pool.
ConnectionPool` created at startup, with `_conn()` in each module rewritten to borrow from it. The
risk is that 117 modules currently assume `with _conn() as conn` closes the connection; with a pool
it returns it, and any code relying on close-as-rollback changes behaviour. That needs its own card
with its own verification, not a line in this one.

**Cheaper interim step, if the full pool is too big to schedule:** fix finding 1 alone. It removes
one connect from every page load for about fifteen lines of code, and it is on the only path that
every user takes on every request.

---

## 8 · `auth_gate` REWRITES THE WHOLE HTML BODY ON THE EVENT LOOP

**MEASURED (sizes) + STRUCTURAL (the path).**

After `await call_next(request)`, the same `async def` middleware buffers the *entire* response body
into a Python `bytes` and rewrites it:

```python
body = b""
async for chunk in response.body_iterator:
    body += chunk
```

then runs **4 sequential `body.replace(...)` passes** plus several `in` membership tests. Byte-string
replace copies the whole buffer each time.

```
v8_dashboard.html : 369 KB
scorr_cockpit.html:  56 KB
scorr_home.html   :  55 KB
```

Loading the V8 dashboard therefore copies roughly 369 KB × (1 buffer + 4 replaces) ≈ **1.8 MB of
byte-string churn on the event loop**, per load, before the response starts streaming. It also
defeats streaming entirely: the client receives nothing until the last chunk has arrived and been
rewritten.

This is also the code cc#821 had to fix once already, when an injection matched inside an HTML
comment.

**Proposed fix — MEDIUM.** Move the injected tags into the page templates so the middleware does not
have to rewrite anything, or at minimum do the rewrite in a single pass and only for the paths that
genuinely need it. The `+=` accumulation should become a `b"".join(chunks)` regardless — that one is
small and safe.

---

## 9 · ITEM 7 — PHONE-SIDE "STUCK" (P0-B)

### The service worker is not the cause

`pwa_endpoints.py:98` `SW_JS`, read in full:

- `SHELL = ['/', '/pwa.js', '/static/manifest.json', '/static/icon-192.png', '/static/icon-512.png']`
  — five entries, cache-first (line 148).
- **`/api/*` is never cached** — explicit early return (line 121).
- **Navigations are network-first** (line 123–124):
  `if (req.mode === 'navigate') { e.respondWith(fetch(req).catch(() => caches.match('/'))); return; }`
  That branch has **no `cache.put()`**, so a page response is never stored at all.
- `/pwa.js` is network-first *with* a cache refresh on `res.ok` — a non-ok response is never cached,
  so a bad deploy cannot be pinned.
- On a slow or failed fetch: navigation falls back to `caches.match('/')`, the app shell. Worst case
  offline you get Home, never yesterday's page.

**Verdict: the SW cannot serve a stale page, and it cannot hang.** It has no timeout of its own, but
it never blocks either — the `fetch()` it issues is the browser's, and a pending navigation shows
browser chrome, not a spinner. (This also independently re-confirms the cc#867 / cc#868 conclusion
for `/preview`.)

### The front-end fetches are the cause

**MEASURED count across the served front end:**

```
served .html/.js files containing fetch() : 32
total fetch() call sites                  : 134
files using AbortController               : 3
total AbortController instances           : 5
fetch() sites with NO timeout (upper bound): 129
```

Only `scorr_cockpit.html`, `pwa_endpoints.py` (the injected JS) and one other carry an
`AbortController` at all. The worst offenders by count:

```
 12  scorr_check.html
 11  scorr_health.html
 11  v8_dashboard.html
  8  scorr_filters.html
  8  scorr_v13.html
  7  quant_basket.html
  7  scorr_intraday.html
  6  scorr_v15.html
  6  screener.html
  5  scorr_home.html
  4  scorr_card_common.js      <- shared module, so this multiplies across every page
```

cc#858 established the principle: a hung request with no `AbortController` animates a shimmer
indefinitely and reads as "still loading" rather than "failed". It was applied to the R card and
nowhere else. **`v8_dashboard.html` has 11 untimed fetches and `scorr_card_common.js` — imported by
every page — has 4.**

Combine this with finding 1 and finding 2 and the reported symptom is fully explained: one blocked
event loop makes every in-flight request slow; none of those requests has a deadline; so the phone
shows spinners that never resolve and never error. **Slow and stuck are the same bug seen from two
ends.**

**Proposed fix — MEDIUM.** One shared `fetchWithTimeout(url, opts, ms)` helper in
`scorr_card_common.js` (which every page already loads), then replace the 134 call sites
mechanically. A 15 s default with a visible failed state is the whole fix. This is the single
highest-value front-end change in this document.

---

## PROPOSED MAINTENANCE — CONSOLE ONLY, WEEKEND, FOUNDER APPROVAL

Nothing below was run. MAINTENANCE_LOCK_RULE cc#351 applies to every line.

```sql
-- 1. cheapest, highest value: give the planner statistics for a 383 MB table
ANALYZE gvm_history;

-- 2. the index that would remove two seq scans from every GVM report load
CREATE INDEX CONCURRENTLY idx_gvm_history_segment_date ON gvm_history (segment, score_date DESC);

-- 3. dead-tuple reclaim. PLAIN VACUUM, never VACUUM FULL.
VACUUM (ANALYZE) gvm_history;
VACUUM (ANALYZE) raw_prices;
VACUUM (ANALYZE) intraday_prices;

-- 4. the one high-confidence index drop: 23 scans in the table's lifetime
DROP INDEX CONCURRENTLY idx_intraday_timeframe;

-- 5. worth having, but it is an extension install — its own decision
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

The three medium-confidence index drops (`idx_intraday_symbol_ts` 272 MB,
`idx_raw_prices_symbol_date` 85 MB, `idx_gvm_history_symbol_date` 67 MB) are **not** in the list
above on purpose. They should be proven redundant against real query plans first.

---

## FOLLOW-UP CARDS THIS AUDIT SHOULD PRODUCE

Each finding becomes its own card so it can be verified on its own. Nothing was fixed here.

| Card | Finding | Size | Priority |
|---|---|---|---|
| Cache the auth token check | 1 | Small | **P0** |
| `fetchWithTimeout` across all 134 call sites | 7 / P0-B | Medium | **P0** |
| Move `ensure_*` DDL to startup hooks — the 16 GET+ALTER first | 3 / P0-A | Medium | **P0** |
| Drop `async` on the 20 blocking handlers, starting with `/mcp` | 2 | Small | P1 |
| `gvm_history(segment, score_date)` index + `ANALYZE` | 4, 10 | Small | P1 |
| Single-pass, non-buffering `auth_gate` body injection | 8 | Medium | P1 |
| Connection pool | 9 | Large | P1 |
| Index drops, proven one at a time | 6 | Small each | P2 |
| Install `pg_stat_statements` | — | Small | P2 |

**Binding on cc#874:** findings 2 and 3 are the two checks every new `mobile_endpoints.py` handler
must pass — `def` not `async def`, and no DDL on the request path.
