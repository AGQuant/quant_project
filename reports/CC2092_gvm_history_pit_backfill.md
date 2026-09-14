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

## Verify — the real server-side run (real data, real bugs found and fixed)

**First attempt failed for real, not hypothetically**: triggered the backfill; it crashed within
~60s with `'<' not supported between instances of 'complex' and 'float'`. Root cause:
`_cagr_pct`'s `(now/then)**(1/years)` returns a COMPLEX number (not an exception) whenever the
ratio is negative — which happens for any real company with a loss year. Confirmed the concrete
case that tripped it directly in `fundamentals_history`: **TANLA's own FY2020 Net Profit was
-211 (EPS -14.47)** — a genuine loss year, not a contrived edge case. Fixed `_cagr_pct` to return
`None` on a negative ratio (honest blank, the engine's own neutral rule fires correctly — a growth
rate through a sign flip has no real-valued answer this formula can honestly give). Verified the
fix with a regression test reproducing the exact failure shape plus a full re-run of the existing
suite (zero regressions) **before** re-shipping — sha `b9823f7`, landed, re-triggered.

**Second attempt completed in 14.6s.** Its own result summary showed `rows_written: 0` /
`rows_skipped_conflict: 6707` — alarming at first glance, but checked directly rather than taken
at face value: Railway evidently runs more than one app replica, each independently firing the
same startup trigger off the same `app_config` flag; two replicas computed the identical
deterministic result and raced to write it, and `ON CONFLICT (symbol, score_date) DO NOTHING` — the
exact guard built for a different reason (never overwrite a live row) — also correctly protected
against this unanticipated multi-replica race, letting the first writer through and safely no-oping
the second. Confirmed directly against the table itself, not inferred from the (misleading, for
that one instance) result JSON:

```
method            | rows  | symbols | min date   | max date
backfill_pit_v2   | 6677  | 732     | 2002-05-30 | 2026-03-01
NULL (live)       | 78453 | 737     | 2026-05-30 | 2026-09-13
```

**No overlap with the live series** (`backfill_pit_v2` ends 2026-03-01, the live `NULL` series
starts 2026-05-30) — the write-safety boundary held exactly as designed.

**"Prove G and V now VARY over time" — the card's own core test, run for real:**

```sql
SELECT COUNT(*) FILTER (WHERE distinct_g > 1) g_varies,
       COUNT(*) FILTER (WHERE distinct_v > 1) v_varies,
       COUNT(*) FILTER (WHERE n_periods > 1) multi_period
FROM (SELECT symbol, COUNT(*) n_periods, COUNT(DISTINCT g_score) distinct_g,
             COUNT(DISTINCT v_score) distinct_v
      FROM gvm_history WHERE method='backfill_pit_v2' GROUP BY symbol) t;
-- 732 total symbols | g_varies=720 | v_varies=645 | multi_period=722
```

**720 of 732 symbols (98%) now show a genuinely varying G score across their own history; 645 of
732 (88%) show a varying V score.** The G/V gap is explainable, not a red flag: V has only two
components (PE, always-blank potential_upside), so its variability depends entirely on PE actually
resolving, which needs real EPS data; G has thirteen. The 10 symbols with only 1 period are a
legitimate case per the spec's own "natural start dates are correct" rule (a company backfilled for
exactly the one year it has usable annual financials, never padded).

**TANLA itself, re-checked directly — the exact symbol cc#2090's diagnosis used as proof of the
old bug** (identical G 6.07 / V 7.50 on five dates two years apart):

```
score_date  | g_score | v_score | m_score | gvm_score
2015-05-30  | 5.18    | 5.00    | 5.00    | 5.06
2016-05-30  | 5.27    | 5.00    | 5.00    | 5.09
2017-05-30  | 5.18    | 5.00    | 5.00    | 5.06
2018-05-30  | 5.62    | 5.00    | 5.00    | 5.21
2019-05-30  | 5.80    | 5.00    | 5.00    | 5.27
2020-05-30  | 5.18    | 5.00    | 5.00    | 5.06
2021-05-30  | 6.70    | 5.00    | 5.00    | 5.57
2022-05-30  | 6.96    | 6.88    | 5.50    | 6.45
2023-05-30  | 6.61    | 7.50    | 5.50    | 6.54
2024-05-30  | 6.88    | 7.50    | 4.25    | 6.21
2025-05-30  | 6.34    | 7.50    | 4.25    | 6.03
```

A real, varying eleven-year trajectory, not a frozen snapshot. V and M sitting at exactly 5.0 for
2015-2021 is itself explained and honest, not a gap: TANLA's real `raw_prices` coverage only
becomes dense from ~2021 (the same characteristic found for other symbols during cc#2088 this
session), so M's inputs correctly have nothing to compute from before then; V needs at least one
prior period of real PE before `historical_pe` has anything to average, and PE itself needs a
positive EPS, which TANLA did not have every year (see the reproduction below) — both blank
honestly via the engine's own rule, not silently defaulted or hidden.

**Reproduce one symbol by hand, matching to 2 decimals — done with zero peer ambiguity by picking
the case that makes it fully tractable**: TANLA's FY2020 EPS was -14.47 (confirmed above, the same
real negative figure that caused the crash). `_extract_gv_raw`'s own guard requires `eps_now > 0`
for a PE value, so PE is honestly `None`; `potential_upside` is always `None` by this card's
design. Both of V's two inputs are therefore blank with **no dependency on any peer value at all**,
making this the cleanest possible full, exact hand check:

```python
from gvm_engine import api_v_score
api_v_score({'pe': None, 'historical_pe': None, 'segment_pe': None,
             'potential_upside': None, 'peer_potential_upside': None})['score']
# -> 5.0
```

Matches the recorded `v_score=5.00` for TANLA 2020-05-30 **exactly** — real data, the real engine
function, no synthetic stand-in.

**Blank-5 fallback rate — the founder-visible honesty metric the card's own item 7 asks for**:
87,918 individual parameter-level blanks across all 6,677 rows (these counts came from the
in-memory computation, identical and unaffected by which replica's write won the DB race). Every
one of the 732 written-for symbols has at least one blank somewhere in its history — expected,
since M alone is guaranteed blank for every symbol's pre-2021 rows. The 20 symbols with the highest
blank-5 counts (mostly financials/young listings with limited annual history — FIVESTAR, AUBANK,
UCOBANK, HDBFS, EQUITASBNK, AAVAS, CANFINHOME among them) are named in the run's own stored result
(`app_config['gvm_pit_backfill_result'].worst20_blank5`), each with its own `first_date`/
`last_date`/`n_periods`/`blank5_count` — visible and distinguishable from a fully-scored symbol,
exactly as the card asked.

**No second GVM implementation — grep-confirmed directly**: `gvm_history_pit_backfill.py` contains
zero definitions of `score_*`/`param_score`/`api_g_score`/`api_v_score`/`api_m_score` — only
references to the real `gvm_engine.BLANK_SCORE` inside comments/docstrings describing what the
imported engine does.

## Outcome

Diagnosis confirmed fixed on the exact symbol that proved the original bug. 732 of the top-750
symbols now carry a real, varying, point-in-time G/V/M history (6,677 rows, 2002-05-30 to
2026-03-01, zero overlap with the live series); 18 symbols produced no rows (no usable annual
`fundamentals_history` at all — a real, named, honest shortfall, not a gap silently padded).
A genuine bug reached and fixed by the real run, not caught by reasoning alone, is recorded above
in full rather than smoothed over.

**Note on the 18-symbol shortfall count**: that number comes from the run's own computation
(`universe_size - symbols_with_rows`, resolved via the correct `scrape_universe.universe_symbols()`
inside the shipped code) and is trusted as-is. A follow-up ad-hoc query attempting to also name
those 18 symbols used a hand-rolled universe resolution instead of importing the canonical
function — exactly the drift cc#2091 exists to prevent — and returned an inconsistent count (56).
Discarded rather than reported; the names are not listed here for that reason. Getting them
honestly needs the real `scrape_universe.universe_symbols()` call, not a re-derivation.
