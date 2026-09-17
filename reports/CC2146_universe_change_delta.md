# cc#2146 — QB Universe builder: CAT_4 Change & Delta wired (7 locked rows), gvm_history self-joins

Step 4 of the QB Universe sprint. Seven rows from `gvm_history` only, at date offsets, in the same
query as CAT_1/CAT_2. No table, no job, no cache.

## Two deviations, stated up front
1. **Sector GVM change (90d) weights the offset rows with TODAY's market caps.** `gvm_history`
   carries no market cap and no table keeps one at past dates (`mcap_rank_daily` holds a single
   rank_date), so "the same expression as cc#2145 evaluated on the offset rows" cannot be built as
   written. The 90-day sector mean uses the mcap-weighted expression with constant (current)
   weights over the members that have a 90d row — the change is then a pure score movement.
   Examples: Pharma - Formulations 6.982 today, **+0.229** vs 90d (10 of 15 members resolved);
   IT - Small **−0.427** (6 of 32); Shipping & Maritime **+0.490** (4 of 7). The resolved-member
   count is in the query (`members_90d`) so a thin basis is visible, not hidden.
2. **As-of rows must sit within 45 days of the offset** (`ASOF_GRACE_DAYS`). The card's rule is
   "latest score_date ≤ D − N"; taken literally, 722 symbols would "resolve" at 180d from yearly
   snapshots as old as 2010, which is not a 180-day change. With the grace window 30d/90d resolve
   exactly as the card measured (737 / 729) and 180d resolves for 12 symbols from a 2026-03-01
   snapshot; the option stays **disabled** until the daily era is 180 days deep (see item 3).

## Backend (`qb_universe_builder.py`)
- `_SCORED_CTE` gains: `LEFT JOIN LATERAL` as-of lookups at −30 / −90 / −180 days (index-bound on
  `idx_gvm_history_symbol_date (symbol, score_date DESC)`), giving `gvm_change_30d/90d/180d`,
  `m_change_30d/90d`, `g_change_90d`, `v_change_90d` (2 dp; NULL = absent, cc#1822); a `uni90 →
  sect90` universe pass for `sector_gvm_change_90d` (3 dp); `verdict_migration` = today's verdict
  vs the row **N snapshots back** (distinct score_dates, never days; ordinal Weak < Average < Good <
  Excellent from `gvm_nightly._verdict`) → `upgraded | unchanged | downgraded`, NULL when fewer
  than N snapshots exist; `gvm_pos_snapshots` = how many of the last N snapshot-to-snapshot changes
  were positive, NULL when fewer than N changes exist.
- Seven filters appended to `_FILTER_ORDER` after CAT_2: `gvm_change_min/max` + `gvm_change_days`
  (30 | 90 | 180 — the column follows the window), `m_change_*` + `m_change_days` (30 | 90),
  `g_change_*`, `v_change_*`, `sector_change_*`, `verdict_migration=` (repeated) +
  `verdict_migration_n`, `gvm_consistency_k` + `gvm_consistency_n`. The snapshot windows and the
  grace are bound on every query (defaults 5 / 5 / 45) because the CTE always needs them; the
  response echoes them as `delta_windows`.
- New `GET /api/qb/universe2/coverage` (item 3, measured on every call): per window `resolves`,
  `of`, the as-of date span, `enabled`, `reason`; `daily_history_floor` (2026-05-30),
  `daily_era_days` (109), `snapshots_last_90d` (90). A window is **enabled only when the daily
  era is at least that deep** — 180d is offered disabled with the reason "daily history starts
  2026-05-30 (109 days back); 180-day changes resolve from 2026-11-26". Never a silent empty set.

## Page (`scorr_qb_universe.html`)
- "Change & Delta — Coming" is replaced by a live block of 7 rows in the CAT_1 pattern. GVM change
  and M Score change carry an **"over" window selector** (disabled windows greyed with the reason
  as a tooltip; a row whose default window is disabled moves to the first enabled one); Verdict
  migration has the three states + "vs the snapshot N snapshots back"; Consistency reads "GVM rose
  in at least K of the last N snapshots".
- Every row: **POINT-IN-TIME** (green). Rows on a 90d window (and the block header) add
  **LIMITED HISTORY FROM 2026-05-30** (amber). Every duration row prints the coverage line
  ("resolves for 737 of 1,773 symbols (as-of row on 2026-08-17 …)"); the snapshot rows print the
  snapshot note ("counts snapshots (distinct score_dates), never days — 90 in the last 90 days").
- Header: "n active of 18 live · 40 in the locked set" (6 + 5 + 7). Four categories stay "Coming".
  Result table gains the applied CAT_4 columns, window rows labelled with their window.

## Verify (the card's own list, on real rows)
- **Hand-computed 30d changes match to 2 dp** — `gvm_history` row on 2026-08-17 vs today:
  TCS 5.67 → 5.41 = **−0.26**; RELIANCE 5.14 → 4.77 = **−0.37**; INFY 6.10 → 5.83 = **−0.27**
  (ABCAPITAL 8.07 → 7.03 = −1.04). The CTE returns the same three values.
- **180d disabled** with the coverage reason; 30d / 90d rows read **737 / 729 of 1,773**
  (as-of rows 2026-08-17 / 2026-06-18); 180d would resolve for 12 (2026-03-01 rows) — reported,
  not offered.
- **Consistency K=3 of N=5, hand-verified**: AJAXENGG's last five changes +0.17, +0.10, 0.00,
  −0.21, +0.06 → 3 positive → `gvm_pos_snapshots = 3` (13 symbols in the universe reach ≥ 3 of 5;
  738 have five changes at all; RELIANCE reads 0 of 5 — flat for five days then −0.06).
- **verdict_migration = upgraded (N = 5)**: 20 symbols in the universe, only symbols whose ordinal
  rose; named: ABCAPITAL Average → Good (also APLLTD, APOLLOTYRE, BALKRISIND Weak → Average).
  641 unchanged, 76 downgraded, 1,036 have no 5th snapshot (absent, not failed).
- **Alone + step counts flow**: Nifty 500 (490; 30d resolves for all 490, 90d for 486) — GVM
  change ≥ 0.5 over 90d alone **112**, consistency ≥ 3 of 5 alone **10**, AND → **3**; over 30d
  the GVM row alone is 45; upgraded (N=5) in the pool 11. Same-day re-runs return the same numbers
  (one EOD snapshot).
- **EXPLAIN ANALYZE** (Nifty 500, GVM change 90d ≥ 0.5 AND consistency ≥ 3 of 5): **execution
  33.7 ms, planning 3.8 ms**; every `gvm_history` touch is an Index Scan on
  `idx_gvm_history_symbol_date` (490 probes per lateral in the pool pass, 1,773 in the universe
  90d pass). No cache.
- Fake-DB unit test of the endpoint (window column selection, list/ordinal params, the CTE
  fragments, the bound windows on every count query, a bad window falling back to 30); Playwright
  on the real page (dark 1280 / light 1280 / dark 375): 7 rows, 7 badges, 180d disabled with the
  reason, coverage lines, window switch → badge + 729 line, request
  `gvm_change_min=0.5&gvm_change_days=90&gvm_consistency_k=3&gvm_consistency_n=5`, rows "112 of
  490 pass on its own" / "10 … | 3 remain after this step", result 3, columns, migration request
  `verdict_migration=upgraded&verdict_migration_n=5` → 11; no overflow, no page errors;
  `node --check` clean; 0 literal fallbacks. Screenshots looked at (VISUAL_VERIFY_GATE_V1).

## Not touched
`gvm_history` writes / `gvm_nightly`; `v12_backtest.py`; the Alpha Multicap preset string
(dGVM(180d) > +0.5 stays hardcoded there — this row makes it editable here once 180d resolves);
the pool section; CAT_1 / CAT_2 rows. Out of scope: backfilling gvm_history so 180d resolves.
