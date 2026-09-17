# cc#2148 -- QB Universe builder: night-window precompute (CAT_5 + CAT_6 + CAT_3 remainder)

Status at push: **BUILT AND REGISTERED, NOT LIVE.** The job has not run yet. Per
ENGINE_LIVENESS_RULE (session_log 13829) the card stays `in_progress` until first-run evidence
(row count, score_date, runtime) is logged. First run: **02:30 IST on 18-Sep-2026**, or earlier via
the admin endpoint after 15:30 IST (it refuses market hours).

## 1. What was built

| File | Change |
|---|---|
| `qb_universe_derived.py` (new, 26 KB) | The precompute: `run()` writes one EOD-stamped row per scored symbol to `qb_universe_derived`. Status endpoint `GET /api/qb/universe2/derived/status`. Admin `POST /api/admin/run_qb_universe_derived` (refuses market hours). Every column is defined ONCE in the module docstring. |
| `scheduler.py` | `_bg_qb_universe_derived()` + dispatch `if h == 2 and m == 30` (after GVM 01:30 and universe_technicals 02:05). Health row `qb_universe_derived`, alert `qb_universe_derived_error` on failure. One run per IST day (`_qbd_ran_today`). |
| `main.py` | import + `include_router` (wiring only). |
| `qb_universe_builder.py` | `LEFT JOIN qb_universe_derived qd ON qd.symbol = g.symbol AND qd.score_date = (SELECT MAX(score_date) ...)` + 19 columns. 12 new filter params. `3M` / `6M` price windows now real (`ret_3m` / `ret_6m`). `_FILTER_ORDER` 27 -> 39 keys. Coverage endpoint returns a `derived` block (`ready, date, floor, rows, quarterly_symbols, beta_rows, beta_days, universe, next_run_ist`). Reconstruct returns `not_yet` when a derived row is applied before the first run. |
| `scorr_qb_universe.html` | CAT_5 (6 rows), CAT_6 (5 rows), CAT_3 `Consecutive up months`, the 3M / 6M options. All gated on `coverage.derived.ready`: until the first run they stay COMING with `first run <date>`. `CATEGORIES_COMING` is now empty. |

Table (created 17-Sep, 0 rows): `qb_universe_derived (symbol, score_date, price_date, 18 value
columns, computed_at)`, PK `(symbol, score_date)`, index `idx_qb_universe_derived_date (score_date)`.

## 2. The job

- **When:** 02:30 IST nightly. Inside the 00:00-06:00 window (cc#2123 ruling_2_SERVER_LOAD clause d). Never on request, never in market hours.
- **Date stamp:** `score_date = MAX(score_date) FROM gvm_scores` (the same stamp the night chain just built). A weekend or holiday never gets a row of its own, so the boundary rolls forward by construction. `price_date` = last `raw_prices` session on or before `score_date`, stored on the row.
- **Idempotent:** a `score_date` that already has rows is skipped unless `force=True`.
- **Registry:** `scheduler_master.enumerate_scheduler_jobs()` walks `_scheduler_loop` by AST for every `_spawn(fn)` call, so `_bg_qb_universe_derived` is enumerated from the dispatch line, not a hardcoded list. The `scheduler_master` row is seeded by the startup audit on deploy; its presence is checked after landing and stated in the task result.
- **Runtime (dry-run pieces on production):** quarters SQL, drawdown SQL, month-end SQL and the as-of / beta subqueries each ran in well under a second on the sampled symbols. Full-run wall time will be stated with the first-run evidence.

## 3. What each column is (short form; the module docstring is the source)

| Column | Definition |
|---|---|
| `yoy_sales_growth` | latest quarter Sales vs the quarter one year earlier, in %. Consolidated preferred per (symbol, period_end), `Sales`/`Revenue` from the metrics jsonb. Same arithmetic as `results_endpoints._peer_comparison`. Base must be > 0. |
| `yoy_profit_growth` | same, on `Net Profit`. |
| `opm_latest_quarter` | `OPM %` of the latest quarter. Stored as text like `14%` in the jsonb, so `%` is stripped before the cast. Banks carry NULL (financing margin), not 0. |
| `opm_change_yoy` | OPM latest minus OPM same quarter last year, percentage points. |
| `consecutive_quarters_profit_growth` | counting back from the latest quarter, how many quarters in a row beat their own year-ago Net Profit. Stops at the first miss or gap. |
| `quarters_since_last_result` | whole quarters between `latest_quarter_end` and `score_date` (`days / 91.31`, floored). |
| `alpha_vs_nifty500_1y` / `_3y` | stock return over the window MINUS NIFTY500's return, percentage points. Difference, never a ratio (Basket_Protocol Annexure C change 4). |
| `alpha_vs_sector_1y` | stock 1y return MINUS the segment composite's 1y return. Composite = `v12_backtest._segment_composite_series` (fixed-weight mcap, price-return chained), reused not re-derived. |
| `beta_1y`, `beta_sessions`, `beta_asof` | latest `beta_daily` row (benchmark NIFTY50). Forward-accruing: 3 days of history today. |
| `max_drawdown_1y` | deepest peak-to-trough fall over the last 365 days: `MIN(close / running-max close - 1) * 100`. |
| `rs_percentile_1y` | `PERCENT_RANK` of the 1y return across the scored universe, 0..100. |
| `ret_3m` / `ret_6m` | close vs close as-of `price_date - 91 / 182` days, %. |
| `consecutive_up_months` | counting back from the last COMPLETED month, months in a row that closed above the prior month's close. The running month is never counted. |

## 4. Hand-checks on real rows (spec verify items 2 and 4)

Quarterly, from `fundamentals_history` rows directly (latest quarter 2026-06-30 vs 2025-06-30):

| Symbol | YoY sales | YoY net profit | OPM latest vs year-ago | Change |
|---|---|---|---|---|
| JSWSTEEL | +9.77% | +112.6% | 20 vs 17 | +3 pts |
| RELIANCE | +27.02% | -24.65% | 15 vs 18 | -3 pts |
| TCS | +13.93% | +4.69% | 26 vs 27 | -1 pt |

The module's `_QUARTERS_SQL` returned these same figures for the three symbols.

Alpha vs NIFTY500, 1 year. NIFTY500 1y return = -3.58% (22,532.00 on 2026-09-16 vs 23,369.30 on 2025-09-16).

| Symbol | alpha_vs_nifty500_1y (pts) | Sign check |
|---|---|---|
| BHEL | +80.42 | known 1y outperformer -> positive, correct |
| JSWSTEEL | +15.57 | positive |
| BEL | -0.68 | about flat |
| RELIANCE | -8.18 | negative |
| TCS | -26.84 | known 1y underperformer -> negative, correct |

Max drawdown 1y from `_DRAWDOWN_SQL` on production: RELIANCE -22.42%, TCS -40.37%, BHEL -20.11%.
Month-end closes (`_MONTH_ENDS_SQL`) for BHEL matched the raw_prices last-session-of-month rows.

Coverage ceilings, measured 17-Sep:

| Measure | Value |
|---|---|
| Symbols with quarterly rows in `fundamentals_history` | 739 (660 consolidated); latest period_end 2026-06-30 |
| Of those inside the 1,773 scored universe | 729 -> the CAT_5 header reads "729 of 1,773 symbols have quarterly history" |
| `beta_daily` | 3 distinct days, latest 2026-09-16, 1,795 rows that day, benchmark NIFTY50 -> badge "FORWARD-ACCRUING - 3 days of history" |

The spec's figures (738 symbols, 5 beta days) were counted on 16-Sep; the numbers above are today's measured values and the page reads them live from the coverage endpoint, never from a constant.

## 5. Read path: EXPLAIN (spec item 6, verify item 3)

`EXPLAIN (ANALYZE, BUFFERS)` on the preview SQL with the join in place, run on production 17-Sep:

- total 25.841 ms
- `Index Scan using qb_universe_derived_pkey on qb_universe_derived qd` (the `MAX(score_date)` subquery is an InitPlan over `idx_qb_universe_derived_date`)
- no function calls, no aggregation over raw_prices or fundamentals_history anywhere in the plan: reads only

Saved as `scratchpad/cc2148_explain.sql` for re-run after the first night.

## 6. Page (spec item 5, verify item 4)

Gating: every derived row and the 3M / 6M options check `coverage.derived.ready`.

- **Before the first run:** CAT_5 and CAT_6 headers show `COMING - first run 2026-09-18 02:30 IST`; their rows are disabled; the 3M / 6M `<option>`s are disabled with the same note; `Consecutive up months` is disabled. CAT_3 header counts `4 of 5`. Applying a derived row via a saved definition shows an amber `not yet` line from the reconstruct endpoint instead of a date.
- **After the first run:** CAT_5 header `729 of 1,773 symbols have quarterly history` + badges `POINT-IN-TIME (quarterly)` and `NIGHT PRECOMPUTE - FROM <floor date>`; CAT_6 badge `POINT-IN-TIME` and the beta row `FORWARD-ACCRUING - 3 days of history`; CAT_3 `5 of 5`; the live counter reads `N active of 39 live`.
- Integer columns (`consecutive_*`, `beta_sessions`, `quarters_since_last_result`) render without decimals.

Harness `scratchpad/cc2148_test.py` (Playwright, local server, coverage/preview stubbed both ways) prints
`=== ALL cc#2148 PAGE CHECKS PASS ===` for four states: not-ready dark 1280, ready dark 1280,
ready light 1280, ready dark 375. Screenshots looked at (VISUAL_VERIFY_GATE_V1):
`cc2148_notready_dark.png`, `cc2148_ready_dark.png`, `cc2148_ready_light.png`, `cc2148_cat5_375.png`
-- COMING headers carry the date, ready headers carry the counts, badges present, phone rows wrap
with no horizontal overflow. `node --check` on the page script: OK. `ast.parse` on all four Python
files: OK.

## 7. Unit tests (`scratchpad/cc2148_unit.py`, imports the module's four pure helpers, no DB)

`profit_growth_streak` (stops at miss, stops at gap, missing base), `quarters_since` (0 / 1 / 4),
`consecutive_up_months` (running month excluded, gap breaks the streak), `percent_ranks`
(PERCENT_RANK semantics, ties share the lower rank). 15 assertions, all pass.

## 8. Spec cross-check

| Spec item | Done |
|---|---|
| 1. One night-window precompute, EOD-stamped rows, new table, never on request | yes -- section 2 |
| 2. CAT_5 six columns on the `_peer_comparison` basis, header ceiling, POINT-IN-TIME badge | yes -- sections 3, 4, 6 |
| 3. CAT_6 five columns, difference alphas, composite reused, beta forward-accruing badge | yes -- sections 3, 4, 6 |
| 4. CAT_3 remainder: 3M / 6M + consecutive up months | yes -- the cc#2147 disabled options now light up once rows exist |
| 5. COMING until the first run, then live | yes -- section 6 |
| 6. Plain JOIN, EXPLAIN reads only | yes -- section 5 |
| verify: scheduler_master row + first-run evidence | **PENDING** -- first run 02:30 IST 18-Sep or admin run after 15:30 IST; row count, score_date and runtime go in the task result then |

## 9. Live checks after deploy

- `https://scorr.in/api/qb/universe2/derived/status` -> `{ready: false, rows: 0, next_run_ist: "2026-09-18 02:30 IST", ...}` until the first run
- `https://scorr.in/api/qb/universe2/coverage` -> `derived.ready = false`, `derived.quarterly_symbols` and `derived.beta_days` populated after the run
- `https://scorr.in/qb/universe2` -> CAT_5 / CAT_6 headers read `COMING - first run 2026-09-18 02:30 IST`
- After the first run: `SELECT score_date, COUNT(*), COUNT(latest_quarter_end), COUNT(beta_1y) FROM qb_universe_derived GROUP BY 1`

## 10. Not done, on purpose

- Widening the fundamentals scrape past the 739 symbols (spec: own card).
- Nothing under `worker/**` was touched.
