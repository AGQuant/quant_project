# cc#2205 — Option IV solve-on-read: store the raw day-end slice, solve when a page is opened

**Card:** cc#2205 P1 ENGINE (founder ruling 17-Sep-2026 ~18:30 IST: IV is an answer, not data. Do not store it,
do not back it up, clean the DB.) Supersedes cc#2202 (A4-ALL stored re-solve, nothing was written) and cc#2204
(cancelled). Steps 8-9 (the two DROPs + the app_config delete) are GATED on Fable's line `GO DROP cc#2205`.

## Pushes (all on main, git clock IST)

| Push | SHA | Landed | What |
|---|---|---|---|
| 1 | 8b03fc0 | 18:52:30 | `option_eod_slice` (CREATE IF NOT EXISTS, no iv, no oi) · `solve_iv()` the one solver · `ingest_date` writes raw rows only · solve-on-read in `option_ivp` + the cc#1994 gap map · the flag-triggered measure-then-fill job |
| 1b | eced34f | 18:59:46 | the parity-anchor tie rule stated in `solve_iv` (lower strike when two are equidistant from spot) |
| 2 | this commit | see the room line | retention at the end of every tick (130 sessions) · A4 dead code + the fill job + the transitional fallback out · tests · docstring cleanup |

## GATE 0 — every reader, before any code (room log 7075)

| Reader | Columns read | What happened to it |
|---|---|---|
| `option_ivp.atm_iv_history` | trade_date, option_type, strike, iv, spot, expiry (band in SQL) | reads `solved_rows()`; band applied in Python after the solve |
| `option_ivp.bucket_skew_history` | same six | same |
| `option_ivp.chain_tags` (callers: `deriv_metrics` index + stock chain) | via the two above — two fetches per request | ONE fetch + ONE solve per request, handed to both helpers |
| `option_ivp.ivp_and_fair_value`, `strike_fair_tag` | via the two above; no caller in the repo | unchanged, read the cache |
| `deriv_metrics._stored_iv_gap_map` (cc#1994; callers: both chain branches) | trade_date, strike, option_type, iv of the latest session | `option_ivp.latest_session_iv` — the same solve, same cache |
| writer side (`option_iv_history`): `_ensure_tables`, `_retire_superseded_expiry_rows`, `ingest_date`, `a4_run` | — | slice DDL; retire ported to the slice; raw-only ingest; A4 removed |
| `scheduler._bg_option_iv_daily` → `run_forward_tick` | — | unchanged trigger; the tick now also applies retention |
| `worker/**` | — | zero mentions, no STOP |
| `oi` | — | no reader anywhere → the slice has no oi column |
| `_bs_price_vec` / `_bs_iv_vec` | only `tests/test_option_iv_a4.py` | removed with the test |

## The design

- **One table, raw facts only.** `option_eod_slice (symbol, trade_date, expiry, strike, option_type, close, spot,
  is_settlement, loaded_at)`, PK on the first five, index (trade_date, symbol). The settlement-price substitution is the
  one decision kept at load time (it is a fact about the bhavcopy row). No iv column, no backup, no gated re-solve.
- **One solver.** `option_iv_history.solve_iv(rows)` — pure. Per (trade_date, expiry) group: the parity forward
  `F = K_atm + (C_atm - P_atm) * e^(R_FREE*T)` off the strike nearest spot with both legs priced (two equidistant →
  the lower one), carry fallback `spot * e^(R_FREE*T)` when no such pair, then the vectorised Black-76 bisection
  (`_b76_iv_vec`, [1e-4, 5.0], 64 iterations). Exactly the cc#2031 A2 load-time solve, now run on read.
- **Solve on read, once per symbol per day.** `option_ivp.solved_rows(cur, symbol)`: one `MAX(trade_date)` lookup
  decides hit or miss; a miss fetches the symbol's rows once, solves once, caches under a lock keyed
  (symbol, max trade_date), LRU of 64 symbols. The G3 band (0.03–1.50) and spot > 0 are applied after the solve,
  where the SQL WHERE applied them. `chain_tags` does one fetch + one solve per request and hands the rows to both
  helpers. The method (LEVEL / SHAPE / WINDOW / BANDS rulings) is untouched — only where iv comes from changed.
- **Retention by construction.** At the end of every forward tick, rows older than the 130th most recent
  trade_date in the slice are deleted (plain DELETE). option_ivp's window is 120 sessions; 10 of margin.
- **Fill.** One server-side job (flag `option_eod_slice_fill`, the same app_config pattern the backfill used) measured
  first, then filled from the old table's RAW columns (never its stored iv), outside market hours. Width was decided
  by the measurement the card asked for, not by a guess.

## The measurement (job runs at 18:55:13 and 19:01:41 IST, 28.5 s / 35.8 s, real rows, 226 symbols, 130 sessions 26-Feb → 16-Sep)

- **Parity** (NIFTY / BANKNIFTY / RELIANCE all sessions + every symbol's rows since 15-Sep): 33,030 rows compared,
  32,959 equal to 1e-9, null flips 0. The 71 that differ (max 0.0179) are the three sessions where the futures
  close sat exactly midway between two priced strikes — CHOLAFIN 15-Sep (spot 1790, 1780/1800), FEDERALBNK 16-Sep
  (342.5, 340/345), TATAELXSI 16-Sep (3375, 3350/3400). The load-time solve took the anchor in bhavcopy row order
  (not reproducible from the table); `solve_iv` takes the lower strike and says so. No group has two spots, no
  split loads (checked by SQL). The gate STOPPED run 1 on this; run 2 was forced after the diagnosis (room log 7078).
- **Width**: for an ATM±5 chain request on every symbol, ATM±5 history returns 3,673 tags vs 4,470 from ATM±10
  (of 4,972 cells) — a 17.83 % loss, far over the 2 % rule → the slice is filled at ATM±10: 1,148,528 rows
  (ATM±5 would have been 609,697).
- **ATM |CE−PE| gap** (median vol points, stored → on read): NIFTY 0.0 → 0.0, BANKNIFTY 0.0 → 0.0, RELIANCE 0.0 → 0.0,
  NIFTYNXT50 31.04 → 0.0, SAIL 4.97 → 0.0; median of the 226 symbol medians 3.546 → 0.0 (cc#2202's dry run: 3.60 → 0.000).
  **NIFTYNXT50 is odd**: its options barely trade. On 16-Sep, 38 of its 42 rows carry carried/theoretical closes that all
  solve to a flat ~0.59 (a 59 % flat surface across 21 strikes is not a market); only four strikes (70000/71000/71500 CE,
  70800 PE) have real closes. The parity forward built from the fictitious pair at 70900 lands ~1,850 points above spot,
  so the real closes sit below intrinsic and floor at 1e-4. Its 0.0 gap is a property of the fake closes, not of
  liquidity — its tags are noise.
- **Tag diff** (ATM cell, 226 symbols, stored-iv history → on read): CHEAP 120 → 127, FAIR 88 → 80, EXPENSIVE 8 → 9,
  no tag 10 → 10. A change was expected (that was cc#2202's point); this is its size.
- **Latency** (`chain_tags`, 20 symbols, real table): cold p50 43.9 ms / p95 61.5 ms (fetch + solve),
  warm p50 7.0 ms / p95 9.4 ms (cache hit).
- **Slice after the fill**: 1,148,528 rows, 130 sessions, 226 symbols,
  2026-02-26 → 2026-09-16 (matches the fill's own SELECT: rows_130 = 1,148,528 in the source at 18:47 IST).

## What is NOT done here (gated / out of scope, per the card)

- Steps 8-9: `DROP TABLE option_iv_daily_bak_cc2031; DROP TABLE option_iv_daily;` and `DELETE FROM app_config WHERE
  key='option_iv_a4_resolve'` wait for `GO DROP cc#2205` after one clean 23:05 tick has written the slice
  (ENGINE_LIVENESS evidence posted separately). Sizes at 18:30 IST: option_iv_daily 689 MB, backup 222 MB, DB 5789 MB.
- The step-8 re-grep for `option_iv_daily` on main matches only the scheduler job name `_bg_option_iv_daily` /
  `bg_option_iv_daily` (a registry row name, kept; step 9 updates its notes) — no table reference remains in code.
- option_chain index bloat, VACUUM FULL, older bhavcopies: out of scope, untouched.
- `option_iv_backfill_status` / `option_iv_expiry_fix_log`: kept (still used / evidence).

## Verify (Fable)

Diff at the three SHAs; `SELECT COUNT(*), COUNT(DISTINCT trade_date), COUNT(DISTINCT symbol), MIN(trade_date),
MAX(trade_date) FROM option_eod_slice`; after the 23:05 tick `MAX(trade_date)` = 17-Sep and `scheduler_master`
`bg_option_iv_daily.last_status = ok`; open one stock chain and the NIFTY chain on /m and confirm the tags render
(CC's own check below); the tag-diff table above is the expected change, not a defect.
