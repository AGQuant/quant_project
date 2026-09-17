# cc#2147 — QB Universe builder: CAT_3 Price & Momentum + CAT_7 Quality / Valuation / Size / Ownership

Date: 17-Sep-2026. Files: `qb_universe_builder.py`, `scorr_qb_universe.html`, this report.
Page: `/qb/universe2` (also step 1 of `/quant-basket`). Same single query as cc#2143–2146, no job, no table.

## What is live now

**CAT_3 Price & Momentum — 3 live rows, 1 shown as Coming (V2)**, read from `universe_technicals`
at its latest `score_date` (an EOD table, written the evening before that date).

| Row | Column | Note |
|---|---|---|
| Price change over 1W / 1M / 1Y / 3Y | `week_return`, `month_return`, `year_return`, `return_3y` | ONE row + window selector. 3M and 6M are rendered but DISABLED ("V2 -- needs raw_prices derivation"). Default window 1M. |
| 52-week range position | `week_index_52` | See the label note below. |
| Return vs index (52W) | `return_52w_vs_index` | |
| Consecutive up months | — | Coming (V2), cc#2148 |

Every CAT_3 row carries POINT-IN-TIME (green) plus **LIMITED HISTORY FROM 2026-07-03** (amber).
The date is measured by `/api/qb/universe2/coverage` (`technicals_floor`), not typed in.

**Label note — `week_index_52` is a range position, not a distance from the high.** The column is the
0–100 position of the last close inside the 52-week range (0 = at the 52W low, 100 = at the 52W high).
The spec calls the row "distance from 52W high"; that name would read the number backwards, so the row
is labelled "52-week range position (0 = at 52W low, 100 = at 52W high)". RELIANCE and VEDL sit at 1.2
(near their lows); INDUSINDBK at 64.2. Nothing is inverted or rescaled.

**CAT_7 Quality / Valuation / Size / Ownership — all 6 rows**, CURRENT VALUE ONLY (amber, with the
screener load time in the group header).

| Row | Source | Note |
|---|---|---|
| Market-cap rank | `mcap_rank_daily` at its latest `rank_date` (2026-09-16) | never `input_raw` |
| PE vs 10Y avg PE | `screener_raw.pe / historical_pe` | `historical_pe` maps from the CSV header "Historical PE 10Years", so the label says 10Y, not 5Y |
| ROCE % | `screener_raw.roce` | |
| Debt to equity (max) | `screener_raw."Debt to equity"` | max-only; BFSI rule imported from v12 (below) |
| Promoter holding % (min) + pledge % (max) | `"Promoter holding"`, `"Unpledged promoter holding"` | ONE row, two numbers; pledge is derived (below) |
| Dividend yield % (min) | `screener_raw.dividend_yield` | |

Sources on this path: `universe_technicals`, `screener_raw`, `mcap_rank_daily`, `gvm_scores`,
`gvm_history`. A grep of the builder for `v8_metrics`, `tc_universe_ticks`, `intraday_prices`,
`cmp_prices`, `cmp_resolver`, `fyers`, `yahoo` returns nothing.

## Decisions taken on the card

1. **BFSI rule = v12's, imported, not re-declared.** `from v12_endpoints import _BFSI_PATTERNS,
   _UNI_LEVERAGE_KEYS`. When a leverage filter (`de`) is applied, the query appends
   `NOT (segment ILIKE '%bank%' OR … '%asset manag%')`, exactly what v12 does: banks, NBFCs,
   insurers, AMCs and exchanges are removed from the result, because their D/E is not a leverage read.
   The row says so ("banks / NBFCs / insurance auto-excluded") and the response carries `bfsi_excluded`.
2. **Pledge semantics confirmed as % of TOTAL shares.** In the screener CSV, "Unpledged promoter
   holding" is on the same base as "Promoter holding" (both % of total shares), so
   `pledge = GREATEST(holding − unpledged, 0)`. Checked on three known names:
   RELIANCE 50.48 / 50.48 → 0.00; JSWSTEEL 44.29 / 39.16 → 5.13; INDUSINDBK 15.82 / 9.05 → 6.77.
   Twelve rows in the load have unpledged > holding (a data quirk); they clamp to 0, never negative.
3. **Promoter is one row with two inputs** — holding at least X % · pledged at most Y % of total
   shares — sent as `promoter_min` / `pledge_max`; one key in the filter order (27 live in total:
   CAT_1 6, CAT_2 5, CAT_4 7, CAT_3 3, CAT_7 6).
4. **Reconstruct-from line.** Under the result: "Earliest date this universe can be honestly
   reconstructed". It is the latest floor across the active rows (CAT_4 = daily-era floor + window,
   CAT_3 = the technicals floor 2026-07-03). Any CAT_7 row makes it **today only** (amber) with the
   screener load time. The response carries `reconstruct_from` + `sources` so the Backtest step can
   read the same answer.

## VERIFY block

**1. Five symbols, value-for-value** (latest `universe_technicals` score_date 2026-09-17;
`screener_raw` loaded 2026-09-16 09:53:59; `mcap_rank_daily` rank_date 2026-09-16):

| Symbol | 1W | 1M | 1Y | 3Y | 52W pos | vs index | mcap rank | PE | hist PE | PE mult | ROCE | D/E | Promoter | Unpledged | Pledge | Div yld |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RELIANCE | -2.67 | -5.42 | -11.76 | 0.90 | 1.2 | -3.95 | 1 | 22.64 | 24.22 | 0.93 | 10.26 | 0.45 | 50.48 | 50.48 | 0.00 | 0.49 |
| TCS | -0.69 | -4.38 | -30.42 | -39.17 | 15.4 | -22.67 | 6 | 14.68 | 26.89 | 0.55 | 63.03 | 0.11 | 71.77 | 71.77 | 0.00 | 2.91 |
| JSWSTEEL | -4.23 | -2.87 | 11.99 | 53.64 | 63.2 | 20.75 | 28 | 25.22 | 19.63 | 1.28 | 10.96 | 0.99 | 44.29 | 39.16 | 5.13 | 0.58 |
| INDUSINDBK | -4.71 | -6.90 | 27.52 | -34.72 | 64.2 | 36.41 | 149 | 55.93 | 23.3 | 2.40 | 5.68 | 6.73 | 15.82 | 9.05 | 6.77 | 0.16 |
| VEDL | -4.85 | -2.51 | -44.50 | 8.34 | 1.2 | -35.54 | 113 | 9.15 | 3.24 | 2.82 | 16.08 | 0.66 | 54.72 | 54.72 | 0.00 | 13.33 |

The page shows the same numbers because the CTE selects these columns unchanged (2dp rounding only).

**2. BFSI gate.** In the Nifty 500 pool (490 scored names) 87 are BFSI by the rule
(Capital Markets 15, PSU Banks 13, Private Banks 13, Life Insurance 12, Housing Finance 10, NBFC 8,
MSME Finance 9, Exchanges & Ratings 5, Small Finance Banks 2). With D/E ≤ 1 applied:
raw pass 376 → with the rule 347 (5 names have no D/E). INDUSINDBK (Private Banks, D/E 6.73) is
removed by the rule, not by the gate; JSWSTEEL (0.99) stays; a non-BFSI name above the max
(ADANIENT 1.32) is dropped by the gate. HDFCBANK, INDUSINDBK and BAJFINANCE all leave by the rule.

**3. PE label** reads "PE vs 10Y avg PE (PE / historical PE)".

**4. Amber badge, load time, reconstruct line, disabled windows.** Confirmed in the Playwright
harness and looked at in screenshots (desktop dark + light at 1280, phone at 375, no horizontal
overflow, 375/375): the CAT_7 group header shows "CURRENT VALUE ONLY — NOT POINT-IN-TIME · loaded
2026-09-16 09:53"; every CAT_7 row carries CURRENT VALUE ONLY; with D/E applied the line under the
result goes amber "today only"; 3M / 6M appear disabled with the V2 reason in the selector.

**5. Determinism + EXPLAIN.** Nifty 500 · price change 1M ≥ 5 · D/E ≤ 1 returned 40 on two runs.
`EXPLAIN (ANALYZE, BUFFERS)` on the count query for that filter set: execution 28.9 ms, planning
5.3 ms, 403 rows into the filter (490 − 87 BFSI), 40 out; buffers 5,041 hit / 932 read; the
planner pushes the BFSI exclusion down into the `gvm_scores` scan.

## Real counts on the Nifty 500 pool (alone)

price change 1M ≥ 5: 53 · 52W position ≥ 80: 69 · mcap rank ≤ 100: 100 · PE mult ≤ 1: 232 ·
ROCE ≥ 15: 246 · D/E ≤ 1 (rule on): 347 · promoter ≥ 50 and pledge ≤ 0: 249 · div yield ≥ 2: 72 ·
1M ≥ 5 AND D/E ≤ 1: 40.

## For the live check (the sandbox cannot reach scorr.in)

- `https://scorr.in/api/qb/universe2/preview?pools=nifty500&price_change_min=5&price_change_window=1M&de_max=1&ops=price_change:AND,de:AND` → `count` 40, `bfsi_excluded` true, `reconstruct_from.today_only` true.
- `https://scorr.in/api/qb/universe2/preview?pools=nifty500&promoter_min=50&pledge_max=0&ops=promoter:AND` → `count` 249.
- `https://scorr.in/api/qb/universe2/coverage` → `technicals_floor` 2026-07-03, `screener_loaded_at` 2026-09-16 09:53:59.
- `https://scorr.in/qb/universe2` → Price & Momentum "3 of 5 filters" with the amber floor badge; Quality group header amber with the load time; header "0 active of 27 live".

## Not in this card

- Price change 3M / 6M and consecutive up months: V2, `raw_prices` derivation — cc#2148.
- `v12_endpoints._BASE_SQL` intraday leak — its own card (cc#2150).
