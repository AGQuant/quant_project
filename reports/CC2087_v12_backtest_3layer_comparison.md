# cc#2087 (normal) — V12 BACKTEST: 3-layer performance comparison + benchmark widening

Built under session-standing founder authorization (Fable unavailable, "clear the queue push all
to main, founder decision"). Discovery-first per the card's own scope item 1.

## Discovery: what V12 backtest already reports (scope item 1)

Read `v12_backtest.py`'s `run_backtest()`/`_stats_pack()` directly rather than assuming. It
**already computes two of the three layers**, just not labelled as "layers":
- **Layer 1 (standalone)** already exists: `stats.absolute_return_pct` / `stats.cagr_pct` — what
  the basket made on its own terms. Nothing new needed here beyond surfacing it as "Layer 1" in
  the UI.
- **Layer 3 (index)** already exists too: `stats.benchmark_return_pct` / `benchmark_cagr_pct` /
  `alpha_pct`, against a SINGLE, selectable benchmark (`_BENCH` dict, resolved to a `raw_prices`
  symbol). The web wizard (`scorr_v12.html` Step 4) already has a benchmark `<select>` — so "make
  it selectable" was **already built**; this card widens which names are real choices (see below).
- **Layer 2 (sector) did not exist at all** — this is the card's one genuinely new piece.

## A real data-source correction, found before building (matches the card's own "report gaps,
don't silently drop" instruction)

`_BENCH` already listed 4 options: NIFTY50, NIFTY100, NIFTY200, NIFTY500. Checked `raw_prices`
directly rather than trusting the dict: **`NIFTY100` and `NIFTY200` have ZERO rows** — a
pre-existing bug, not introduced by this card. `run_backtest` already fails safely if one is
picked (`"insufficient benchmark history"`, not a fabricated series — the existing code was
already honest), but the web dropdown offered two options that always error. Checked what real
alternatives exist:

| Requested (card scope item 3) | `raw_prices` rows | Verdict |
|---|---|---|
| NIFTY 50 | 1314 (2021-05-24 → today) | already had |
| NIFTY 500 | 1252 (2021-08-11 → today) | already had |
| NIFTY 100 | **0** | dead, removed from the UI dropdown |
| NIFTY 200 | **0** | dead, removed from the UI dropdown |
| Bank Nifty | 1313 (2021-05-24 → today) | **added** — real, substantial history |
| Nifty Midcap 150 | 0 (exact name) | **not added** — see note below |
| Nifty Smallcap 250 | 0, and no symbol matches `%SMALL%` at all | **not added, genuine gap** |
| Sector indices (e.g. Nifty IT/Pharma) | not checked individually — none of the obvious names appeared in a broad `%NIFTY%` scan | **not added, flagged** |

**Midcap note**: `MIDCAPNIFTY` (1287 rows) exists, but it is a **different, narrower index** —
Nifty Midcap SELECT, the ~25-30 stock F&O index — not "Nifty Midcap 150" (a 150-stock broad
index). Not substituted silently; reported as a gap, per the card's own instruction.

## TRI (Total Return Index) — scope item 4

No TRI data exists for any index in this platform (checked `information_schema` for any
tri/total_return table or column — the handful of hits were false-positive substring matches on
"me**tri**cs", not real TRI data). Price-return is the only option for every benchmark, index or
sector, stated once here rather than repeated per-index since the answer is the same everywhere.

## Design decision — Layer 2 (sector) methodology, stated precisely

No historical daily market-cap series exists in this platform (only today's `gvm_scores.market_cap`
snapshot) — so a true daily-rebalanced cap-weighted sector index cannot be built from what's here.
Built instead: a **fixed-weight composite** — each segment member's weight is its mcap SHARE
within the touched segments TODAY, applied to that member's own day-over-day `raw_prices` return
and compounded over the backtest period. This is the standard construction a custom index uses
without full historical constituent-weight history (equivalent to a cap-weighted index at
inception, drifting like a real one without needing a second historical mcap series) — stated
here and in the code, never presented as a true daily-rebalanced index.

**Which segments**: every segment the basket held at ANY rebalance during the run (scanned from
the full `rebalance_log`, not the truncated 60-row payload), not just its final holding — a basket
that rotated through several sectors is compared against all of them.

**A verification finding that corrected the code's own comment, not just tested it.** The new
function reuses `_Series.as_of()` — the SAME point-in-time lookup the basket's own equity walk
already uses — which is a bisect-to-latest-price-AT-OR-BEFORE lookup, not an exact-date match. A
member missing only THAT day's row forward-fills to its last known price (0% contribution that
day, still counted in the weight base) — it is **never** excluded for a same-day gap. A member is
excluded (and the rest renormalised) only when it has genuinely no price at all at-or-before that
point. The first draft's inline comment said "renormalise over members actually priced that day",
which is imprecise about which case triggers exclusion — corrected in the code once the real
behaviour was proven (see Verify).

## What changed

- **`v12_backtest.py`**: `_BENCH` gains `BANKNIFTY`; new `_segment_composite_series(cur, segments,
  cal)` (fixed-weight sector composite, described above); `run_backtest()` now also collects every
  segment the basket held across the full run, computes the sector composite over the same
  benchmark calendar, and returns a new `sector_stats` key (`segments`, `n_members`,
  `sector_return_pct`, `sector_cagr_pct`, `vs_sector_alpha_pct`, `methodology`) alongside the
  existing `stats`/`benchmark_series`. `/api/v12/backtest`'s response is a straight DB
  pass-through of the whole result dict (confirmed by reading `v12_backtest_get` directly) — no
  endpoint change needed for the new field to reach the client.
- **`scorr_v12.html`**: Step 4 benchmark `<select>` narrowed to the 3 real options (NIFTY50,
  NIFTY500, BANKNIFTY) instead of 4 options half of which always errored; `renderResults()` gains
  two new stat tiles (`vs Sector`, `Sector Alpha`) beside the existing `vs <benchmark>`/`Alpha`
  pair, plus a labelled note line naming the segments compared, member count, and the composite's
  methodology (never buried — the card's own instruction was "clearly... not just one").

## What did NOT change

`_stats_pack()`, `_uni_where`/`_UNI_COLS`/`_UNI_BASE` (cc#2086, untouched), the walk/rebalance
logic, `atr_stop` (cc#2088, untouched), `_load_series`/`_Series` (reused, not modified),
`v12_universe_*` endpoints, `applyPreset()`. `global_indices.py` — read (via `information_schema`
checks) to confirm it is NOT the source for domestic Nifty variants (it holds international/VIX
series, a different concern), never touched.

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `v12_backtest.py`; `node --check` clean on
`scorr_v12.html`'s inline script.

**The new function, against real production data — not simulated.** Imported the actual edited
`v12_backtest` module directly. Fetched the real `Telecom Equipment & Services` segment (5 real
members, real `gvm_scores.market_cap`) and their real `raw_prices` closes for 14 real trading days
(2026-08-25 → 2026-09-11) — a window that happens to contain a genuine data gap (GTLINFRA/NELCO
have no row on 2026-09-07, an ordinary real-world absence, not contrived. Ran
`_segment_composite_series` on this real data, then **manually recomputed the same 14-day
composite day by day using the correct as-of/forward-fill semantics** and got an **exact match to
9 decimal places on every single day** — not an approximate/close match. A second synthetic member
with a large weight (35% of the total) and ZERO price data was grafted onto the same real 5 (same
technique cc#2032 used for its own exclusion proof) to confirm a genuinely data-less member is
excluded and renormalised over, not zeroed-in and diluting the real members — confirmed: the
composite with the ghost member is bit-for-bit identical to the composite without it.

**`_BENCH`**: confirmed `BANKNIFTY` present via the real, imported module (not re-typed).

**End-to-end, real deploy, real DB — the load-bearing check.** This module already has a
self-test hook from cc#2085 (`app_config['v12_bt_selftest']`, startup-gated, re-armable on any
boot — confirmed by reading `_v12_selftest_trigger`'s `startup` hook: it checks the flag fresh
every boot, not a one-time-ever consumption). Re-armed it (`value='run'`) before this push, so it
fired a REAL `run_backtest()` call end-to-end against production on this deploy's own boot.

**Result, stated precisely — what this does and does not prove.** `app_config['v12_bt_selftest']`
came back `done` (not `error`) at `2026-09-15 05:56:54`, ~9 minutes after the push — the run
completed in 10.6s: `universe_size=737`, `rebalances=62`, `total_trades=262`, `cagr_pct=39.06`,
`benchmark_cagr_pct=5.84`, `alpha_pct=33.16`, `first_rebal` matching cc#2085's own recorded
first-rebalance exactly (2021-11-15, ESCORTS/RADICO/APLLTD/AUROPHARMA) — the walk itself is stable
and unaffected by this card's additions. This proves `run_backtest()` executed **through** this
card's new segment-collection/composite code path on a real 5-year, 737-symbol, 62-rebalance run
without raising — a bug in the new SQL or wiring (a bad column name, a malformed query) would have
set `value='error'` with the exception captured, and it did not. **What it does NOT prove**: the
actual `sector_stats` values from this run. `_v12_selftest_run` is cc#2085's own pre-existing
function; its summary (`out = {..., "stats": res.get("stats"), ...}`) captures a fixed field
subset that does not include `sector_stats` — not extended here, since widening what an
already-shipped, already-relied-upon self-test captures is a separate decision from this card's
own scope. So the load-bearing evidence for the sector math ITSELF stays the unit-level proof
above (exact 9-decimal match + the ghost-member exclusion test); this self-test's contribution is
narrower but still real: confirmed integration, not confirmed output.

**Live check**: `/v12` Step 4, run a real backtest, confirm all 3 layers render with real numbers
and the benchmark selector changes Layer 3's output — CC's container has no egress path to
scorr.in to self-check the rendered page (established constraint this session).
