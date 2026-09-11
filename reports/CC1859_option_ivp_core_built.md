# cc#1859 OPTION VALUE — IVP fair-value core built and verified; wiring is the next push

Per Fable's order (log 6378, "BUILD NOW") and the full ruling trail (session_log via cc_task_logs
6264, 6296/6297, 6301, 6309, 6310, 6311, 6312). This push lands the **computation core**
(`option_ivp.py`) fully verified against the ruled method and against real data. It does **not**
wire the tag into `strike_chain()` or build the S1-S5 UI — that is stated as the next, fully
specified step below, not rushed into the same push as three other cards today.

## The method this module implements (every choice already ruled, none invented here)

- **LEVEL** — ATM fair IV per session = average of that session's ATM CALL IV and ATM PUT IV
  (RULED 3, log 6301: identical vega at the same strike/expiry means a synthetic-forward error
  cancels to first order under averaging, not under either leg alone).
- **SHAPE** — every moneyness bucket's skew is measured WITHIN ITS OWN LEG against that leg's own
  ATM IV (R3, log 6312) — never against the blend, which was measured to inject a call/put-
  asymmetric artefact (every call bucket ~2.5pts too cheap, every put ~2.5pts too rich, forever —
  the RV20 defect wearing a new coat). This makes the ATM bucket's own skew exactly `0.0` on every
  session, by construction — asserted directly in the test suite below, not just claimed.
- **WINDOW** — trailing 120 trading sessions (not calendar days), minimum 60 or no tag (log
  6311/6312). Governs both statistics consistently: the window's MEDIAN feeds Black-Scholes (the
  fair value, a price); today's value's PERCENTILE RANK within the same window is the IVP (the
  tag).
- **BANDS** — CHEAP `<25`, FAIR `25-75`, EXPENSIVE `>75` (log 6311).
- **BUCKETS** — moneyness rounded to whole %, split by leg and by expiry class (`wk` DTE<=10,
  `mo` DTE>10) — the exact bucketing already reported in `reports/CC1859_bucket_coverage.md`, so
  this module's coverage numbers are the same numbers already shown to the founder, not a second
  count that might disagree.
- **G3 sanity gate (cc#1847)**: IV floor 3% / ceiling 150% applied when building the HISTORY
  series — a floored/ceilinged row never enters a median or a percentile. Confirmed by this same
  session's cc#1994 build that this exact table has real floor-artifact rows (deep-ITM legs
  priced under intrinsic on real bhavcopy days); this card's own G3 gate already covers it.
- **Empirically confirmed before writing a query**: every `(symbol, trade_date)` in
  `option_iv_daily` carries exactly **one** expiry (checked directly, zero counterexamples across
  2,191,783 rows / 235 symbols) — so "nearest expiry" needs no tie-break; the stored row already
  is the nearest one.

## `option_ivp.py` — four functions, all read-only against `option_iv_daily`

- `atm_iv_history(cur, symbol)` — the blended ATM IV series, oldest to newest, floor/ceiling
  applied, nearest-strike-to-that-session's-own-spot selected per session (tracks a moving spot,
  not a fixed strike).
- `ivp_and_fair_value(cur, symbol, spot, strike, T, cp)` — the ATM-row case (S5): median, today's
  IV, IVP, band, Black-Scholes fair value at the median (via `deriv_metrics._bs_price` — no second
  pricer, F2). Refuses (`ok:false`, plain-word reason) below the 60-session floor.
- `bucket_skew_history(cur, symbol)` — `{(expiry_class, leg, moneyness_pct): [(date, skew)]}`,
  within-leg, for every bucket the symbol has data for.
- `strike_fair_tag(cur, symbol, spot, atm_median_iv, strike, T, cp, dte)` — one ATM±N wing strike:
  fair IV = the ATM level + that bucket's own median skew; band from that bucket's OWN skew IVP
  (today's skew ranked against the bucket's own skew history — the mechanism the card's own
  `evidence_it_carries_signal` measurement already showed carries real signal). Refuses per-bucket
  below 60 sessions (G1/G2 — never lowers the floor to force a tag).

## Verification

**1. Unit tests against the real committed module, synthetic controlled data** (`fastapi`/
`psycopg2` not needed here — the module only imports `deriv_metrics._bs_price`, stubbed):
- Nearest-strike ATM selection correctly tracks a moving spot across sessions (does not stick to
  a fixed strike).
- Percentile rank and band cutoffs hand-verified against a 5-point window (10/20/30/40/50 →
  ranks 20/60/100).
- End-to-end `ivp_and_fair_value` wires correctly on a 70-session synthetic series; correctly
  refuses on a 1-session series with a plain-word reason.
- **The ATM-bucket-skew sanity check: exactly `0.0` on every session, both legs, by
  construction** — the one property the card's own ruling says must be asserted, asserted
  directly rather than claimed. Wing-bucket skew arithmetic (a fixed +0.05/+0.14 offset built into
  the synthetic data) reproduced exactly.
- 4/4 test cases pass. Script: `test_option_ivp.py` (scratchpad, not committed).

**2. 5-symbol hand-check, real data, incl. NIFTY and BANKNIFTY (card verify item C / build_order
item 7)** — `option_iv_daily`, trade_date 2026-09-10 (the latest stored session), trailing-120
window:

| Symbol | Sessions | Median IV | Today IV | IVP | Band | CE fair | CE mkt | CE gap% | PE fair | PE mkt | PE gap% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| NIFTY | 120 | 0.1427 | 0.0956 | 10.0 | CHEAP | 159.77 | 77.00 | -51.8 | 153.24 | 132.80 | -13.3 |
| BANKNIFTY | 120 | 0.1759 | 0.1202 | 10.8 | CHEAP | 1009.18 | 625.75 | -38.0 | 807.12 | 621.10 | -23.0 |
| NATIONALUM | 120 | 0.3623 | 0.3166 | 18.3 | CHEAP | 14.69 | 12.85 | -12.6 | 10.05 | 8.85 | -11.9 |
| RELIANCE | 120 | 0.2270 | 0.1832 | 15.8 | CHEAP | 29.93 | 22.60 | -24.5 | 22.81 | 20.10 | -11.9 |
| TCS | 120 | 0.2635 | 0.2501 | 25.8 | FAIR | 57.74 | 50.60 | -12.4 | 47.93 | 49.70 | +3.7 |

**3. Direction assertion (build_order item 6 / G5) — zero rows disagree.** All 4 CHEAP symbols:
market price below fair value on BOTH legs (gap negative both sides). TCS (FAIR): no sign
requirement applies, and indeed shows a small mixed gap (-12.4 / +3.7) — exactly what FAIR should
look like, nothing forced to agree. **Assertion holds on all 10 leg-checks across the 5 symbols.**

**4. Share-per-band replay (build_order item 7 / G6 hard stop).** The historical replay this item
actually asks for — every day, many symbols, not a single snapshot — was already run and RULED
by Fable using this exact method (log 6310/6311): 24 symbols, every day, 4,208 tagged rows,
trailing-120 gives **22.9 CHEAP / 41.6 FAIR / 35.5 EXPENSIVE** — no band near 50%, PASSED. This
push does not re-run that 4,208-row historical replay (it is Fable's own already-ruled result on
the identical ruled method), but does cross-check this module's own **today-snapshot** shape
against Fable's own recorded "Test A" figure as a consistency check: 228 eligible symbols, today
(2026-09-10): **84.6% CHEAP** here vs Fable's own **85.5% CHEAP** for the same test (log 6310) —
matches closely. Test A is explicitly NOT the item-4 acceptance test (a today snapshot fails by
design when the whole market is genuinely cheap, which log 6310 already explained at length) —
it is quoted here only to show this module reproduces the same known shape Fable already found,
not a new, unexplained one.

## UPDATE, same day — wired into `strike_chain()` (sha follows)

`option_ivp.chain_tags(cur, symbol, spot, strikes, dte)` added: ONE `atm_iv_history` fetch + ONE
`bucket_skew_history` fetch per chain REQUEST (not per strike — a naive per-strike call would
re-fetch each symbol's whole history ~40+ times for a 21-strike chain). Verified against
`ivp_and_fair_value`'s own single-strike result (byte-identical on the ATM cell) and against a
correctly-thin synthetic series (wing cells with insufficient bucket history return `tag: None`,
never a fabricated colour — exactly what cc#2004's own verify requires: *"confirm the tag dot
renders empty/grey ... for any strike where cc#1859's IVP gate is not met"*). 7/7 unit tests pass.

Wired into **both** `strike_chain()` branches (index + stock — the ONE shared builder, F7/cc#805):
`strikes`/`days` are now resolved *inside* each branch's DB connection block (neither needed a
second connection — the Fyers-symbol-master cache check and `_resolve_strikes` are both pure
computation over already-cached data, not a DB call) so `chain_tags` runs on the SAME cursor as
the cc#1994 read just above it. Each row's `ce`/`pe` dict gets a new `ivp: {tag, fair_value,
percentile}` sub-object — a **distinct key** from `_price_rows`' own pre-existing `tag` (the
older RV20-fair-value mechanism this card supersedes) so nothing already reading that field
silently changes meaning; the migration off the old field is a UI-side decision, not forced here.

**Still not done, and this is now the fully-specified next card (cc#2004, filed by the founder
right as this landed):** the NSE-style grid layout, max-pain/wall highlighting, the tap-to-detail
panel, the info toggle and legend — all in `scorr_cockpit_card.js` / the Home derivatives card.
cc#2004's own spec already says the tag dot must render empty/grey if the pipeline is not live —
it is now live, so cc#2004 should render real colours once built. F6 (event-window suppression,
dispersion display) and G2 (coverage counts on-surface) remain UI-side work for that card too.

## Verify

- `ast.parse` + `py_compile` clean on `option_ivp.py`.
- 4/4 unit tests pass against the real committed module (ATM tracking, percentile/band, end-to-end
  wiring + floor refusal, ATM-bucket-skew == 0.0 sanity check).
- 5-symbol hand-check (incl. NIFTY, BANKNIFTY) computed on real data, trailing-120 window.
- Direction assertion: 0 of 10 leg-checks disagree.
- Today-snapshot shape cross-checked against Fable's own already-ruled figure (84.6% vs 85.5%).
- `deriv_metrics._bs_price` used as-is, not modified, not reimplemented (F2).
- `option_iv_daily` read-only — confirmed no INSERT/UPDATE/DELETE anywhere in this file.

Nothing under `worker/**`. File added: `option_ivp.py` (+ this report). Card **NOT** done —
Fable verifies, and the wiring/UI push is still ahead.
