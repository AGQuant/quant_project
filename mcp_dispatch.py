import os
import re
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


# cc#351 MAINTENANCE_LOCK_RULE: lock-taking maintenance ops must NOT run through the single-connection
# run_sql MCP path (10-Jul incident: a REINDEX wedged ~45 min behind an idle-in-transaction backfill
# lock). These are Railway-console-only, weekends, propose-first.
_MAINT_BLOCK_RE = re.compile(r"^\s*(REINDEX|CLUSTER|VACUUM\s+FULL|VACUUM\s+\(\s*FULL|ALTER\s+TABLE)\b", re.I)


def _maintenance_block(query):
    """Return the blocked op name if the query is a lock-taking maintenance statement, else None."""
    # strip a leading run of line/block comments + whitespace so the guard sees the real first keyword
    s = re.sub(r"^(?:\s|--[^\n]*\n|/\*.*?\*/)+", "", query or "", flags=re.S)
    m = _MAINT_BLOCK_RE.match(s)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1).upper())

MCP_TOOLS = [
    {"name":"server_now","description":"Authoritative India time (Asia/Kolkata, UTC+5:30).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"health_report","description":"Full Scorr system health report card.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"run_diagnosis","description":"Full system diagnosis — 6 sections, traffic-light per section.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"digest_daily","description":"Daily Digest V3 — the full digest, same payload as the /digest page (cc#851).","inputSchema":{"type":"object","properties":{},"required":[]}},
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
    {"name":"v8_metrics_gapfill","description":"cc#1048: fill v8_metrics history for the ~113 active futures symbols the one-time backfill skipped (it only covered symbols with CURRENT gvm>=6.5). dry_run defaults TRUE and reports what it would write without touching anything; dry_run=false performs the insert and is refused during market hours. Never restates an existing row (ON CONFLICT DO NOTHING).","inputSchema":{"type":"object","properties":{"dry_run":{"type":"boolean"}},"required":[]}},
    {"name":"sync_futures_universe","description":"Sync futures_universe with Fyers feed (last 7 days). Strips expiry suffix, adds missing symbols, deactivates absent 2+ Mondays. Also runs auto Monday 08:00 IST.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"get_v8_metrics","description":"Get computed V8 metrics for one stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"get_v8_metrics_all","description":"Get all metrics for the full universe (latest date).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"get_v8_live_metrics","description":"Get real-time CMP, day%, hourly gain for the universe.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v8_run_signal_writer","description":"V8: manually trigger live signal writer (19 metrics + qualified).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"bt7_run","description":"BT7 parity harness (cc#218/220): ASYNC — starts the 09:15-15:30 walk (real writer+exits under the bt7_sim sandbox into harness_* shadows) in the background and returns {started:true,label} immediately (or {busy:true} if a run is already walking). Poll bt7_status(label) for status running->ok/error. Args: date (YYYY-MM-DD), label.","inputSchema":{"type":"object","properties":{"date":{"type":"string"},"label":{"type":"string"}},"required":["date","label"]}},
    {"name":"bt7_status","description":"BT7 run poll (cc#220): current row for a label — status (running/ok/error), ticks, quals/entries/exits, and error_detail if it failed. Args: label.","inputSchema":{"type":"object","properties":{"label":{"type":"string"}},"required":["label"]}},
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
    {"name":"v10_signal","description":"V10 ST+EMA: current directional signal for BOTH NIFTY50 and BANKNIFTY (ST 150/3 10m + EMA 3/10 30m gate). Returns both indices when symbol omitted; pass symbol=NIFTY50|BANKNIFTY for one (cc#746).","inputSchema":{"type":"object","properties":{"symbol":{"type":"string","description":"NIFTY50 | BANKNIFTY (omit for both)"}},"required":[]}},
    {"name":"v10_tick","description":"V10 ST+EMA: run one 5-min cycle — append 5m bar, compute signal, Telegram alert on BUY/SELL.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"tc_sim_summary","description":"TC OUTCOME SIM (cc#748): open sim positions + closed stats — win-rate, avg pnl%, avg hours, exit_reason split (TARGET/STOP/TIME) — grouped by style AND direction, plus an overall closed line. STRONG-verdict entries scored via tc_resolver, +/-3% exits, 5-trading-day TIME cap.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"pcr_intraday","description":"5-min intraday PCR trend (ATM±5 + total) for NIFTY/BANKNIFTY from pcr_intraday.","inputSchema":{"type":"object","properties":{"underlying":{"type":"string"},"days":{"type":"integer"}},"required":[]}},
    {"name":"compute_pcr_intraday","description":"Compute/self-heal 5-min PCR into pcr_intraday (ts optional = single bar, else heal all missing).","inputSchema":{"type":"object","properties":{"ts":{"type":"string"}},"required":[]}},
    {"name":"pcr_backfill","description":"One-time index option OI+PCR backfill (NIFTY+BANKNIFTY ATM+-10 monthly). Fetches OI via Fyers History API (oi_flag=1), upserts onto option_chain, recomputes pcr_intraday + pcr_daily. start/end=YYYY-MM-DD. Fail-loud if no OI column. cc#1057: OI on an already-populated bar is PRESERVED by default (gaps only); force_oi=true overwrites it with the coarser History series and is how the 10-14 Aug intraday OI path was lost — use only to repair a known-bad capture.","inputSchema":{"type":"object","properties":{"start":{"type":"string"},"end":{"type":"string"},"force_oi":{"type":"boolean","description":"Overwrite existing OI (default false = preserve)"}},"required":["start","end"]}},
    {"name":"v8_replay_run","description":"V8 PAPER REPLAY: true 5-min stepped replay from start date. wipe=true clears the paper book first (DESTRUCTIVE). Walks intraday bar-by-bar, point-in-time entries/exits.","inputSchema":{"type":"object","properties":{"start":{"type":"string"},"end":{"type":"string"},"wipe":{"type":"boolean"}},"required":["start"]}},
    {"name":"v8_replay_summary","description":"V8 PAPER REPLAY: current paper book stats — open positions + realized trade stats by basket.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"sector_brief_batch","description":"Generate AI sector briefs for all 129 segments via Claude Haiku and cache in sector_briefs table. Runs in background. refresh=true regenerates all.","inputSchema":{"type":"object","properties":{"refresh":{"type":"boolean"}},"required":[]}},
    {"name":"sector_brief_status","description":"Check how many of the 129 sector briefs are cached in DB vs pending generation.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"anthropic_chat","description":"Call Claude via Anthropic API (bypasses chat limit). Returns response, tokens used, cost estimate. Use when chat is at 98%+ weekly limit.","inputSchema":{"type":"object","properties":{"prompt":{"type":"string"},"model":{"type":"string"},"max_tokens":{"type":"integer"}},"required":["prompt"]}},
    {"name":"backfill_futures_fyers","description":"cc#159, cc#488: on-demand Fyers REST 5-min futures backfill (fixes cc#152/153 fut/eq source-collision gap). start (YYYY-MM-DD, default 2026-06-26), end (default today), symbols (optional array, default all ~212 active futures). Fire-and-forget — spawns a background thread server-side and returns status=started immediately (the full universe run is ~15-20 min at 5s/symbol REST pacing). Poll backfill_futures_fyers_status for symbols_processed, bars_written, gaps_remaining. Blocked automatically during market hours (09:15-15:30 IST).","inputSchema":{"type":"object","properties":{"start":{"type":"string"},"end":{"type":"string"},"symbols":{"type":"array","items":{"type":"string"}}},"required":[]}},
    {"name":"backfill_futures_fyers_status","description":"cc#488: poll the outcome of the most recent backfill_futures_fyers fire-and-forget run — state (idle/running/complete/error), started_at, finished_at, result, error.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"theme_validate","description":"cc#1185 P8 THEME_TOKEN_LOCK: the two theme checks, run on the deployed files. (A) SET COMPLETENESS \u2014 the contract is the UNION of the keys the three body[data-theme] blocks declare, and any set missing one is named, because a missing key does not render blank, it falls through to another theme\u0027s value and looks correct on the theme you are testing. The two legacy sets (dark, goldday) are EXPECTED to fail and the card says report, do not fix. (B) RAW PRIMITIVES per file as a RATCHET against reports/theme_baseline_v1.json \u2014 a themed file may go down or stay level, never up. P\u0026L green/red and pass/fail are exempt by the card invariant and are allowlisted on the SELECTOR, never on the hue. Same check the github_push gate runs, so what this reports is what a push will be judged by.","inputSchema":{"type":"object","properties":{"paths":{"type":"array","items":{"type":"string"},"description":"repo paths to check; default is every file in the baseline"},"detail":{"type":"boolean","description":"include the per-declaration rows for the worst file (default false)"}},"required":[]}},
    {"name":"tc_replay_run","description":"cc#1220: fire the cc#1211 TC score entry replay — five sessions, all four buckets, score100 at every 15-min tick, then the threshold x hold sweep. ASYNC: starts a background daemon and returns {started:true,phase,run_id} immediately, or {busy:true} if one is already walking. Roughly 100k card evaluations, so poll tc_replay_status rather than waiting. Safe to fire twice: scoring upserts on (ts,symbol,bucket) and the sweep clears each cell before refilling it. phase = all (default) | score | sweep | portfolio | portfolio_gated. cc#1221 PORTFOLIO mode re-walks the ALREADY-STORED ticks under a capped book - score100 >= 80 entry, candidates ranked by score and filled into at most 20 open positions across all four buckets, one per symbol, no same-day re-entry, +/-2% or a 3-session time exit. It never re-scores, so it is fast, and it is deliberately NOT part of `all`. cc#1224 portfolio_gated is the same walk with the LOCKED CONFIG as the entry test - per-bucket bars 80/80/65/60, the sector and monthly-RSI gates per side, and 5 per bucket per day - read from the one TC_SCANNER_CONFIG dict the scanner renders, over the SAME stored ticks, so gated and ungated can be read side by side. This exists because POST /api/admin/run-tc-replay is unreachable — scorr.in is egress-blocked from the CC and Fable seats.","inputSchema":{"type":"object","properties":{"phase":{"type":"string","description":"all (default), score, sweep, portfolio, or portfolio_gated"}},"required":[]}},
    {"name":"invest_check_v2","description":"cc#1174: Investment Check V2 score for ONE symbol — the /10 weighted engine (GVM 40 anchor + dGVM-90d + 7 more, session_log 27979), registry-driven weights, weight-ordered components with per-component credit and the raw reading behind each. Returns the full payload the report renders: score10, band, company, segment, gvm, components[], excluded_components[] with the reason each was excluded, computable_weight. Exists because /api/investment-check-v2 is unreachable — scorr.in is egress-blocked from both the CC and the Fable container (403 on CONNECT).","inputSchema":{"type":"object","properties":{"symbol":{"type":"string","description":"NSE symbol"}},"required":["symbol"]}},
    {"name":"invest_check_v2_batch","description":"cc#1174: Investment Check V2 across a LIST of symbols — the validation batch. One slim row per symbol (score10, band, company, segment, gvm, computable_weight, excluded keys, weakest computable component and its credit), sorted by score descending, PLUS every failure listed rather than dropped: a batch that silently omits what it could not score reads as a clean sweep. Capped at 200 symbols.","inputSchema":{"type":"object","properties":{"symbols":{"type":"array","items":{"type":"string"},"description":"NSE symbols, max 200"}},"required":["symbols"]}},
    {"name":"tc_replay_status","description":"cc#1220: where the TC score replay stands. Returns the run state (running/phase/run_id/started/finished/error), LIVE row counts of tc_score_replay_ticks and tc_score_replay_trades, the coverage honesty flags (thin ticks under 150 symbols, 21-Aug bar sources), the 15-cell sweep table as markdown with the best two cells and their bucket breakdowns, the cc#1221 PORTFOLIO block when a capped-book run exists, the cc#1224 GATED block and the GATED-vs-UNGATED comparison when the gated run exists (trades, accuracy, avg and total pnl, by bucket, by day, exit mix, average and max open book size, slot utilisation, average slot rank taken), and the selfcheck — the ONE test that catches the as-of loader having drifted from the live scorer. Read the selfcheck before trusting any cell. An empty sweep reports markdown=null and says so in words rather than rendering a grid of dashes that reads like a result.","inputSchema":{"type":"object","properties":{"selfcheck":{"type":"boolean","description":"run the as-of vs live-scorer selfcheck (default true)"}},"required":[]}},
    {"name":"smartgain_reconcile","description":"cc#237/247: atomic FIFO reconcile of ONE SmartGain orderbook batch (orders + journal + holdings + round-trips) in a single transaction — dedup + broker checksum. Never hand-write orderbook SQL. account default MHK40. batch_id required (unique per orderbook upload). rows = list of {symbol, side (BUY/SELL), qty, price, trade_date?, order_ts?, order_id?, instrument?, expiry?}. Empty rows + an existing batch_id runs a dedup-safe self-check. Returns the cc#237 self-check dict (matches_broker_checksum, counts, residual).","inputSchema":{"type":"object","properties":{"account":{"type":"string"},"batch_id":{"type":"string"},"rows":{"type":"array","items":{"type":"object","properties":{"symbol":{"type":"string"},"side":{"type":"string"},"qty":{"type":"number"},"price":{"type":"number"},"trade_date":{"type":"string"},"order_ts":{"type":"string"},"order_id":{"type":"string"},"instrument":{"type":"string"},"expiry":{"type":"string"}},"required":["symbol","side","qty","price"]}}},"required":["batch_id"]}},
    {"name":"smartgain_backfill","description":"cc#237/247: idempotent re-reconcile of EVERY SmartGain batch since inception (FIFO cascade, dedup-safe). Re-aggregates split closes into round-trip rows. account default MHK40. Returns per-batch self-check summary.","inputSchema":{"type":"object","properties":{"account":{"type":"string"}},"required":[]}},
    {"name":"fetch_hist_5m","description":"cc#377 Phase B: fetch Fyers 5-min EQUITY history for ONE symbol/window into intraday_prices source='fyers_hist' (PURGE-EXEMPT, for backtest replay). symbol, from_date, to_date (YYYY-MM-DD). Chunks <=100d/req, 5s pacing, idempotent. Post-market only. Returns bars, chunks.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"from_date":{"type":"string"},"to_date":{"type":"string"}},"required":["symbol","from_date","to_date"]}},
    {"name":"backfill_signals","description":"cc#377 Phase B: batch 5m hist fetcher — for each (symbol,date) pair fetch [date, date+trailing_days] into source='fyers_hist' so a backtest's entries + exit windows replay on real bars. pairs=list of {symbol, date}. trailing_days default 15.","inputSchema":{"type":"object","properties":{"pairs":{"type":"array","items":{"type":"object","properties":{"symbol":{"type":"string"},"date":{"type":"string"}},"required":["symbol","date"]}},"trailing_days":{"type":"integer"}},"required":["pairs"]}},
    {"name":"probe_5m_depth","description":"cc#377 Phase 0: probe actual Fyers 5-min history depth (READ-ONLY). Fetches one-week windows at ~T-2m/6m/9m/12m for one symbol (default SBIN), logs candles-per-window to session_log (data_audit / FYERS_5M_DEPTH_PROBE). Gates Phase A warehouse scope.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":[]}},
    {"name":"v13_theme_run","description":"cc#461 V13 Theme Bridge: execute a preset's {fieldkey:{min,max}} filter set through the REAL V13 engine WITHOUT saving (correct field semantics — dma_* are %-distance from the MA; return_3y/pe/roce from screener_raw; futures ~212 scope). Fable's pre-save validation. Returns {count, scope_used, top rows}. Valid keys: dma_20/50/200, rsi_month/weekly, daily_rsi, rvol, vol_p, vol_gate (RVOL>=1.2 OR VOL P>=1.5 — cc#1452: vol_ratio/vol_ratio_21 retired, shimmed to these), week/month_return, week_index_52, mom_2d, day_1d, sector_week/month, gvm_score, g/v/m_score, market_cap, return_1y/3y, return_52w_vs_index, pe, roce.","inputSchema":{"type":"object","properties":{"filters":{"type":"object"},"sort_key":{"type":"string"},"sort_dir":{"type":"integer"},"limit":{"type":"integer"}},"required":["filters"]}},
    {"name":"v13_theme_save","description":"cc#461 V13 Theme Bridge: validate filter keys against the registry whitelist (rejects unknown keys with the valid list), run the screen, REFUSE the save when count=0 or count>500 (returns the count so you can tune), then upsert into v13_presets (scope=global). Args: name, filters, sort_key, sort_dir(-1 desc/1 asc), mode(insert|update), id. Returns {id, count}.","inputSchema":{"type":"object","properties":{"name":{"type":"string"},"filters":{"type":"object"},"sort_key":{"type":"string"},"sort_dir":{"type":"integer"},"mode":{"type":"string"},"id":{"type":"integer"}},"required":["name","filters"]}},
    {"name":"v13_theme_list","description":"cc#461 V13 Theme Bridge: list all global theme presets (id, name, compact filter summary, sort). Delete stays via run_sql (deliberate friction per guardrail).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"hr_report_generate","description":"cc#651 Portfolio Health: generate/refresh a client Portfolio Health Report using the EXACT /health template. Pass {portfolio_id} to (re)generate a saved portfolio (e.g. 4 = Vishal Bhosale), OR {name, holdings:[{symbol,qty,avg_price}]} to create one from scratch (symbols resolved to Scorr nse_code server-side). Runs the full pipeline and returns {portfolio_id, report_url}. white_label defaults true (client-shareable, zero Scorr branding).","inputSchema":{"type":"object","properties":{"portfolio_id":{"type":"integer"},"name":{"type":"string"},"holdings":{"type":"array","items":{"type":"object","properties":{"symbol":{"type":"string"},"qty":{"type":"number"},"avg_price":{"type":"number"}},"required":["symbol"]}},"white_label":{"type":"boolean"},"alpha_start_date":{"type":"string","description":"YYYY-MM-DD; alpha vs Nifty 500 is measured from this date to live. Omit -> earliest holding entry date (never a 1yr default)."}},"required":[]}},
    {"name":"restate_symbol_history","description":"cc#657 raw_prices corporate-action fix: full 5y ADJUSTED re-pull from Yahoo for split/bonus/demerger-polluted symbols (one/sec), restating the whole series so fake -34%..-90% single-day cliffs disappear. Pass {symbols:[...]} for an explicit list, or {detect:true} to auto-select the current cliff backlog. Returns per-symbol {bars, first_date, last_date, residual_cliffs} (a residual cliff after re-pull is a TRUE market move).","inputSchema":{"type":"object","properties":{"symbols":{"type":"array","items":{"type":"string"}},"detect":{"type":"boolean"},"lookback":{"type":"string"}},"required":[]}},
    {"name":"hr_report_pdf","description":"cc#652 Portfolio Health: get a fetchable white-label PDF of a saved portfolio's Health Report. Returns {url} — an absolute, short-lived (~10 min) signed URL you can web_fetch directly (no login) and share in chat. Zero Scorr branding. Pass {portfolio_id} (e.g. 4 = Vishal Bhosale).","inputSchema":{"type":"object","properties":{"portfolio_id":{"type":"integer"}},"required":["portfolio_id"]}},
    {"name":"stock_views_shortlist","description":"cc#737 / STOCK_VIEWS_FRAMEWORK_V1: TC-gated stock-views shortlist. DISTINCT symbols mentioned in polished_news over the last `hours` -> canonical Trade Check (best of LONG/SHORT) -> keep VALID/STRONG -> sorted by score DESC. Returns {hours, universe_scanned, count, candidates:[{symbol,direction,verdict,score,max,cmp}]}. Read-only, writes nothing. Reuses the same helper as the /api/news/stock_views/shortlist HTTP route (which Claude web can't call — it's login-gated).","inputSchema":{"type":"object","properties":{"hours":{"type":"integer","description":"lookback window, default 48, max 168"}},"required":[]}},
    {"name":"run_fundamentals_scrape","description":"cc#790: kick the Screener quarterly-fundamentals scrape. Pass {symbols:['ABB','TITAN']} for a TARGETED re-scrape that BYPASSES the 'already ok' resume filter — required for refreshing a symbol scraped in a prior season, because that filter never expires and would otherwise skip it forever (the cause of the 02-Aug gap: 102 announced companies frozen at their 11-Jul scrape with no Q1FY27 row). Omit symbols for the normal resumable full-universe run, or pass {mode:'test'} for a 3-symbol spot check. Runs in a background daemon and returns immediately; poll fundamentals_scrape_status via run_sql on fundamentals_scrape_status. Writes fundamentals_history + fundamentals_scrape_status.","inputSchema":{"type":"object","properties":{"symbols":{"type":"array","items":{"type":"string"},"description":"explicit symbols for a targeted re-scrape"},"mode":{"type":"string","description":"'run' (default) or 'test'"}},"required":[]}},
    {"name":"stock_views_feed","description":"cc#787 FUNNEL 2 (Stock Views raw feed): raw_news from the last `hours` that is EITHER broker/analyst recommendation content (is_reco) OR a catalyst on a stock in the ACTIVE futures universe. Scope is deliberately narrow so volume cannot explode — source_type domestic|company ONLY (no Reuters/Bloomberg global), IPO/listing/GMP content excluded, canonical rows only. Returns {hours, total_count, returned, capped_at, truncated, reco_count, reco_column_present, articles:[{raw_id,symbol,headline,description,source_name,source_type,url,published_at,is_reco}]} — capped at 200 rows newest-first, with total_count so you see real volume without pulling everything. This is what you scan on 'stock views' for P1/P2 candidates. Read-only. Distinct from stock_views_shortlist, which TC-scores already-POLISHED news; this is the raw upstream feed and is the only place reco content appears (funnel 1 / news-polish never shows it).","inputSchema":{"type":"object","properties":{"hours":{"type":"integer","description":"lookback window, default 48, max 168"}},"required":[]}},
]

# cc#879 item 6 — the two synchronous DB sections of _call_tool, lifted out so they can run in a
# worker thread instead of on the event loop. Bodies are unchanged; only where they execute moves.
def _run_sql_blocking(q):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(q)
            if cur.description:
                cols = [d[0] for d in cur.description]; rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                conn.commit(); return {"rows": rows, "count": len(rows)}
            conn.commit(); return {"status": "ok", "rowcount": cur.rowcount}
    except Exception as e:
        return {"error": str(e)}


def _blackout_rows_blocking(sym):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT ticker,ex_date,event_type FROM earnings_calendar WHERE UPPER(ticker)=%s "
                    "ORDER BY id DESC LIMIT 5", (sym,))
        return cur.fetchall()


async def _http_json(client, method, url, tool, **kw):
    """cc#1249 — call the app and ALWAYS come back with a dict, never a parse error.

    A tool that fails should say why in the same breath. Three outcomes and all three are named:
    the request itself failed (no response at all), the body would not parse as JSON, or it parsed
    and is returned untouched. The body snippet is capped because a 500 page can be an entire HTML
    document and the useful part is at the front.
    """
    try:
        r = await client.request(method, url, **kw)
    except Exception as e:
        return {"error": f"{tool}: request failed before any response",
                "error_type": type(e).__name__, "detail": str(e)[:300], "url": url}
    try:
        return r.json()
    except Exception as e:
        return {"error": f"{tool}: response was not JSON", "error_type": type(e).__name__,
                "http_status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "body_snippet": (r.text or "")[:400],
                "body_len": len(r.text or ""),
                "note": "cc#1249: an empty or non-JSON body is itself a defect — this names it "
                        "instead of surfacing a json parse error with no context."}


async def _call_tool(name, args):
    async with httpx.AsyncClient(timeout=600) as client:
        h = {"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}
        if name == "server_now": r = await client.get(f"{BASE_URL}/api/now"); return r.json()
        elif name == "health_report": r = await client.get(f"{BASE_URL}/api/health/report"); return r.json()
        elif name == "hr_report_generate": r = await client.post(f"{BASE_URL}/api/health/generate", json=args, headers=h); return r.json()
        elif name == "hr_report_pdf": r = await client.get(f"{BASE_URL}/api/health/report_pdf_link/{args['portfolio_id']}", headers=h); return r.json()
        elif name == "restate_symbol_history":
            _p = {"lookback": args.get("lookback", "5y")}
            if args.get("detect"): _p["detect"] = True
            if args.get("symbols"): _p["symbols"] = ",".join(args["symbols"]) if isinstance(args["symbols"], list) else args["symbols"]
            r = await client.post(f"{BASE_URL}/api/admin/restate_symbols", params=_p, headers=h); return r.json()
        elif name == "run_diagnosis": r = await client.get(f"{BASE_URL}/api/diagnosis"); return r.json()
        elif name == "digest_daily": r = await client.get(f"{BASE_URL}/api/digest/v3"); return r.json()   # cc#851: repointed off the retired v2.3 builder
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
        elif name == "v8_metrics_gapfill":
            _dry = args.get("dry_run", True) if args else True
            r = await client.post(f"{BASE_URL}/api/v8/backfill/metrics_gapfill",
                                  params={"dry_run": str(bool(_dry)).lower()}, headers=h)
            return r.json()
        elif name == "sync_futures_universe": r = await client.post(f"{BASE_URL}/api/v8/backfill/sync_universe", headers=h); return r.json()
        elif name == "get_v8_metrics": r = await client.get(f"{BASE_URL}/api/v8/metrics/{args['symbol']}"); return r.json()
        elif name == "get_v8_metrics_all": r = await client.get(f"{BASE_URL}/api/v8/metrics/all"); return r.json()
        elif name == "get_v8_live_metrics": r = await client.get(f"{BASE_URL}/api/v8/live_metrics"); return r.json()
        elif name == "v8_run_signal_writer": r = await client.post(f"{BASE_URL}/api/v8/run_signal_writer", headers=h); return r.json()
        elif name == "bt7_run": r = await client.post(f"{BASE_URL}/api/v8/bt7_run", headers=h, params={"date": args["date"], "label": args["label"]}); return r.json()
        elif name == "bt7_status": r = await client.get(f"{BASE_URL}/api/v8/bt7_status", headers=h, params={"label": args["label"]}); return r.json()
        elif name == "bt7_diff": r = await client.get(f"{BASE_URL}/api/v8/bt7_diff", headers=h, params={"label_a": args["label_a"], "label_b": args["label_b"]}); return r.json()
        elif name == "health_feeds": r = await client.get(f"{BASE_URL}/api/health/feeds"); return r.json()
        elif name == "env_check": r = await client.get(f"{BASE_URL}/api/admin/env_check", headers=h); return r.json()
        elif name == "run_sql":
            q = args["query"]
            _blocked = _maintenance_block(q)
            if _blocked:
                return {"error": f"BLOCKED by MAINTENANCE_LOCK_RULE (cc#351): '{_blocked}' is a lock-taking "
                                 f"maintenance op and must not run via the single-connection run_sql path. "
                                 f"Run it from the Railway console on a weekend, propose-first."}
            # cc#879 item 6 (cc#869 finding 2): this ran psycopg DIRECTLY on the event loop, inside
            # an async handler. run_sql accepts an ARBITRARY query, so the block was unbounded —
            # cc#869 measured a 11.2s query against gvm_history, and for those 11 seconds nothing
            # else on the service could progress. Every Claude tool call enters here, which is why
            # the app felt stuck precisely while Claude was working on it.
            # The handler itself is properly async (httpx.AsyncClient throughout) and stays that
            # way; only the blocking section moves to a worker thread.
            return await asyncio.to_thread(_run_sql_blocking, q)
        elif name == "load_input_from_drive": r = await client.post(f"{BASE_URL}/api/admin/load_input_from_drive", json={"file_id": args["file_id"]}); return r.json()
        elif name == "load_screener_from_drive": r = await client.post(f"{BASE_URL}/api/admin/load_screener_from_drive", json={"file_id": args["file_id"]}); return r.json()
        elif name == "load_earnings_from_screener": r = await client.post(f"{BASE_URL}/api/admin/load_earnings_from_screener", headers=h); return r.json()
        elif name == "check_blackout":
            sym = args["symbol"].upper()
            rows = await asyncio.to_thread(_blackout_rows_blocking, sym)   # cc#879: off the loop
            return {"symbol": sym, "events": [{"ex_date": str(r[1]), "event_type": r[2]} for r in rows]}
        # cc#1249: these four went dark together and each reported only
        # "Expecting value: line 1 column 1 (char 0)" — json failing on an empty string, which tells
        # the caller nothing. r.json() was called with no status check, so any non-JSON body (a
        # FastAPI plain-text 500, a proxy error page, an empty response) became that parse error and
        # the real cause never left the server. _http_json reports the status and a body snippet
        # instead. github_ops now also answers in JSON on its own side; this is the second half of
        # the same contract, for the cases the server never got to answer at all.
        elif name == "github_read": return await _http_json(client, "GET", f"{BASE_URL}/api/admin/github_read", name, params={"filepath": args["filepath"]}, headers=h)
        elif name == "github_list": return await _http_json(client, "GET", f"{BASE_URL}/api/admin/github_list", name, params={"path": args.get("path","")}, headers=h)
        elif name == "github_push": return await _http_json(client, "POST", f"{BASE_URL}/api/admin/github_push", name, json=args, headers=h)
        elif name == "github_delete": return await _http_json(client, "POST", f"{BASE_URL}/api/admin/github_delete", name, json=args, headers=h)
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
            _sym = (args or {}).get("symbol")
            if _sym:
                r = await client.get(f"{BASE_URL}/api/v10/signal", params={"symbol": _sym}); return r.json()
            # cc#746: expose BOTH indices by default (BANKNIFTY was hidden — the surface that let the
            # inverted-label + expiry-day defects go unnoticed).
            rn = await client.get(f"{BASE_URL}/api/v10/signal", params={"symbol": "NIFTY50"})
            rb = await client.get(f"{BASE_URL}/api/v10/signal", params={"symbol": "BANKNIFTY"})
            return {"NIFTY50": rn.json(), "BANKNIFTY": rb.json()}
        elif name == "v10_tick":
            r = await client.post(f"{BASE_URL}/api/v10/tick", headers=h); return r.json()
        elif name == "tc_sim_summary":
            r = await client.get(f"{BASE_URL}/api/tc-sim/summary"); return r.json()
        elif name == "pcr_intraday":
            r = await client.get(f"{BASE_URL}/api/pcr/intraday", params={"underlying": args.get("underlying","NIFTY"), "days": args.get("days",2)}); return r.json()
        elif name == "compute_pcr_intraday":
            params = {"ts": args["ts"]} if args.get("ts") else {}
            r = await client.post(f"{BASE_URL}/api/pcr/intraday/compute", params=params, headers=h); return r.json()
        elif name == "pcr_backfill":
            # cc#1057: force_oi defaults to False both here and server-side — the destructive
            # mode has to be typed, it can never be reached by omission.
            r = await client.post(f"{BASE_URL}/api/pcr/backfill", params={"start": args["start"], "end": args["end"], "force_oi": bool(args.get("force_oi", False))}, headers=h); return r.json()
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
        elif name == "fetch_hist_5m": r = await client.post(f"{BASE_URL}/api/admin/fetch_hist_5m", json={"symbol":args["symbol"],"from_date":args["from_date"],"to_date":args["to_date"]}, headers=h); return r.json()
        elif name == "backfill_signals": r = await client.post(f"{BASE_URL}/api/admin/backfill_signals", json={"pairs":args["pairs"],"trailing_days":args.get("trailing_days",15)}, headers=h); return r.json()
        elif name == "probe_5m_depth": r = await client.post(f"{BASE_URL}/api/admin/probe_5m_depth", params={"symbol":args.get("symbol","SBIN")}, headers=h); return r.json()
        elif name == "v13_theme_run": r = await client.post(f"{BASE_URL}/api/v13/theme/run", json=args, headers=h); return r.json()
        elif name == "v13_theme_save": r = await client.post(f"{BASE_URL}/api/v13/theme/save", json=args, headers=h); return r.json()
        elif name == "v13_theme_list": r = await client.get(f"{BASE_URL}/api/v13/theme/list"); return r.json()
        elif name == "backfill_futures_fyers":
            # cc#488: endpoint is now fire-and-forget (spawns its own background
            # thread and returns immediately) — was previously synchronous and ran
            # ~15-20 min in-request, which this connector's earlier 2 retries
            # never survived (0 bars written each time despite the 1500s override).
            body = {"start": args.get("start"), "end": args.get("end"), "symbols": args.get("symbols")}
            r = await client.post(f"{BASE_URL}/api/admin/backfill_futures_fyers", json=body, headers=h)
            return r.json()
        elif name == "backfill_futures_fyers_status":
            r = await client.get(f"{BASE_URL}/api/admin/backfill_futures_fyers/status", headers=h)
            return r.json()
        elif name == "smartgain_reconcile":
            # cc#247: call the canonical FIFO cascade IN-PROCESS (it opens its own DB
            # conn). Deliberately NOT via HTTP — the Railway host is not in the egress
            # allowlist, so curl fails; in-process sidesteps that entirely.
            import smartgain_reconcile
            return await asyncio.to_thread(
                smartgain_reconcile.reconcile_smartgain_batch,
                args.get("account", "MHK40"),
                args.get("rows") or [],
                args["batch_id"],
            )
        elif name == "smartgain_backfill":
            import smartgain_reconcile
            return await asyncio.to_thread(
                smartgain_reconcile.backfill_all_batches,
                args.get("account", "MHK40"),
            )
        elif name == "run_fundamentals_scrape":
            # cc#790: internal-trusted — call the scraper directly rather than the admin HTTP route,
            # so a missing/rotated ADMIN_TOKEN can't silently block the season refresh. Backgrounded
            # the same way the route does it: the scrape walks hundreds of symbols with a throttle
            # and must never hold the MCP request open.
            import threading, fundamentals_scraper
            _syms = [str(s).strip().upper() for s in (args.get("symbols") or []) if str(s).strip()]
            if _syms:
                threading.Thread(target=fundamentals_scraper.run_scrape,
                                 kwargs={"symbols": _syms}, name="cc790-mcp-targeted", daemon=True).start()
                return {"status": "started", "mode": "targeted", "symbols": len(_syms),
                        "note": "Targeted re-scrape running in background; poll fundamentals_scrape_status."}
            _m = "test" if str(args.get("mode", "run")).lower() == "test" else "run"
            threading.Thread(target=fundamentals_scraper.run_scrape, args=(_m,),
                             name="cc790-mcp-full", daemon=True).start()
            return {"status": "started", "mode": _m,
                    "note": "Scrape running in background; poll fundamentals_scrape_status."}
        elif name == "invest_check_v2":
            # cc#1174 push 6, per Fable RECO 3109. THE ROUTE FUNCTION IS CALLED, not compute().
            # The route is a thin wrapper here, but the BATCH route below is not — it derives the
            # weakest component, sorts, and lists failures inline. Reaching past the route to
            # compute() would mean re-implementing that here, which is exactly the second code
            # path the RECO forbids. These are plain sync defs, and passing the argument binds it
            # so the FastAPI Query default is never evaluated.
            # OFF THE EVENT LOOP: it opens a connection and scores a symbol (cc#879/cc#869).
            import invest_check_v2 as _icv2
            return await asyncio.to_thread(_icv2.investment_check_v2,
                                           str(args.get("symbol", "")).strip().upper())
        elif name == "invest_check_v2_batch":
            # The route takes a comma-separated string; the tool takes an array because that is
            # what the RECO specifies and what a caller actually has. Joining here keeps the ONE
            # code path — the route still does the scoring, the sorting and the error listing.
            import invest_check_v2 as _icv2
            _syms = [str(s).strip().upper() for s in (args.get("symbols") or []) if str(s).strip()]
            if not _syms:
                return {"error": "symbols must be a non-empty array of NSE symbols"}
            return await asyncio.to_thread(_icv2.investment_check_v2_batch, ",".join(_syms))
        elif name == "tc_replay_run":
            # cc#1220: IN-PROCESS, deliberately. The rest of this file calls the app back over
            # BASE_URL, which depends on the public domain resolving from inside the container and
            # on ADMIN_TOKEN being current. The replay is the one job that must not fail for
            # either reason — it is the only way cc#1211 can ever produce first-run evidence — so
            # it imports and calls directly, the same choice cc#790 made for the scrape.
            import tc_replay_runner
            return tc_replay_runner.start(args.get("phase", "all"))
        elif name == "theme_validate":
            # In-process and off the event loop. It walks every themed page and sheet on disk,
            # which is a bounded but real amount of parsing — the cc#879 rule applies.
            import theme_validator as _tv
            def _run():
                out = _tv.validate(args.get("paths") or None)
                if args.get("detail"):
                    worst = max(out["files"], key=lambda f: f["now"], default=None)
                    if worst:
                        content = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    worst["path"]), encoding="utf-8").read()
                        _n, rows = _tv.count_raw(_tv.extract_css(worst["path"], content), detail=True)
                        out["detail"] = {"path": worst["path"], "rows": rows[:40], "of": _n}
                return out
            return await asyncio.to_thread(_run)
        elif name == "tc_replay_status":
            # OFF THE EVENT LOOP. This one opens a connection and runs the sweep table and the
            # selfcheck, which is exactly the unbounded blocking section cc#879 moved out of
            # run_sql after cc#869 measured an 11-second stall freezing the whole service.
            import tc_replay_runner
            return await asyncio.to_thread(tc_replay_runner.status,
                                           bool(args.get("selfcheck", True)))
        elif name == "stock_views_feed":
            # cc#787: internal-trusted (no auth gate) — call the SHARED funnel-2 core directly,
            # bypassing the login-gated HTTP route. Same query, one definition.
            import stock_views_funnel
            return await asyncio.to_thread(
                stock_views_funnel.stock_views_feed_data,
                int(args.get("hours", 48)),
            )
        elif name == "stock_views_shortlist":
            # cc#737: internal-trusted (no auth gate) — call the SHARED helper directly, bypassing the
            # login-gated HTTP route. Same computation, canonical TC scorer via tc_resolver (cc#738).
            import news_endpoints
            return await asyncio.to_thread(
                news_endpoints.stock_views_shortlist_data,
                int(args.get("hours", 48)),
            )
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
