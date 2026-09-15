# cc#2095 — CUSTOM ALERTS V1: multi-condition alert builder

A symbol-level alert that carries MULTIPLE conditions (not just one price level), each picked
from a categorized registry, chained strictly left-to-right with a per-condition AND/OR toggle.
Fully separate from the existing single-price `trade_alerts` flow — both coexist.

## Three corrections found during discovery, applied before building (data-source gate)

Checked every source the spec named against the real DB/codebase before pointing the registry or
evaluator at it — three of them were wrong, all caught before shipping:

1. **TC Score source.** Spec named `v8_tc_score_daily.score_pct`, cadence `daily_eod`. Real check:
   `v8_tc_score_daily` has **zero rows, ever** (`MAX(score_date) IS NULL`). `v8_pivot_star.py`'s own
   schema comment confirms why: *"the amendment-superseded daily table, exists empty in the live DB
   from the first cut of this card — flagged for a weekend console DROP, never written."* It was
   superseded by `v8_tc_score_ticks` (a 5-min series) before a single row landed. **Corrected**: the
   registry points at `v8_tc_score_ticks` instead — the real, live, populated table
   `/api/v8/tc_score_latest` already serves the dashboard's TC % column from — cadence corrected to
   `live_5min`. Also corrected "TC Score Buy/Sell" (would need 2 rows) against the spec's own
   "exactly 10 rows" verify bullet: real data shows one `(symbol, side)` combination live per symbol
   at a time (whichever side its actual open position is on — the engine scores the open book, not
   both directions speculatively), so this ships as **one row, "TC Score"**, side-agnostic
   latest-tick read.
2. **Day Move % source.** Spec said "live cmp_prices vs raw_eod prev_close, same basis as
   `get_v8_live_metrics.day_pct`." The real `/api/v8/live_metrics` endpoint actually reads `cmp` from
   `intraday_prices`' latest `fyers_eq` bar, scoped to `futures_universe` only — which would silently
   exclude equity-only alert symbols. **Corrected**: reused the part of "same basis" that is a
   formula, not a table — `prev_close.prev_session_close` + `prev_close.day_pct`, the exact
   anchor/formula `get_v8_live_metrics` itself imports (cc#1565) — fed with `cmp_prices.cmp` as the
   live price (universal coverage, and literally the table the spec's own words name).
3. **Notification path.** Spec item 5 said surface a trigger through "the existing bell/alerts
   endpoint AND the existing Telegram alert function." Checked `trade_alerts_endpoints.check_triggers()`
   and its dispatcher directly: **neither sends a Telegram message** — a `trade_alerts` trigger today
   is bell-only (an app-log line, nothing else). There is no second existing path to match.
   **Not built**: inventing one here would make custom alerts more notified than `trade_alerts`
   itself, breaking the "one inbox" parity in the other direction. Bell-only, correctly matching
   what `trade_alerts` triggers actually do.

## Schema (real, created in production)

Three tables — `alert_metric_registry` (10 rows, seeded), `custom_alerts`, `custom_alert_conditions`
— exactly per spec item 1, `alert_metric_registry` as the one source of truth both the evaluator and
the picker read.

| metric_key | category | label | cadence | source |
|---|---|---|---|---|
| day_move_pct | PRICE MOVE | Day Move % | live_5min | cmp_prices + prev_close (corrected, see #2) |
| week_move_pct | PRICE MOVE | Week Move % | daily_eod | v8_metrics.week_return |
| month_move_pct | PRICE MOVE | Month Move % | daily_eod | v8_metrics.month_return |
| daily_rsi | MOMENTUM | Daily RSI | daily_eod | v8_metrics.daily_rsi |
| monthly_rsi | MOMENTUM | Monthly RSI | daily_eod | v8_metrics.rsi_month |
| price_vs_50dma_pct | TREND | Price vs 50-DMA % | daily_eod | v8_metrics.dma_50 |
| price_vs_200dma_pct | TREND | Price vs 200-DMA % | daily_eod | v8_metrics.dma_200 |
| volume_ratio | VOLUME | Volume Ratio | daily_eod | v8_metrics.vol_ratio |
| gvm_score | SCORE | GVM Score | daily_eod | v8_metrics.gvm_score |
| tc_score | SCORE | TC Score | live_5min | v8_tc_score_ticks (corrected, see #1) |

Verified `dma_50`/`dma_200` are already stored as percentages (real sample: 360ONE cmp=1052.1,
dma_50=-7.90) — no unit transform needed, a plain column read is correct.

## Evaluation — one shared function, two callers

`evaluate_alert(cur, alert_id)` always recomputes the FULL chain (every condition, any cadence) and
updates `last_value`/`last_evaluated_at` on each. Never forked in two, per the spec's own instruction:
- **Daily** (`_bg_custom_alerts_daily`): every active alert, once `v8_metrics` is confirmed fresh for
  today (`MAX(computed_at)::date = today IST`) — a **data-driven gate**, not a fixed clock time.
  Dispatched as a 5-min retry window from 15:00 IST onward (trading days) rather than one fixed
  minute, so a late `v8_eod` run still gets picked up the same day instead of being missed entirely.
  Its own `_ran_today` guard only latches once a real (non-skipped) run happens.
- **Live** (`_bg_custom_alerts_live`): every 5 min market hours, only alerts carrying ≥1
  `live_5min` condition — but the FULL chain, since a live leg can be AND/OR'd with a daily leg.

## Chain evaluation — strict left-to-right, proven against algebraic precedence

Verified with a case specifically chosen to differ under the two interpretations: `cond1 OR cond2
AND cond3` with cond1=True, cond2=False, cond3=False (real RELIANCE data: daily_rsi=33.22 below 50,
monthly_rsi=13.57 above 50 is False, gvm_score=4.77 above 8 is False).
- **Left-to-right** (this build): `((c1 OR c2) AND c3)` = `(True AND False)` = **False**.
- **Algebraic precedence** (AND-before-OR): `(c1 OR (c2 AND c3))` = `(True OR False)` = **True**.

The evaluator returned **False** — confirmed strict left-to-right, never algebraic grouping.

## Verification — stub-cursor harness, real production data throughout

Real schema created and registry seeded in production (idempotent `CREATE TABLE IF NOT EXISTS` +
`ON CONFLICT` UPSERT, confirmed via a direct `SELECT` — exactly 10 rows, correct fields). The
evaluator itself was exercised by importing `custom_alerts.py` directly and running it against real
RELIANCE (`v8_metrics`, `cmp_prices`, `raw_prices`) and ADANIPORTS (`v8_tc_score_ticks`) data:

| # | Test | Result |
|---|---|---|
| 1 | Registry exactly 10 rows | PASS |
| 2 | `day_move_pct` special case: RELIANCE cmp=1235.3, prev_close=1257.50 (raw_eod, 11-Sep) → -1.77% | PASS, exact |
| 3 | `tc_score` special case: ADANIPORTS latest tick = 62.0 | PASS, exact |
| 4 | Plain lookup: RELIANCE `month_return` = -5.4198...% | PASS, exact |
| 5 | 2-condition AND, both true → fires, status flips active→triggered | PASS |
| 6 | 2-condition AND, one false → does not fire | PASS |
| 7 | 3-condition left-to-right vs algebraic precedence (above) | PASS — proves the correct semantics |
| 8 | Daily gate: real `v8_metrics.computed_at` is today → fresh=True | PASS |
| 9 | Daily gate: synthetic stale day → skipped, zero evaluated | PASS |

## API (item 6)

- `GET /api/custom_alerts/registry` — categories grouped for the picker.
- `POST /api/custom_alerts/create` — `{symbol, label, conditions:[{metric_key,operator,threshold,join_operator}]}`; validates every `metric_key`/`operator` against the registry server-side.
- `GET /api/custom_alerts/list?symbol=&status=` — active/triggered/paused/deleted, filterable.
- `POST /api/custom_alerts/delete` — soft-delete only (v1 scope, per spec).

## Bell/inbox merge (item 5, corrected per #3 above)

`trade_alerts_endpoints.list_alerts()` now appends triggered `custom_alerts` rows (via
`custom_alerts.triggered_for_bell()`) alongside `trade_alerts` rows — `do_not_touch`'s "only ADD
rows alongside them" honored, nothing about the existing rendering changed. Appended **after**
`_attach_seen()` runs (on `trade_alerts` rows only) so the namespaced `"c123"` ids never reach
`trade_alert_seen`'s integer-keyed lookup — a real collision risk (both tables' ids restart at 1)
avoided by construction, not by luck. v1 scope, stated plainly: custom-alert rows are always
`seen=true` (they never move the bell's unseen badge count) — a real, deliberate simplification,
not a silent gap.

## UI (item 7)

`scorr_bell.js` gained a third footer button, "+ CUSTOM ALERT", opening a new shared module
**`scorr_custom_alert_create.js`** (mounted identically on app and web, same architecture as the
existing `scorr_alert_create.js` — self-owned overlay, own scoped CSS, `window.ScorrCustomAlertCreate`).
Symbol search reuses `/api/gvm/search`. The categorized picker reads `/api/custom_alerts/registry`
live (never hardcoded) — a condition row shows "Pick a metric…", opens the category list on click,
and every row after the first carries the AND/OR toggle pill on its right edge (default AND), per
item 7 exactly. `feedRow()` in `scorr_bell.js` got one new branch (`alert_type === 'custom'`) to
render a condition-chain summary instead of a direction pill — everything else (link, layout,
opacity rule) unchanged, so the two row types read as one feed.

Wiring mirrors `scorr_alert_create.js` exactly: `_MOBILE_HEAD` script tag + `main.py`'s build-id
stamp list (app pages), `pwa.js`'s runtime injection guard (web pages), a new
`SCORR_CUSTOM_ALERT_CREATE_JS` constant + `/scorr_custom_alert_create.js` GET route in
`pwa_endpoints.py` (same `_CACHE_1D` header as every sibling shared-JS route). Not added to
`_PWA_INJECT_PATHS`/`PROTECTED`/`NAV`/`NAV_REGISTRY` — confirmed `scorr_alert_create.js` itself
isn't either; a shared script asset is not a page under rule 8.

## First-run evidence (ENGINE_LIVENESS_RULE)

Both scheduler_master rows are registered, `active=true`. Real first-run evidence (a genuine
created alert firing on a real tick) will be armed immediately after this lands on main — logged
as a follow-up in cc_task_logs once confirmed, per the same registered-is-not-live discipline this
session has applied throughout.

## What did NOT change (do_not_touch, honored)

`trade_alerts`/`trade_alert_levels` and the existing single-price alert flow — untouched, still the
only thing `check_triggers()` reads. `v8_metrics`/`v8_tc_score_ticks` computation — read-only use,
zero new metric engines. The existing `/api/alerts/list` rendering of `trade_alerts` rows — only
appended to. `worker/fyers_feed.py` and the `cmp_prices` write cadence — untouched; this reads
`cmp_prices`, never writes it.

## Out of scope (per spec, unchanged)

No parenthesized/nested boolean logic (v1 is strict left-to-right only). No edit-after-create (v1 is
create + soft-delete only). No metric types beyond the 10 seeded here.
