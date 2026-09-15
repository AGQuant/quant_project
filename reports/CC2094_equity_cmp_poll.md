# cc#2094 — EQUITY LIVE PRICE VIA YAHOO (5-min poll)

Replaces the Fyers ad-hoc-subscribe plan in cc#2041/cc#2042 (both `status='superseded'`).
Founder-dictated design (voice session, 15-Sep-2026): equity trade alerts and QB equity
holdings get their live price from Yahoo on a 5-min poll — no new Fyers subscription
mechanism, no subscribe/unsubscribe state to leak or reconcile.

## What shipped

- **`equity_cmp_poll.py`** (new): `equity_universe(cur)` — 3-source UNION (trade_alerts
  approved+open, trade_alerts pending entry not yet triggered, QB open positions in active
  baskets), minus `futures_universe(is_active=true)`. `run_equity_cmp_poll()` — one Yahoo
  batch fetch for the whole universe, UPSERT into `cmp_prices(source='yahoo')`, then refresh
  `quant_paper_positions` for the equity-only QB subset.
- **`scheduler.py`** — `_bg_equity_cmp_poll()` wrapper, dispatched on the existing 5-min
  market-hours beat alongside `_bg_trade_alerts_check`.
- **`main.py`** — `include_router` wiring only.
- **`scheduler_master`** — `bg_equity_cmp_poll` row inserted, `active=true`, cadence matches
  the real dispatch condition.

## Reuse, not a second implementation

- **`yahoo_live_quote.fetch_live_quotes()`** (cc#1417) is the batched Yahoo quote call. Its
  own docstring scopes its callers to index rows + ADR breadth (a Fyers-outage fallback);
  this is a second, legitimate caller for a different purpose. Documented in the new file's
  docstring so a future reader isn't confused by importing from a module titled for a
  different original purpose.
- **`qb_eod_checker._pct_change()`** — the exact pnl% formula the (basket-only, unscoped)
  15-min `qb_intraday_mark` job already uses. Reused verbatim so the two jobs' numbers agree
  by construction wherever they overlap, not by coincidence.
- **`source='yahoo'`** is a distinct tag from `yahoo_live_quote`'s own `'yahoo_live_fallback'`
  — two different sourcing semantics (always-Yahoo-for-equity vs Yahoo-only-when-Fyers-is-down)
  kept separate in `cmp_prices.source` rather than conflated.

## What did NOT change

- `futures_universe` symbols are untouched — that leg's price source (Fyers feed) is
  out of scope for this card (`do_not_touch`), and the universe query explicitly excludes
  them.
- `_bg_qb_intraday_mark` (the existing 15-min basket-mark job) is untouched. It queries ALL
  open `quant_paper_positions` with no `futures_universe` exclusion and never writes
  `cmp_prices` — not the right shape for this card, so this card builds a separate path
  rather than modifying it.

## A suspected finding, investigated and retracted

While building this, an early read of `scheduler_master` looked like `_bg_qb_intraday_mark`
had stalled mid-day (`last_run_at` looked like "10:01" against a market that stays open to
15:30 IST). Checked before writing that up anywhere permanent — **it does not hold**:

| Check | Value |
|---|---|
| `last_run_at` (raw, `timestamptz`) | `2026-09-15 10:01:28.536921+00:00` |
| `last_run_at` converted to IST | **15:31:28 IST** — the last market-hours slot of the day (market closes 15:30) |
| `last_status` | `ok` |
| `last_error` | `NULL` |
| `last_duration_ms` | `29045` (29.0s) |
| `quant_paper_positions.updated_at` spread (134 open QB rows) | `10:00:59.531` → `10:01:28.504` UTC — a 29-second window, matching `last_duration_ms` exactly |

The earlier read had taken a UTC timestamp for IST. There is no stall — this was a clean run
finishing at end of day, with the classic caveat that each run overwrites every touched row's
`updated_at`, so column state alone can't distinguish "ran once" from "ran all day, last one
just finished" — but `last_status='ok'` with a normal 29s duration at the correct end-of-day
slot gives no reason to suspect either. Retracted, not carried forward as a real finding.

## Verification — stub-cursor harness against real production data

Ran a stub-cursor test harness importing the real, committed `equity_cmp_poll` module
directly (never re-typed), against real `futures_universe`/`quant_paper_positions` shape,
with synthetic-but-real-symbol Yahoo quotes for 3 of today's real equity-universe members.

| Test | Result |
|---|---|
| 1. `equity_universe()` vs real-data-shaped stub | **PASS** — 9-symbol sample, futures_universe overlap (`ABCAPITAL`, `ADANIENSOL`, `ADANIPORTS`, `ADANIPOWER`, `APOLLOHOSP`, `AUBANK`, `LTM`) correctly excluded |
| 2. `run_equity_cmp_poll()` end-to-end | **PASS** — universe/quoted/unquoted counts match exactly |
| 3. `cmp_prices` UPSERT rows | **PASS** — exactly the 3 quoted symbols, at the exact quoted prices, `source='yahoo'` |
| 4. `quant_paper_positions` UPDATE formula | **PASS** — manual recompute (same `_pct_change` formula) matches the engine's pnl/current_value/pnl_pct output exactly for all 3 updated positions |
| 5. Zero-update correctness | **PASS** — `ABCAPITAL` (real positions id 94/190, futures_universe overlap, never enters the pipeline) and `ASKAUTOLTD` (real position id 142, in-universe but unquoted this cycle) all correctly receive **zero** updates — nothing fabricated or carried forward |

**Real production scale, checked separately from the test harness**: today's real equity
universe (post-`futures_universe` exclusion) is **52 symbols**.

## First-run evidence (ENGINE_LIVENESS_RULE)

Market is closed at push time (19:0x IST, `market_open=false`, trading day). The gated
self-test trigger (`app_config['equity_cmp_poll_run']='run'`, same pattern as
cc#2032/2033/2085/2087) was armed **before** this push, so the redeploy boot fires it
automatically — matching this card's own 5-min-market-hours code path exactly, just off the
scheduler beat for a first look. **Result pending confirmation on the next heartbeat tick**
(Railway auto-deploy is ~90s; this file will be amended with the real
`universe_size`/`quoted`/`cmp_written`/`qb_marked` numbers once confirmed, per the same
addendum pattern used for cc#2087).

An after-hours first run proves the pipeline executes correctly end-to-end against real data
(Yahoo will return last-close prices, not intraday ticks) — it does **not** prove true
live-market-hours behavior. That needs tomorrow's 9:15–15:30 IST window.

## Scope note

`_bg_qb_intraday_mark`'s own gap — no `cmp_prices` write, no `futures_universe` exclusion —
is unaffected by this card either way (see retraction above: investigated, found healthy, no
gap after all).
