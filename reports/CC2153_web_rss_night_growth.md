# cc#2153 — READ-ONLY diagnostic: web RSS 270 → 737 MB overnight, and which job it is

Date: 17-Sep-2026. No code, job or config change. Every number is from SELECTs on production plus
the repo as landed.

## 0. The clock, first

`session_log.session_ts` and `ops_log.session_ts` are **naive UTC**, not IST. Proof: the
"boot baseline" probes at raw 04:11–05:24 on 17-Sep land exactly on this seat's own pushes at
09:41–10:54 IST; `universe_shrink` rows carry an `ist` field 5 h 30 ahead of their `session_ts`.
So the three probes the card cites are:

| Probe id | raw | **IST** | RSS |
|---|---|---|---|
| 47494 | 16-Sep 15:18 | 16-Sep **20:48** | 269.8 MB |
| 47504 | 16-Sep 19:18 | 17-Sep **00:48** | 317.1 MB |
| 47574 | 16-Sep 23:18 | 17-Sep **04:48** | 737.4 MB |

The +420 MB lands between **00:48 and 04:48 IST** — the night chain, not the evening. The card's
suspect (investment_score_eod, chained after gvm_recompute) IS inside that window; the chain runs at
01:30 IST, not 20:35.

## 1. Timeline against the probes (IST)

| Window | Jobs that ran (ops_log + scheduler_master) | RSS at the probe |
|---|---|---|
| boot 16:48 → 20:48 | result-analysis regen passes (all "unchanged"), 20:00 Yahoo symbol resolve + NSE price heal, two feed reconnects | 274 → **269.8** (flat) |
| 20:48 → 00:48 | 21:10–21:16 `fetch_universe_reco_news` (357 s, 2,451 items parsed), 23:00 F&O EOD ingest, 00:31 `fetch_stock_news` (1,014 parsed, 50 s), two reconnects | **317.1** (+47, threads 16 → 22) |
| 00:48 → 04:48 | 01:00–01:04 `yahoo_daily_sync` (272 s) · 01:05 lot sync · 01:15 qb_eod · 01:20 MF averages/scores · **01:30 `gvm_recompute`** (`bg_gvm` 61 s — its first step is `momentum_daily.compute_momentum`) → chained `screeners_eod` 1.2 s · `gvm_coverage_guard` · `tc_scanner_score_daily` (import error, 3 ms) · **`investment_score_eod` 38.6 s** · 01:35 MF derived · 01:45/01:47/01:55 pivots (1,850) · 01:50–01:52 retention purges · 02:00 paper exit EOD · **02:05 `universe_technicals` 37.9 s (1,773)** · 02:10–02:15 rvol profiles · 02:20/02:40 gvm backfill (skipped) · 03:10–03:21 `fetch_universe_reco_news` twice (catch-up) · 04:11 reconnect | **737.4** (+420; +87 modules, +4,379 function objects; no module global over 1 MB) |

**Same shape every night, weekends included** (all IST):

| Night | From → to | RSS |
|---|---|---|
| 12/13-Sep | boot 23:10 → 03:10 | 274 → 599 (+325) |
| 13/14-Sep (Sunday, no market jobs) | boot 23:37 → 03:37 | 271 → 617 (+346) |
| 14/15-Sep | 00:54 → 04:54 → 08:54 | 268 → 512 → 615 |
| 15/16-Sep | boot 23:43 → 03:43 | 274 → 580 (+306) |
| 16/17-Sep | 00:48 → 04:48 | 317 → 737 (+420) |

A Sunday night with no market or V8 job shows the same climb, so the cause is in the daily night
chain. Boot baselines of 400–745 MB (15-Sep 21:49–22:07, 16-Sep 14:11, 17-Sep 09:41) are probes
taken 120 s after a redeploy while cc#841's `SCHEDULER_CATCHUP` was already re-running a missed
job — the same retained-peak effect, not a boot leak.

## 2. The suspect, with the measured evidence

**`momentum_daily._load_prices()`, the first step of the 01:30 GVM chain**, reads the whole price
table in one call:

```
SELECT symbol, price_date, close, adjusted_close, volume FROM raw_prices ORDER BY symbol, price_date
```

`raw_prices` holds **1,926,201 rows (801 MB on disk)**. `pd.read_sql_query` receives the numerics
as Python `Decimal` objects (object columns, ~100 bytes a cell), so the frame plus its derived `px`
column is a **500–700 MB transient** on top of the baseline. Python and glibc do not hand that back
to the OS when the frame is dropped, so RSS keeps the high-water mark — exactly the "+400 MB that
only a redeploy resets". Two probe facts fit: the gc census on the 16-Sep 14:39 IST probe (id
47350) shows **224,541 live `Decimal` objects** (Decimal-heavy frames are real in this process),
and the 04:48 probe lists **no module global over 1 MB** — nothing is leaked, the memory is a
retained peak. The momentum math needs 1,095 days back (`d3y`), i.e. **1,177,461 rows** — 39% of
what is read; and it needs floats, not Decimals.

**`investment_score_eod` (the card's suspect):** 38.6 s for 1,773 symbols. Yes, `compute()`
re-fetches the NIFTY50 3-year bars **on every call** (`invest_check_v2.py` line 508,
`cur.execute(_BARS_SQL, (BENCHMARK,))` inside `compute`): 738 rows × 1,773 symbols = 1.3 M rows a
night. That is wasted DB work, but each fetch is sequential and dropped before the next, so it
cannot set a 420 MB high-water mark. A runtime RSS measurement of `compute()` on a 20-symbol sample
is not possible from this seat (no database access to run it); the static read is the evidence.
Verdict: **not the RSS driver**; still worth the one-time benchmark load.

Not separable by the 4-hourly probes but ranked below on working-set size: `yahoo_daily_sync`
(272 s, streamed upserts), `universe_technicals` (per-symbol lookbacks), the MF nightly (8,581 NAV
rows). Ruled out: `fetch_universe_reco_news` — its 21:10 run sits inside the +47 MB window.

## 3. Railway limit and headroom

The memory limit is not in the repo or the environment (no `MEMORY_*` variable; `railway.*.json`
carry no resource block), so it cannot be read from this seat. Railway's per-service default is
8 GB on Hobby and 32 GB on Pro; at 737 MB the headroom is at least 7.3 GB on either. The risk on a
day with no redeploy is a slow stack of retained peaks (the 12-Sep series also climbed 657 → 738
during market hours), not an imminent OOM. The founder can read the exact cap on the service's
Resources tab.

## 4. Options (not executed — the founder reads this first)

1. `momentum_daily._load_prices`: read only the 3-year lookback (`price_date >= target − 1095 d`,
   −39% rows) and cast in SQL (`close::float8`, `adjusted_close::float8`, `volume::float8`) so
   pandas gets float64 columns instead of `Decimal` objects — together roughly a 6–8× smaller
   frame; or stream per symbol (peak ÷ ~1,800). Momentum math untouched.
2. `investment_score_eod`: load the benchmark bars once and pass them in — an optional
   `bench_bars=None` argument on `compute()` with the default path unchanged, so the founder-locked
   engine's math is not touched (session_log 27979).
3. Cheap mitigation: `MALLOC_ARENA_MAX=2` on the web service and a `malloc_trim(0)` after the
   night chain, so freed arenas go back to the OS.
4. Longer term: run the night chain in the worker/night window, not inside the web process.
