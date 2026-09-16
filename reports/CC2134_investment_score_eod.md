# cc#2134 — Investment Score (IC V2 /10) computed EOD for the whole universe, persisted, on Screeners

## Step 1 — timing (measured, not estimated)
120 real symbols through the production engine (`/api/investment-check-v2/batch`, the same
`compute()` loop the runner uses, on the production DB): **120/120 scored, 0 failed, ≤34 s
wall-clock** (an upper bound — it includes my tool latency). ≈ **0.28 s/symbol** → the 1,793-symbol
universe ≈ **8–9 minutes**. Fits the nightly window with room. Deviation stated: the spec asked for
the loop "in Python calling the function directly"; this sandbox has no `DATABASE_URL`, so the
measurement went through the production endpoint that runs exactly that loop. The benchmark
re-fetch the spec worries about is real (NIFTY50's 3-year bars are refetched per call) but at
0.28 s/symbol it does not need optimising — `invest_check_v2.py` is untouched, and a benchmark
cache stays a proposal, not a change.

## Step 2 — universe (registry-derived)
`SELECT symbol FROM gvm_scores WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)` →
**1,793 symbols** at score_date **2026-09-15** (1,770 of them also in `screener_raw`; screener_raw
itself holds 1,865). gvm_scores is the right set, not screener_raw: `compute()` raises "not in GVM
universe" for anything outside it, so it is exactly the set the engine can score — and it is what the
nightly GVM job has just rebuilt when this runs. Never a hardcoded list or count.

## Step 3 — table
`investment_check_v2_scores (symbol, score_date, score10, band, gvm, market_cap_cr,
computable_weight, earned_weight, weighted, weight_source, excluded_components jsonb,
components jsonb, price_date, created_at, PK (symbol, score_date))` + index on `score_date`.
Created via `run_sql` as a plain `CREATE TABLE IF NOT EXISTS` (no ALTER — MAINTENANCE_LOCK_RULE
does not apply). The same DDL lives in `investment_score_eod.SCHEMA_SQL` and the runner executes it
idempotently on every run.

**score_date = gvm_scores' latest score_date at run time**, the trading date the inputs describe,
not the wall-clock date of the 01:30 run. A weekend or holiday therefore never gets a row of its own
— the boundary rolls forward by construction (ENGINE_LIVENESS corollary).

## Step 4 — runner: `investment_score_eod.py` (new file, rule 5)
- `run(conn=None, limit=None)`: universe → `invest_check_v2.compute(cur, sym)` imported directly (no
  200 cap) → upsert one row per symbol. Per-symbol `commit`; any failure `rollback`s and is appended
  to `errors` (symbol + reason) — listed, never dropped, same as the batch endpoint. Returns
  `{score_date, universe, scored, failed, errors, duration_s}`.
- `GET /api/investment-score/status` — latest score_date, row + band counts, dates stored, whether
  the table is behind gvm_scores, and the last in-process run summary.
- `POST /api/admin/run_investment_score_eod` (admin token) — manual trigger in a background thread
  with a run lock (a full run outlives a request); poll the status endpoint.
- `main.py`: import + `include_router` only.
- Unit-tested with a fake DB and the **real RELIANCE payload** captured from the production engine
  (score10 1.96, AVOID, 9 components, earned 19.6/100, price_date 2026-09-16): every column maps
  correctly, one injected failure is rolled back and listed, two good rows committed.

## Step 5 — scheduling
Checked the registry rather than guessing: `bg_nse_eod_ingest` = weekdays 18:30 / 19:30 / 20:30 IST;
`bg_gvm` = 01:30 IST and it rebuilds gvm_scores/gvm_history first thing. So the job is **chained
LAST inside `scheduler._bg_gvm()`**, after `tc_scanner_score_daily` — the established convention for
"after the GVM rebuild" (screeners_eod, gvm_coverage_guard, tc_scanner_score_daily all sit there),
and it is after both inputs by construction. Explicit `scheduler_master.record_run(...)` because a
chained job never passes through `_spawn`; a scoring failure can never mark the GVM run bad.
`scheduler_master._CHAINED_JOBS` gains the entry (a chained job missing there is auto-retired by the
drift audit — the cc#1095 lesson). `scheduler_master` row **inserted**: `investment_score_eod`,
category `chained`, active, added 2026-09-16, notes state the seed.

## Step 6 — Screeners column
`screeners_endpoints.screener_detail`: one extra query per screen — `score10, band` from
`investment_check_v2_scores` at its `MAX(score_date)` for the screen's symbols; rows carry
`inv_score` / `inv_band` (null when no row), payload carries `inv_score_date`. Wrapped like the CMP
overlay: a failure logs and the page still renders. `scorr_screeners.html`: **INV SCORE · <date>**
column right after GVM (score10 2dp + band chip), sortable (desc first tap, `--` rows last, meta line
names it), `--` when there is no row — never derived on the page. Band chip colours are the
`/check` page's own IC V2 convention (`scorr_check.html` bandChip): STRONG_BUY green on grn-d,
ACCUMULATE blue, WATCH amber, AVOID muted. Mobile Screeners: not touched (out of scope unless
trivial — the mobile page has its own table module; noted, same as cc#2133).

## First-run evidence (ENGINE_LIVENESS)
Decision taken under the founder's "decide yourself" instruction and logged in the Fable Room first:
the first rows for score_date **2026-09-15** are **seeded now through the real batch endpoint**
(slim rows: score10, band, gvm, computable_weight, excluded keys; `components` NULL and
`weight_source` says "seed: /api/investment-check-v2/batch (slim row; components not stored)"), so
the column is live today. The 11 members of screen 13 (52-Week Breakout) were scored and written
first (11/11, e.g. SPECTRUM 9.11 STRONG_BUY, WABAG 7.02 ACCUMULATE); the remaining universe is being
written by the same path in 200-symbol batches and the final count is logged on the card. **Tonight's
01:30 chained run writes the first FULL rows (all columns) for 2026-09-16** and records its own
timing in `scheduler_master.last_duration_ms` — that is the real first run; the seed is real engine
output, not a substitute for it.

## Verify
`ast.parse` + `py_compile` clean on all five Python files; `node --check` clean on the page script;
zero new `var(--x, #literal)` fallbacks in any added line (the ratchet's regressions are the same
pre-existing files). Playwright on the real page with the **real screen-13 payload** (the endpoint's
own join, captured from the DB): 11 rows rendered (positive count), every INV SCORE cell equals the
real score10 + band, `--` on the no-row case (BIRLACABLE blanked in the harness copy for that check;
its real value is 7.47 ACCUMULATE), chips distinct per band, header carries the as-of date, sort
desc with nulls last, 375 px no page overflow. Screenshots looked at (VISUAL_VERIFY_GATE_V1).

## Not touched
`invest_check_v2.py` (compute, grading, weights, bands), `investment_check.py` (V1),
`ic_rule_weights`, any existing job's cadence, mobile Screeners.
