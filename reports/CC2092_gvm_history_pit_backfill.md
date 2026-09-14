# cc#2092 (P0) — GVM HISTORY BACKFILL: honest point-in-time G/V/M for the top-750

Built under explicit founder authorization (14-Sep ~19:44 IST, "fable not available, limit
finished and clear the queue push all to main, founder decision") — Fable is unavailable, the
founder is directing CC to build the remaining engine/backend queue directly for this reason.

Sequencing confirmed before starting: this card's own spec requires cc#2091 to run first (universe
cleaned). cc#2091 shipped and landed on `main` earlier this session (sha `1b3daf8`) — confirmed via
this session's own cc_task_logs before claiming this card.

## What this fixes

`gvm_history`'s old `method=backfill_step_partial` rows froze TODAY's G and V onto every past date
— proof already on record (cc#2090's diagnosis): TANLA read G 6.07 / V 7.50 identically on five
dates two years apart. cc#2091 deleted every one of those rows this session — gone, not archived —
so M (which WAS genuinely honest, price-derived) is gone too and had to be rebuilt here as well,
per the spec's own `AMENDMENT_14SEP_M_SCORE_MUST_ALSO_BE_REBUILT`.

## THE_ONE_HARD_RULE — followed literally

`gvm_history_pit_backfill.py` imports and calls `gvm_engine.api_g_score` / `api_v_score` /
`api_m_score` directly (`from gvm_engine import api_g_score, api_v_score, api_m_score`). This
module builds their INPUTS from real historical data and lets the engine's own `BLANK_SCORE=5.0`
rule fire on `None` — it never re-implements banding, peer-relative or blank logic. Grep-confirmed:
no second `BLANK_SCORE`, no second band table anywhere in this file.

## Where every G/V/M input actually comes from — stated per the card's own instruction

This is also the module's own docstring, verbatim, so the method travels with the code:

- **roce, opm** — read DIRECTLY from `fundamentals_history` (`ratios."ROCE %"`,
  `profit-loss."OPM %"`) for the historical period. The same published ratio, just for a past
  year. No construction.
- **sales_growth_5y/3y, profit_growth_5y/3y** — `gvm_nightly._load_merged_df` does **not** compute
  these itself; they flow through untouched from `screener_raw`, i.e. they are scraped directly
  from Screener.in's own undocumented calculation. Screener keeps no historical archive of its own
  past snapshots, so there is nothing in this codebase to "replicate" for a past date. Built
  instead as a standard CAGR from `fundamentals_history`'s raw Sales/Net Profit figures. Calibrated
  against real current data before writing any code: **sales-growth CAGR matched screener_raw's
  live `sales_growth_5y`/`sales_growth_3y` EXACTLY** on both symbols checked (RELIANCE 17.76%/6.4%,
  TCS 10.22%/5.8%, both to the reported decimal). Profit-growth CAGR did **not** match as closely
  (2-4 points off on the same two symbols) — Screener's own profit-growth method likely adjusts for
  exceptional items this module cannot see. Both are real, standard, honestly-computed CAGRs from
  trusted raw figures; profit growth specifically is flagged as an approximation of a proprietary
  third-party figure, not a bug to chase further inside an already-large card.
- **qoq_sales_growth, qoq_profit_growth, opm_expansion** — the one place `gvm_nightly` DOES compute
  its own formula (not a passthrough) — replicated exactly, applied to `fundamentals_history`'s
  quarterly rows instead of `screener_raw`'s current snapshot. `opm_expansion` keeps the live
  formula's exact `(latest_q - prior_year_q) * 100` shape, oddly large-magnitude as that is —
  matching the live construction outranks this module's opinion of it. Not a place to quietly "fix"
  a value while rebuilding history; flagged, not altered.
- **inst_holding_abs, inst_holding_change** — FIIs + DIIs from `fundamentals_history`'s quarterly
  shareholding section, same formula gvm_nightly already uses.
- **fixed_asset_growth** — not a codebase-derived field either; 3-year CAGR of Fixed Assets from
  the annual balance-sheet section (standard, stated construction).
- **interest_coverage** — not stored historically; computed as EBIT/Interest =
  `(Profit before tax + Interest) / Interest` from the annual profit-loss section — the standard
  textbook definition. BFSI segments SKIP (`gvm_engine.score_interest_coverage`'s own `is_bfsi`
  branch), matching live behaviour exactly.
- **dividend_yield** — not stored historically; derived as `(Dividend Payout % × EPS) / price × 100`
  from the annual profit-loss section and a point-in-time price — a stated approximation.
- **pe** — `price (point-in-time, raw_prices) / EPS` — the standard definition, using a real
  point-in-time price, so this one is not an approximation.
- **historical_pe** — Screener's own "10-year historical PE" cannot be reconstructed as Screener
  computes it (no per-day PE archive exists anywhere). Built as the plain average of this module's
  own computed `pe` across up to 10 STRICTLY PRIOR annual periods — no look-ahead, however many are
  actually available. A stated construction, not a reproduction of Screener's specific method.
- **segment_pe** — point-in-time peer MEDIAN of this module's own computed `pe`, same peer
  machinery as everything else — mirrors cc#506's live "segment_pe = live segment median pe"
  design, just historically.
- **potential_upside** — **always `None`**, per the spec's own explicit instruction. `fy27_growth`
  (input_raw) is a forward-looking analyst estimate with no historical record anywhere — using
  today's value for a past date WOULD BE the exact look-ahead bias this card exists to remove.
  `gvm_engine.score_potential_upside`'s own blank rule fires, honestly. Verified directly in a real
  test (see Verify) — never conditionally filled.
- **gvm_segment, is_bfsi** — today's segment (`input_raw` has no historical segment membership
  anywhere) — the spec's own stated, sanctioned simplification.
- **M (price, dma_50, dma_200, return_1y, return_3y, return_52w_vs_index)** — 100% from
  `raw_prices`, point-in-time, nothing scraped. `dma_50/200` are plain trailing simple moving
  averages of daily close (N most recent *trading* days as of the date, not calendar days).
  `return_1y/3y` are simple point-to-point returns (**not** CAGR) — calibrated directly against a
  real `momentum_scores` row (RELIANCE, 13-Sep-2026, `ret_1y=-9.44`): hand-computed -9.09% from
  real `raw_prices` closes confirms simple-return, not CAGR (CAGR would have been an order of
  magnitude smaller); the small residual gap is a date-anchor rounding difference (13-Sep score
  date vs the 11-Sep close used for the check), not a formula difference. `return_52w_vs_index` =
  stock's own `return_1y` minus NIFTY50's `return_1y` over the same window — confirmed the same
  way. M can only start where `raw_prices`' own dense daily coverage starts (varies by symbol,
  commonly ~2021 — some symbols carry only one snapshot row per year before that, a real,
  pre-existing characteristic of `raw_prices` found this session during cc#2088); a period whose M
  inputs are not yet available honestly gets `None`, neutral-scored by the engine's own blank rule
  — never padded backward, per the spec's own natural-start-dates rule.

## As-known-on-date

Prefer a `reported`-status `earnings_calendar` row for that symbol within `[-30d, +150d]` of the
annual `period_end` (a genuine announcement-date match); else `period_end + 60 days`, a stated
conservative lag. Checked `earnings_calendar` directly before designing this: it is a recent
lead/tracking feed (rows carry `status`/`verified`, oldest `loaded_at` in this session's check was
mid-2026), not a historical archive — so the great majority of annual periods will correctly fall
through to the `+60d` lag, by design, not by a missed join. Every written row records which basis
applied (`basis_used`, per symbol, in the run result).

## Peer averages — point-in-time, not hand-waved (the spec's own words)

Every peer_* field gvm_engine.py actually reads (checked by reading `api_g_score`/`api_v_score`/
`api_m_score` directly, not assumed from `gvm_nightly.PEER_PARAMS`, which includes one field
—`peer_inst_holding_abs`— the engine never reads) is computed as the point-in-time segment MEDIAN
of every OTHER top-750 symbol's own most-recently-known value **as of the same date**, via a bisect
lookup over each peer's own sorted history — the identical mechanism `_Series.as_of()` already uses
for prices elsewhere in this codebase (cc#2088, same session), just applied to fundamentals. A real
peer at a date before it has ANY known data contributes nothing (excluded from the median, not
zero) — verified directly (see Verify).

## Write safety — gvm_history has one unique key, not two

`(symbol, score_date)` is the ONLY unique constraint on `gvm_history` — it does **not** include
`method`. Checked directly (`pg_get_constraintdef`) before writing a single row. The live nightly
process (`method IS NULL`) already owns every date from its own start forward (2026-05-30 to
today, confirmed this session during cc#2091). Overwriting one of those rows would corrupt a
genuine, already-correct live score — so this module computes that boundary itself at runtime
(`MIN(score_date) WHERE method IS NULL`) and never writes a row on or after it, **and** uses
`ON CONFLICT (symbol, score_date) DO NOTHING` (not `recompute_gvm`'s `DO UPDATE`) as a second,
independent guard — a collision is skipped and counted, never silently overwritten. New rows carry
`method='backfill_pit_v2'`, never touching a `backfill_step_partial` row (already gone) or a
live/NULL row.

## Execution mechanism

This session has no direct database connection (no `DATABASE_URL` locally) and this is a genuine
bulk job — up to ~750 symbols × several annual periods each, each needing point-in-time peer
averaging across its own segment — not something to run row-by-row over an MCP SQL tool. Wired the
same way `v12_backtest.py`'s own boot self-test already works in this codebase: gated behind
`app_config['gvm_pit_backfill']='run'`, triggered on the next app startup, progress/result written
to `app_config['gvm_pit_backfill_result']` for polling. An established pattern already shipped in
this codebase, not a new mechanism invented for this card. Wired via `include_router()` in
`main.py` only (rule 4/5 — main.py stays wiring-only).

## Verify — pre-deploy (real code, real data; the live run follows once this deploys)

**Syntax**: `ast.parse` + `py_compile` clean on `gvm_history_pit_backfill.py` and `main.py`.

**Real-data + mechanics tests against the actual, just-written module** (imported directly, not a
re-typed copy):
- `_PxSeries.dma()` for 50 and 200 days matches a hand-computed mean EXACTLY, on a series built so
  the expected value is known by construction; correctly returns `None` (never a short window)
  when fewer than N points of history exist.
- `_PxSeries.ret()` correctly returns `None` when the lookback predates all available data (no
  wrong-window substitute), and matches a hand-computed point-to-point return exactly when the
  lookback does resolve.
- `_extract_gv_raw()` run against REAL RELIANCE `fundamentals_history` figures (profit-loss,
  balance-sheet, ratios — fetched from production Postgres this session): `sales_growth_5y`/`3y`
  match a hand-computed CAGR exactly; `roce`/`opm` match the raw published figures exactly (direct
  passthrough); `pe` matches `price/EPS` exactly using a real point-in-time price; `interest_coverage`
  matches `EBIT/Interest` exactly; **`potential_upside` confirmed always `None`**.
- **Look-ahead guard, checked directly, not assumed**: calling `_extract_gv_raw()` for an EARLY
  period (2021-03-31) with a fixture that deliberately has no ~2016 comparator returns `None` for
  `sales_growth_5y` — confirms the function never reaches forward or substitutes a later value when
  the correct historical comparator is unavailable.
- **Peer point-in-time check, checked directly**: a synthetic two-peer scenario confirms a query at
  a date between two peers' own report dates correctly uses each peer's OWN most-recent PRIOR value
  (not a later one) — the median differs from what it would be if later values leaked in (16.0 vs
  19.0 in the test), and both peers are honestly `None` before either has ever reported.
- **Full engine call**: `api_g_score`/`api_v_score`/`api_m_score` run cleanly end-to-end on an
  assembled input dict built from the real RELIANCE-derived values above plus a synthetic peer
  set (confirms wiring, not a claim about real peer numbers) — all three scores land in the valid
  0-10 range, breakdown keys match the engine's own parameter list exactly.

**Not yet run: the actual server-side backfill** — this is a genuine bulk data job that must run
where `DATABASE_URL` is live (the deployed app), not from this session. Sequence from here: push →
fast-forward `main` → verify landing → set `app_config['gvm_pit_backfill']='run'` → Railway's
~90s auto-deploy restarts the app → the startup trigger fires → poll
`app_config['gvm_pit_backfill_result']` until done → run the card's own verify queries against the
real written rows (`COUNT(DISTINCT g_score)`/`COUNT(DISTINCT v_score)` per symbol > 1, reproduce
one symbol by hand, report the blank-5 fallback rate). This report will be extended with those real
results once the run completes — not claimed here in advance.
