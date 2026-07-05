import os
import json
import asyncio
import httpx
import psycopg
from fastapi import APIRouter, Request, Response

import yahoo_ondemand

# ── MCP dispatch layer ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Extracted from main.py (File 5/5 split, piece B). Self-contained:
# reads env vars directly, owns its get_conn, imports yahoo_ondemand.
# NO import from main.py -> no circular import.
# Exposes: MCP_TOOLS, router (POST /mcp).

DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN", "quantproject-production.up.railway.app")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
VERSION = os.getenv("APP_VERSION", "2.9.22")

router = APIRouter()

def get_conn():
    return psycopg.connect(DATABASE_URL)

MCP_TOOLS = [
    {"name":"server_now","description":"Authoritative India time (Asia/Kolkata, UTC+5:30).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"health_report","description":"Full Scorr system health report card.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"run_diagnosis","description":"Full system diagnosis — 6 sections, traffic-light per section.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"digest_daily","description":"Daily Digest sections 1-5 baked from DB.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"run_momentum","description":"GVM: recompute daily momentum (M) for all stocks from raw_prices.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"gvm_recompute","description":"GVM: full recompute.","inputSchema":{"type":"object","properties":{"refresh_momentum":{"type":"boolean"}},"required":[]}},
    {"name":"gvm_history","description":"GVM: get the GVM score trend series for a stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"days":{"type":"integer"}},"required":["symbol"]}},
    {"name":"get_gvm","description":"Fetch full GVM score for a stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"gvm_company","description":"GVM: full peer-benchmarked company analytics report (rating, G/V/M, per-parameter peer comparison, segment rank, overview/takeaways). Persists detail to gvm_scores.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"gvm_search","description":"GVM: autocomplete search companies by symbol or name.","inputSchema":{"type":"object","properties":{"q":{"type":"string"},"limit":{"type":"integer"}},"required":["q"]}},
    {"name":"get_top_stocks","description":"Get top N stocks by GVM.","inputSchema":{"type":"object","properties":{"n":{"type":"integer"},"verdict":{"type":"string"}},"required":["n"]}},
    {"name":"get_sector","description":"Get all stocks in a sector ordered by GVM.","inputSchema":{"type":"object","properties":{"sector":{"type":"string"}},"required":["sector"]}},
    {"name":"get_filter","description":"Filter stocks by GVM range.","inputSchema":{"type":"object","properties":{"min_gvm":{"type":"number"},"max_gvm":{"type":"number"}},"required":[]}},
    {"name":"get_sector_rating","description":"Get sector-level mcap-weighted GVM ratings.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"get_intraday","description":"Intraday OHLC for ANY stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"days":{"type":"integer"},"interval":{"type":"string"},"source":{"type":"string"}},"required":["symbol"]}},
    {"name":"get_cmp","description":"Get latest CMP for a stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"fyers_quote","description":"Fetch live futures quote from Fyers for a symbol.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"backfill_intraday","description":"MANUAL Yahoo fallback: fetch 7 days of 5-min OHLC for all futures.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"heal_intraday","description":"Fill TODAY's morning 1-min gap in intraday_prices from Yahoo.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"run_yahoo_daily","description":"Trigger Yahoo daily OHLC update for raw_prices (background).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"backfill_indices","description":"Backfill NIFTY50 + BANKNIFTY 1-min OHLC into intraday_prices.","inputSchema":{"type":"object","properties":{"days":{"type":"integer"}},"required":[]}},
    {"name":"backfill_indian_indices","description":"One-time daily-OHLC backfill of SENSEX, FINNIFTY, MIDCAPNIFTY into raw_prices from Yahoo (default 5yr). dry_run=true test-fetches and reports per-symbol row counts without writing.","inputSchema":{"type":"object","properties":{"lookback":{"type":"string"},"dry_run":{"type":"boolean"}},"required":[]}},
    {"name":"paper_compute_pivots","description":"PAPER: compute rolling-5-day pivots for all futures.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"paper_tick","description":"PAPER: run one paper-engine tick.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"paper_status","description":"PAPER: open positions + recent closed trades + summary.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"paper_pivots","description":"PAPER: latest rolling-5 pivot levels per stock.","inputSchema":{"type":"object","properties":{"limit":{"type":"integer"}},"required":[]}},
    {"name":"run_v8_engine","description":"Run the V8 EOD engine — compute metrics + write signals to DB.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"run_v8_for_date","description":"Backfill v8_metrics for a PAST date (YYYY-MM-DD).","inputSchema":{"type":"object","properties":{"target_date":{"type":"string"}},"required":["target_date"]}},
    {"name":"backfill_v8_metrics","description":"One-time backfill: compute + insert v8_metrics for Jun 2025-Jun 2026 (258 days, 80 symbols, ~20560 rows). Takes ~5-10 mins server-side. Run once then check v8_metrics row count.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"sync_futures_universe","description":"Sync futures_universe with Fyers feed (last 7 days). Strips expiry suffix, adds missing symbols, deactivates absent 2+ Mondays. Also runs auto Monday 08:00 IST.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"get_v8_metrics","description":"Get computed V8 metrics for one stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"get_v8_metrics_all","description":"Get all metrics for the full universe (latest date).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"get_v8_live_metrics","description":"Get real-time CMP, day%, hourly gain for the universe.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v8_run_signal_writer","description":"V8: manually trigger live signal writer (19 metrics + qualified).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"bt7_run","description":"BT7 parity harness (cc#218): walk a trading day 09:15-15:30 in 5-min steps, driving the REAL writer+exits under the bt7_sim sandbox into harness_* shadow tables. Args: date (YYYY-MM-DD), label.","inputSchema":{"type":"object","properties":{"date":{"type":"string"},"label":{"type":"string"}},"required":["date","label"]}},
    {"name":"bt7_diff","description":"BT7 zero-diff report (cc#218) between two run labels on quals+trades (symbol/side/basket). label_b may be 'golden_YYYYMMDD' to compare against the archived live-latched quals (D6).","inputSchema":{"type":"object","properties":{"label_a":{"type":"string"},"label_b":{"type":"string"}},"required":["label_a","label_b"]}},
    {"name":"health_feeds","description":"Status dashboard for all data feeds.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"env_check","description":"Diagnostic: which env vars are visible.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"run_sql","description":"Run any SQL query on Railway PostgreSQL.","inputSchema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}},
    {"name":"load_input_from_drive","description":"Reload input_raw from Drive CSV.","inputSchema":{"type":"object","properties":{"file_id":{"type":"string"}},"required":["file_id"]}},
    {"name":"load_screener_from_drive","description":"Reload screener_raw (WIDE schema) from a Drive CSV.","inputSchema":{"type":"object","properties":{"file_id":{"type":"string"}},"required":["file_id"]}},
    {"name":"load_earnings_from_screener","description":"Scrape Screener.in and refresh earnings_calendar.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"check_blackout","description":"Check if a symbol is in earnings blackout.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"github_read","description":"Read any file from the GitHub repo.","inputSchema":{"type":"object","properties":{"filepath":{"type":"string"}},"required":["filepath"]}},
    {"name":"github_list","description":"List files in the repo.","inputSchema":{"type":"object","properties":{"path":{"type":"string"}},"required":[]}},
    {"name":"github_push","description":"Create or update a file.","inputSchema":{"type":"object","properties":{"filepath":{"type":"string"},"new_content":{"type":"string"},"commit_message":{"type":"string"},"create_if_missing":{"type":"boolean"}},"required":["filepath","new_content","commit_message"]}},
    {"name":"github_delete","description":"Delete a file.","inputSchema":{"type":"object","properties":{"filepath":{"type":"string"},"commit_message":{"type":"string"}},"required":["filepath"]}},
    {"name":"v8_market_mood","description":"V8: Market Mood gate (ADR + Nifty D/W/M) + Buy/Sell slot allocation.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v8_qualified","description":"V8: Get qualified stocks for a basket.","inputSchema":{"type":"object","properties":{"basket":{"type":"string"},"limit":{"type":"integer"}},"required":["basket"]}},
    {"name":"v8_filter_config","description":"V8: Get filter thresholds for a basket.","inputSchema":{"type":"object","properties":{"basket":{"type":"string"}},"required":["basket"]}},
    {"name":"v8_sell_overbought","description":"V8: Get Sell Overbought signals.","inputSchema":{"type":"object","properties":{"limit":{"type":"integer"}},"required":[]}},
    {"name":"v8_futures_list","description":"V8: List active futures universe stocks.","inputSchema":{"type":"object","properties":{"active_only":{"type":"boolean"}},"required":[]}},
    {"name":"v8_futures_upload","description":"V8: Replace futures universe with new stock list.","inputSchema":{"type":"object","properties":{"stocks":{"type":"array","items":{"type":"string"}}},"required":["stocks"]}},
    {"name":"get_top_gainers","description":"Top gainers by day% from EOD data, joined with GVM scores.","inputSchema":{"type":"object","properties":{"price_date":{"type":"string"},"n":{"type":"integer"},"min_gvm":{"type":"number"},"min_day_pct":{"type":"number"},"universe":{"type":"string"},"min_volume":{"type":"integer"}},"required":[]}},
    {"name":"get_global","description":"Latest global scorecard — indices, commodities, currency.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"fetch_global","description":"Manually trigger global scorecard fetch into global_indices.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"backfill_global","description":"One-time backfill of N years daily global history.","inputSchema":{"type":"object","properties":{"years":{"type":"integer"},"clean":{"type":"boolean"}},"required":[]}},
    {"name":"get_global_intraday","description":"Commodity/crypto 5-min intraday bars (7-day rolling).","inputSchema":{"type":"object","properties":{"name":{"type":"string"},"days":{"type":"integer"}},"required":["name"]}},
    {"name":"fetch_global_intraday","description":"Manually trigger commodity/crypto 5-min intraday fetch.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"qb_eod_check","description":"Quant Basket: run EOD stop-loss check + P&L mark for a basket.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"}},"required":[]}},
    {"name":"qb_positions","description":"Quant Basket: get open positions with P&L, stop prices.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"},"status":{"type":"string"}},"required":[]}},
    {"name":"qb_summary","description":"Quant Basket: portfolio summary — market value, unrealised P&L, realised P&L.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"}},"required":[]}},
    {"name":"qb_rebalance_log","description":"Quant Basket: rebalance + EOD check history.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"},"limit":{"type":"integer"}},"required":[]}},
    {"name":"qb_registry","description":"Quant Basket: registry of all baskets.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"}},"required":[]}},
    {"name":"fix_all_allocations","description":"Quant Basket: fix allocation column + insert NIFTYBEES residual for all 4 baskets.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"daily_adr","description":"ADR trend last N days from adr_daily.","inputSchema":{"type":"object","properties":{"days":{"type":"integer"}},"required":[]}},
    {"name":"daily_pcr","description":"PCR trend last N days from pcr_daily.","inputSchema":{"type":"object","properties":{"underlying":{"type":"string"},"days":{"type":"integer"}},"required":[]}},
    {"name":"compute_daily_metrics","description":"Manually trigger ADR + PCR compute-and-store.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"refresh_status","description":"Show AI content refresh status.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"content_update","description":"Manual content writer for input_raw.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"field":{"type":"string","enum":["overview","key_takeaway","result_analysis"]},"content":{"type":"string"}},"required":["symbol","field","content"]}},
    {"name":"v9_discover","description":"V9 Pair Strategy: run pair discovery — find valid pairs from 209 futures (GVM>=6, same segment, corr>=0.75, cointegrated).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v9_backtest","description":"V9 Pair Strategy: run full backtest — 10 parameter combos on all valid pairs, 2025 EOD data.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v9_results","description":"V9 Pair Strategy: get backtest results summary — all combos ranked by total PnL.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v9_best_combo","description":"V9 Pair Strategy: get best parameter combo by total PnL.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v10_signal","description":"V10 ST+EMA: current NIFTY directional signal (ST 150/3 10m + EMA 3/10 30m gate, SL100/T200).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v10_tick","description":"V10 ST+EMA: run one 5-min cycle — append 5m bar, compute signal, Telegram alert on BUY/SELL.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"pcr_intraday","description":"5-min intraday PCR trend (ATM±5 + total) for NIFTY/BANKNIFTY from pcr_intraday.","inputSchema":{"type":"object","properties":{"underlying":{"type":"string"},"days":{"type":"integer"}},"required":[]}},
    {"name":"compute_pcr_intraday","description":"Compute/self-heal 5-min PCR into pcr_intraday (ts optional = single bar, else heal all missing).","inputSchema":{"type":"object","properties":{"ts":{"type":"string"}},"required":[]}},
    {"name":"pcr_backfill","description":"One-time index option OI+PCR backfill (NIFTY+BANKNIFTY ATM+-10 monthly). Fetches OI via Fyers History API (oi_flag=1), upserts onto option_chain, recomputes pcr_intraday + pcr_daily. start/end=YYYY-MM-DD. Fail-loud if no OI column.","inputSchema":{"type":"object","properties":{"start":{"type":"string"},"end":{"type":"string"}},"required":["start","end"]}},
    {"name":"v8_replay_run","description":"V8 PAPER REPLAY: true 5-min stepped replay from start date. wipe=true clears the paper book first (DESTRUCTIVE). Walks intraday bar-by-bar, point-in-time entries/exits.","inputSchema":{"type":"object","properties":{"start":{"type":"string"},"end":{"type":"string"},"wipe":{"type":"boolean"}},"required":["start"]}},
    {"name":"v8_replay_summary","description":"V8 PAPER REPLAY: current paper book stats — open positions + realized trade stats by basket.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"sector_brief_batch","description":"Generate AI sector briefs for all 129 segments via Claude Haiku and cache in sector_briefs table. Runs in background. refresh=true regenerates all.","inputSchema":{"type":"object","properties":{"refresh":{"type":"boolean"}},"required":[]}},
    {"name":"sector_brief_status","description":"Check how many of the 129 sector briefs are cached in DB vs pending generation.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"anthropic_chat","description":"Call Claude via Anthropic API (bypasses chat limit). Returns response, tokens used, cost estimate. Use when chat is at 98%+ weekly limit.","inputSchema":{"type":"object","properties":{"prompt":{"type":"string"},"model":{"type":"string"},"max_tokens":{"type":"integer"}},"required":["prompt"]}},
    {"name":"backfill_futures_fyers","description":"cc#159: on-demand Fyers REST 5-min futures backfill (fixes cc#152/153 fut/eq source-collision gap). start (YYYY-MM-DD, default 2026-06-26), end (default today), symbols (optional array, default all ~212 active futures). One-at-a-time REST pacing (5s/symbol) — runs ~15-20 min for the full universe, blocked automatically during market hours (09:15-15:30 IST). Returns symbols_processed, bars_written, gaps_remaining.","inputSchema":{"type":"object","properties":{"start":{"type":"string"},"end":{"type":"string"},"symbols":{"type":"array","items":{"type":"string"}}},"required":[]}},
]

async def _call_tool(name, args):
    async with httpx.AsyncClient(timeout=600) as client:
        h = {"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}
        if name == "server_now": r = await client.get(f"{BASE_URL}/api/now"); return r.json()
        elif name == "health_report": r = await client.get(f"{BASE_URL}/api/health/report"); return r.json()
        elif name == "run_diagnosis": r = await client.get(f"{BASE_URL}/api/diagnosis"); return r.json()
        elif name == "digest_daily": r = await client.get(f"{BASE_URL}/api/digest/daily"); return r.json()
        elif name == "run_momentum": r = await client.post(f"{BASE_URL}/api/momentum/run", headers=h); return r.json()
        elif name == "gvm_recompute": r = await client.post(f"{BASE_URL}/api/gvm/recompute", params={"refresh_momentum": args.get("refresh_momentum",True)}, headers=h); return r.json()
        elif name == "gvm_history": r = await client.get(f"{BASE_URL}/api/gvm/history/{args['symbol']}", params={"days": args.get("days",180)}); return r.json()
        elif name == "get_gvm": r = await client.get(f"{BASE_URL}/api/gvm/{args['symbol']}"); return r.json()
        elif name == "gvm_company": r = await client.get(f"{BASE_URL}/api/gvm/company/{args['symbol']}"); return r.json()
        elif name == "gvm_search": r = await client.get(f"{BASE_URL}/api/gvm/search", params={"q": args["q"], "limit": args.get("limit",12)}); return r.json()
        elif name == "get_top_stocks":
            params = {}
            if args.get("verdict"): params["verdict"] = args["verdict"]
            r = await client.get(f"{BASE_URL}/api/gvm/top/{args['n']}", params=params); return r.json()
        elif name == "get_sector": r = await client.get(f"{BASE_URL}/api/sectors", params={"segment": args["sector"]}); return r.json()
        elif name == "get_filter": r = await client.get(f"{BASE_URL}/api/filter", params={"min_gvm": args.get("min_gvm",0), "max_gvm": args.get("max_gvm",10)}); return r.json()
        elif name == "get_sector_rating": r = await client.get(f"{BASE_URL}/api/sectors"); return r.json()
        elif name == "get_intraday":
            sym = (args.get("symbol") or "").upper()
            try: days = int(args.get("days") or 15)
            except (TypeError, ValueError): days = 15
            interval = (args.get("interval") or "5m").lower(); source = (args.get("source") or "auto").lower()
            return await asyncio.to_thread(yahoo_ondemand.get_intraday_smart, sym, days, interval, "NS", source)
        elif name == "get_cmp": r = await client.get(f"{BASE_URL}/api/cmp/{args['symbol']}"); return r.json()
        elif name == "fyers_quote": r = await client.get(f"{BASE_URL}/api/fyers/quote/{args['symbol'].upper()}"); return r.json()
        elif name == "backfill_intraday": r = await client.post(f"{BASE_URL}/api/admin/backfill_intraday", headers=h); return r.json()
        elif name == "heal_intraday": r = await client.post(f"{BASE_URL}/api/admin/heal_intraday", headers=h); return r.json()
        elif name == "run_yahoo_daily": r = await client.post(f"{BASE_URL}/api/admin/run_yahoo_daily", headers=h); return r.json()
        elif name == "backfill_indices": r = await client.post(f"{BASE_URL}/api/admin/backfill_indices", params={"days": args.get("days",7)}, headers=h); return r.json()
        elif name == "backfill_indian_indices":
            import admin_index_backfill
            return await asyncio.to_thread(admin_index_backfill.run_backfill, args.get("lookback","5y"), args.get("dry_run", False))
        elif name == "paper_compute_pivots": r = await client.post(f"{BASE_URL}/api/paper/compute_pivots", headers=h); return r.json()
        elif name == "paper_tick": r = await client.post(f"{BASE_URL}/api/paper/tick", headers=h); return r.json()
        elif name == "paper_status": r = await client.get(f"{BASE_URL}/api/paper/status"); return r.json()
        elif name == "paper_pivots": r = await client.get(f"{BASE_URL}/api/paper/pivots", params={"limit": args.get("limit",250)}); return r.json()
        elif name == "run_v8_engine": r = await client.post(f"{BASE_URL}/api/v8/run", headers=h); return r.json()
        elif name == "run_v8_for_date": r = await client.post(f"{BASE_URL}/api/v8/run_for_date", params={"target_date": args["target_date"]}, headers=h); return r.json()
        elif name == "backfill_v8_metrics": r = await client.post(f"{BASE_URL}/api/v8/backfill/metrics", headers=h); return r.json()
        elif name == "sync_futures_universe": r = await client.post(f"{BASE_URL}/api/v8/backfill/sync_universe", headers=h); return r.json()
        elif name == "get_v8_metrics": r = await client.get(f"{BASE_URL}/api/v8/metrics/{args['symbol']}"); return r.json()
        elif name == "get_v8_metrics_all": r = await client.get(f"{BASE_URL}/api/v8/metrics/all"); return r.json()
        elif name == "get_v8_live_metrics": r = await client.get(f"{BASE_URL}/api/v8/live_metrics"); return r.json()
        elif name == "v8_run_signal_writer": r = await client.post(f"{BASE_URL}/api/v8/run_signal_writer", headers=h); return r.json()
        elif name == "bt7_run": r = await client.post(f"{BASE_URL}/api/v8/bt7_run", headers=h, params={"date": args["date"], "label": args["label"]}); return r.json()
        elif name == "bt7_diff": r = await client.get(f"{BASE_URL}/api/v8/bt7_diff", headers=h, params={"label_a": args["label_a"], "label_b": args["label_b"]}); return r.json()
        elif name == "health_feeds": r = await client.get(f"{BASE_URL}/api/health/feeds"); return r.json()
        elif name == "env_check": r = await client.get(f"{BASE_URL}/api/admin/env_check", headers=h); return r.json()
        elif name == "run_sql":
            q = args["query"]
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(q)
                    if cur.description:
                        cols = [d[0] for d in cur.description]; rows = [dict(zip(cols,r)) for r in cur.fetchall()]
                        conn.commit(); return {"rows": rows, "count": len(rows)}
                    conn.commit(); return {"status":"ok","rowcount":cur.rowcount}
            except Exception as e: return {"error": str(e)}
        elif name == "load_input_from_drive": r = await client.post(f"{BASE_URL}/api/admin/load_input_from_drive", json={"file_id": args["file_id"]}); return r.json()
        elif name == "load_screener_from_drive": r = await client.post(f"{BASE_URL}/api/admin/load_screener_from_drive", json={"file_id": args["file_id"]}); return r.json()
        elif name == "load_earnings_from_screener": r = await client.post(f"{BASE_URL}/api/admin/load_earnings_from_screener", headers=h); return r.json()
        elif name == "check_blackout":
            sym = args["symbol"].upper()
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT ticker,ex_date,event_type FROM earnings_calendar WHERE UPPER(ticker)=%s ORDER BY id DESC LIMIT 5", (sym,))
                rows = cur.fetchall()
            return {"symbol": sym, "events": [{"ex_date": str(r[1]), "event_type": r[2]} for r in rows]}
        elif name == "github_read": r = await client.get(f"{BASE_URL}/api/admin/github_read", params={"filepath": args["filepath"]}, headers=h); return r.json()
        elif name == "github_list": r = await client.get(f"{BASE_URL}/api/admin/github_list", params={"path": args.get("path","")}, headers=h); return r.json()
        elif name == "github_push": r = await client.post(f"{BASE_URL}/api/admin/github_push", json=args, headers=h); return r.json()
        elif name == "github_delete": r = await client.post(f"{BASE_URL}/api/admin/github_delete", json=args, headers=h); return r.json()
        elif name == "v8_market_mood": r = await client.get(f"{BASE_URL}/api/v8/market_mood"); return r.json()
        elif name == "v8_qualified": r = await client.get(f"{BASE_URL}/api/v8/qualified/{args['basket']}", params={"limit": args.get("limit",50)}); return r.json()
        elif name == "v8_filter_config": r = await client.get(f"{BASE_URL}/api/v8/filter_config/{args['basket']}"); return r.json()
        elif name == "v8_sell_overbought": r = await client.get(f"{BASE_URL}/api/v8/sell_overbought", params={"limit": args.get("limit",50)}); return r.json()
        elif name == "v8_futures_list": r = await client.get(f"{BASE_URL}/api/v8/futures/list", params={"active_only": args.get("active_only",True)}); return r.json()
        elif name == "v8_futures_upload": r = await client.post(f"{BASE_URL}/api/v8/futures/upload", json={"stocks": args["stocks"]}); return r.json()
        elif name == "get_global": r = await client.get(f"{BASE_URL}/api/global"); return r.json()
        elif name == "fetch_global": r = await client.post(f"{BASE_URL}/api/admin/fetch_global", headers=h); return r.json()
        elif name == "backfill_global": r = await client.post(f"{BASE_URL}/api/admin/backfill_global", params={"years": args.get("years",5), "clean": args.get("clean",True)}, headers=h); return r.json()
        elif name == "get_global_intraday": r = await client.get(f"{BASE_URL}/api/global/intraday/{args['name']}", params={"days": args.get("days",7)}); return r.json()
        elif name == "fetch_global_intraday": r = await client.post(f"{BASE_URL}/api/admin/fetch_global_intraday", headers=h); return r.json()
        elif name == "get_top_gainers":
            params = {}
            for k in ("price_date","n","min_gvm","min_day_pct","universe","min_volume"):
                if args.get(k) is not None: params[k] = args[k]
            r = await client.get(f"{BASE_URL}/api/market/top_gainers", params=params); return r.json()
        elif name == "qb_eod_check":
            r = await client.post(f"{BASE_URL}/api/qb/eod_check", params={"basket_name": args.get("basket_name","large_cap")}, headers=h); return r.json()
        elif name == "qb_positions":
            r = await client.get(f"{BASE_URL}/api/qb/positions", params={"basket_name": args.get("basket_name","large_cap"), "status": args.get("status","open")}); return r.json()
        elif name == "qb_summary":
            r = await client.get(f"{BASE_URL}/api/qb/summary", params={"basket_name": args.get("basket_name","large_cap")}); return r.json()
        elif name == "qb_rebalance_log":
            r = await client.get(f"{BASE_URL}/api/qb/rebalance_log", params={"basket_name": args.get("basket_name","large_cap"), "limit": args.get("limit",30)}); return r.json()
        elif name == "qb_registry":
            params = {}
            if args.get("basket_name"): params["basket_name"] = args["basket_name"]
            r = await client.get(f"{BASE_URL}/api/qb/registry", params=params); return r.json()
        elif name == "fix_all_allocations":
            r = await client.post(f"{BASE_URL}/api/qb/fix_all_allocations", headers=h); return r.json()
        elif name == "daily_adr":
            r = await client.get(f"{BASE_URL}/api/daily/adr", params={"days": args.get("days",5)}); return r.json()
        elif name == "daily_pcr":
            r = await client.get(f"{BASE_URL}/api/daily/pcr", params={"underlying": args.get("underlying","NIFTY"), "days": args.get("days",5)}); return r.json()
        elif name == "compute_daily_metrics":
            r = await client.post(f"{BASE_URL}/api/daily/compute_metrics", headers=h); return r.json()
        elif name == "refresh_status":
            r = await client.get(f"{BASE_URL}/api/admin/refresh_status", headers=h); return r.json()
        elif name == "content_update":
            r = await client.post(f"{BASE_URL}/api/admin/content_update",
                json={"symbol": args["symbol"], "field": args["field"], "content": args["content"]}, headers=h)
            return r.json()
        elif name == "v9_discover":
            r = await client.post(f"{BASE_URL}/api/v9/discover", headers=h); return r.json()
        elif name == "v9_backtest":
            r = await client.post(f"{BASE_URL}/api/v9/backtest", headers=h); return r.json()
        elif name == "v9_results":
            r = await client.get(f"{BASE_URL}/api/v9/results"); return r.json()
        elif name == "v9_best_combo":
            r = await client.get(f"{BASE_URL}/api/v9/best_combo"); return r.json()
        elif name == "v10_signal":
            r = await client.get(f"{BASE_URL}/api/v10/signal"); return r.json()
        elif name == "v10_tick":
            r = await client.post(f"{BASE_URL}/api/v10/tick", headers=h); return r.json()
        elif name == "pcr_intraday":
            r = await client.get(f"{BASE_URL}/api/pcr/intraday", params={"underlying": args.get("underlying","NIFTY"), "days": args.get("days",2)}); return r.json()
        elif name == "compute_pcr_intraday":
            params = {"ts": args["ts"]} if args.get("ts") else {}
            r = await client.post(f"{BASE_URL}/api/pcr/intraday/compute", params=params, headers=h); return r.json()
        elif name == "pcr_backfill":
            r = await client.post(f"{BASE_URL}/api/pcr/backfill", params={"start": args["start"], "end": args["end"]}, headers=h); return r.json()
        elif name == "v8_replay_run":
            params = {"start": args["start"], "wipe": args.get("wipe", True)}
            if args.get("end"): params["end"] = args["end"]
            r = await client.post(f"{BASE_URL}/api/v8/replay/run", params=params, headers=h); return r.json()
        elif name == "v8_replay_summary":
            r = await client.get(f"{BASE_URL}/api/v8/replay/summary"); return r.json()
        elif name == "sector_brief_batch":
            r = await client.post(f"{BASE_URL}/api/admin/sector/brief/batch", params={"refresh": args.get("refresh", False)}, headers=h); return r.json()
        elif name == "sector_brief_status":
            r = await client.get(f"{BASE_URL}/api/admin/sector/brief/status"); return r.json()
        elif name == "anthropic_chat":
            prompt = args["prompt"]
            model = args.get("model", "claude-sonnet-4-6")
            max_tokens = args.get("max_tokens", 1024)
            r = await client.post(
                f"{BASE_URL}/api/anthropic/chat",
                json={"prompt": prompt, "model": model, "max_tokens": max_tokens}
            )
            return r.json()
        elif name == "backfill_futures_fyers":
            # cc#159: full-universe run is ~15-20 min (sequential, 5s/symbol REST
            # pacing) — well past the client's default 600s timeout, so this call
            # gets its own longer budget instead of raising a premature ReadTimeout.
            body = {"start": args.get("start"), "end": args.get("end"), "symbols": args.get("symbols")}
            r = await client.post(f"{BASE_URL}/api/admin/backfill_futures_fyers", json=body, headers=h, timeout=1500)
            return r.json()
        return {"error": f"Unknown tool: {name}"}

@router.post("/mcp")
async def mcp_endpoint(req: Request):
    body = await req.json(); method = body.get("method"); params = body.get("params",{}); msg_id = body.get("id")
    if method == "initialize":
        return {"jsonrpc":"2.0","id":msg_id,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{"listChanged":False}},"serverInfo":{"name":"Scorr","version":VERSION}}}
    if method == "tools/list":
        return {"jsonrpc":"2.0","id":msg_id,"result":{"tools":MCP_TOOLS}}
    if method == "tools/call":
        name = params.get("name"); args = params.get("arguments",{})
        try:
            result = await _call_tool(name, args)
            return {"jsonrpc":"2.0","id":msg_id,"result":{"content":[{"type":"text","text":json.dumps(result,default=str)}]}}
        except Exception as e:
            return {"jsonrpc":"2.0","id":msg_id,"error":{"code":-32603,"message":str(e)}}
    if method in ("notifications/initialized","notifications/cancelled"):
        return Response(status_code=204)
    return {"jsonrpc":"2.0","id":msg_id,"error":{"code":-32601,"message":f"Method not found: {method}"}}
