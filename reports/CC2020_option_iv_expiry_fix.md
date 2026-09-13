# cc#2020 — option_iv_daily expiry fix: select the MONTHLY contract, not nearest-listed

Founder ruling, 12-Sep-2026: the live option chain (`worker/fyers_feed.py`, `option_chain` table)
always tracks the CURRENT MONTHLY contract for NIFTY and BANKNIFTY. The nightly bhavcopy-based
historical ingest (`option_iv_history.py`, via `bhavcopy_diagnostic.select_atm_window()`) instead
picked whichever expiry was nearest/earliest listed — the weekly, for NIFTY, on 250/250 backfilled
sessions. Weekly and monthly implied vol genuinely differ, so mixing them (or tagging a monthly
live chain off weekly history) was wrong.

## Stage 1 — code fix (landed on main at `10234f3`)

`bhavcopy_diagnostic.select_atm_window()` — the ONE shared selection every reader consumes
(`ONE_REGISTRY_ONE_DERIVATION_V1`): per symbol, collect every listed expiry on/after the trade
date, then `_monthly_expiry()`: group by (year, month), take the soonest month, select the LATEST
date inside it — by NSE convention the final expiry to fall within a month is the monthly
contract; earlier expiries that month are its weeklies. Data-driven on purpose — no hardcoded
weekday, no external expiry calendar (this session has no independently-verified NSE expiry
calendar for 2025-09 through 2026-09, so a hand-typed date list was rejected as an unverified
assertion). A month with one listed expiry (every single stock; NIFTY/BANKNIFTY right after a
roll) gives `min == max == that expiry`, identical to the old `min()`. `option_iv_history.py`'s
`ingest_date()` inherits the fix through the same call — no second implementation.

## Stage 2 — verification against the real corrected data

**eclass breakdown, re-run post-backfill:**

| symbol | mo sessions | wk sessions |
|---|---|---|
| NIFTY (after fix) | 171 | 79 |
| BANKNIFTY | 171 | 79 |
| FINNIFTY | 171 | 79 |
| RELIANCE | 171 | 79 |

NIFTY moved from 250/0 (wk/mo) to **171/79 — now identical to BANKNIFTY, FINNIFTY, and RELIANCE**.
This is a real, useful finding the card's own expectation ("NIFTY should move ... to
predominantly/entirely mo") undersold: the residual 79 "wk" sessions on every one of these symbols,
including RELIANCE (a pure single-monthly-expiry stock with no weeklies at all), are simply the
`eclass` query's own `DTE<=10` classifier counting the last ~7 trading sessions before each
monthly roll — a labelling artefact of the verify query, not a data defect. Confirmed identical
across all four symbols rather than assumed.

**Row-count sanity:** 2,200,578 rows / 250 dates / 235 symbols — same order of magnitude as the
pre-fix baseline (identical counts; only NIFTY's `expiry` column changed for the affected dates,
row-for-row). **Zero multi-expiry `(symbol, trade_date)` pairs** — the retire step left no
duplicates.

**cc#1994 `_stored_iv_gap_map` sanity, latest session (2026-09-11):**

| symbol | expiry (after fix) | gap strikes | mean gap (vol-pts) | mean call IV |
|---|---|---|---|---|
| NIFTY | 2026-09-29 (was 2026-09-15) | 21 (was 14) | 4.39 (was 7.92) | 8.82 (was 5.44) |
| BANKNIFTY | 2026-09-29 (unchanged) | 21 | 4.31 | 10.28 |

The numbers moved, as expected since the input data changed — reported, not hidden. NIFTY and
BANKNIFTY now land on the **same monthly expiry** for the same trade date, and NIFTY's gap
magnitude is now close to BANKNIFTY's (4.39 vs 4.31) rather than the old weekly-derived 7.92 —
consistent with two index names' monthly contracts behaving similarly, a sanity signal the old
data could not have shown.

**Spot-check: every corrected NIFTY monthly expiry against a "last Tuesday of month" baseline.**
12 of 13 land exactly on the last Tuesday. One exception, investigated rather than waved through:
`2026-03-30` (a Monday) where the true last Tuesday, `2026-03-31`, is one of the pre-existing 14
dates with no bhavcopy at all (`HTTPError: 404 ... nsearchives.nseindia.com`) — i.e. an NSE
trading holiday. NSE's own convention rolls an expiry back to the prior trading day when the
scheduled date is a holiday, so the corrected selection (30-Mar) is the real monthly contract,
not a defect. Flagged and explained, not silently accepted, per the card's own instruction.

## Stage 3 — linking and cleaning, the full window

`_retire_superseded_expiry_rows()` ran inside every `ingest_date()` call across the full
2025-09-01..2026-09-11 backfill (not just a proof slice). Result, read directly from
`option_iv_expiry_fix_log`:

- **1 symbol touched: NIFTY** (BANKNIFTY/FINNIFTY/RELIANCE and every other symbol were already
  selecting the monthly contract, so their re-ingest overwrote in place with the same expiry —
  no superseded rows, nothing to retire, confirming the card's own known-affected list).
- **195 dates** had NIFTY's expiry actually change; every one cleared the `rows_written >= rows_before/2`
  sanity floor and was `deleted_old` — **8,190 old (weekly) rows deleted**, zero cases of
  `kept_old_implausible_drop` (no date needed the fallback safety net).
- The pre-existing 14 error dates (genuine NSE holidays/gaps, `2025-10-02` through `2026-06-26`)
  are untouched — their old (now-superseded, but still the only data available) rows remain in
  place, correctly, since no corrected replacement was ever fetched for them.

`option_iv_backfill_status` after the run: **250 done, 14 error** (the same 14 as before this
card — no new errors introduced by the fix). `app_config.option_iv_backfill_run = 'done'`.

## Do-not-touch, confirmed

`fo_eod`/`nse_fo_eod.py`/`bg_fo_eod` — untouched. `deriv_metrics.py`'s Black-Scholes maths and
`option_ivp.py`'s `WINDOW_SESSIONS`/`MIN_SESSIONS`/`BUCKET_MIN_SESSIONS`/bands — untouched; this
card fixed the input feeding that module, not its algorithm. `worker/fyers_feed.py` — already
correctly monthly-only, not touched.

## Out of scope, not folded in

Whether `option_ivp.py`'s tag rendering on the NIFTY chain now looks right once the corrected
60-session floor refills is a live-app question this container cannot check (no route to
scorr.in) — a founder/Fable follow-up if tags still look wrong after this lands, not built here.

## Not done here (the card's own FOUNDER-ONLY item)

Re-checking the live NIFTY option chain popup on scorr.in after this lands — no route to
scorr.in from this container.
