# cc#2104 — V10 Trades modal: remove OPEN tile, real Gross/Net P&L split

## What shipped

### 1. New constant (`v10_st_ema.py`)
`V10_FUT_BROKERAGE_PER_TRADE = 1000.0`, placed directly beside the existing
`V10_OPT_BROKERAGE_PER_TRADE = 100.0`, same one-named-constant convention (cc#1883/cc#1891).
Confirmed by grep before writing it: no FUT brokerage figure existed anywhere in the codebase.
This module does not apply the new constant itself — `get_performance()` stays OPT-only,
untouched, per do_not_touch.

### 2. Real gross/net split (`mobile_home2.py`, `mobile_v10chart()`)
The `by_leg` accumulator's `net_pnl` field was a **raw sum with nothing ever subtracted from it**
— confirmed by reading `v10_st_ema._close_leg()`, which computes `pnl` with no brokerage term for
either leg. Renamed to `gross_pnl`; a genuine `net_pnl = gross_pnl − (closed_trades × that leg's
own rate)` is now computed per leg, importing both constants from `v10_st_ema` (never retyped as
literals). `profit_factor` is untouched — still gross win / gross loss, per do_not_touch.

**A separate, unrelated `stats.net_pnl` field in this same function was found and deliberately not
touched.** It's a whole-book (FUT+OPT combined) raw sum, but grep + an existing cc#1598 comment in
`mobile/home.html` confirm the client stopped reading `d.stats` entirely (migrated onto the same
per-leg source this card fixes, for exactly the mixed-leg-netting reason cc#1598 describes). Dead,
unread output — verified, not assumed, and left alone since nothing on screen can contradict it.

### 3. `.v10sum` strip (`mobile/home.html`)
Tile order changes from **trades / profit factor / net / open** to **trades / profit factor /
gross / net** — the OPEN tile is gone, a GROSS tile added beside NET. `v10LegStats(d, leg)` gained
a `gross` field sourced from `by_leg[leg].gross_pnl`, alongside its existing `net` read. The strip
stays a 4-column grid — CSS unchanged, just which tile occupies the last slot.

### 4. `renderV10()`'s top summary line — no code change, confirmed as an intended side-effect
It already reads `v10LegStats(d, view).net` for its "Net Rs X" caption. Once `by_leg[leg].net_pnl`
became the true post-brokerage figure, this line **automatically** shows the same number, with no
edit required — exactly as item 4 predicted. Stated here explicitly, per the card's own
instruction, rather than treated as a silent side-effect.

## Real evidence, not assumed
`v10_trades`, queried directly, per (symbol, leg):

| Symbol | Leg | Closed trades | Gross (raw, unchanged) | Net (new, post-brokerage) |
|---|---|---|---|---|
| NIFTY50 | FUT | 38 | +₹78,825.50 | +₹40,825.50 |
| NIFTY50 | OPT | 33 | +₹55,334.50 | +₹52,034.50 |
| BANKNIFTY | FUT | 36 | −₹352.50 | −₹36,352.50 |
| BANKNIFTY | OPT | 32 | +₹14,298.00 | +₹11,098.00 |

**NIFTY50 OPT's real gross (55,334.50 → "+₹55,335" rounded) exactly matches the founder's own
screenshot** of the OLD, mislabelled "NET" figure cited in the spec's evidence — independent,
real-world confirmation that the bug this card fixes is exactly as described, not a
misdiagnosis.

## Verification
- `ast.parse`/`py_compile` clean on `v10_st_ema.py` and `mobile_home2.py`; `node --check` clean on
  the extracted `mobile/home.html` script.
- **Formula verified against real data**: the exact new `by_leg` loop logic (copied, not
  re-derived) run against all 4 real (symbol, leg) aggregates above — 4/4 correct.
- **Client render verified against the same real data**: the actual committed `v10LegStats()` +
  `.v10sum` strip-building code (extracted verbatim) fed the real by-leg numbers — 12/12
  assertions pass, including: no `open` tile anywhere in the output, gross and net render as two
  different real figures (not the same number twice), the OPT tile's gross matches the founder's
  screenshot number exactly, and a real loss leg (BANKNIFTY FUT) renders net more negative than
  gross (brokerage always worsens the raw figure for a leg with real trades).

## What did NOT change
`profit_factor` — still gross win / gross loss, everywhere. `v10PfBreakdown()`/`v10PfSheet()` (the
`i` info sheet) — deliberately reads gross rupee figures per its own cc#1473 comment, untouched.
`get_performance()`'s own OPT-only brokerage/net_pnl/CAGR (`/api/v10/performance`, cc#1883/1891) —
a separate endpoint, separate consumer, untouched. The desktop `v10_dashboard.html` — untouched,
out of scope.

## Flagged, not fixed (per the card's own explicit instruction)
After this ships, the FUTURES view will show **two different net figures** in the same modal: the
strip's new true net (brokerage-adjusted, this card) and `paired_trades()`'s own footer line
(`d.paired.summary.net_pnl`, a different derivation — `fut_pnl+opt_pnl` per paired trade, no
brokerage, `v10_endpoints.py`, do_not_touch). Reconciling them is explicitly out of scope here and
would be its own card if wanted — stated per the same precedent-flagging pattern cc#2099 used.
