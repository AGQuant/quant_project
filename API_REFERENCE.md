# Scorr API Reference

Base URL: `https://quantproject-production.up.railway.app`

> **How this file works (cc#1984).** Everything written by a person — the conventions below, the
> per-router notes, the "three TC systems" warning — is maintained by hand and is never touched by
> tooling. The complete route inventory at the bottom, between the `BEGIN/END GENERATED ROUTES`
> sentinels, is produced from the code by **`tools/gen_api_reference.py`** and is rewritten on
> every run; do not edit it by hand. `python3 tools/gen_api_reference.py --check` exits 1 if the
> routes have changed since it was last generated, so drift is detectable rather than discovered.
> The census that prompted this (cc#1979) measured the old hand-maintained version at **27%
> accurate** — 179 of 665 live paths documented, 485 undocumented, 4 documented routes that no
> longer existed.

**Conventions**
- `🔒 admin` = requires `X-Admin-Token` header (validated by `_check_admin`; enforced only when `ADMIN_TOKEN` env is set).
- `🛡 guard` = additionally requires `DEPLOY_GUARD=true` (`_check_deploy_guard`).
- `🔑 auth` = page sits behind login middleware (`PROTECTED` in `scorr_auth.py`): `/`, `/dashboard`, `/cio`, `/cio2`, `/ask`, `/check`, `/sector`.
- The hand-written sections below cover the endpoints defined **directly in `main.py`** plus curated notes per router; routers mounted via `include_router(...)` live in their own files — see [Mounted Routers](#mounted-routers). For the COMPLETE and current list, including everything these notes do not reach, use the generated inventory at the end of this file.

---

## Pages (HTML)

| Method | Path | Handler | Serves | Notes |
|---|---|---|---|---|
| GET | `/` | `home` | `scorr_home.html` | 🔑 auth |
| GET | `/dashboard` | `dashboard` | `v8_dashboard.html` | 🔑 auth |
| GET | `/cio` | `cio` | `scorr_cockpit.html` | 🔑 auth — Max AICIO shell |
| GET | `/cio2` | `cio2` | `scorr_cio_dashboard.html` | 🔑 auth — GVM/multi-model |
| GET | `/ask` | `ask` | `scorr_ask.html` | 🔑 auth |
| GET | `/check` | `check` | `scorr_check.html` | 🔑 auth — Trade Check v3.4 |
| GET | `/intraday` | `intraday` | `scorr_intraday.html` | de-listed from nav, still reachable |
| GET | `/sector` | `sector` | `scorr_sector.html` | 🔑 auth |
| GET | `/fpc` | `fpc` | `fpc_v11.html` | Financial Planning Calculator |
| GET | `/scanners` | `scanners` | `scorr_scanners.html` | 3-tab screener page |

---

## System & Health

| Method | Path | Handler | Description |
|---|---|---|---|
| GET | `/status` | `status` | Service name, version, status |
| GET | `/api/health` | `health` | `{status, version}` liveness |
| GET | `/api/now` | `server_now` | IST clock, weekday, holiday/trading-day flags, market-open |
| GET | `/api/health/report` | `health_report` | Full diagnostic report (`build_health_report`) — checks passed/total, issues, warnings |
| GET | `/api/health/feeds` | `health_feeds` | Per-table freshness: latest date, record count, staleness (>7d = stale) across 13 data sources |

---

## Daily Digest & Market Data

| Method | Path | Handler | Description |
|---|---|---|---|
| GET | `/api/digest/daily` | `digest_daily` | Composite daily digest: global indices, domestic (NIFTY/BANKNIFTY live+EOD), ADR, support levels, pivots (rolling-5d), PCR trend |
| GET | `/api/daily/adr` | `daily_adr` | ADR series. Query: `days` (1–30, default 5) |
| GET | `/api/daily/pcr` | `daily_pcr` | PCR series. Query: `underlying` (default NIFTY), `days` (1–30) |
| POST | `/api/daily/compute_metrics` | `compute_daily_metrics_now` | 🔒 admin — compute & store ADR + PCR |

---

## V8 Engine & Metrics

| Method | Path | Handler | Description |
|---|---|---|---|
| POST | `/api/v8/run` | `v8_run` | 🔒 admin — run EOD V8 engine |
| POST | `/api/v8/run_for_date` | `v8_run_for_date` | 🔒 admin — run V8 for `target_date` (ISO) |
| POST | `/api/v8/run_signal_writer` | `v8_run_signal_writer` | 🔒 admin — run live 5-min signal writer |
| GET | `/api/v8/metrics/all` | `v8_metrics_all` | All ~23 metrics for latest `score_date`, all symbols |
| GET | `/api/v8/metrics/{symbol}` | `v8_metrics_single` | Single-symbol metrics. Query: `score_date` (defaults today, falls back to latest) |
| GET | `/api/v8/live_metrics` | `v8_live_metrics` | MOVED to `v8_endpoints.py` (cc#1565, rule 4) — see the V8 router section |
| POST | `/api/momentum/run` | `momentum_run` | 🔒 admin — run `momentum_daily.compute_momentum()` |

---

## Paper Engine (V8 paper)

| Method | Path | Handler | Description |
|---|---|---|---|
| POST | `/api/paper/compute_pivots` | `paper_compute_pivots` | 🔒 admin — compute paper pivots |
| POST | `/api/paper/tick` | `paper_tick_now` | 🔒 admin — run paper tick (pulls buy/sell slots from market_mood) |
| GET | `/api/paper/status` | `paper_status` | Open positions (with unrealised P&L), recent trades, missed, summary (cutover era), `era_block`. cc#1604: `all_trades` = the cutover-era list with no LIMIT and `all_summary` = the suspended payload while the full ledger is suspended |
| GET | `/api/paper/pivots` | `paper_pivots` | Latest paper pivots. Query: `limit` (default 250) |

---

## Admin — Data Ops

| Method | Path | Handler | Description |
|---|---|---|---|
| GET | `/api/admin/refresh_status` | `admin_refresh_status` | 🔒 admin — content refresh status |
| POST | `/api/admin/mark_refresh_complete` | `mark_refresh_complete` | 🔒 admin — mark refresh done. Query: `field`, `tier`, `count` |
| POST | `/api/admin/content_update` | `content_update` | 🔒 admin — write `overview`/`key_takeaway`/`result_analysis` to `input_raw` (takeaway/result_analysis are top-500 only). Body: `{symbol, field, content}` |
| GET | `/api/admin/env_check` | `env_check` | 🔒 admin — presence/length of key env vars |
| POST | `/api/admin/backfill_intraday` | `backfill_intraday` | 🔒 admin — backfill 7d intraday candles (Yahoo) for futures |
| POST | `/api/admin/heal_intraday` | `heal_intraday` | 🔒 admin — fill morning 1m gaps (15-min lag window) |
| POST | `/api/admin/run_yahoo_daily` | `run_yahoo_daily_now` | 🔒 admin — trigger background Yahoo daily fetch |
| POST | `/api/admin/backfill_indices` | `backfill_indices_now` | 🔒 admin — backfill index EOD. Query: `days` (default 7) |
| POST | `/api/admin/fetch_global` | `fetch_global_now` | 🔒 admin — fetch global indices snapshot |
| POST | `/api/admin/backfill_global` | `backfill_global_now` | 🔒 admin — backfill global. Query: `years` (default 5), `clean` (default true) |
| POST | `/api/admin/fetch_global_intraday` | `fetch_global_intraday_now` | 🔒 admin — fetch + prune (7d) global intraday |

---

## Admin — GitHub Proxy

| Method | Path | Handler | Description |
|---|---|---|---|
| GET | `/api/admin/github_read` | `github_read` | 🔒 admin — read file from repo. Query: `filepath` |
| GET | `/api/admin/github_list` | `github_list` | 🔒 admin — list repo dir. Query: `path` |
| POST | `/api/admin/github_push` | `github_push` | 🔒 admin 🛡 guard — create/update file. Body: `{filepath, new_content, commit_message?, create_if_missing?}` |
| POST | `/api/admin/github_delete` | `github_delete` | 🔒 admin 🛡 guard — delete file. Body: `{filepath, commit_message?}` |

---

## OAuth (MCP client auth)

| Method | Path | Handler | Description |
|---|---|---|---|
| GET | `/.well-known/oauth-authorization-server` | `oauth_metadata` | OAuth server metadata |
| GET | `/.well-known/oauth-protected-resource` | `oauth_resource` | Protected-resource metadata |
| POST | `/oauth/register` | `oauth_register` | Dynamic client registration |
| GET | `/oauth/authorize` | `oauth_authorize` | Authorization endpoint (issues code, redirects) |
| POST | `/oauth/token` | *(handler at main.py:1090)* | Token exchange (authorization_code) |

---

## Mounted Routers — Endpoints

These routers are wired in `main.py` via `include_router(...)`. Full paths below already include each router's `prefix=`. Endpoints marked `include_in_schema=False` are hidden from `/openapi.json`.

### Auth — `scorr_auth.py` / `scorr_authset_probe.py`
| Method | Path | Notes |
|---|---|---|
| GET/POST | `/login` | login form + submit (hidden from schema) |
| GET | `/logout` | clears session |
| GET | `/authdebug` | auth debug (hidden) |
| GET | `/authset` | cookie-set probe (hidden) |
| GET | `/authdebug2` | cookie probe (hidden) |

### V8 — `v8_endpoints.py` (prefix `/api/v8`)
| Method | Path | Description |
|---|---|---|
| GET | `/api/v8/market_mood` | market mood + buy/sell slots |
| GET | `/api/v8/era` | cc#1604 (`v8_era.py`): `{era, since, era_label, cutover_ts, full_ledger_allowed}` — the caption every V8 surface prints ("Since 18-Jul-2026"), read from `app_config` at request time, never typed |
| GET | `/api/v8/book_canon` | rule 13 canon book. Query `era` (`fresh` default); `era=all` → **410** suspended payload while `app_config.v8_full_ledger_suspended` is true (cc#1604). Payload carries `era_block` |
| GET | `/api/mobile/trades` | app closed-trades list, cutover era; `era=all` → **410** while suspended (cc#1604); carries `era_block` |
| GET | `/api/v8/live_metrics` | cc#1565: per active-futures symbol `cmp`, `day_open`, `prev_close`, `prev_close_basis` (`raw_eod` / `auction_bar` / `last_bar`, from `prev_close.py`), `day_pct` = cmp vs PREVIOUS-SESSION close (the Fyers/NSE day change), `open_pct` = cmp vs today's open (the old since-open number, now labelled), `hour_ago_close`, `hourly_pct`. `as_of` = last fyers_eq bar date; prev close is taken BEFORE `as_of`, so a weekend read pairs Friday's tick with Thursday's close. Null when a leg is missing, never 0. The Market Gate (`nifty_dwm.py`) Day anchor reads the same `prev_close.py` close |
| GET | `/api/v8/scan` | full V8 scan |
| GET | `/api/v8/filter_config/{basket}` | filter config for basket |
| GET | `/api/v8/qualified/{basket}` | qualified signals for basket |
| GET | `/api/v8/funnel/{basket}` | qualification funnel |
| GET | `/api/v8/funnel_detail/{basket}` | funnel detail |
| GET | `/api/v8/stock_passcount/{basket}` | per-stock pass counts |
| GET | `/api/v8/raw` | raw metrics |
| GET | `/api/v8/buy_s1_bounce` | S1-bounce buy candidates |
| GET | `/api/v8/sell_overbought` | overbought sell candidates |
| GET | `/api/v8/adr` | ADR |
| GET | `/api/v8/domestic_live` | live domestic indices |
| GET | `/api/v8/positions` | positions |
| GET | `/api/v8/trades` | trades |
| GET | `/api/v8/daylog` | Day Log: per-exit-date opened/closed/gross/brokerage/net rows + summary + `era_block`. Query: `era` (`fresh` default; `all` → **410** `full ledger suspended` while `app_config.v8_full_ledger_suspended` is true, cc#1604 V8_ERA_CUTOVER_ONLY_V1 36757), `view` (`equity` default / `futures`) |
| GET | `/api/v8/daylog/series` | cc#1561 (`v8_daylog_extras.py`): the `/api/v8/daylog` payload folded into cumulative gross/net points by exit date, plus return facts (window_start = first exit day, table_start, trading/calendar days, capital, return_pct, cagr_pct null under 7 calendar days, cagr_note). Feeds the Day Log chart panel + Overall Return (i). Query: `view` |
| GET | `/api/v8/pivot_star` | `v8_pivot_star.py`. Read-only, never writes. Four marker families on the OPEN paper book, LOG reads (never a live re-eval): `stars` (BLUE/RED pivot, `direction` BUY/SELL), `activity` (bolt, volume/OI), `dma_state` (cc#1682: STATE not cross — every open symbol's own square, `{symbol, color, dma5, dma20, data_date, note}`; `dma_cross` is a one-release alias returning the same list), `tc_strong` (amber, Trade Check). Plus `legend[]`, the one shared wording source both `/dashboard` and `/m/v8` render. Query: `star_date` (defaults to the latest session with rows) |

### V8 Futures — `v8_futures.py` (prefix `/api/v8/futures`)
| Method | Path | Description |
|---|---|---|
| GET | `/api/v8/futures/list` | active futures universe |
| POST | `/api/v8/futures/upload` | bulk upload universe |
| POST | `/api/v8/futures/add` | add symbol |
| POST | `/api/v8/futures/remove` | remove symbol |

### Quant Basket (QB) — `qb_endpoints.py` (prefix `/api/qb`)
| Method | Path | Description |
|---|---|---|
| POST | `/api/qb/eod_check` | EOD check (single) |
| POST | `/api/qb/eod_check_all` | EOD check (all) |
| POST | `/api/qb/mark_intraday` | mark intraday |
| POST | `/api/qb/fix_allocations` | fix allocations |
| POST | `/api/qb/fix_all_allocations` | fix all allocations |
| GET | `/api/qb/positions` | open positions |
| GET | `/api/qb/summary` | summary |
| GET | `/api/qb/rebalance_log` | raw rebalance/EOD-check log (one row per trading night) |
| GET | `/api/qb/rebalance_history` | cc#1703: same log, classified — rebalance / stop exit / cash move rows only by default, nightly-check noise hidden behind a count; real IN/OUT symbols + a stock-only HELD count (bees excluded) |
| GET | `/api/qb/registry` | registry |
| GET | `/api/qb/gated_rebalances` | cc#1704: {basket: {due, n_candidates}} for every basket whose latest rebalance is still awaiting a founder confirm — powers the card pill |
| POST | `/api/qb/rebalance/confirm` | cc#1704: buy a gated rebalance's stored candidates (admin-token gated; never called by CC against a live basket) |
| POST | `/api/qb/rebalance/skip` | cc#1704: dismiss a gated rebalance's candidates, no buy (admin-token gated) |

### GVM — `gvm_nightly.py`, `gvm_report_endpoints.py`, `gvm_market_endpoints.py`, `gvm_universe_pivots.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/admin/load_screener_json` | load screener JSON (nightly) |
| POST | `/api/gvm/recompute` | recompute GVM scores |
| GET | `/api/gvm/history/{symbol}` | GVM history |
| GET | `/api/gvm/company/{symbol}` | full company report |
| GET | `/api/gvm/search` | search companies |
| GET | `/api/gvm/{symbol}` | GVM score for symbol |
| GET | `/api/gvm/top/{n}` | top-N by GVM |
| GET | `/api/filter` | filtered list |
| GET | `/api/sectors` | sector ratings |
| GET | `/api/market/top_gainers` | top gainers |
| GET | `/api/cmp/{symbol}` | current market price |
| GET | `/api/candles/{symbol}` | daily OHLC from raw_prices; `days=N` (5..1825, `<=0` = ALL). cc#1566: `tf=3Y` = 1095-day window with a 620-session depth gate → `{kind:"unavailable", reason, sessions_available, sessions_required}` when short; `probe=1` returns the gate answer only (`kind:"ok"` / `"unavailable"`) |
| GET | `/api/intraday/{symbol}` | intraday series |
| GET | `/api/intraday_ondemand/{symbol}` | on-demand intraday |
| GET | `/api/global` | global indices |
| GET | `/api/global/history/{name}` | global index history |
| GET | `/api/global/intraday/{name}` | global index intraday |
| POST | `/api/admin/build_universe_pivots` | build universe pivots |

### Admin Data — `admin_data.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/admin/load_input_from_drive` | load input_raw from Drive |
| POST | `/api/admin/load_screener_from_drive` | load screener from Drive |
| POST | `/api/admin/load_earnings_from_screener` | load earnings |

### Fyers — `fyers_endpoints.py` (prefix `/api/fyers`)
| Method | Path | Description |
|---|---|---|
| GET | `/api/fyers/quote/{symbol}` | live Fyers quote |

### Diagnosis — `diagnosis.py` (prefix `/api`)
| Method | Path | Description |
|---|---|---|
| GET | `/api/diagnosis` | system diagnosis |

### Earnings calendar backstop — `earnings_calendar_diag.py` (cc#1707)
| Method | Path | Description |
|---|---|---|
| GET | `/api/diag/earnings_calendar` | read-only: `{phantom_reported, phantom_upcoming, leads, offenders[], ok}` — rows with `verified='false'` that read `reported`/`upcoming`. Same check is a `data_feeds` line in `/api/health/report` ("Earnings calendar phantoms"). |

**EARNINGS_CALENDAR_PROVENANCE (cc#1707, 05-Sep-2026).** `earnings_calendar.verified` is the source
flag: `confirmed` / `estimated` come from the calendar scrape or the NSE feed; `false` means a
cc#602 news-discovered LEAD (a company merely named in an article). Rules:
- The news-lead writer (`result_corner.reconcile`) inserts `status='lead'`, never `upcoming`/`reported`.
- The two status flips (`admin_data._earnings_lifecycle`, `ops_metrics_pipeline.flip_earnings_status`)
  skip `verified='false'` rows. A lead becomes `reported` only when a confirmed source upserts the
  same `(ticker, ex_date)`.
- Every "has this company reported" reader carries `verified <> 'false'`: R card, results lists,
  digest, mobile results, native cards, HR report, GVM page, watchdog preconditions, scrape targets.
- R card date pairing: `/api/results/card` `ex_date` is the confirmed row for the card's OWN quarter
  (`_q_label_end` + `_paired_result_date`), never `MAX(ex_date)`.
- Trading gates (`guards.blackout`, `v8_paper`, `tc_intraday`, `native_trade_check`, `v8_endpoints`,
  `v14_engine`, `tc_v4_*`, `tc_score_replay`, `v12`, `deriv_metrics`) read this table UNFENCED —
  flagged separately on cc#1707; fence lands as P4 on the Fable Room ruling.

### V9 Pairs — `v9_endpoints.py` (prefix `/api/v9`)
| Method | Path | Description |
|---|---|---|
| POST | `/api/v9/discover` | discover pairs |
| POST | `/api/v9/backtest` | run backtest |
| GET | `/api/v9/pairs` | list pairs |
| GET | `/api/v9/results` | results |
| GET | `/api/v9/results/{combo_id}` | result detail |
| GET | `/api/v9/trades/{combo_id}` | trades for combo |
| GET | `/api/v9/best_combo` | best combo |

### V10 — `v10_endpoints.py` (prefix `/api/v10`)
| Method | Path | Description |
|---|---|---|
| GET | `/api/v10/signal` | latest signal |
| POST | `/api/v10/append` | append signal |
| POST | `/api/v10/tick` | tick |
| GET | `/api/v10/positions` | positions |
| GET | `/api/v10/trades` | trades |
| GET | `/api/v10/summary` | summary |

### PCR — `pcr_endpoints.py` (prefix `/api/pcr`)
| Method | Path | Description |
|---|---|---|
| GET | `/api/pcr/intraday` | intraday PCR |
| GET | `/api/pcr/mood?underlying=NIFTY\|BANKNIFTY` | `pcr_mood.py` (cc#1568, session_log 36200): label, band, dial_segments, label_colour, reason, note, basis, as_of. **cc#1576 (36294)** adds `interpret`: `state` (STRENGTH \| CAUTION \| COMPLACENCY \| WEAK_SUPPORT \| NEUTRAL, the three-input rule: PCR vs yesterday, Nifty day %, VIX vs prev close), `band` (read band), `headline`, `read[]`/`read_text`, `change_line`, `hour_line`, `range_line`, `caveats[]`, `evidence {n, up, down, avg_next_pct, scored}` + `evidence_line` for the current state (pcr_daily × raw_prices × INDIAVIX, last 120 sessions, scored=false below 20), `evidence_by_state`, `evidence_by_band`, `inputs`, `option_price` (ATM CE/PE ltp vs Black-Scholes fair via `/api/deriv/strike-chain`, cached 5 min). Descriptive only. |
| POST | `/api/pcr/intraday/compute` | compute intraday PCR |
| POST | `/api/pcr/backfill` | backfill PCR |

### PCR mood composer — `pcr_mood.py` (prefix `/api/pcr`, cc#1568, session_log 36200)
| Method | Path | Description |
|---|---|---|
| GET | `/api/pcr/mood` | ONE mood word for the latest PCR of `underlying` (default NIFTY; BANKNIFTY same bands, same Nifty week). Read by the app hero (`pcr_mood` in `/m/home2` payload), the web Index Intel PCR card and the Digest PCR tile — no surface re-bands. Output: `pcr`, `nifty_week_pct` (Market Gate week via `nifty_dwm.live_nifty_dwm`), `label` (EXTREME FEAR / CAUTIOUS / NEUTRAL / GREED / EXTREME GREED, null when no PCR), `band` 0–4, `label_colour` (token name red/amber/grn), `dial_cuts` `[0,50,80,100,150,200]`, `dial_segments` `[{lo,hi,colour,band}]`, `reason` (plain-words line, set only for the >1.50 CAUTIOUS reading), `note` ("week return unavailable" when the week input is missing at the extreme), `basis` LIVE/EOD, `as_of`, `spec`. Bands: <0.5 EXTREME FEAR · 0.5–0.8 CAUTIOUS · 0.8–1.0 NEUTRAL · 1.00–1.50 GREED · >1.50 → CAUTIOUS if Nifty week ≤ −1.0% else EXTREME GREED. |

### V8 Replay / Backtest / Backfill
| Method | Path | Source | Description |
|---|---|---|---|
| POST | `/api/v8/replay/run` | `v8_replay_endpoints.py` | run replay |
| GET | `/api/v8/replay/summary` | `v8_replay_endpoints.py` | replay summary |
| POST | `/api/v8/backtest/run` | `v8_intra_backtest_endpoints.py` | run intra backtest |
| POST | `/api/v8/backtest/simulate` | `v8_intra_backtest_endpoints.py` | simulate |
| GET | `/api/v8/backtest/log` | `v8_intra_backtest_endpoints.py` | backtest log |
| GET | `/api/v8/backtest/last` | `v8_intra_backtest_endpoints.py` | last run |
| POST | `/api/v8/backfill/metrics` | `v8_backfill_endpoints.py` | backfill metrics |
| POST | `/api/v8/backfill/sync_universe` | `v8_backfill_endpoints.py` | sync universe |

### MCP & Anthropic — `mcp_dispatch.py`, `anthropic_endpoints.py`
| Method | Path | Description |
|---|---|---|
| POST | `/mcp` | MCP tool dispatch |
| POST | `/api/anthropic/chat` | Anthropic chat |
| GET | `/api/anthropic/usage` | usage/cost |
| GET | `/api/anthropic/health` | health |

### Scorr Assistant — `scorr_endpoints.py`, `scorr_chat_endpoint.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/scorr/query` | NL query |
| GET | `/api/scorr/health` | health |
| POST | `/api/scorr/chat` | chat |
| GET | `/api/scorr/chat/health` | chat health |

### The three "TC" systems (cc#1549 — read this before touching any Trade Check code)

The name "TC" / "Trade Check" is shared by three unrelated systems. Mixing them up is exactly how
cc#1540 shipped a live engine drift (cc#1548). Know which one you're in before you edit or call one:

1. **tc_resolver-routed TC v4 (the real primary).** `tc_resolver.py` is the ONE import point —
   `get_primary_tc()` (single LONG/SHORT verdict, `tc_v4_endpoints.trade_check_v4`) and
   `get_primary_styles()` (4-bucket BUY/SELL x MOMENTUM/REVERSAL, `tc_v4_dual.trade_check_v4_dual`).
   This is what `scorr_check.html` (the live `/check` page), the V8 dashboard marker detail, the
   derivative cockpit TC chip, the position stars, and the v4 screener all read. **Every new Python
   consumer of Trade Check must import from here — never a versioned `tc_*` module directly** (a
   direct import is now caught automatically, see `test_tc_resolver_guard.py` below).
2. **TC Scanner / TC13** (`tc_scanner_endpoints.py`, engine in the same file) — a standalone binary
   13-check bucket (founder-approved OPTION B, 12-Jul-2026), confirmed by its own docstring: "NOT the
   TC V4 R1-R16 scoring engine used on the /check page." Two independent buckets (BUY/SELL), scored
   and stored on its own schedule, nothing to do with tc_resolver. `intraday_scanner_endpoints.py` is
   the same category. Do not route these through the resolver — they are a different product.
3. **Deprecated/legacy modules — never a new-consumer import.** `native_trade_check.py` (v3.3/v3.4),
   `trade_check_v34.py` (v3.5), `trade_check_v36.py` (v3.6) predate tc_resolver and the v4 family.
   Four call sites still use them directly, each flagged in cc#1549 rather than silently swapped
   (swapping the engine version is a behaviour change, not import hygiene — it needs a founder call):
   `check_endpoint.py` (`/api/check`, legacy v3.4 shape, no confirmed live UI caller today),
   `tc_intraday.py` (dormant phase-1 prototype, not mounted/scheduled),
   `trade_check_v34_endpoints.py` (`/api/trade-check/v34`/`v36`, deliberately version-scoped legacy
   routes, no confirmed live UI caller today), and **`native_router.py`** (the Ask Scorr chat's
   "Trade Check v3.3" trigger — the highest-priority one of the four, since it is a LIVE, user-facing
   surface answering on the old engine while every dashboard shows v4).

`test_tc_resolver_guard.py` (repo root, `python3 test_tc_resolver_guard.py` or pytest) AST-scans every
`.py` file for a direct import of a versioned TC scoring entrypoint outside `tc_resolver.py`, fails
loudly on anything not in its documented `KNOWN_EXCEPTIONS` (the four above), and passes clean today.

### Trade Check — `trade_check_v34_endpoints.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/trade-check/v34` | run Trade Check v3.4 |
| POST | `/api/trade-check/v34/promote` | promote result |
| GET | `/api/trade-check/v34/health` | health |
| GET | `/api/trade-check/screen-nifty50` | screen Nifty50 |
| POST | `/api/trade-check/tc-cache/refresh` | refresh TC cache |
| GET | `/api/trade-check/intraday-scan` | intraday scan |
| GET | `/api/trade-check/intraday-paper/status` | intraday paper status |
| POST | `/api/trade-check/intraday-paper/run` | run intraday paper |
| GET | `/api/intraday/dashboard` | intraday dashboard (replaces retired intraday_router) |
| POST | `/api/intraday/tick` | intraday tick |

### TC Position Stars v2 — `tc_position_stars_v2.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/admin/run-position-stars-v2` | admin: run the four-bucket star batch (ticks on `v8_paper_positions` OPEN symbols) |
| GET | `/api/trade-check/position-stars-v2` | latest four-bucket star per symbol+side, no params — returns EVERY current row as a flat `rows` list and a `stars` dict (`"symbol\|side"`, side=LONG/SHORT). Each row carries `score100`/`verdict10` (own-direction) and `best_any_score100`/`best_any_bucket`/`best_any_opposes_position` (best of 4 cards, any direction — TC_BEST_OF_FOUR_V1, cc#1603, the headline the V8 dashboard's Open Positions TC /100 column and cc#1693's Web Alerts TC /100 column both read) |

### Check — `check_endpoint.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/check` | run checklist |
| GET | `/api/check/rule/{rule}` | single rule |
| GET | `/api/check/health` | health |

### Sector — `sector_endpoints.py`, `sector_brief_endpoints.py`
| Method | Path | Description |
|---|---|---|
| GET | `/api/sector/rotation` | sector rotation. cc#1700 SEGMENT_SIZE_CLASS_V1: each merged-segment row also carries `size_class` ('LARGE'\|'MID'\|'SMALL'\|null), `size_rank` (1..N, null for an uncomputable row), `avg_mcap` (Rs Cr, total_mcap/stocks_count), `mcap_n`, `mcap_total` (both = stocks_count — sector_ratings has no separate "members with a market cap" count to split them). Ranked over the display (merged) segments: 1-30 LARGE, 31-60 MID, rest SMALL. |
| GET | `/api/sector/brief` | sector brief |
| POST | `/api/admin/sector/brief/batch` | 🔒 batch-generate briefs |
| GET | `/api/admin/sector/brief/status` | batch status |
| GET | `/api/sector/themes` | sector themes |

### Investment Check — `investment_check.py`
| Method | Path | Description |
|---|---|---|
| GET | `/api/investment-check` | investment checklist |
| GET | `/api/investment-check/screener` | screener view |
| GET | `/api/investment-check/summary` | summary |

### Scanners — `scanner_endpoints.py`
| Method | Path | Description |
|---|---|---|
| GET | `/api/scanners/intraday` | live intraday strength. Query: `min_day1d`, `min_vol_ratio`, `limit` |
| GET | `/api/scanners/positional` | V8 qualified swing setups. Query: `basket`, `min_gvm`, `limit` |
| GET | `/api/scanners/investment` | GVM≥7 quality. Query: `min_gvm`, `verdict`, `limit` |

### Derivatives — `deriv_metrics.py` (index path added by cc#1576)
| Method | Path | Description |
|---|---|---|
| GET | `/api/deriv/strike-chain/{symbol}` | cc#666: ATM±10 CE/PE chain with Black-Scholes fair (σ=RV20, r=7%), tags EXPENSIVE >+25% / REASONABLE / CHEAP. Stocks: Fyers symbol master + live quotes. **cc#1576:** NIFTY / NIFTY50 / BANKNIFTY take the `option_chain` path (latest tick of the nearest expiry, spot from cmp_prices NIFTY50 / BANKNIFTY, RV20 from the same symbol) through the same row builder; payload adds `source` and `chain_tick`. |

### OI Structure — `oi_structure.py` (cc#1575, OI_STRUCTURE_INTERPRET_V1 · session_log 36283)
| Method | Path | Description |
|---|---|---|
| GET | `/api/oi/structure?underlying=NIFTY\|BANKNIFTY` | Max Pain (i) read for both surfaces: live snapshot (spot + basis, max pain, call/put walls + OI, second walls, PCR, one_sided, mp_dist_pct, range_width_pct, days_to_expiry), `scenario` (PIN \| RANGE \| ABOVE_CALL_WALL \| BELOW_PUT_WALL \| MAX_PAIN_FAR \| ONE_SIDED), `headline` + `read[]` + `caveats[]` in plain words, `evidence {n, up, down, avg_next_pct, scored}` + `evidence_line` counted only from `oi_structure_daily` close rows with a filled next day (same scenario, last 60 sessions; `scored=false` below 20), `history[]` = last 10 close snapshots. Descriptive only, never a signal. |

Snapshots: `oi_structure_daily` (PK underlying, d, snapshot_kind) written by the app scheduler at 11:00 IST (`bg_oi_structure_mid`) and 15:25 IST (`bg_oi_structure_close`); `bg_oi_structure_fill` at 09:20 fills `next_day_pct/high/low` from index spot bars; `bg_oi_structure_backfill` is armed by `app_config.oi_structure_backfill_run='pending'`. Max pain math stays in `max_pain.py`.

### Wall of Trades — `trade_wall_endpoints.py` (cc#991 / cc#1295 / cc#1587)
| Method | Path | Description |
|---|---|---|
| GET | `/api/tradewall` | Keyset-paged feed read by BOTH `/trades` (web) and `/m/trades` (app). Query: `limit` (default 40, max 100), `cursor`, `instrument` (FUTURES\|EQUITY\|ALL), `status` (open\|closed\|ALL). Alias: `/api/mobile/tradewall` |

**cc#1587 (session_log 36394, WOT_APPROVED_ONLY_V1):** the wall shows **approved trades only** by default.
Which source buckets reach the feed is a server-side flag, `app_config.wot_buckets_enabled`
(JSON list or comma string; default `["approved_alerts"]`). Known bucket names: `approved_alerts`,
`v8`, `index_intel`, `tc_scanner`, `qb_basket`, `investment_scanner`. Widening the flag restores the
engine hierarchy on both pages with no deploy; unknown names are ignored and logged. The engine union
SQL is gated, not deleted. Response fields added by cc#1587: `buckets_enabled` (list in force),
`buckets_known`, `buckets_source` (`default` \| `app_config`), `approved_as_of` (latest
`trade_alerts.approved_at`, IST, head-of-feed only), and per event `origin` (`manual` or the
`source_engine` that raised the alert; `null` for non-alert rows).

---

> Router descriptions are inferred from route names/signatures, not full handler review — verify against source or the live `GET /openapi.json` (machine-readable, always current) before relying on request/response shapes.

---

<!-- BEGIN GENERATED ROUTES -->

## Generated route inventory

**Generated 11-Sep-2026 08:02 IST by `tools/gen_api_reference.py`.** Do not edit this block by hand — it is rewritten from the code on every run, and a hand edit will be lost. Everything outside the two sentinel comments is written by people and is never touched.

| | count |
|---|---|
| live paths (router mounted in `main.py`, or declared directly on `app`) | **666** |
| distinct method+path pairs | **666** |
| defined but NOT mounted (present in code, unreachable) | **10** |
| paths defined more than once (first registration wins) | **0** |

### Defined but not mounted

These exist in code and answer nothing, because no `include_router` in `main.py` reaches them. `qsr_endpoints.py` is unmounted **on purpose** (session_log 33844) — recorded here so it is not rediscovered as a bug.

| Method | Path | File | Handler |
|---|---|---|---|
| GET | `/api/qsr/funnel` | `qsr_endpoints.py` | `qsr_funnel` |
| GET | `/api/qsr/positions` | `qsr_endpoints.py` | `qsr_positions` |
| GET | `/api/qsr/preview` | `qsr_endpoints.py` | `qsr_preview` |
| POST | `/api/qsr/run-exits` | `qsr_endpoints.py` | `qsr_run_exits` |
| POST | `/api/qsr/run-scan` | `qsr_endpoints.py` | `qsr_run_scan` |
| GET | `/api/qsr/trades` | `qsr_endpoints.py` | `qsr_trades` |
| POST | `/api/admin/backfill_signals` | `worker/fyers_hist_backfill.py` | `backfill_signals_now` |
| POST | `/api/admin/fetch_hist_5m` | `worker/fyers_hist_backfill.py` | `fetch_hist_5m_now` |
| POST | `/api/admin/probe_5m_depth` | `worker/fyers_hist_backfill.py` | `probe_5m_depth_now` |
| POST | `/api/admin/run_phase_a` | `worker/fyers_hist_backfill.py` | `run_phase_a_now` |

### Every live path, by file

#### `admin_data.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/load_earnings_from_screener` | `load_earnings_from_screener` |
| POST | `/api/admin/load_input_from_drive` | `load_input` |
| POST | `/api/admin/load_screener_from_drive` | `load_screener` |

#### `admin_index_backfill.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/backfill-indian-indices` | `backfill_indian_indices` |
| GET | `/api/admin/indices-excel` | `indices_excel` |

#### `aicio_app_mobile.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/aicio_app` | `mobile_aicio_app` |
| GET | `/m/aicio` | `m_aicio` |

#### `anthropic_endpoints.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/anthropic/chat` | `anthropic_chat` |
| GET | `/api/anthropic/health` | `health_check` |
| GET | `/api/anthropic/usage` | `get_usage` |

#### `app_check_endpoints.py` — 5 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/check/invest` | `app_check_invest` |
| GET | `/api/mobile/check/scan` | `app_check_scan` |
| GET | `/api/mobile/check/scan/progress` | `app_check_scan_progress` |
| GET | `/api/mobile/check/scan/start` | `app_check_scan_start` |
| GET | `/api/mobile/check/tc` | `app_check_tc` |

#### `basket_rebalance_endpoints.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/adaptive/baskets/available` | `get_available_baskets` |
| GET | `/api/adaptive/baskets/repair` | `get_repair_sheet` |
| GET | `/api/adaptive/baskets/repair_all` | `get_repair_all` |
| POST | `/api/adaptive/baskets/subscribe` | `subscribe_basket` |

#### `bhavcopy_diagnostic.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/bhavcopy-diagnostic` | `bhavcopy_diagnostic_run` |

#### `bt6_endpoints.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/bt6/run` | `bt6_run` |
| DELETE | `/api/bt6/run/{run_id}` | `bt6_delete` |
| GET | `/api/bt6/runs` | `bt6_runs` |
| GET | `/api/bt6/trades` | `bt6_trades` |

#### `chart_peers.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/chart/peers/{symbol}` | `chart_peers` |
| GET | `/api/chart/tradecard/{symbol}` | `chart_trade_card` |

#### `check_endpoint.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/check` | `api_check` |
| GET | `/api/check/health` | `api_check_health` |
| GET | `/api/check/rule/{rule}` | `api_check_rule` |
| GET | `/api/trade-check/fibcheck` | `api_fibcheck` |

#### `client_index_endpoints.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/client-index/encrypt-migrate` | `encrypt_migrate` |
| POST | `/api/admin/client-index/reveal` | `reveal` |
| GET | `/api/admin/client-index/security-status` | `security_status` |

#### `deriv_metrics.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/delivery/series` | `delivery_series_endpoint` |
| GET | `/api/deriv-metrics/{symbol}` | `deriv_metrics` |
| GET | `/api/deriv/strike-chain/{symbol}` | `strike_chain` |

#### `diagnosis.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/diagnosis` | `run_diagnosis` |

#### `digest_v3.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/digest/internals/series` | `digest_internals_series` |
| GET | `/api/digest/v3` | `digest_v3` |
| GET | `/digest` | `digest_page` |

#### `earnings_calendar_diag.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/diag/earnings_calendar` | `diag_earnings_calendar` |

#### `engine_watchdog.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/watchdog/gaps` | `get_gaps` |

#### `feed_health_endpoints.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/health/feed` | `feed_health` |

#### `fpc_app_mobile.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/api/mobile/fpc/calc` | `mobile_fpc_calc` |

#### `fundamentals_scraper.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/admin/fundamentals_scrape_status` | `fundamentals_scrape_status` |
| POST | `/api/admin/run_fundamentals_scrape` | `run_fundamentals_scrape` |
| POST | `/api/admin/run_shareholding_quarterly` | `run_shareholding_quarterly_now` |
| POST | `/api/admin/run_shareholding_scrape` | `run_shareholding_scrape_now` |

#### `fy_end_backfill.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/backfill_fy_end_prices` | `backfill_fy_end_prices_now` |

#### `fyers_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/fyers/oi/stock_full/{symbol}` | `fyers_oi_stock_full` |
| GET | `/api/fyers/quote/{symbol}` | `_ist_now` |

#### `fyers_range_backfill_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/backfill_futures_fyers` | `backfill_futures_fyers_now` |
| GET | `/api/admin/backfill_futures_fyers/status` | `backfill_futures_fyers_status` |

#### `galaxy_endpoints.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/galaxy_map.js` | `galaxy_map_js` |

#### `github_ops.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/github_delete` | `github_delete` |
| GET | `/api/admin/github_list` | `github_list` |
| POST | `/api/admin/github_push` | `github_push` |
| GET | `/api/admin/github_read` | `github_read` |

#### `global_heatstrip.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/global/chart/{symbol:path}` | `global_chart` |
| GET | `/api/global/heatstrip` | `heatstrip` |
| GET | `/api/global/heatstrip/{symbol:path}` | `heatstrip_detail` |

#### `gvm_market_endpoints.py` — 15 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/candles/{symbol}` | `get_candles` |
| GET | `/api/cmp/{symbol}` | `get_cmp` |
| GET | `/api/filter` | `get_filter` |
| GET | `/api/fo-ban/today` | `get_fo_ban_today` |
| GET | `/api/global` | `get_global` |
| GET | `/api/global/history/{name}` | `get_global_history` |
| GET | `/api/global/intraday/{name}` | `get_global_intraday` |
| GET | `/api/gvm/snapshot/{symbol}` | `get_gvm_snapshot` |
| GET | `/api/gvm/top/{n}` | `get_top` |
| GET | `/api/gvm/trend/{symbol}` | `get_gvm_trend` |
| GET | `/api/gvm/{symbol}` | `get_gvm` |
| GET | `/api/intraday/{symbol}` | `get_intraday` |
| GET | `/api/intraday_ondemand/{symbol}` | `intraday_ondemand` |
| GET | `/api/market/top_gainers` | `get_top_gainers` |
| GET | `/api/sectors` | `get_sectors` |

#### `gvm_nightly.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/load_screener_json` | `load_screener_json` |
| GET | `/api/gvm/history/{symbol}` | `gvm_history` |
| POST | `/api/gvm/recompute` | `gvm_recompute` |
| GET | `/api/gvm/trend_verdict/{symbol}` | `gvm_trend_verdict` |

#### `gvm_report_endpoints.py` — 6 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/gvm/company/{symbol}` | `gvm_company_report` |
| GET | `/api/gvm/recent` | `gvm_recent` |
| POST | `/api/gvm/recent` | `gvm_recent_add` |
| GET | `/api/gvm/recent-searches` | `gvm_recent_searches` |
| POST | `/api/gvm/recent/remove` | `gvm_recent_remove` |
| GET | `/api/gvm/search` | `gvm_search` |

#### `gvm_twopager.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/gvm/2pager/{symbol}` | `gvm_two_pager` |

#### `gvm_universe_pivots.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/build_universe_pivots` | `build_universe_pivots_endpoint` |

#### `health_app_mobile.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/health_app` | `mobile_health_app` |
| GET | `/api/mobile/health_app/list` | `mobile_health_list` |
| GET | `/api/mobile/health_app/report` | `mobile_health_report` |
| GET | `/m/health` | `m_health` |

#### `hr_endpoints.py` — 11 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/health/generate` | `health_generate` |
| GET | `/api/health/ledger` | `health_ledger` |
| GET | `/api/health/portfolio/{pid}` | `health_portfolio` |
| POST | `/api/health/portfolio/{pid}/holdings` | `health_edit_holdings` |
| POST | `/api/health/portfolio/{pid}/merge` | `health_merge` |
| GET | `/api/health/portfolio/{pid}/merge_preview` | `health_merge_preview` |
| POST | `/api/health/portfolio/{pid}/toggle_active` | `health_toggle_active` |
| POST | `/api/health/portfolio_broker` | `health_portfolio_broker` |
| GET | `/api/health/portfolios` | `health_portfolios` |
| POST | `/api/health/save` | `health_save` |
| POST | `/api/health/upload` | `health_upload` |

#### `hr_report.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/health/refresh/{portfolio_id}` | `health_refresh` |
| GET | `/api/health/report/{portfolio_id}` | `health_report` |

#### `hr_report_pdf.py` — 5 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/health/pdf_selftest` | `pdf_selftest` |
| GET | `/api/health/report_html/{pid}` | `report_html` |
| GET | `/api/health/report_pdf/{pid}` | `report_pdf` |
| GET | `/api/health/report_pdf_link/{pid}` | `report_pdf_link` |
| GET | `/api/health/report_pdf_self/{pid}` | `report_pdf_self` |

#### `index_tape.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/index/tape` | `index_tape` |

#### `intraday_scanner_endpoints.py` — 9 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/scanners/intraday` | `scanner_intraday` |
| GET | `/api/scanners/intraday/short` | `scanner_intraday_short` |
| GET | `/api/scanners/intraday/tc/{symbol}` | `scanner_intraday_tc` |
| GET | `/api/scanners/intraday/watchlist` | `scanner_intraday_watchlist` |
| GET | `/api/scanners/orb_ag` | `scanner_orb_ag` |
| GET | `/api/scanners/registry` | `scanners_registry` |
| GET | `/api/scanners/result_radar` | `scanner_result_radar` |
| GET | `/api/scanners/result_radar/accuracy` | `scanner_result_radar_accuracy` |
| GET | `/api/scanners/result_radar/rules` | `scanner_result_radar_rules` |

#### `inv_scanner_endpoints.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/inv-scanner/board` | `board` |
| GET | `/api/inv-scanner/symbol/{symbol}` | `symbol_card` |
| GET | `/inv-scanner` | `inv_scanner_page` |

#### `inv_scanner_rules.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/run-inv-scanner-rules` | `admin_run` |
| GET | `/api/inv-scanner/signals` | `get_signals` |
| GET | `/api/inv-scanner/state` | `get_state` |

#### `inv_scanner_scoring.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/run-inv-scanner-scoring` | `admin_run` |
| GET | `/api/inv-scanner/scores` | `get_scores` |

#### `inv_scanner_universe.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/run-inv-scanner-universe` | `admin_run` |
| GET | `/api/inv-scanner/universe` | `get_universe` |

#### `invest_check_v2.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/investment-check-v2` | `investment_check_v2` |
| GET | `/api/investment-check-v2/batch` | `investment_check_v2_batch` |
| GET | `/api/investment-check-v2/weights` | `investment_check_v2_weights` |

#### `investment_check.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/investment-check` | `investment_check` |
| GET | `/api/investment-check/detail` | `investment_check_detail` |
| GET | `/api/investment-check/screener` | `investment_screener` |
| GET | `/api/investment-check/summary` | `investment_summary` |

#### `knowledge_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/knowledge/articles` | `knowledge_articles` |
| GET | `/api/knowledge/articles/{slug}` | `knowledge_article` |

#### `main.py` — 70 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/` | `home` |
| GET | `/.well-known/oauth-authorization-server` | `oauth_metadata` |
| GET | `/.well-known/oauth-protected-resource` | `oauth_resource` |
| GET | `/adaptive` | `adaptive_dashboard_page` |
| POST | `/api/admin/backfill_global` | `backfill_global_now` |
| POST | `/api/admin/backfill_indices` | `backfill_indices_now` |
| POST | `/api/admin/backfill_intraday` | `backfill_intraday` |
| POST | `/api/admin/ca_run` | `ca_run` |
| POST | `/api/admin/ca_sweep` | `ca_sweep_symbols` |
| POST | `/api/admin/content_update` | `content_update` |
| GET | `/api/admin/env_check` | `env_check` |
| POST | `/api/admin/fetch_global` | `fetch_global_now` |
| POST | `/api/admin/fetch_global_intraday` | `fetch_global_intraday_now` |
| POST | `/api/admin/heal_intraday` | `heal_intraday` |
| POST | `/api/admin/mark_refresh_complete` | `mark_refresh_complete` |
| GET | `/api/admin/refresh_status` | `admin_refresh_status` |
| POST | `/api/admin/restate_symbols` | `restate_symbols_now` |
| POST | `/api/admin/run_yahoo_daily` | `run_yahoo_daily_now` |
| GET | `/api/daily/adr` | `daily_adr` |
| POST | `/api/daily/compute_metrics` | `compute_daily_metrics_now` |
| GET | `/api/daily/pcr` | `daily_pcr` |
| GET | `/api/data-integrity/status` | `data_integrity_status` |
| GET | `/api/health` | `health` |
| GET | `/api/health/feeds` | `health_feeds` |
| GET | `/api/health/report` | `health_report` |
| POST | `/api/momentum/run` | `momentum_run` |
| GET | `/api/now` | `server_now` |
| POST | `/api/paper/compute_pivots` | `paper_compute_pivots` |
| GET | `/api/paper/pivots` | `paper_pivots` |
| POST | `/api/paper/rebuild_cutover` | `paper_rebuild_cutover` |
| GET | `/api/paper/status` | `paper_status` |
| POST | `/api/paper/tick` | `paper_tick_now` |
| GET | `/api/v8/bt7_diff` | `v8_bt7_diff` |
| POST | `/api/v8/bt7_run` | `v8_bt7_run` |
| GET | `/api/v8/bt7_status` | `v8_bt7_status` |
| GET | `/api/v8/metrics/{symbol}` | `v8_metrics_single` |
| POST | `/api/v8/run` | `v8_run` |
| POST | `/api/v8/run_for_date` | `v8_run_for_date` |
| POST | `/api/v8/run_signal_writer` | `v8_run_signal_writer` |
| GET | `/ask` | `ask` |
| GET | `/check` | `check` |
| GET | `/cio` | `cio` |
| GET | `/cio2` | `cio2` |
| GET | `/dashboard` | `dashboard` |
| GET | `/filters` | `filters_page` |
| GET | `/fpc` | `fpc` |
| GET | `/health` | `health_report_page` |
| GET | `/holdings` | `holdings_page` |
| GET | `/intel` | `intel_page` |
| GET | `/intraday` | `intraday` |
| GET | `/news` | `news_page` |
| GET | `/oauth/authorize` | `oauth_authorize` |
| POST | `/oauth/register` | `oauth_register` |
| POST | `/oauth/token` | `oauth_token` |
| GET | `/performance` | `performance` |
| GET | `/quant-basket` | `quant_basket` |
| GET | `/result-corner` | `result_corner_page` |
| GET | `/scanners` | `scanners` |
| GET | `/scheduler-master` | `scheduler_master_page` |
| GET | `/screeners` | `screeners_page` |
| GET | `/sector` | `sector` |
| GET | `/status` | `status` |
| GET | `/structure` | `structure_page` |
| GET | `/v10` | `v10_dashboard_page` |
| GET | `/v12` | `v12_builder_page` |
| GET | `/v13` | `v13_filter_registry_page` |
| GET | `/v14` | `v14_intraday_page` |
| GET | `/v15` | `v15_mf_page` |
| GET | `/v4scan` | `tc_v4_scan_page` |
| GET | `/v9` | `v9_pairs_page` |

#### `max_ivr_endpoints.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/max/ivr/tap` | `ivr_tap` |
| GET | `/api/max/ivr/taps/summary` | `ivr_taps_summary` |
| GET | `/api/max/ivr/tree` | `ivr_tree` |

#### `max_native_cards.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/max/card/{intent}` | `max_card` |

#### `mcp_dispatch.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/mcp` | `mcp_endpoint` |

#### `mf_app_mobile.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/mf_app` | `mobile_mf_app` |
| GET | `/api/mobile/mf_app/fund` | `mobile_mf_fund` |
| GET | `/api/mobile/mf_app/list` | `mobile_mf_list` |
| GET | `/m/mf` | `m_mf` |

#### `mf_pipeline.py` — 21 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v15/fund/{scheme_code}` | `v15_fund` |
| POST | `/api/v15/mf/backfill_curated_nav` | `mf_backfill_curated_nav` |
| GET | `/api/v15/mf/coverage_report` | `mf_coverage_report` |
| GET | `/api/v15/mf/curated` | `mf_curated` |
| POST | `/api/v15/mf/ensure` | `mf_ensure` |
| GET | `/api/v15/mf/fund/{scheme_code}` | `mf_fund` |
| GET | `/api/v15/mf/mc_discover` | `mf_mc_discover` |
| POST | `/api/v15/mf/mc_discover_run` | `mf_mc_discover_run_arm` |
| POST | `/api/v15/mf/nav_refresh` | `mf_nav_refresh` |
| POST | `/api/v15/mf/reconcile` | `mf_reconcile` |
| POST | `/api/v15/mf/returns_backfill` | `mf_returns_backfill_arm` |
| POST | `/api/v15/mf/run_mc_oneshot` | `mf_run_mc_oneshot_arm` |
| POST | `/api/v15/mf/run_monthly` | `mf_run_monthly_arm` |
| POST | `/api/v15/mf/run_weekly` | `mf_run_weekly_arm` |
| POST | `/api/v15/mf/score_recompute` | `mf_score_recompute` |
| GET | `/api/v15/mf/search` | `mf_search` |
| POST | `/api/v15/mf/wire_all` | `mf_wire_all_arm` |
| GET | `/api/v15/screener` | `v15_screener` |
| GET | `/api/v15/screener/selftest` | `v15_screener_selftest` |
| GET | `/api/v15/search` | `v15_search` |
| GET | `/api/v15/stats` | `v15_stats` |

#### `mobile_cards_endpoints.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/m/cards` | `m_cards_preview` |

#### `mobile_dash_hub.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/m/dash` | `m_dash` |

#### `mobile_endpoints.py` — 23 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/check` | `mobile_check` |
| GET | `/api/mobile/digest` | `mobile_digest` |
| GET | `/api/mobile/gvm` | `mobile_gvm` |
| GET | `/api/mobile/home` | `mobile_home` |
| GET | `/api/mobile/intel` | `mobile_intel` |
| GET | `/api/mobile/models` | `mobile_models` |
| GET | `/api/mobile/now` | `mobile_now` |
| GET | `/api/mobile/qb` | `mobile_qb` |
| GET | `/api/mobile/results` | `mobile_results` |
| GET | `/api/mobile/v8` | `mobile_v8` |
| GET | `/api/mobile/v8_positions` | `mobile_v8_positions` |
| GET | `/m/check` | `m_check` |
| GET | `/m/gvm` | `m_gvm` |
| GET | `/m/home` | `m_home` |
| GET | `/m/intel` | `m_intel` |
| GET | `/m/learn` | `m_learn` |
| GET | `/m/login` | `m_login` |
| GET | `/m/models` | `m_models` |
| GET | `/m/positions` | `m_positions` |
| GET | `/m/qb` | `m_qb` |
| GET | `/m/results` | `m_results` |
| GET | `/m/v8` | `m_v8` |
| GET | `/static/mobile_app.css` | `mobile_app_css` |

#### `mobile_ext.py` — 14 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/holdings` | `mobile_holdings` |
| GET | `/api/mobile/portfolio` | `mobile_portfolio` |
| GET | `/api/mobile/result_analysis` | `mobile_result_analysis` |
| GET | `/api/mobile/result_analysis_index` | `mobile_result_analysis_index` |
| GET | `/api/mobile/sector/caps` | `mobile_sector_caps` |
| GET | `/api/mobile/sector/detail` | `mobile_sector_detail` |
| GET | `/api/mobile/trades` | `mobile_trades` |
| GET | `/api/mobile/v8book` | `mobile_v8book` |
| GET | `/api/mobile/v8funnel` | `mobile_v8_funnel` |
| GET | `/api/mobile/v8lower` | `mobile_v8_lower` |
| GET | `/m/fpc` | `m_fpc` |
| GET | `/m/holdings` | `m_holdings` |
| GET | `/m/screeners` | `m_screeners` |
| GET | `/m/sector` | `m_sector` |

#### `mobile_home2.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/breadth` | `mobile_breadth` |
| GET | `/api/mobile/home2` | `mobile_home2` |
| GET | `/api/mobile/trends` | `mobile_trends` |
| GET | `/api/mobile/v10chart` | `mobile_v10chart` |

#### `mobile_home_derivatives.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/home/derivatives` | `home_derivatives` |

#### `mobile_qb_holdings.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/m/qb/holdings` | `m_qb_holdings` |

#### `mobile_scanners.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/m/invscan` | `m_invscan` |
| GET | `/m/qbbuilder` | `m_qbbuilder` |
| GET | `/m/tcscan` | `m_tcscan` |

#### `mobile_watchlist_stub.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/m/mywatchlist` | `m_mywatchlist` |

#### `model_launcher.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/models/status` | `models_status` |

#### `news_endpoints.py` — 8 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/news/company/{symbol}` | `news_company` |
| GET | `/api/news/live` | `news_live` |
| GET | `/api/news/market` | `news_market` |
| GET | `/api/news/polished` | `news_polished` |
| GET | `/api/news/raw_search` | `news_raw_search` |
| GET | `/api/news/stock_views/shortlist` | `stock_views_shortlist` |
| GET | `/api/news/top` | `news_top` |
| GET | `/api/news/unpolished` | `news_unpolished` |

#### `nse_eod_ingest.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/nse-eod/run` | `nse_eod_run` |
| GET | `/api/nse-eod/status` | `nse_eod_status` |

#### `nse_fo_eod.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/fo_eod` | `fo_eod_run` |

#### `oi_structure.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/oi/structure` | `oi_structure` |

#### `ondemand_bars.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/bars/_cache/stats` | `cache_stats` |
| GET | `/api/bars/{symbol}` | `bars` |

#### `ops_control_plane.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/ops/control-plane/diagnosis` | `diagnosis_endpoint` |
| GET | `/api/ops/control-plane/findings` | `findings_endpoint` |
| POST | `/api/ops/control-plane/review` | `mark_reviewed` |
| POST | `/api/ops/control-plane/seed` | `seed_endpoint` |

#### `ops_metrics_pipeline.py` — 17 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/ops_metrics/arm_text_fetch` | `admin_arm_text_fetch` |
| POST | `/api/admin/ops_metrics/run_backfill` | `admin_arm_backfill` |
| POST | `/api/admin/ops_metrics/run_company/{symbol}` | `admin_run_company` |
| POST | `/api/admin/ops_metrics/run_company_depth/{symbol}` | `admin_run_company_depth` |
| POST | `/api/admin/ops_metrics/run_saturday_retry` | `admin_run_saturday_retry` |
| POST | `/api/admin/ops_metrics/run_season_sweep` | `admin_run_season_sweep` |
| POST | `/api/admin/ops_metrics/run_t1` | `admin_run_t1` |
| POST | `/api/admin/ops_metrics/run_text_fetch/{symbol}` | `admin_run_text_fetch` |
| POST | `/api/admin/ops_metrics/seed_registry` | `admin_seed_registry` |
| GET | `/api/admin/ops_metrics/status` | `admin_status` |
| GET | `/api/admin/ops_metrics/storage` | `admin_storage` |
| GET | `/api/ops_metrics/company/{symbol}` | `get_company_ops_metrics` |
| GET | `/api/ops_metrics/concall/{symbol}` | `get_concall_summary` |
| GET | `/api/ops_metrics/guidance/{symbol}` | `get_guidance_tracker` |
| GET | `/api/ops_metrics/registry` | `get_registry` |
| GET | `/api/ops_metrics/sector_trend` | `get_sector_trend` |
| GET | `/api/ops_metrics/segment_trends` | `get_segment_trends` |

#### `ops_peer_benchmark.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/ops-peer/card/{symbol}` | `card` |
| POST | `/api/ops-peer/rebuild` | `rebuild_now` |
| GET | `/api/ops-peer/screen` | `screen_endpoint` |
| GET | `/api/ops-peer/segments` | `segments` |

#### `option_iv_history.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/option_iv/run` | `option_iv_run` |
| POST | `/api/admin/option_iv/seed` | `option_iv_seed` |
| GET | `/api/admin/option_iv/status` | `option_iv_status` |

#### `pcr_endpoints.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/pcr/backfill` | `pcr_backfill_run` |
| GET | `/api/pcr/intraday` | `pcr_intraday_trend` |
| POST | `/api/pcr/intraday/compute` | `pcr_intraday_compute` |
| GET | `/api/pcr/intraday_hourly` | `pcr_intraday_hourly` |

#### `pcr_mood.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/pcr/mood` | `pcr_mood_endpoint` |

#### `performance_endpoints.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/performance/alpha` | `performance_alpha` |
| GET | `/api/performance/options` | `performance_options` |
| GET | `/api/performance/qb` | `performance_qb` |

#### `preview_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/preview` | `preview_index` |
| GET | `/preview/{name}` | `preview_screen` |

#### `pwa_endpoints.py` — 32 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/app` | `pwa_home` |
| GET | `/index_tape_card.js` | `pwa_index_tape_card_js` |
| GET | `/manifest.json` | `pwa_manifest` |
| GET | `/mobile_tables.js` | `pwa_mobile_tables_js` |
| GET | `/nav_toggle.js` | `pwa_nav_toggle_js` |
| GET | `/pcr_trend_card.js` | `pwa_pcr_trend_card_js` |
| GET | `/pwa.js` | `pwa_js` |
| GET | `/results_card.js` | `pwa_results_card_js` |
| GET | `/scorr_alert_create.js` | `pwa_scorr_alert_create_js` |
| GET | `/scorr_analysis_card.js` | `pwa_scorr_analysis_card_js` |
| GET | `/scorr_bell.js` | `pwa_scorr_bell_js` |
| GET | `/scorr_card_common.js` | `pwa_scorr_card_common_js` |
| GET | `/scorr_card_strip.js` | `pwa_scorr_card_strip_js` |
| GET | `/scorr_chart_card.js` | `pwa_scorr_chart_card_js` |
| GET | `/scorr_cockpit_card.js` | `pwa_scorr_cockpit_card_js` |
| GET | `/scorr_mobile_cards.js` | `pwa_scorr_mobile_cards_js` |
| GET | `/scorr_model_portfolio.js` | `pwa_scorr_model_portfolio_js` |
| GET | `/scorr_news_row.js` | `pwa_scorr_news_row_js` |
| GET | `/scorr_segment_results.js` | `pwa_scorr_segment_results_js` |
| GET | `/scrub_layer.js` | `pwa_scrub_layer_js` |
| GET | `/service_worker.js` | `pwa_service_worker` |
| GET | `/static/icon-192.png` | `pwa_icon_192` |
| GET | `/static/icon-512.png` | `pwa_icon_512` |
| GET | `/static/manifest.json` | `pwa_manifest` |
| GET | `/static/mobile.css` | `pwa_mobile_css` |
| GET | `/static/scorr_appshell.css` | `pwa_scorr_appshell_css` |
| GET | `/static/scorr_appshell.js` | `pwa_scorr_appshell_js` |
| GET | `/static/scorr_theme_r5.css` | `pwa_scorr_theme_r5_css` |
| GET | `/static/scorr_themes.css` | `pwa_scorr_themes_css` |
| GET | `/static/scorr_web_tokens.css` | `pwa_scorr_web_tokens_css` |
| GET | `/static/theme_mobile.css` | `pwa_theme_mobile_css` |
| GET | `/v8_ladder_v2.js` | `pwa_v8_ladder_v2_js` |

#### `qb_app_mobile.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/qb_app` | `mobile_qb_detail` |
| GET | `/api/mobile/qb_app/holdings` | `mobile_qb_holdings` |
| GET | `/api/mobile/qb_app/list` | `mobile_qb_list` |

#### `qb_discretionary_rebalance.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/api/qb/discretionary/rebalance` | `discretionary_rebalance` |

#### `qb_endpoints.py` — 26 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/qb/alpha/propose` | `qb_alpha_propose` |
| GET | `/api/qb/breakout/propose` | `qb_breakout_propose` |
| GET | `/api/qb/contra/propose` | `qb_contra_propose` |
| POST | `/api/qb/eod_check` | `qb_eod_check_now` |
| POST | `/api/qb/eod_check_all` | `qb_eod_check_all` |
| POST | `/api/qb/fix_all_allocations` | `qb_fix_all_allocations` |
| POST | `/api/qb/fix_allocations` | `qb_fix_allocations` |
| GET | `/api/qb/gated_rebalances` | `qb_gated_rebalances` |
| GET | `/api/qb/largecap/propose` | `qb_largecap_propose` |
| GET | `/api/qb/ledger` | `qb_ledger` |
| POST | `/api/qb/mark_intraday` | `qb_mark_intraday_now` |
| GET | `/api/qb/midcap/propose` | `qb_midcap_propose` |
| GET | `/api/qb/nav` | `qb_nav_series` |
| POST | `/api/qb/nav/rebuild` | `qb_nav_rebuild` |
| GET | `/api/qb/positions` | `qb_positions` |
| POST | `/api/qb/rebalance/confirm` | `qb_rebalance_confirm` |
| POST | `/api/qb/rebalance/skip` | `qb_rebalance_skip` |
| POST | `/api/qb/rebalance_due` | `qb_rebalance_due` |
| GET | `/api/qb/rebalance_history` | `qb_rebalance_history` |
| GET | `/api/qb/rebalance_log` | `qb_rebalance_log` |
| POST | `/api/qb/rebalance_now` | `qb_rebalance_now` |
| GET | `/api/qb/registry` | `qb_registry` |
| GET | `/api/qb/seed/preview` | `qb_seed_preview` |
| POST | `/api/qb/seed/run` | `qb_seed_run` |
| GET | `/api/qb/smallcap/propose` | `qb_smallcap_propose` |
| GET | `/api/qb/summary` | `qb_summary` |

#### `result_analysis_gen.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/result_analysis/regenerate` | `regenerate_now` |

#### `result_corner.py` — 5 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/result_corner/backfill` | `backfill_now` |
| GET | `/api/admin/result_corner/preview` | `preview` |
| GET | `/api/admin/result_corner/status` | `status` |
| GET | `/api/result-corner` | `result_corner_list` |
| GET | `/api/result-corner/v2` | `result_corner_v2` |

#### `results_app_mobile.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/results_app` | `mobile_results_app` |
| GET | `/api/mobile/results_app/analysis` | `mobile_results_analysis` |
| GET | `/api/mobile/results_app/companies` | `mobile_results_companies` |
| GET | `/api/mobile/results_app/season` | `mobile_results_season` |

#### `results_endpoints.py` — 7 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/results/card` | `results_card` |
| GET | `/api/results/card/context` | `results_card_context` |
| GET | `/api/results/peers/{symbol}` | `results_peers` |
| GET | `/api/results/segment/{segment}` | `results_segment` |
| GET | `/api/results/v2` | `result_analysis_v2_list` |
| GET | `/api/results/v2/queue/status` | `result_analysis_v2_queue` |
| GET | `/api/results/v2/{symbol}` | `result_analysis_v2` |

#### `room_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/room/feed` | `room_feed` |
| GET | `/room` | `room_page` |

#### `scanner_endpoints.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/scanners/day_range_oi` | `scanner_day_range_oi` |
| GET | `/api/scanners/investment` | `scanner_investment` |
| GET | `/api/scanners/positional` | `scanner_positional` |

#### `scanners_app_mobile.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/invscan` | `mobile_invscan` |
| GET | `/api/mobile/tcscan` | `mobile_tcscan` |

#### `scheduler_health_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/health/memory` | `health_memory` |
| GET | `/api/health/scheduler` | `health_scheduler` |

#### `scheduler_master.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/scheduler_master/audit` | `admin_audit` |
| POST | `/api/admin/scheduler_master/seed` | `admin_seed` |
| GET | `/api/scheduler/master` | `get_scheduler_master` |

#### `scorr_auth.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/login` | `login_get` |
| POST | `/login` | `login_post` |
| GET | `/logout` | `logout` |

#### `scorr_authset_probe.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/authdebug2` | `authdebug2` |
| GET | `/authset` | `authset` |

#### `scorr_chat_endpoint.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/scorr/chat` | `scorr_chat` |
| GET | `/api/scorr/chat/health` | `scorr_chat_health` |

#### `scorr_endpoints.py` — 12 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/clients/position/source_tag` | `clients_set_source_tag` |
| GET | `/api/clients/positions` | `clients_positions` |
| GET | `/api/clients/realised` | `clients_realised` |
| POST | `/api/position/close` | `position_close` |
| POST | `/api/position/open` | `position_open` |
| GET | `/api/scorr/health` | `scorr_health` |
| POST | `/api/scorr/query` | `scorr_query` |
| GET | `/api/smartgain/m2m` | `smartgain_m2m` |
| POST | `/api/smartgain/position/source_tag` | `smartgain_set_source_tag` |
| POST | `/api/test/position/source_tag` | `test_set_source_tag` |
| GET | `/api/test/positions` | `test_positions` |
| GET | `/api/test/realised` | `test_realised` |

#### `screener_expectations.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/screener_expectations/backfill` | `admin_backfill` |
| GET | `/api/gvm/expectations/{symbol}` | `gvm_expectations` |

#### `screeners_app_mobile.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/screeners_app` | `mobile_screeners_app` |
| GET | `/api/mobile/screeners_app/list` | `mobile_screeners_list` |
| GET | `/api/mobile/screeners_app/screen` | `mobile_screeners_screen` |

#### `screeners_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/screeners` | `screeners_list` |
| GET | `/api/screeners/{screen_id}` | `screener_detail` |

#### `sector_app_mobile.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/sector_app` | `mobile_sector_app` |
| GET | `/api/mobile/sector_app/list` | `mobile_sector_list` |
| GET | `/api/mobile/sector_app/segment` | `mobile_sector_segment` |

#### `sector_brief_endpoints.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/sector/brief/batch` | `sector_brief_batch` |
| GET | `/api/admin/sector/brief/status` | `sector_brief_status` |
| GET | `/api/sector/brief` | `sector_brief` |
| GET | `/api/sector/themes` | `sector_themes` |

#### `sector_endpoints.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/sector/rotation` | `sector_rotation` |

#### `smartgain_app_portfolio.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/myportfolio` | `mobile_myportfolio` |
| GET | `/m/myportfolio` | `m_myportfolio` |

#### `smartgain_daily_m2m.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/smartgain/daily_m2m` | `smartgain_daily_m2m` |

#### `smartgain_reconcile.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/smartgain/backfill` | `api_backfill` |
| POST | `/api/smartgain/reconcile` | `api_reconcile` |
| POST | `/api/smartgain/repair_journal` | `api_repair_journal` |

#### `stock_options_backfill.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/backfill_stock_options` | `backfill_stock_options_now` |

#### `stock_views_funnel.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/news/stock_views/feed` | `stock_views_feed` |

#### `structure_endpoints.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/structure/{symbol}` | `structure` |

#### `tc_lite_scanner.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/scanners/tc_lite` | `tc_lite_signals` |
| POST | `/api/scanners/tc_lite/scan` | `tc_lite_scan_now` |

#### `tc_position_stars_v2.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/run-position-stars-v2` | `admin_run_position_stars_v2` |
| GET | `/api/trade-check/position-stars-v2` | `position_stars_v2` |

#### `tc_scanner_config.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/tc-scanner/config` | `tc_scanner_config` |

#### `tc_scanner_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/scanners/tc/holds` | `tc_scanner_holds` |
| GET | `/api/scanners/tc/spec` | `tc_scanner_spec` |

#### `tc_score_replay_endpoints.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/run-tc-replay` | `run_tc_replay` |
| GET | `/api/tc/replay/selfcheck` | `replay_selfcheck` |
| GET | `/api/tc/replay/summary` | `replay_summary` |
| GET | `/api/tc/replay/table` | `replay_table` |

#### `tc_screener_v2.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/run-tc-screener-v2` | `admin_run_tc_screener_v2` |
| GET | `/api/trade-check/screen-v2` | `screen_v2` |

#### `tc_sim_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/tc-sim/summary` | `tc_sim_summary` |
| POST | `/api/tc-sim/tick` | `tc_sim_tick` |

#### `tc_v4_dual.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/trade-check/v4/detail` | `v4_detail_get` |
| GET | `/api/trade-check/v4/dual` | `v4_dual_get` |
| POST | `/api/trade-check/v4/dual` | `v4_dual_post` |
| GET | `/api/trade-check/v4/health-dual` | `v4_dual_health` |

#### `tc_v4_endpoints.py` — 5 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/trade-check/position-stars` | `trade_check_position_stars` |
| GET | `/api/trade-check/position-stars/history` | `trade_check_position_stars_history` |
| GET | `/api/trade-check/v4` | `trade_check_v4_get` |
| POST | `/api/trade-check/v4` | `trade_check_v4_post` |
| GET | `/api/trade-check/v4/health` | `trade_check_v4_health` |

#### `tc_v4_scan.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/trade-check/v4/scan` | `v4_scan` |

#### `test_cio_endpoints.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/test-cio` | `test_cio_page` |
| POST | `/test-cio/gemini` | `test_cio_gemini` |
| POST | `/test-cio/haiku` | `test_cio_haiku` |

#### `trade_alerts_app.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/myalerts` | `mobile_myalerts` |
| GET | `/m/myalerts` | `m_myalerts` |

#### `trade_alerts_endpoints.py` — 13 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/alerts` | `web_alerts` |
| GET | `/api/alerts/approval_window` | `approval_window_state` |
| POST | `/api/alerts/approve` | `approve_alert` |
| POST | `/api/alerts/approve_signal` | `approve_signal` |
| GET | `/api/alerts/approved_map` | `approved_map` |
| POST | `/api/alerts/create` | `create_alert` |
| POST | `/api/alerts/dismiss` | `dismiss_alert` |
| POST | `/api/alerts/dismiss_signal` | `dismiss_signal` |
| GET | `/api/alerts/ideas` | `alerts_ideas` |
| GET | `/api/alerts/list` | `list_alerts` |
| GET | `/api/alerts/pending_manual` | `alerts_pending_manual` |
| POST | `/api/alerts/seen` | `mark_alerts_seen` |
| GET | `/m/alerts` | `m_alerts` |

#### `trade_check_v34_endpoints.py` — 16 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/run-tc-screener` | `run_tc_screener` |
| GET | `/api/intraday/dashboard` | `intraday_dashboard` |
| POST | `/api/intraday/tick` | `intraday_tick` |
| GET | `/api/trade-check` | `trade_check_v36_get` |
| POST | `/api/trade-check` | `trade_check_v36_post` |
| POST | `/api/trade-check/intraday-paper/run` | `intraday_paper_run` |
| GET | `/api/trade-check/intraday-paper/status` | `intraday_paper_status` |
| GET | `/api/trade-check/intraday-scan` | `intraday_scan` |
| GET | `/api/trade-check/movers` | `movers` |
| GET | `/api/trade-check/screen-cached` | `screen_cached` |
| GET | `/api/trade-check/screen-nifty50` | `screen_nifty50` |
| POST | `/api/trade-check/tc-cache/refresh` | `tc_cache_refresh` |
| POST | `/api/trade-check/v34` | `check` |
| GET | `/api/trade-check/v34/health` | `health` |
| POST | `/api/trade-check/v34/promote` | `promote` |
| GET | `/api/trade-check/v36/health` | `health_v36` |

#### `trade_wall_approved.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/tradewall/approved` | `tradewall_approved` |
| POST | `/api/tradewall/approved/close` | `tradewall_approved_close` |
| POST | `/api/tradewall/approved/levels` | `tradewall_approved_levels` |

#### `trade_wall_endpoints.py` — 6 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/tradewall` | `tradewall_mobile_alias` |
| GET | `/api/tradewall` | `tradewall` |
| GET | `/api/tradewall/engine-rules` | `tradewall_engine_rules` |
| GET | `/api/tradewall/other-engines` | `tradewall_other_engines` |
| GET | `/m/trades` | `m_trades` |
| GET | `/trades` | `web_trades` |

#### `v10_endpoints.py` — 20 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/v10/append` | `v10_append` |
| POST | `/api/v10/backfill` | `v10_backfill` |
| GET | `/api/v10/buildup` | `v10_buildup` |
| GET | `/api/v10/candles` | `v10_candles` |
| GET | `/api/v10/divergence` | `v10_divergence` |
| POST | `/api/v10/gap-exit` | `v10_gap_exit` |
| GET | `/api/v10/maxpain` | `v10_maxpain` |
| GET | `/api/v10/performance` | `v10_performance` |
| GET | `/api/v10/performance/series` | `v10_performance_series` |
| GET | `/api/v10/positions` | `v10_positions` |
| GET | `/api/v10/positions/paired` | `v10_positions_paired` |
| GET | `/api/v10/signal` | `v10_signal` |
| GET | `/api/v10/strike_oi` | `v10_strike_oi` |
| GET | `/api/v10/summary` | `v10_summary` |
| GET | `/api/v10/tc_screen` | `v10_tc_screen` |
| POST | `/api/v10/tick` | `v10_tick` |
| GET | `/api/v10/trades` | `v10_trades` |
| GET | `/api/v10/trades/legs` | `v10_trades_legs` |
| GET | `/api/v10/trades/paired` | `v10_trades_paired` |
| GET | `/api/v10/vix` | `v10_vix` |

#### `v10_page_endpoints.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/m/digest` | `m_digest` |
| GET | `/m/gvm2` | `m_gvm2_fightcard` |
| GET | `/m/v10` | `m_v10_signal` |

#### `v12_backtest.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/v12/backtest` | `v12_backtest_run` |
| GET | `/api/v12/backtest/{bt_id}` | `v12_backtest_get` |

#### `v12_endpoints.py` — 15 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v12/basket` | `v12_basket_list` |
| POST | `/api/v12/basket` | `v12_basket_create` |
| POST | `/api/v12/basket/validate` | `v12_basket_validate` |
| DELETE | `/api/v12/basket/{bid}` | `v12_basket_delete` |
| GET | `/api/v12/basket/{bid}` | `v12_basket_get` |
| PUT | `/api/v12/basket/{bid}` | `v12_basket_update` |
| GET | `/api/v12/filters` | `v12_filters_meta` |
| GET | `/api/v12/filters/meta` | `v12_filters_meta` |
| GET | `/api/v12/presets` | `v12_presets` |
| GET | `/api/v12/screen` | `v12_screen` |
| GET | `/api/v12/universe` | `v12_universe_list` |
| POST | `/api/v12/universe/preview` | `v12_universe_preview` |
| POST | `/api/v12/universe/save` | `v12_universe_save` |
| GET | `/api/v12/universe/{uid}` | `v12_universe_get` |
| GET | `/screener` | `screener_page` |

#### `v13_presets_endpoints.py` — 7 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v13/presets` | `list_presets` |
| POST | `/api/v13/presets` | `save_preset` |
| DELETE | `/api/v13/presets/{pid}` | `delete_preset` |
| PATCH | `/api/v13/presets/{pid}` | `rename_preset` |
| GET | `/api/v13/theme/list` | `theme_list` |
| POST | `/api/v13/theme/run` | `theme_run` |
| POST | `/api/v13/theme/save` | `theme_save_validated` |

#### `v14_endpoints.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v14/positions` | `v14_positions` |
| GET | `/api/v14/summary` | `v14_summary` |
| POST | `/api/v14/tick` | `v14_tick` |
| GET | `/api/v14/trades` | `v14_trades` |

#### `v8_approved_trades.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/mobile/home/approved-trades` | `approved_trades` |

#### `v8_backfill_endpoints.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/v8/backfill/metrics` | `backfill_metrics` |
| POST | `/api/v8/backfill/sync_nifty50` | `sync_nifty50_endpoint` |
| POST | `/api/v8/backfill/sync_universe` | `sync_universe_endpoint` |

#### `v8_book_canon.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v8/book_canon` | `api_book_canon` |

#### `v8_daylog_extras.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v8/daylog/series` | `v8_daylog_series` |

#### `v8_endpoints.py` — 31 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v8/adr` | `adr_only` |
| GET | `/api/v8/adr_history` | `adr_history` |
| GET | `/api/v8/adr_intraday` | `adr_intraday_trend` |
| GET | `/api/v8/bm_stock_detail/{symbol}` | `bm_stock_detail` |
| GET | `/api/v8/br_stock_detail/{symbol}` | `br_stock_detail` |
| GET | `/api/v8/daylog` | `v8_daylog` |
| GET | `/api/v8/domestic_live` | `domestic_live` |
| GET | `/api/v8/filter_config/{basket}` | `filter_config` |
| GET | `/api/v8/forthcoming-results` | `forthcoming_results` |
| GET | `/api/v8/funnel/{basket}` | `funnel_counts` |
| GET | `/api/v8/funnel_detail/{basket}` | `funnel_detail` |
| GET | `/api/v8/global_indices` | `v8_global_indices` |
| GET | `/api/v8/indiavix_intraday` | `v8_indiavix_intraday` |
| GET | `/api/v8/live_metrics` | `v8_live_metrics` |
| GET | `/api/v8/market_mood` | `market_mood` |
| GET | `/api/v8/metrics/all` | `metrics_all` |
| GET | `/api/v8/nifty50_sectors` | `v8_nifty50_sectors` |
| GET | `/api/v8/nifty50_sectors/{theme}/holdings` | `v8_nifty50_sector_holdings` |
| GET | `/api/v8/ohol` | `ohol` |
| GET | `/api/v8/positions` | `v8_positions` |
| GET | `/api/v8/qualified/{basket}` | `qualified` |
| GET | `/api/v8/raw` | `raw_metrics` |
| GET | `/api/v8/scan` | `scan` |
| GET | `/api/v8/segment_day` | `segment_day` |
| GET | `/api/v8/sm_stock_detail/{symbol}` | `sm_stock_detail` |
| GET | `/api/v8/sr_stock_detail/{symbol}` | `sr_stock_detail` |
| GET | `/api/v8/stock_passcount/{basket}` | `stock_passcount` |
| GET | `/api/v8/theme_sectors` | `v8_theme_sectors` |
| GET | `/api/v8/theme_sectors/{theme}/holdings` | `v8_theme_sector_holdings` |
| GET | `/api/v8/trades` | `v8_trades` |
| GET | `/api/v8/v9_pairs_sectors` | `v9_pairs_sectors` |

#### `v8_era.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v8/era` | `api_v8_era` |

#### `v8_futures.py` — 5 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/v8/futures/add` | `add_futures` |
| GET | `/api/v8/futures/list` | `list_futures` |
| POST | `/api/v8/futures/remove` | `remove_futures` |
| POST | `/api/v8/futures/sync_lots` | `sync_lots` |
| POST | `/api/v8/futures/upload` | `upload_futures` |

#### `v8_futures_book.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v8/futures_book` | `v8_futures_book` |

#### `v8_intra_backtest_endpoints.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v8/backtest/last` | `last_result` |
| GET | `/api/v8/backtest/log` | `backtest_log` |
| POST | `/api/v8/backtest/run` | `run_backtest` |
| POST | `/api/v8/backtest/simulate` | `simulate` |

#### `v8_marker_ticks.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v8/marker_ticks/{symbol}` | `marker_ticks_symbol` |

#### `v8_metrics_gapfill.py` — 1 route

| Method | Path | Handler |
|---|---|---|
| POST | `/api/v8/backfill/metrics_gapfill` | `metrics_gapfill` |

#### `v8_pivot_star.py` — 3 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v8/pivot_star` | `pivot_star` |
| POST | `/api/v8/pivot_star/run` | `pivot_star_run` |
| GET | `/api/v8/tc_score_latest` | `tc_score_latest` |

#### `v8_replay_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/v8/replay/run` | `replay_run` |
| GET | `/api/v8/replay/summary` | `replay_summary` |

#### `v9_endpoints.py` — 11 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/v9/backtest` | `v9_backtest` |
| GET | `/api/v9/best_combo` | `v9_best_combo` |
| POST | `/api/v9/discover` | `v9_discover` |
| GET | `/api/v9/pairs` | `v9_pairs` |
| GET | `/api/v9/paper/closed` | `v9_paper_closed` |
| GET | `/api/v9/paper/open` | `v9_paper_open` |
| POST | `/api/v9/paper/rebalance` | `v9_paper_rebalance` |
| GET | `/api/v9/paper/summary` | `v9_paper_summary` |
| GET | `/api/v9/results` | `v9_results` |
| GET | `/api/v9/results/{combo_id}` | `v9_results_combo` |
| GET | `/api/v9/trades/{combo_id}` | `v9_trades` |

#### `volume_flow_endpoints.py` — 2 routes

| Method | Path | Handler |
|---|---|---|
| GET | `/api/quality-bullish-basis` | `quality_bullish_basis` |
| GET | `/api/volume-flow` | `volume_flow` |

#### `yahoo_symbol_resolver.py` — 4 routes

| Method | Path | Handler |
|---|---|---|
| POST | `/api/admin/yahoo/heal-nse` | `yahoo_heal_nse` |
| POST | `/api/admin/yahoo/resolve` | `yahoo_resolve` |
| GET | `/api/feeds/price-excluded` | `price_excluded` |
| GET | `/api/feeds/symbol-map` | `symbol_map` |

<!-- END GENERATED ROUTES -->
