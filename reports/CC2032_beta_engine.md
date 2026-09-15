# cc#2032 (P1) — DATA+ENGINE: nightly per-stock Beta + Quant Basket-level Beta rollup

Built under explicit founder authorization (Fable unavailable, session-standing "clear the queue
push all to main, founder decision"). Founder request 13-Sep-2026: no beta figure existed anywhere
in the schema (confirmed before building: zero columns named `*beta*` across the whole DB).

## Definition shipped (founder-proposed, stated as a proposal — the floor never moves, the window
can be retuned)

- **benchmark**: NIFTY50 (`raw_prices`) — the same spot series `option_ivp.py`/`deriv_metrics.py`
  already read for NIFTY. Reused, not a second index source.
- **window**: trailing 252 trading sessions (~1yr). "Session" = the benchmark's own `raw_prices`
  trading dates — the identical convention `qb_nav.py`'s `compute_series()` already uses ("the
  trading calendar is the benchmark's own bar dates"), not a calendar-day window.
- **min_sessions**: 60 — mirrors the `MIN_SESSIONS` floor already standing in `option_ivp.py`
  (G1/G2): never fabricate a stat off too little history. Below this: no row in `beta_daily` for
  that symbol that date, not a zeroed or guessed value.
- **stock_beta**: `Cov(r_stock, r_bench) / Var(r_bench)`, `r` = daily log returns over the aligned
  window.
- **basket_beta**: holdings-weighted average of each CURRENT holding's own stock beta. Weight =
  that holding's `quant_paper_positions.current_value` as a fraction of the basket's total open
  non-cash-park value — the **same** weight `qb_app_mobile.py`'s own `weight_pct` already computes
  at read time (`current_value / sum(current_value)`), confirmed by reading that file before
  building, not a second weighting scheme. `NIFTYBEES`/`LIQUIDBEES` (the established cash-parking
  sleeve, `qb_nav.py`'s own `CASH_PARK_SYMBOLS`) are excluded from the rollup — not real equity
  exposure, same reason `qb_nav.py` marks them at cost. A holding with no computable beta (new
  listing, <60 sessions) is excluded from **both** the numerator and denominator of the weighted
  average — restricting the sum to the subset that has a beta, using each one's own already-
  normalised weight, **is** "redistribute its weight proportionally across the rest" (proven below
  with real holdings): never a fabricated beta=1 or beta=0.

## Storage — two NEW tables, no ALTER TABLE

The spec offered a choice for the basket-level number: a new `qb_nav_daily` column, or a new small
table. **CC's call, stated here**: a new table (`qb_beta_daily`), specifically to avoid an ALTER
TABLE on `qb_nav_daily` — a live table other nightly jobs write every session, and MAINTENANCE_LOCK_RULE
(cc#351) gates ALTER TABLE as Railway-console-only/weekends/propose-first (the 10-Jul incident this
rule exists for was exactly this kind of lock contention on a busy table). A CREATE TABLE is not
gated. Per-stock beta is similarly wired into quant-basket-surface responses **at read time** (a
LEFT JOIN on `(symbol, latest d)`), never as a new `quant_paper_positions` column, for the identical
reason.

```
beta_daily(symbol, d, beta, n_sessions, benchmark_sym, computed_at)              PK(symbol, d)
qb_beta_daily(basket_name, nav_date, beta, n_holdings_used, n_holdings_excluded, computed_at)
                                                                                   PK(basket_name, nav_date)
```

## What changed

- **`beta_engine.py`** (new file): `compute_all_stock_betas()` — one bulk query for the whole
  ~1850-symbol universe's closes (not 1850 round trips, same "one read for the whole set"
  discipline `qb_nav.py`'s `_closes()` already established), aligns each symbol against NIFTY50's
  own calendar via `_aligned_log_returns()` (inner join on date, never forward-filled — a filled
  day would read as a fake zero return and bias beta toward 0), computes beta via `Cov/Var`, upserts
  `beta_daily`. `compute_all_basket_betas()` — per ACTIVE basket (`quant_basket_registry.is_active`,
  registry-derived, never a hardcoded list), the holdings-weighted rollup, upserts `qb_beta_daily`.
  `run_beta_engine()` orchestrates both. Gated `app_config['beta_engine_run']` startup trigger +
  `/api/admin/beta/run` + `/api/admin/beta/status` — same pattern as every other engine trigger this
  session (`option_iv_history.py`, `gvm_history_pit_backfill.py`).
- **`scheduler.py`**: `_bg_beta_engine()` (single-flight + trading-day guarded, same shape as
  `_bg_qb_eod`), scheduled **01:20 IST** — after raw_prices' EOD close lands (01:00,
  `_bg_yahoo_daily_sync`) and after `quant_paper_positions.current_value` is marked fresh by
  `_bg_qb_eod` (01:15), before the 01:30 GVM recompute — the same "prices→QB→GVM" dependency order
  this file's own 01:00–01:45 batch comment already documents. **Registers in `scheduler_master`
  via the existing AST-derived code enumeration + drift audit** (ENGINE_LIVENESS_RULE 13829:
  registry-derived, never a hand-typed INSERT that could drift from the code) — no manual row
  written by this card; the audit auto-inserts it the moment this deploy boots (also runs on every
  app startup, not just 08:45 IST).
- **`main.py`**: `include_router(beta_engine_router)` — wiring only.
- **`qb_endpoints.py`**: `/api/qb/positions` gains `beta` per row (read-only `DISTINCT ON (symbol)`
  latest-row join, same idiom this file already uses elsewhere for `DISTINCT ON (basket_name)`) —
  `None`, never fabricated, when the engine hasn't scored that symbol. `/api/qb/summary` gains
  `basket_beta`/`basket_beta_n_used`/`basket_beta_n_excluded`.
- **`qb_app_mobile.py`** (app surface — CC_DEFAULT_BUILD_RULE_V1, rule 14: no pusher named on this
  card, CC builds both surfaces by default): both `mobile_qb_detail`'s holdings rows and
  `mobile_qb_holdings`'s rows gain `beta` (these reuse `qb_endpoints.qb_positions()` directly, so
  the DB-side join above already computed it — this is just not dropping the field in the app's own
  explicit row reconstruction); `mobile_qb_detail`'s response gains `basket_beta`.

**A genuine ordering/connection wrinkle found while wiring the Fyers stock path** — not applicable
here; noted in cc#2031's report, not this one. No comparable wrinkle found in this card's wiring.

## What did NOT change

`raw_prices`, `quant_basket`, `qb_nav_daily` — no existing columns touched, additive only.
`quant_paper_positions` — read-only. `technical_rating`/`gvm_score`/`sector_rating` and every other
existing Key Metrics field — beta is an addition, nothing replaced. `quant_basket_config`
rebalance/weighting logic — basket beta is a read/report computed FROM current weights; it does not
feed rebalancing (out of scope per the card).

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `beta_engine.py`, `scheduler.py`, `main.py`,
`qb_endpoints.py`, `qb_app_mobile.py`.

**Method**: every test ran the ACTUAL edited `beta_engine.py` module (imported directly, not
re-typed) against REAL `raw_prices` (NIFTY50, 260 real sessions; 83 real symbols currently held
across all 13 active baskets, 21,517 real close rows) and REAL `quant_paper_positions` (134 real
open positions, real `current_value`) fetched this session, through a stub cursor serving the exact
SQL shapes the module issues and holding genuine state (a symbol's `beta_daily` row, written by the
stock-beta pass, is read back by the basket-rollup pass in the same run — a true end-to-end test of
the two functions interacting, not two isolated unit tests).

- **Algebraic correctness**: `Cov(r_stock,r_bench)/Var(r_bench)` vs an independent OLS regression
  (`numpy.polyfit(bench_ret, stock_ret, 1)`) on 5 real symbols (ABCAPITAL, ABSLAMC, ACUTAAS,
  ADANIENSOL, ADANIPORTS) — **diffs of 2e-16 to 1.8e-15 (float noise), exact match** on every one.
- **Sanity**: NIFTY50 regressed against itself — beta = **exactly 1.0** (251 sessions), the one
  value this formula must produce by construction.
- **Real universe run**: 83 symbols, all with enough history to score (0 excluded in this snapshot
  — the thinnest real holding, AEQUS, listed 2025-12-10, still clears 191 sessions in the window,
  above the 60 floor); 47 with a complete ~252-session window, 36 partial (newer listings).
- **The card's own explicit basket-rollup verify, done with real data**: manually recomputed
  `large_cap`'s beta from `beta_daily` + its real 15 holdings' real `current_value` — **1.0239**,
  used=15, excluded=0 — matches the engine's own stored output **exactly**, digit for digit.
- **Exclusion + proportional redistribution, proven, not just coded**: no *real* currently-held
  symbol falls under 60 sessions (checked directly — every basket's `n_holdings_excluded` was 0 in
  the real run), so the mechanism itself was proven by grafting one synthetic thin-history symbol
  (35 sessions, clearly a test artifact, never presented as real) onto `large_cap`'s real 15
  holdings: `large_cap`'s beta is **identical** (1.0239) with or without it — exactly what
  "proportional redistribution among the same used holdings" predicts mathematically (excluding a
  holding from both the numerator and denominator of a weighted average leaves the ratio among the
  remaining holdings unchanged) — and a manual recompute confirms it exactly. `n_holdings_excluded`
  correctly reads 1, `n_holdings_used` correctly stays 15 — never a fabricated beta=0 or beta=1 for
  the excluded name.

**First-run evidence** (ENGINE_LIVENESS_RULE 13829 — a badge follows the data, never precedes it):
triggered via the gated `app_config['beta_engine_run']` mechanism immediately after this deploys;
real row counts and coverage stats posted as a follow-up to this report and to `cc_task_logs`, not
asserted here in advance.
