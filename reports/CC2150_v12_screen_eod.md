# cc#2150 — v12 `/api/v12/screen`: the intraday leak is out of the basket / screen path

Date: 17-Sep-2026. File: `v12_endpoints.py` (`_BASE_SQL`, the seven technical filter conditions,
the page's Vol R read, the sort tie-break, `/api/v12/filters/meta`), this report.
Ruling applied: FOUNDER_RULING_16SEP_ALL_DATA_EOD_BASIS. Defect named on cc#2123.

## What changed, and why each one

| Before | After | Why |
|---|---|---|
| `COALESCE(m.x, ut.x)` for the eleven technical fields; `v8_metrics` joined at its latest score_date | `ut.x` only; `v8_metrics` is not in the query | v8_metrics is rewritten on every 5-minute tick (17-Sep: one score_date, first write = last write = 10:35:29, then 10:45:14). All 207 futures rows differed from their EOD row on every one of the eleven fields (rsi_weekly by up to 35.9 points, daily_rsi 12.6, week_index_52 13.7). A fallback to a live table is still a live read, so it is dropped, not demoted. |
| `LEFT JOIN LATERAL … tc_universe_ticks ORDER BY ts DESC` | `tc_scanner_score_daily` at its latest score_date, best score100 across side/bucket, deterministic tie order | The ticks table is the 5-minute Trade Check feed (last tick 10:45 IST). The daily table is cc#2126's EOD stamp (828 rows, 207 symbols, 4 side/buckets, 2026-09-16). `tc_asof` is now a date. |
| `g.price` served as `price` | `rp.close AS price` from `raw_prices` at its latest `price_date`, plus `price_date`; `g.price` kept as `screener_price` | See item 3 below. |
| `live_rvol_batch` (intraday_prices) on the returned page | `eod_rvol_pair_batch` (raw_prices only) + `rvol_asof` | The live form anchors to the latest intraday session — a live read on the same path. The EOD form is the same canon (cc#1441 / cc#1978). |
| `v8_qualified … signal_date = CURRENT_DATE` | the last COMPLETED session (`MAX(signal_date) < CURRENT_DATE`) | v8_qualified is written during the session by v8_signal_writer (`source = live_5min`: 9 rows between 09:20 and 10:45 on 17-Sep, 37 by the close on 16-Sep). Reading today's set changes the response every time a name qualifies. |
| `sector_week, sector_month, sector_day, vol_ratio, ma9_vs_ma21, eod_chg` from v8_metrics | `NULL` under the same names | v8_metrics-only; no EOD source. `rvol` is the number to read for volume. |
| `ORDER BY "<col>" DESC NULLS LAST` | `…, g.symbol ASC` | Ties in the sort column made page order arbitrary between two reads. |
| `/api/v12/filters/meta` slider bounds from v8_metrics | from universe_technicals | The bounds now describe the table the screen filters on. |

Kept as they were: `v8_paper_pivots` (EOD-derived; equal to universe_technicals' pivots on all 1,771
overlapping symbols, max abs diff 0.00), `screener_raw`, `sector_ratings`, `earnings_calendar`.

## Item 3 — what `g.price` / `s.price` are

`gvm_scores.price` is the screener CSV's "Current Price", copied by gvm_nightly at CSV load
(`SCREENER_COLUMNS`: "Current Price" → `price`); `screener_raw.price` is the same column. Both carry
the price at export time of a weekly CSV (loaded 2026-09-16 09:53), not the EOD close of the
evaluated date:

| Symbol | g.price (screener) | raw_prices close 2026-09-16 | diff |
|---|---|---|---|
| RELIANCE | 1246.70 | 1240.00 | +0.54% |
| TCS | 2180.00 | 2188.80 | −0.40% |
| JSWSTEEL | 1243.30 | 1248.90 | −0.45% |
| HDFCBANK | 720.70 | 721.50 | −0.11% |
| INDUSINDBK | 948.75 | 946.50 | +0.24% |
| VEDL | 256.05 | 256.00 | +0.02% |

On `/api/v12/screen` the `price` field is now the raw_prices close at its latest `price_date`.
Slot sizing in the backtest already reads raw_prices closes (`v12_backtest._price_series`). Still on
the screener price: `_UNI_COLS["price"] = "g.price"`, the universe filter vocabulary shared with
`v12_backtest`'s validator — flagged below, not changed here.

## Item 4 — callers, and the before / after diff

`_BASE_SQL` is used in one place: `v12_screen` (the count query and the row query). The only
consumer of `/api/v12/screen` is `screener.html` (line 375; the page is retired — `/screener` 301s to
`/v13` — the file is kept for rollback), which also reads `/api/v12/filters/meta` (line 238). No other
file, page or job calls either. `v12_backtest` and the mobile builder use `_UNI_BASE` / the universe
endpoints, not this query.

Before / after on the same rows, 17-Sep 10:45 IST:

| Symbol | kind | rsi_weekly before → after | daily_rsi | week_return | week_index_52 | TC before → after | price before → after |
|---|---|---|---|---|---|---|---|
| HDFCBANK | futures | 3.25 → 35.83 | 47.48 → 51.59 | 2.92 → 3.99 | 9.49 → 11.70 | 59.7 WATCH SELL @10:45 tick → 50.5 WATCH SELL @2026-09-16 | 720.70 → 721.50 |
| RELIANCE | futures | 18.59 → 34.33 | 35.99 → 34.20 | −2.34 → −2.67 | 2.36 → 1.25 | 67.5 VALID SELL → 75.3 VALID SELL | 1246.70 → 1240.00 |
| JSWSTEEL | futures | 45.39 → 43.30 | 39.76 → 39.73 | −4.22 → −4.23 | 63.28 → 63.25 | 52.2 WATCH SELL → 52.2 WATCH SELL | 1243.30 → 1248.90 |
| VAML | non-futures | 25.13 → 25.13 | 32.33 → 32.33 | −6.35 → −6.35 | — | — | 409.10 → 409.30 |
| TMCV | non-futures | 43.64 → 43.64 | 33.40 → 33.40 | −3.41 → −3.41 | — | — | 424.10 → 424.25 |

Every change on a futures row is the live number giving way to the EOD number; a non-futures row
(present in universe_technicals, absent from v8_metrics) keeps the values it always had, only the
price moves from the CSV figure to the EOD close.

## VERIFY block

**1. Two reads in the same trading day are byte-identical.** Measured as an md5 over every row of
the base query, ordered by symbol (the sandbox cannot call scorr.in; the URL pair is below).

| Query | 1st read | 2nd read | Same? |
|---|---|---|---|
| old `_BASE_SQL` | 10:43:34 `8667cdb1…` (1,774 rows) | 10:46:41 `ff99606b…` (1,775 rows) | **no** — v8_metrics rewritten 10:45:14, a TC tick at 10:45:15, one more qualification |
| new, before the v8_qualified move | 10:46:27 `576feea3…` | 10:48:03 `576feea3…` | yes |
| new, final | 10:49:07 `bef46f00…` (1,773 rows) | 10:49:38 `bef46f00…` | yes |

The row count is 1,773 = one row per scored symbol. After the close the EOD writers legitimately
move the response once each (universe_technicals ~20:35, gvm nightly 22:00, the raw_prices EOD load,
tc_scanner_score_daily, and v8_qualified at the day roll).

**2. EXPLAIN shows no live table.** `EXPLAIN (ANALYZE, BUFFERS)` on the count query with a filter:
13.2 ms; on the row query (sort + LIMIT 50): 30.0 ms. Tables in the plans: gvm_scores,
universe_technicals, raw_prices, screener_raw, v8_qualified, v8_paper_pivots,
tc_scanner_score_daily, sector_ratings, earnings_calendar. Not present: v8_metrics,
tc_universe_ticks, intraday_prices.

**3. A symbol in universe_technicals but not in v8_metrics keeps its EOD values.** VAML and TMCV
above: identical before and after on every technical field (1,568 of the 1,773 scored symbols are in
this position; 205 of the 207 futures names have both rows).

Unit test (fake cursor): the count and row SQL contain no `v8_metrics`, `tc_universe_ticks`,
`intraday_prices` or `signal_date = CURRENT_DATE`; the seven range filters are `ut.*`; the order
carries the symbol tie-break; the page calls `eod_rvol_pair_batch` and never `live_rvol_batch`;
`/api/v12/filters/meta` reads universe_technicals.

## For the live check

- `https://scorr.in/api/v12/screen?size=5&sort_by=symbol&sort_dir=asc` twice, minutes apart during the session → identical bodies; `price_date`, `technicals_date`, `tc_asof`, `rvol_asof` all dated; `vol_ratio` null.
- `https://scorr.in/api/v12/screen?rsi_weekly_min=40&size=1` → `total` 1,062 (EOD rows with weekly RSI ≥ 40 on 17-Sep).
- `https://scorr.in/api/v12/filters/meta` → `rsi_weekly` bounds from universe_technicals.

## Seen while reading — separate cards, not changed here

1. **Duplicate rows when a symbol qualifies in two V8 baskets on one day.** The v8_qualified join is
   one row per (symbol, basket); LTM and MPHASIS qualified twice on 17-Sep, so the old query returned
   1,775 rows for 1,773 symbols. 16-Sep had no such pair, so today's 1,773 is clean, but the shape
   allows it. Fix: one row per symbol (LATERAL LIMIT 1, or baskets aggregated).
2. **`_UNI_COLS["price"]` is the screener CSV price**, on the universe filter vocabulary that
   v12_backtest also validates against. Under the EOD ruling it should be the raw_prices close.
3. **HDFCBANK's live weekly RSI read 3.25 at 10:35** against an EOD 35.83 (RELIANCE 18.59 vs 34.33).
   The intraday weekly-RSI computation in v8_signal_writer looks wrong, not just fresh. V8 surfaces
   still read it. Worth its own check.
