# Scorr API Reference

Auto-documented from `main.py` (v2.9.53). Base URL: `https://quantproject-production.up.railway.app`

**Conventions**
- `🔒 admin` = requires `X-Admin-Token` header (validated by `_check_admin`; enforced only when `ADMIN_TOKEN` env is set).
- `🛡 guard` = additionally requires `DEPLOY_GUARD=true` (`_check_deploy_guard`).
- `🔑 auth` = page sits behind login middleware (`PROTECTED` in `scorr_auth.py`): `/`, `/dashboard`, `/cio`, `/cio2`, `/ask`, `/check`, `/sector`.
- All endpoints below are defined **directly in `main.py`**. Endpoints mounted via `include_router(...)` live in their own files — see [Mounted Routers](#mounted-routers).

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
| GET | `/api/v8/live_metrics` | `v8_live_metrics` | Live CMP, day%, hourly% for active futures universe |
| POST | `/api/momentum/run` | `momentum_run` | 🔒 admin — run `momentum_daily.compute_momentum()` |

---

## Paper Engine (V8 paper)

| Method | Path | Handler | Description |
|---|---|---|---|
| POST | `/api/paper/compute_pivots` | `paper_compute_pivots` | 🔒 admin — compute paper pivots |
| POST | `/api/paper/tick` | `paper_tick_now` | 🔒 admin — run paper tick (pulls buy/sell slots from market_mood) |
| GET | `/api/paper/status` | `paper_status` | Open positions (with unrealised P&L), recent trades, missed, summary |
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
| GET | `/api/qb/rebalance_log` | rebalance log |
| GET | `/api/qb/registry` | registry |

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
| POST | `/api/pcr/intraday/compute` | compute intraday PCR |
| POST | `/api/pcr/backfill` | backfill PCR |

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

### Check — `check_endpoint.py`
| Method | Path | Description |
|---|---|---|
| POST | `/api/check` | run checklist |
| GET | `/api/check/rule/{rule}` | single rule |
| GET | `/api/check/health` | health |

### Sector — `sector_endpoints.py`, `sector_brief_endpoints.py`
| Method | Path | Description |
|---|---|---|
| GET | `/api/sector/rotation` | sector rotation |
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

---

> Router descriptions are inferred from route names/signatures, not full handler review — verify against source or the live `GET /openapi.json` (machine-readable, always current) before relying on request/response shapes.
