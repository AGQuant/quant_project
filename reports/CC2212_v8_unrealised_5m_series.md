# cc#2212 — v8 unrealised 5-min series: table + tick snapshot + endpoint, and the /m/v8 unrealised sheet as a 100-bar 5-min line

Spec: session_log 48588 (V8_APP_UNREAL_5MIN_100_BARS_V1). Supersedes cc#2211 items 1–3 (the daily 100 BARS pill);
cc#2211 items 4–6 (note removals) stand and are still in the file.

## What changed

| where | change |
|---|---|
| `v8_unrealised_daily.py` (same module as the daily series; `main.py` already includes its router, so `main.py` is untouched) | `v8_unrealised_5m(ts timestamptz PK, unrealised, long_unrealised, short_unrealised, open_n, computed_at)` created in code (`_ensure_5m_table`, the cc#2097 CREATE-only pattern). `bar_ts(now)` floors any clock to the IST 5-min boundary (09:17:43 → 09:15:00+05:30). `snapshot_unrealised_5m(conn, ts)` reads `book_canon(conn)` (rule 13, the one formula) and UPSERTs on `ts`; a canon error is raised, never stored as a NULL row. `GET /api/v8/unrealised_5m/series?slice=all|long|short&n=100` → `{points:[{ts, value}], n, n_max, first_ts, last_ts, as_of, kind, slice, bar}`; `n` clamped to 1..100 server-side (`clamp_n`); rows with a NULL figure are excluded before the cap; `ts` served as IST ISO (`+05:30`). |
| `scheduler.py` | `_bg_v8_unrealised_5m(now)`: registry-gated (`bg_v8_unrealised_5m`, `_Skip.disabled()` otherwise), own re-entry guard, dispatched from the SAME market-hours `m % 5 == 0` sweep as `_bg_v8_paper_exit`, right after it. Both are spawned onto the pool, so the job gives the exit pass 2 s to start and then waits (bounded, 90 s) while `_v8_paper_exit_running` is set — the bar reflects that tick's exits. The sweep's IST clock is passed in as `now`, so a wait never moves the bar. The 15:30 tick is the close bar (`_is_market_hours` is inclusive of 15:30), i.e. the last bar of a session equals what the 15:35 daily snapshot stores from the same canon. `_bg_v8_paper_exit` itself is untouched. |
| `scheduler_master` | row `bg_v8_unrealised_5m` inserted (module scheduler.py, cadence `_is_market_hours(now) and _is_trading_day(now.date()) and (m % 5 == 0)`, service app, category scheduler_loop, active true, added 2026-09-18) — before the push, so the job is live from its first tick. |
| `mobile/v8.html` | `URLS.unreal5m` replaces `URLS.unrealSeries` (the daily endpoint is no longer referenced by this page at all — no fallback is possible). `dlSeriesLoad` fetches `/api/v8/unrealised_5m/series?slice=<all|long|short>&n=100` for kind `unreal`. `dlChartSheetHtml` unrealised branch: no window row; the points map to `dlChartSvg`'s shape (`date` = bar ts, `net_cum` = `gross_cum` = value), rebase 0, x by tick order; sub-line `Last N bars · 5-min · HH:MM D Mon – HH:MM D Mon`; empty state `No 5-min history yet — it starts accumulating from the next market tick.` (never the daily series, never resampled). Live hollow end-point: `dlLiveNewerThan(lastTs)` compares the newest `cmp_at` mark across `STATE.book.positions` (`D Mon HH:MM`, /api/mobile/v8book) with the last bar; only a newer mark draws the point, otherwise `dlChartSvg` gets a null slice and draws none. `dlWindowSlice` is back to its cc#2097 form (the `100b` branch is gone); `dlChartOpen` sets `DL_WINDOW='all'` (realised/net unchanged; the unrealised sheet has no windows). Net/Gross focus pills stay on both sheets. `dlChartSvg` untouched. |
| `tests/test_v8_unrealised_5m.py` | 7 DB-free tests: bar flooring (naive IST and aware UTC), the upsert's SQL + params, a canon error writes nothing, `clamp_n`, last-100-ascending with NULLs skipped, fewer bars / empty, the endpoint's column pick + cap. |

## Validation

- `ast.parse` on `v8_unrealised_daily.py`, `scheduler.py`, the test file; `node --check` on both inline blocks of `mobile/v8.html`.
- `pytest tests/test_v8_unrealised_5m.py`: 7 passed.
- Playwright (375×812, the real page with the PWA-injected `scorr_position_row.js`, mocked APIs, sheets opened by the KPI well button) — 18 checks, PASS:

| case | checks |
|---|---|
| A. 100 bars, newest CMP mark = last bar 15:30 | one request `unrealised_5m/series?slice=all&n=100`, none to the daily table; no window pills, Net/Gross present; sub-line `Last 100 bars · 5-min · 13:35 17 Sep – 15:30 18 Sep`; one svg, polyline of 100 points; no hollow live point, axis labels `17 Sep` / `18 Sep`; no notes / windows / CAGR / 9.5 px element; info title + three rows; Long slice asks `?slice=long&n=100` with no info block; no page errors |
| B. last bar 15:25, mark 15:30 | hollow live point drawn, right label `live` |
| C. 30 bars | 30 points, `Last 30 bars · 5-min · 13:05 18 Sep – 15:30 18 Sep` |
| D. no bars | the empty-state sentence, no svg, no pills, no `Live unrealised` fallback |
| E. realised + net | SINCE START / 1W / 1M / 3M, `Since <date> · 150 trading days · cumulative by exit date`, Return / CAGR foot + its note, live end-point on the cumulative curve, three info rows and no note, 1W → `Change over this window`; net: title + rows, no note; no page errors |

Screenshots looked at: `cc2212_375_unreal.png`, `cc2212_375_unreal_empty.png`.

## ENGINE_LIVENESS (session_log 13829) — first-run evidence

Built and registered is not live. The table has no rows until the first market tick after the deploy (no honest
source to backfill 15–17 Sep from). First-run evidence is posted on the card after the first ticks of the
18-Sep session: `SELECT count(*), min(ts), max(ts) FROM v8_unrealised_5m` (expected ~76 rows for a full session,
09:15 → 15:30 at 5-min spacing) plus the `scheduler_master` row's `last_status` / `last_run_at`. Parity check
after 15:35: `v8_unrealised_5m.unrealised` at 15:30 = `v8_unrealised_daily.unrealised` for 2026-09-18 (same
`book_canon` read, marks frozen after close).

## Live check (the sandbox cannot reach scorr.in)

/m/v8 → Unrealised well → chart: until 09:20 IST the sheet shows the empty-state sentence; from the second tick a
5-min line grows to the left, no pills, `Last N bars · 5-min · …` under the title. Realised and Net Total sheets
unchanged.
