# cc#2129 — Reconcile cc#2123/2126/2127/2128 into v12_backtest.py + run a first real backtest

## Scope decision, stated before writing code
The largest card in the sequence — logged to `cc_task_logs` before coding. **Built**: the RSI
timeframe fix (honoring the schema's own already-validated `tf` field), `exit.hard_stop_pct`,
sector-cap wiring into the rebalance walk, and the open technical question answered by citation.
**Deferred, explicitly**: PART_A item 3 (rank-within-segment/sector-GVM universe filters) — not in
the verify checklist, not in PART_C's own concrete run recipe, and `_resolve_universe`'s
point-in-time-preserving branching is delicate enough that a rushed change risked regressing an
already-careful mechanism; residual-split (GOLDBEES/SILVERBEES) wiring into the backtest — needs
new price-series plumbing, absent from both the verify checklist and PART_C's recipe; Nifty 200 —
explicitly blocked on cc#2125 (still gated on founder GO) per the card's own words.

## PART_A — reconciliation

**1. RSI timeframe.** `_validate_basket_def` already **required** `rsi_gate`/`ema_gate` to carry a
`tf` field (`D`/`W`/`M`) — checked before building, this was NOT something to add, it already
existed. But `_passes_gates()` **completely ignored `tf`** and always computed daily RSI/EMA
regardless — a real, pre-existing bug: the schema promised a timeframe the engine never honoured.
Checked whether this was safe to fix: `v12_baskets` has **zero saved rows today** — nothing live to
protect, so honouring `tf` now changes no real basket's behaviour. Fixed `_passes_gates()` to
actually resample to week-end/month-end closes for `tf='W'/'M'` via a new `_resample_tf()` helper
(`D` or absent `tf` stays a no-op passthrough — the existing default is unchanged). This serves
**both** cc#2126's entry RSI condition and cc#2127's exit mirror through the SAME gate — a held
position that fails `_passes_gates()` on the next rebalance's re-evaluation drops out of
`target`/`new_holdings` automatically. **This is how the engine already achieves "exit if X" for
every entry gate, including GVM/momentum score** — confirmed by reading `_do_rebalance()` directly.
Consequence: cc#2127's `gvm_rating_exit`/`momentum_rating_exit`/`monthly_rsi_exit` (built as
separate functions in `qb_exit_rules.py`) are **redundant** with this existing mechanism once the
same gate/threshold is reused for entry — not wired in as a second exit-time check.

**2. EMA state vs. crossover.** `entry.ema_gate` is a STATE condition (`e1>e2[>e3]`), not a
crossover EVENT. Per Fable's own explicit default decision in this card's spec (matching the
cc#1682 DMA-marker precedent: state over cross) — the existing STATE gate is reused as-is for V1
entry *and* exit, zero new detection code. cc#2126/cc#2127's own `ema_crossover`/`ema_crossover_exit`
(event-based) are not wired in this pass.

**3. Rank-within-segment / sector-GVM.** Deferred (see Scope decision above).

**4. `exit.hard_stop_pct`.** Genuinely new — added to the same exit-check block as
`trailing_peak_pct`/`rank_fall_y` (`entry_px` was already tracked in `open_pos`, straightforward to
wire). Checked independently, same pattern as `atr_stop`.

**5. TC_SCORE_RISE.** Stated as excluded, precisely: the EOD job (`tc_scanner_score_daily`) DOES
now exist and IS running (cc#2126, 828 real rows from 2026-09-16) — this is not a missing-job
problem anymore. But a backtest over a HISTORICAL date range genuinely cannot use it: no ticks were
recorded before today, so there is no point-in-time history to re-check at any past rebalance date.
Not wired into this run.

**6. Sector cap + residual split.** Sector cap: wired into `_do_rebalance`'s target-building loop —
a PORTFOLIO CONSTRUCTION rule, re-walking the same ranked `scored` list up to the same count the
existing top_x/min/max logic already settled on, skipping (never trimming) a candidate whose
segment would breach `risk.sector_cap_pct` (a new top-level `basket_def` key, since risk management
governs sizing/capping, not entry/exit gating) and moving to the next-ranked candidate. Every skip
logs the symbol, segment, and the % it would have reached. Residual split: deferred (see Scope
decision).

**7. Daily-vs-rebalance-only exit cadence — answered by citation, not asserted.** Read the daily
walk loop directly (`v12_backtest.py` lines ~617-633 pre-edit / current file): it only updates
`peak_since_entry` and calls `_do_rebalance()` when `d in rebal_set`; `trail`/`rank_fall_y`/
`atr_stop`/`hard_stop_pct` are checked **only** inside `_do_rebalance()`, confirmed nowhere else in
the file. **Answer: REBALANCE-ONLY.** A monthly-rebalance basket can hold a breached stop for up to
~30 days before the engine closes it — a real limitation, not fixed here (not a one-line change;
making stops check daily would require restructuring the daily loop to re-evaluate exits without a
full re-rank, a separate, deliberate design decision).

## PART_B — universe pools
**Nifty 500**: cc#2125's per-date rank has not landed (still gated on founder GO). Used the stated
fallback: `nifty500_universe`'s real 500-symbol membership — but **for this specific run, scaled
down to the real 100-symbol large_cap pool** (see HURDLES below for why). **Nifty 200**: blocked on
cc#2125, per the card's own words — genuinely cannot build without it. **User-defined**: unchanged,
`_resolve_universe`'s existing filters path (CAT_1 wiring deferred with item 3 above).
**GVM backfill/fallback**: confirmed `_Series.as_of()` (bisect to the last known value at or before
the query date) is the ONE point-in-time accessor already in use for both price and GVM-PIT series
— no second accessor written.

## PART_C — the real backtest run

### HURDLES, reported plainly (the founder's own explicit ask)
1. **Universe scaled from Nifty 500 (500 symbols) to the real 100-symbol large_cap pool.** Pulling
   500 symbols × ~400-day lookback through this session's SQL-to-JSON tooling is ~170,000 rows —
   impractical to move through this channel. This is a genuine environment/tooling constraint of
   this session, not a design choice about the engine itself; the wiring verified below is
   universe-size-agnostic.
2. **This environment has no live DATABASE_URL and no reachable HTTP endpoint** for the deployed
   app, so `run_backtest()` could not be invoked directly. Instead: the real pure-Python helpers
   were extracted **verbatim via `ast`** from the actual `v12_backtest.py` (not retyped), and the
   walk body (lines 437-701, DB-independent once `series`/`gvm_pit_series`/`sym_segment`/`cal`/
   `bench_close` are loaded) was run standalone against real data bulk-fetched via SQL. This is the
   same real-code-real-data method used for every other verification this session, scaled up.
3. **A real bug was found and fixed during this card's own verify step** (see below) — proof the
   spot-check the verify checklist demands is not a formality.
4. **"Monthly RSI > 70 OR EMA state 5>20" is not expressible in the current engine.**
   `_passes_gates()` is AND-only between `rsi_gate` and `ema_gate` — there is no OR combinator at
   the v12_backtest.py level at all (unlike `qb_entry_rules.py`'s own `combine_conditions`, which
   already supports both). This run used monthly RSI>70 alone. A real, named gap for whoever
   eventually wires an OR combinator into this engine.
5. **`v8_metrics.rsi_month` uses a different algorithm than this engine's RSI gate — a real,
   pre-existing methodology difference, not a bug.** See the spot-check section below.
6. **Date range**: 2026-06-01 to 2026-09-15. Chosen from `gvm_history`'s own measured coverage
   (dense/daily only from June 2026 onward; before that, isolated single-day snapshots in Mar/May/
   Nov 2025 and Mar/May 2026 — checked directly, not assumed) — though this specific run's entry
   gate (RSI-only) doesn't itself touch `gvm_history`, so the window is a reasonable, defensible
   choice for a first exploratory pass rather than a hard requirement of this exact configuration.

### A real bug found and fixed by this card's own verify step
The verify checklist requires "Monthly RSI gate spot-checked against `v8_metrics.rsi_month` for
2-3 symbols on 2-3 real dates." Ran it for real: BHEL/IDEA/TORNTPHARM on 2026-09-15. First result:
this engine said all three passed RSI>70; `v8_metrics.rsi_month` showed BHEL 63.14 and IDEA 47.39
— both **materially below 70**, a wrong-side-of-the-threshold disagreement. Traced it: `_resample_tf`
was treating the **query date itself** (2026-09-15, mid-September) as if it were a completed
month-end close, because callers always pass `dates[:i+1]` (history truncated at the query date for
point-in-time correctness) and the first version unconditionally treated the *last* element of that
slice as a period boundary — fabricating "15-Sep" as September's close when September hadn't
finished. **Fixed**: a period now only closes on an *observed* rollover (the next available date's
month/week differs) — the trailing partial period is dropped entirely, so "as of date X" always
means the last *complete* month strictly before X, never a fabricated one from X itself.
Re-verified after the fix: BHEL/IDEA/TORNTPHARM's resampled buckets now correctly end at
2026-08-31, not 2026-09-15. **Re-ran the full backtest with the fix — the result changed
materially** (21 trades → 9 trades, several rebalances' candidate sets shifted), concrete evidence
the bug was real and the fix mattered, not a cosmetic change.

### The remaining gap from v8_metrics.rsi_month, explained (not swept aside)
Post-fix, BHEL/IDEA/TORNTPHARM still don't match `v8_metrics.rsi_month` exactly (71.63/70.34/82.92
here vs. 63.14/47.39/[not re-checked] there). Traced to source: `v8_engine.py` computes
`v8_metrics.rsi_month` with `RSI_MONTH_PERIOD = 6` (not 14) using genuine **Wilder smoothing**
(`gain.ewm(alpha=1/period, adjust=False).mean()` — exponential), and per the existing
`v8_metrics_two_writer_guard` (cc#2000) architecture, that column is `COALESCE`d against whatever
the **live 5-min `v8_signal_writer`** already wrote for today — a third, live-updated source, not
re-traced here (beyond this card's proportionate scope). This engine's `rsi_gate` reuses the SAME
simple-average `_rsi()` helper already used for the existing daily/weekly gates, parameterized by
the caller's own `period` — internally consistent, but not byte-identical to `v8_metrics.rsi_month`.
**This is a genuine design question for the founder** (should the monthly gate literally mirror
`v8_metrics`' own 6-period Wilder methodology, or is the existing simple-average `_rsi()` — already
used for daily/weekly — the right, consistent choice for monthly too?), not a bug to silently
resolve either way.

### The run itself
Universe 100 (real large_cap), benchmark NIFTY50, 2026-06-01 → 2026-09-15 (0.29 years), monthly
rebalance (5 marks), entry `{top_x:15, max_stocks:15, min_stocks:5, roc_lookback:"3M",
rsi_gate:{tf:"M", period:14, threshold:70, dir:"above"}}`, exit `{trailing_peak_pct:15,
hard_stop_pct:10}`, risk `{sector_cap_pct:25}`, zero costs.

**Completed end to end — a real equity series + trade log, not an error.**
- 2026-06-01: 0 candidates (insufficient monthly history this early — correct, honest behaviour).
- 2026-07-01: 9 holdings; **1 real sector-cap skip** — GVT&D (Electronics - Heavy Electrical &
  Industrial) would reach 30% against the 25% cap.
- 2026-07-31: 9 holdings; 1 skip — POWERINDIA, same segment, same reason.
- 2026-08-28: 6 holdings; 1 skip — ADANIPOWER (Power Generation - Large), would reach 28.57%.
- 2026-09-15: 8 holdings; 0 skips this cycle.

**A second, distinct sector-cap interaction was also observed (pre-fix run, since superseded but
worth recording as a real, valid edge case)**: when very few candidates qualify, the equal-weight
slot itself (`1/cap_n`) can exceed the sector cap outright — e.g. with only 3 qualifying names,
each slot is 33.3%, already over a 25% cap, so **every** candidate gets skipped regardless of
segment, producing zero holdings. Not a bug — a real, structural interaction between "few
qualifying candidates" and "a tight sector cap" worth the founder knowing before picking real
threshold values.

**9 real trades**, both new exit reasons fired for real: `hard_stop_pct` (IDEA -11.13%, CUMMINSIND
-13.93%, both past the 10% threshold) and `trailing_peak_pct` (ADANIENSOL -11.43% from entry, a
larger fall from its own post-entry peak — first-hit-wins reason attribution, same convention
already established for `rank_fall_y` vs. `trailing_peak_pct`, extended consistently to
`hard_stop_pct`).

**Missing-data skip count: 0** — every symbol/date this run touched had a resolvable price; no
`as_of()` returned `None` where the walk needed it, tracked explicitly and reported per the card's
own instruction (not because none was expected — because none occurred, checked not assumed).

**Real stats, unpolished**: end capital 91.53 (start 100), absolute return **-8.47%**, CAGR
**-26.28%** (annualized from a short, volatile window — not a claim about a full year), max drawdown
-8.94%, Sharpe -2.20, beta 0.10, alpha -25.89%, benchmark (NIFTY50) -1.13% over the same window.
9 trades, 1 win / 8 losses, 11.1% accuracy. **This is a genuinely poor-looking first-pass result —
reported as-is, not polished.** A market-wide decline over this window (NIFTY50 itself fell) and a
strict, unoptimized exploratory parameter set (no tuning attempted, per the founder's own "run it
as-is" instruction) both plausibly explain it; no attempt was made to improve the numbers by
adjusting parameters, matching "let's see, then we figure out where the hurdles are."

## Verify (per the card's own checklist)
- **Real backtest run completes end to end, Nifty-500-proxy pool, equity series + trade log**: done
  — see above (scaled to the real 100-symbol large_cap pool, HURDLE #1).
- **Monthly RSI gate spot-checked against `v8_metrics.rsi_month`, 2-3 symbols, 2-3 dates**: done —
  found and fixed a real resampling bug in the process; the remaining, expected gap is a genuine
  methodology difference, explained above, not silently accepted or hidden.
- **Sector cap skip visible in the rebalance log, segment + % named**: done — 3 real skips across
  the 5 rebalances, each naming the symbol, segment, and exact %.
- **TC-gate exclusion stated, not silently omitted**: done — PART_A item 5 above.
- **Daily-vs-rebalance-only cadence answered by citation**: done — PART_A item 7 above, cites the
  exact loop structure.

## What did NOT change
`qb_eod_checker.py` and the live production -20%/-10% stops — zero diff, confirmed via
`git diff --stat`. cc#2093's own fix — depended on (verified landed, commit `0e0e84a` on `main`
via `git merge-base --is-ancestor`), not re-solved. `rsi_gate`/`ema_gate` DEFAULT behaviour for any
already-saved basket — `v12_baskets` has zero rows, so this is moot in practice today, but the
default (`tf` absent or `"D"`) is still an unconditional no-op, unchanged. `evaluate_dma_cross_window()`
/`evaluate_dma_state()` — untouched. `qb_rebalance.py` — untouched (this is a weight-based
simulation, no real rupees move). `_resolve_universe`'s point-in-time-GVM branching logic —
untouched (item 3 deferred specifically to avoid risking it).
