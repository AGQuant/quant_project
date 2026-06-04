from fastapi import FastAPI, HTTPException, Request, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
import os
import psycopg
import urllib.parse
import secrets
import logging
import json
import asyncio
import time
import base64
from datetime import datetime, date, timedelta
from typing import Optional, Any, Dict, List
import io
import csv
import re
import httpx
import pandas as pd
import numpy as np
import yfinance as yf
from bs4 import BeautifulSoup

from v8_engine import (
    V8_SCHEMA_SQL, run_v8_engine,
    compute_metrics_for_symbol, store_metrics
)
from v8_endpoints import router as v8_router
from v8_futures import router as v8_futures_router
from qb_endpoints import router as qb_router
from gvm_market_endpoints import router as gvm_market_router
from admin_data import router as admin_data_router
from nse_holidays import is_trading_day, is_nse_holiday
from v8_live import build_history_cache, run_live_tick
from gvm_nightly import router as gvm_nightly_router, recompute_gvm, _sql_clean_replace_screener
import yahoo_ondemand
import yahoo_index_backfill
import v8_paper
import global_indices
import v8_signal_writer
import qb_eod_checker
import refresh_takeaways as rt

# ============================================================
# Scorr / Project Quant — main.py v2.9.11
# v2.9.11: REFACTOR file 3/5 — Screener earnings scraper + Drive CSV loaders
#   extracted to admin_data.py (own router). HTTP-only endpoints; scheduler's
#   earnings job still calls /api/admin/load_earnings_from_screener over HTTP.
#   GitHub helpers + ADR/PCR compute kept in main.py (deploy lifeline / scheduler
#   direct-call path) — they move in file 5 with the scheduler.
# v2.9.10: REFACTOR file 2/5 — GVM + market read endpoints to gvm_market_endpoints.py.
# v2.9.9: REFACTOR file 1/5 — QB endpoints to qb_endpoints.py. + ADR 999 fix.
# v2.9.8: POST /api/admin/content_update — manual content writer.
# v2.8.0: COMPUTE-ON-WRITE ADR + PCR (03-Jun-2026)
# ============================================================

VERSION = "2.9.11"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scorr")

DATABASE_URL = os.getenv("DATABASE_URL")
BASE_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN", "quantproject-production.up.railway.app")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
DEPLOY_GUARD = os.getenv("DEPLOY_GUARD", "false").lower() == "true"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

app = FastAPI(title="Scorr API", version=VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

app.include_router(v8_router)
app.include_router(v8_futures_router)
app.include_router(qb_router)
app.include_router(gvm_nightly_router)
app.include_router(gvm_market_router)
app.include_router(admin_data_router)

def get_conn():
    return psycopg.connect(DATABASE_URL)

def create_tables():
    sql = """
    CREATE TABLE IF NOT EXISTS input_raw (id SERIAL PRIMARY KEY, data JSONB);
    CREATE TABLE IF NOT EXISTS screener_raw (id SERIAL PRIMARY KEY, data JSONB);
    CREATE TABLE IF NOT EXISTS earnings_calendar (
        id SERIAL PRIMARY KEY, company_name TEXT, ticker TEXT,
        ex_date DATE, record_date DATE, event_type TEXT,
        loaded_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS intraday_prices (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL,
        open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC, volume BIGINT,
        timeframe TEXT DEFAULT '1m', source TEXT DEFAULT 'fyers',
        UNIQUE(symbol, ts, timeframe)
    );
    CREATE INDEX IF NOT EXISTS idx_intraday_symbol_ts ON intraday_prices(symbol, ts DESC);
    ALTER TABLE intraday_prices ADD COLUMN IF NOT EXISTS timeframe TEXT DEFAULT '1m';
    ALTER TABLE intraday_prices ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'fyers';
    CREATE TABLE IF NOT EXISTS cmp_prices (
        symbol TEXT PRIMARY KEY, cmp NUMERIC, updated_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS futures_universe (
        symbol TEXT PRIMARY KEY, lot_size INTEGER, segment TEXT,
        is_active BOOLEAN DEFAULT TRUE, updated_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS app_config (
        key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT NOW()
    );
    INSERT INTO app_config (key, value) VALUES ('yahoo_cmp_fallback', 'off') ON CONFLICT (key) DO NOTHING;
    INSERT INTO app_config (key, value) VALUES ('takeaway_refresh_due', 'false') ON CONFLICT (key) DO NOTHING;
    INSERT INTO app_config (key, value) VALUES ('overview_refresh_due', 'false') ON CONFLICT (key) DO NOTHING;
    CREATE TABLE IF NOT EXISTS v8_history_cache (
        symbol TEXT PRIMARY KEY, cache_date DATE NOT NULL,
        closes JSONB, highs JSONB, lows JSONB, volumes JSONB, segment TEXT,
        vol_avg10 NUMERIC, hi_252 NUMERIC, lo_252 NUMERIC, hi_21 NUMERIC, lo_21 NUMERIC,
        gvm_score NUMERIC, prev_day_change NUMERIC, built_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS gvm_history (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, score_date DATE NOT NULL,
        g_score NUMERIC, v_score NUMERIC, m_score NUMERIC, gvm_score NUMERIC,
        verdict TEXT, segment TEXT, created_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(symbol, score_date)
    );
    CREATE INDEX IF NOT EXISTS idx_gvm_history_symbol_date ON gvm_history(symbol, score_date DESC);
    CREATE INDEX IF NOT EXISTS idx_gvm_history_date ON gvm_history(score_date DESC);
    CREATE TABLE IF NOT EXISTS quant_basket_config (
        id SERIAL PRIMARY KEY, basket_name TEXT NOT NULL UNIQUE, cap_type TEXT,
        is_active BOOLEAN DEFAULT TRUE, stage1_sector JSONB, stage2_stock JSONB,
        theme_tags JSONB, notes TEXT, updated_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS quant_basket (
        id SERIAL PRIMARY KEY, basket_name TEXT NOT NULL, symbol TEXT NOT NULL,
        score_date DATE NOT NULL, company_name TEXT, sector TEXT, cap_type TEXT,
        gvm_score NUMERIC, technical_rating NUMERIC, sector_rating NUMERIC, cmp NUMERIC,
        ret_1w NUMERIC, ret_1m NUMERIC, ret_1y NUMERIC, dma_50 NUMERIC, dma_200 NUMERIC,
        pe_multiplier NUMERIC, annual_upside NUMERIC, rsi_monthly NUMERIC,
        sector_week NUMERIC, sector_month NUMERIC, sector_year NUMERIC, inst_change TEXT,
        tag_stable BOOLEAN DEFAULT FALSE, tag_multibagger BOOLEAN DEFAULT FALSE,
        tag_momentum BOOLEAN DEFAULT FALSE, tag_dividend BOOLEAN DEFAULT FALSE,
        verdict TEXT, metrics JSONB, qualified_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(basket_name, symbol, score_date)
    );
    CREATE INDEX IF NOT EXISTS idx_qb_basket_date ON quant_basket(basket_name, score_date DESC);
    CREATE TABLE IF NOT EXISTS quant_basket_funnel (
        id SERIAL PRIMARY KEY, basket_name TEXT NOT NULL, score_date DATE NOT NULL,
        stage TEXT NOT NULL, counts JSONB NOT NULL, computed_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(basket_name, score_date, stage)
    );
    CREATE TABLE IF NOT EXISTS quant_paper_positions (
        id SERIAL PRIMARY KEY, basket_name TEXT NOT NULL, symbol TEXT NOT NULL,
        entry_price NUMERIC NOT NULL, entry_date DATE NOT NULL,
        qty NUMERIC, allocation NUMERIC, current_price NUMERIC, current_value NUMERIC,
        pnl NUMERIC, pnl_pct NUMERIC, stop_loss_price NUMERIC, status TEXT DEFAULT 'open',
        exit_price NUMERIC, exit_date DATE,
        gvm_at_entry NUMERIC, g_at_entry NUMERIC, v_at_entry NUMERIC, m_at_entry NUMERIC,
        notes TEXT, created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(basket_name, symbol, entry_date)
    );
    CREATE TABLE IF NOT EXISTS quant_rebalance_log (
        id SERIAL PRIMARY KEY, basket_name TEXT NOT NULL, rebalance_date DATE NOT NULL,
        stocks_in INTEGER, stocks_out INTEGER, stocks_held INTEGER,
        liquidbees_units NUMERIC, liquidbees_value NUMERIC,
        total_portfolio_value NUMERIC, actions JSONB, computed_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS quant_basket_registry (
        basket_name TEXT PRIMARY KEY, cap_type TEXT, capital NUMERIC DEFAULT 500000,
        max_stocks INTEGER DEFAULT 20, rebalance_freq TEXT, weight_band TEXT,
        next_rebalance DATE, is_active BOOLEAN DEFAULT TRUE, notes TEXT,
        updated_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS adr_daily (
        id SERIAL PRIMARY KEY, price_date DATE NOT NULL UNIQUE,
        advances INTEGER DEFAULT 0, declines INTEGER DEFAULT 0, unchanged INTEGER DEFAULT 0,
        adr NUMERIC(6,3), computed_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS pcr_daily (
        id SERIAL PRIMARY KEY, price_date DATE NOT NULL, underlying TEXT NOT NULL,
        put_oi BIGINT DEFAULT 0, call_oi BIGINT DEFAULT 0, pcr NUMERIC(6,3),
        computed_at TIMESTAMP DEFAULT NOW(), UNIQUE(price_date, underlying)
    );
    """ + V8_SCHEMA_SQL
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql); conn.commit()
        log.info("Tables ready (v2.9.11)")
    except Exception as e:
        log.error(f"create_tables failed: {e}")

def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def _is_market_hours() -> bool:
    now = _ist_now()
    if not is_trading_day(now.date()): return False
    return now.replace(hour=9, minute=15, second=0, microsecond=0) <= now <= now.replace(hour=15, minute=30, second=0, microsecond=0)

def _get_futures_symbols() -> List[str]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE ORDER BY symbol")
            rows = cur.fetchall()
            if rows: return [r[0] for r in rows]
            cur.execute("SELECT DISTINCT symbol FROM v8_universe ORDER BY symbol")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"_get_futures_symbols failed: {e}"); return []

def _get_all_gvm_symbols() -> List[str]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM gvm_scores ORDER BY symbol")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"_get_all_gvm_symbols failed: {e}"); return []

def _get_full_cmp_universe() -> List[str]:
    return sorted(set(_get_all_gvm_symbols()) | set(_get_futures_symbols()))

def _get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
            r = cur.fetchone(); return r[0] if r else default
    except Exception as e:
        log.error(f"_get_config {key} failed: {e}"); return default

def _yahoo_cmp_fallback_on() -> bool:
    return str(_get_config("yahoo_cmp_fallback", "off")).lower() in ("on", "true", "1", "yes")

def _yahoo_ticker(symbol: str) -> str:
    return {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK"}.get(symbol, f"{symbol}.NS")

async def _fetch_cmp_yahoo(symbols: List[str]) -> Dict[str, float]:
    results = {}
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for symbol in symbols:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(_yahoo_ticker(symbol))}?interval=1d&range=2d"
            try:
                r = await client.get(url); r.raise_for_status(); data = r.json()
                chart = data.get("chart", {}).get("result", [])
                if chart:
                    closes = chart[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    closes = [x for x in closes if x is not None]
                    if closes: results[symbol] = float(closes[-1])
            except Exception as e:
                log.warning(f"CMP chart API {symbol}: {e}")
            await asyncio.sleep(0.1)
    log.info(f"CMP fetched: {len(results)}/{len(symbols)} symbols")
    return results

async def _fetch_intraday_yahoo(symbol: str, range_str: str = "7d") -> List[dict]:
    ticker = _yahoo_ticker(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?interval=5m&range={range_str}"
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url); r.raise_for_status(); data = r.json()
        chart = data.get("chart", {}).get("result", [])
        if not chart: return []
        result = chart[0]; timestamps = result.get("timestamp", [])
        indicators = result.get("indicators", {}).get("quote", [{}])[0]
        opens, highs, lows, closes, volumes = (indicators.get(k, []) for k in ("open","high","low","close","volume"))
        candles = []
        for j, ts in enumerate(timestamps):
            c_val = closes[j] if j < len(closes) else None
            if c_val is None: continue
            dt = datetime.utcfromtimestamp(ts) + timedelta(hours=5, minutes=30)
            candles.append({"symbol": symbol, "ts": dt,
                "open": opens[j] if j < len(opens) else None, "high": highs[j] if j < len(highs) else None,
                "low": lows[j] if j < len(lows) else None, "close": c_val,
                "volume": volumes[j] if j < len(volumes) else None})
        return candles
    except Exception as e:
        log.warning(f"intraday fetch {symbol} range={range_str}: {e}"); return []

def _upsert_cmp(cmp_map):
    if not cmp_map: return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            for sym, price in cmp_map.items():
                cur.execute("INSERT INTO cmp_prices (symbol, cmp, updated_at, source) VALUES (%s, %s, NOW(), 'yahoo') ON CONFLICT (symbol) DO UPDATE SET cmp = EXCLUDED.cmp, updated_at = NOW(), source = 'yahoo'", (sym, price))
            conn.commit()
        log.info(f"CMP upserted: {len(cmp_map)} symbols")
    except Exception as e:
        log.error(f"_upsert_cmp failed: {e}")

def _insert_intraday(candles):
    if not candles: return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            for c in candles:
                cur.execute("INSERT INTO intraday_prices (symbol, ts, open, high, low, close, volume) VALUES (%(symbol)s, %(ts)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s) ON CONFLICT (symbol, ts, timeframe) DO NOTHING", c)
            conn.commit()
    except Exception as e:
        log.error(f"_insert_intraday failed: {e}")

def _purge_intraday_old():
    cutoff = _ist_now() - timedelta(days=7)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s", (cutoff,)); conn.commit()
    except Exception as e:
        log.error(f"_purge_intraday_old failed: {e}")

# ── State flags ────────────────────────────────────────────────────────────────
_raw_prices_updated_today: Optional[date] = None
_earnings_loaded_today: Optional[date] = None
_yahoo_daily_running: bool = False
_v8_engine_ran_today: Optional[date] = None
_v8_engine_running: bool = False
_cache_built_today: Optional[date] = None
_cache_building: bool = False
_live_tick_running: bool = False
_signal_writer_running: bool = False
_gvm_recompute_ran_today: Optional[date] = None
_gvm_recompute_running: bool = False
_paper_tick_running: bool = False
_paper_pivots_built: Optional[date] = None
_global_fetched_today: Optional[date] = None
_global_fetching: bool = False
_global_intraday_fetching: bool = False
_qb_eod_ran_today: Optional[date] = None
_qb_eod_running: bool = False
_qb_intraday_mark_running: bool = False
_daily_metrics_ran_today: Optional[date] = None
_daily_metrics_running: bool = False
_refresh_check_ran_today: Optional[date] = None

# ── Background tasks ─────────────────────────────────────────────────────────────
async def _task_refresh_cmp():
    if not _yahoo_cmp_fallback_on(): return
    symbols = _get_full_cmp_universe()
    if not symbols: return
    cmp_map = await _fetch_cmp_yahoo(symbols); _upsert_cmp(cmp_map)

async def _bg_yahoo_daily(symbols=None, lookback=None):
    global _raw_prices_updated_today, _yahoo_daily_running
    if _yahoo_daily_running: return
    _yahoo_daily_running = True
    try:
        import yahoo_daily_update as ydu
        result = await ydu.run_async(symbols=symbols, lookback=lookback)
        _raw_prices_updated_today = _ist_now().date()
        log.info(f"yahoo_daily done: {result}")
    except Exception as e: log.error(f"yahoo_daily failed: {e}")
    finally: _yahoo_daily_running = False

async def _task_update_raw_prices():
    global _raw_prices_updated_today
    today = _ist_now().date()
    if _raw_prices_updated_today == today: return
    log.info("21:00 IST: Launching raw_prices update")
    asyncio.create_task(_bg_yahoo_daily())

def _bg_run_v8_engine():
    global _v8_engine_ran_today, _v8_engine_running
    if _v8_engine_running: return
    _v8_engine_running = True
    try:
        with get_conn() as conn:
            results = run_v8_engine(conn)
        _v8_engine_ran_today = _ist_now().date()
        log.info(f"V8 engine done: {results.get('symbols_processed')} symbols")
    except Exception as e: log.error(f"V8 engine failed: {e}")
    finally: _v8_engine_running = False

async def _task_run_v8_engine():
    global _v8_engine_ran_today
    if _v8_engine_ran_today == _ist_now().date(): return
    log.info("15:45 IST: V8 engine auto-run")
    asyncio.create_task(asyncio.to_thread(_bg_run_v8_engine))

def _bg_build_cache():
    global _cache_built_today, _cache_building
    if _cache_building: return
    _cache_building = True
    try:
        with get_conn() as conn:
            res = build_history_cache(conn)
        _cache_built_today = _ist_now().date()
        log.info(f"v8_history_cache built: {res.get('built')}/{res.get('total')}")
    except Exception as e: log.error(f"cache build failed: {e}")
    finally: _cache_building = False

async def _task_build_cache():
    if _cache_built_today == _ist_now().date(): return
    log.info("09:00 IST: Building v8_history_cache")
    asyncio.create_task(asyncio.to_thread(_bg_build_cache))

def _bg_recompute_gvm():
    global _gvm_recompute_ran_today, _gvm_recompute_running
    if _gvm_recompute_running: return
    _gvm_recompute_running = True
    try:
        res = recompute_gvm(refresh_momentum=True)
        _gvm_recompute_ran_today = _ist_now().date()
        log.info(f"GVM recompute done: scored={res.get('scored')}")
    except Exception as e: log.error(f"GVM recompute failed: {e}")
    finally: _gvm_recompute_running = False

async def _task_recompute_gvm_daily():
    if _gvm_recompute_ran_today == _ist_now().date(): return
    log.info("22:00 IST: GVM daily recompute")
    asyncio.create_task(asyncio.to_thread(_bg_recompute_gvm))

def _bg_paper_tick():
    global _paper_tick_running
    if _paper_tick_running: return
    _paper_tick_running = True
    try:
        buy_slots = sell_slots = None
        try:
            with httpx.Client(timeout=30) as c:
                mood = c.get(f"{BASE_URL}/api/v8/market_mood").json()
                buy_slots, sell_slots = mood.get("buy_slots"), mood.get("sell_slots")
        except Exception as e: log.warning(f"paper mood fetch failed: {e}")
        with get_conn() as conn:
            res = v8_paper.paper_tick(conn, buy_slots=buy_slots, sell_slots=sell_slots)
        if res.get("entries") or res.get("exits") or res.get("gate_exits"):
            log.info(f"paper_tick: {len(res.get('entries',[]))}E {len(res.get('exits',[]))}X")
    except Exception as e: log.error(f"paper_tick failed: {e}")
    finally: _paper_tick_running = False

def _bg_build_paper_pivots():
    global _paper_pivots_built
    try:
        with get_conn() as conn:
            res = v8_paper.compute_pivots(conn)
        _paper_pivots_built = _ist_now().date()
        log.info(f"paper pivots built: {res.get('built')}/{res.get('total')}")
    except Exception as e: log.error(f"paper pivots build failed: {e}")

async def _task_build_paper_pivots():
    if _paper_pivots_built == _ist_now().date(): return
    log.info("22:05 IST: Building rolling-5 paper pivots")
    asyncio.create_task(asyncio.to_thread(_bg_build_paper_pivots))

def _bg_fetch_global():
    global _global_fetched_today, _global_fetching
    if _global_fetching: return
    _global_fetching = True
    try:
        with global_indices.get_conn_from_env() as conn:
            res = asyncio.run(global_indices.fetch_global_indices(conn))
            try: global_indices.prune_global_indices(conn, years=5)
            except: pass
        _global_fetched_today = _ist_now().date()
        log.info(f"global_indices done: {res.get('stored')}/{res.get('total')}")
    except Exception as e: log.error(f"global_indices failed: {e}")
    finally: _global_fetching = False

async def _task_fetch_global():
    if _global_fetched_today == _ist_now().date(): return
    log.info("07:00 IST: Fetching global indices")
    asyncio.create_task(asyncio.to_thread(_bg_fetch_global))

def _bg_fetch_global_intraday():
    global _global_intraday_fetching
    if _global_intraday_fetching: return
    _global_intraday_fetching = True
    try:
        with global_indices.get_conn_from_env() as conn:
            res = asyncio.run(global_indices.fetch_global_intraday(conn))
            try: global_indices.prune_global_intraday(conn, days=7)
            except Exception as e: log.warning(f"global_intraday prune failed: {e}")
        log.info(f"global_intraday done: {res.get('stored')} bars")
    except Exception as e: log.error(f"global_intraday failed: {e}")
    finally: _global_intraday_fetching = False

async def _task_fetch_global_intraday():
    asyncio.create_task(asyncio.to_thread(_bg_fetch_global_intraday))

def _bg_live_tick():
    global _live_tick_running
    if _live_tick_running: return
    _live_tick_running = True
    try:
        with get_conn() as conn: run_live_tick(conn)
    except Exception as e: log.error(f"live tick failed: {e}")
    finally: _live_tick_running = False

async def _task_live_tick():
    asyncio.create_task(asyncio.to_thread(_bg_live_tick))

def _bg_signal_writer():
    global _signal_writer_running
    if _signal_writer_running: return
    _signal_writer_running = True
    try:
        with get_conn() as conn:
            res = v8_signal_writer.run_live_signal_writer(conn)
        log.info(f"signal_writer: {res.get('total', 0)} signals — {res.get('qualified', {})}")
    except Exception as e: log.error(f"signal_writer failed: {e}")
    finally: _signal_writer_running = False

def _bg_qb_intraday_mark():
    global _qb_intraday_mark_running
    if _qb_intraday_mark_running: return
    _qb_intraday_mark_running = True
    try:
        with get_conn() as conn:
            res = qb_eod_checker.qb_intraday_mark(conn)
        log.info(f"qb_intraday_mark: {res.get('marked')}/{res.get('symbols')} marked")
    except Exception as e: log.error(f"qb_intraday_mark failed: {e}")
    finally: _qb_intraday_mark_running = False

def _bg_qb_eod_checker():
    global _qb_eod_ran_today, _qb_eod_running
    if _qb_eod_running: return
    _qb_eod_running = True
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT basket_name FROM quant_paper_positions WHERE status='open'")
                baskets = [r[0] for r in cur.fetchall()]
            for basket in baskets:
                res = qb_eod_checker.run_eod_checker(conn, basket_name=basket)
                log.info(f"qb_eod {basket}: marked={res.get('positions_marked')} HS1={res.get('hard_stop_1_exits')} HS2={res.get('hard_stop_2_exits')}")
        _qb_eod_ran_today = _ist_now().date()
    except Exception as e: log.error(f"qb_eod_checker failed: {e}")
    finally: _qb_eod_running = False

async def _task_qb_eod_checker():
    if _qb_eod_ran_today == _ist_now().date(): return
    log.info("21:05 IST: QB EOD stop-loss check + P&L mark (all baskets)")
    asyncio.create_task(asyncio.to_thread(_bg_qb_eod_checker))

def _bg_check_refresh_due():
    global _refresh_check_ran_today
    try:
        result = rt.check_and_flag_due_refreshes()
        _refresh_check_ran_today = _ist_now().date()
        if result.get("flagged"): log.info(f"Refresh due: {result['flagged']}")
        else: log.info("Refresh check: nothing due today")
    except Exception as e: log.error(f"refresh check failed: {e}")

async def _task_check_refresh_due():
    global _refresh_check_ran_today
    now = _ist_now()
    if now.hour == 6 and now.minute < 5 and _refresh_check_ran_today != now.date():
        asyncio.create_task(asyncio.to_thread(_bg_check_refresh_due))

def _compute_and_store_adr(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            WITH latest_date AS (SELECT MAX(price_date) AS pd FROM raw_prices),
            latest AS (
                SELECT r.symbol, r.close FROM raw_prices r
                JOIN futures_universe fu ON r.symbol = fu.symbol AND fu.is_active = TRUE
                WHERE r.price_date = (SELECT pd FROM latest_date)
            ),
            prev AS (
                SELECT DISTINCT ON (r.symbol) r.symbol, r.close AS prev_close
                FROM raw_prices r
                JOIN futures_universe fu ON r.symbol = fu.symbol AND fu.is_active = TRUE
                WHERE r.price_date < (SELECT pd FROM latest_date)
                ORDER BY r.symbol, r.price_date DESC
            ),
            agg AS (
                SELECT (SELECT pd FROM latest_date) AS price_date,
                    COUNT(*) FILTER (WHERE l.close > p.prev_close) AS advances,
                    COUNT(*) FILTER (WHERE l.close < p.prev_close) AS declines,
                    COUNT(*) FILTER (WHERE l.close = p.prev_close) AS unchanged
                FROM latest l JOIN prev p ON l.symbol = p.symbol
            )
            INSERT INTO adr_daily (price_date, advances, declines, unchanged, adr)
            SELECT price_date, advances, declines, unchanged,
                   ROUND(advances::numeric / NULLIF(declines, 0), 3)
            FROM agg
            ON CONFLICT (price_date) DO UPDATE SET
                advances = EXCLUDED.advances, declines = EXCLUDED.declines,
                unchanged = EXCLUDED.unchanged, adr = EXCLUDED.adr, computed_at = NOW()
            RETURNING price_date, advances, declines, unchanged, adr
        """)
        row = cur.fetchone(); conn.commit()
        if row:
            return {"price_date": str(row[0]), "advances": row[1], "declines": row[2], "unchanged": row[3], "adr": float(row[4] or 0)}
        return {"status": "no_data"}

def _compute_and_store_pcr(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pcr_daily (price_date, underlying, put_oi, call_oi, pcr)
            SELECT DATE(ts), underlying,
                SUM(CASE WHEN option_type = 'PE' THEN oi ELSE 0 END),
                SUM(CASE WHEN option_type = 'CE' THEN oi ELSE 0 END),
                ROUND(SUM(CASE WHEN option_type = 'PE' THEN oi ELSE 0 END)::numeric /
                    NULLIF(SUM(CASE WHEN option_type = 'CE' THEN oi ELSE 0 END), 0), 3)
            FROM option_chain
            WHERE ts IN (SELECT MAX(ts2) FROM option_chain oc2 WHERE DATE(oc2.ts) = DATE(option_chain.ts) GROUP BY DATE(oc2.ts))
            AND DATE(ts) = (SELECT MAX(DATE(ts)) FROM option_chain)
            GROUP BY DATE(ts), underlying
            ON CONFLICT (price_date, underlying) DO UPDATE SET
                put_oi = EXCLUDED.put_oi, call_oi = EXCLUDED.call_oi, pcr = EXCLUDED.pcr, computed_at = NOW()
        """)
        rowcount = cur.rowcount; conn.commit()
        return {"status": "ok", "rows": rowcount}

def _bg_compute_daily_metrics():
    global _daily_metrics_ran_today, _daily_metrics_running
    if _daily_metrics_running: return
    _daily_metrics_running = True
    try:
        with get_conn() as conn:
            adr = _compute_and_store_adr(conn); pcr = _compute_and_store_pcr(conn)
        _daily_metrics_ran_today = _ist_now().date()
        log.info(f"daily_metrics: ADR={adr.get('adr')} PCR_rows={pcr.get('rows')}")
    except Exception as e: log.error(f"daily_metrics failed: {e}")
    finally: _daily_metrics_running = False

async def _task_compute_daily_metrics():
    if _daily_metrics_ran_today == _ist_now().date(): return
    log.info("15:50 IST: Computing daily ADR + PCR")
    asyncio.create_task(asyncio.to_thread(_bg_compute_daily_metrics))

async def _task_load_earnings_daily():
    global _earnings_loaded_today
    today = _ist_now().date()
    if _earnings_loaded_today == today: return
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            headers = {"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}
            r = await client.post(f"{BASE_URL}/api/admin/load_earnings_from_screener", headers=headers)
            log.info(f"Earnings daily load: {r.json()}")
            _earnings_loaded_today = today
    except Exception as e:
        log.error(f"_task_load_earnings_daily failed: {e}")

async def _scheduler():
    log.info("Scheduler started (v2.9.11)")
    asyncio.create_task(_live_loop())
    while True:
        try:
            now = _ist_now(); trading_day = is_trading_day(now.date())
            if now.hour == 6 and now.minute < 5: await _task_check_refresh_due()
            if now.hour == 7 and now.minute < 5: await _task_fetch_global()
            await _task_fetch_global_intraday()
            if trading_day and now.hour == 9 and now.minute < 5: await _task_load_earnings_daily()
            if trading_day and now.hour == 9 and now.minute < 10: await _task_build_cache()
            if _is_market_hours(): await _task_refresh_cmp()
            if trading_day and now.hour == 15 and 45 <= now.minute < 55: await _task_run_v8_engine()
            if trading_day and now.hour == 15 and 50 <= now.minute < 60: await _task_compute_daily_metrics()
            if trading_day and now.hour == 21 and now.minute < 5: await _task_update_raw_prices()
            if trading_day and now.hour == 21 and 5 <= now.minute < 15: await _task_qb_eod_checker()
            if now.hour == 22 and now.minute < 10: await _task_recompute_gvm_daily()
            if now.hour == 22 and 5 <= now.minute < 15: await _task_build_paper_pivots()
        except Exception as e: log.error(f"Scheduler error: {e}")
        await asyncio.sleep(300)

async def _live_loop():
    log.info("Live loop started (v2.9.11)")
    tick_count = 0
    while True:
        try:
            if _is_market_hours():
                await _task_live_tick()
                asyncio.create_task(asyncio.to_thread(_bg_paper_tick))
                tick_count += 1
                if tick_count % 5 == 0: asyncio.create_task(asyncio.to_thread(_bg_signal_writer))
                if tick_count % 15 == 0: asyncio.create_task(asyncio.to_thread(_bg_qb_intraday_mark))
            else: tick_count = 0
        except Exception as e: log.error(f"live loop error: {e}")
        await asyncio.sleep(60)

_BG_TASKS: set = set()

@app.on_event("startup")
async def startup():
    async def _init_tables():
        try: await asyncio.to_thread(create_tables)
        except Exception as e: log.error(f"create_tables (bg) failed: {e}")
    t0 = asyncio.create_task(_init_tables())
    _BG_TASKS.add(t0); t0.add_done_callback(_BG_TASKS.discard)
    t = asyncio.create_task(_scheduler())
    _BG_TASKS.add(t); t.add_done_callback(_BG_TASKS.discard)
    log.info(f"Scorr API v{VERSION} started — DEPLOY_GUARD={DEPLOY_GUARD}")

@app.get("/")
def root(): return {"service": "Scorr API", "version": VERSION, "status": "live"}

@app.get("/api/health")
def health(): return {"status": "ok", "version": VERSION}

@app.get("/api/now")
def server_now():
    n = _ist_now(); d = n.date()
    return {"india_time": n.strftime("%Y-%m-%d %H:%M:%S"), "timezone": "Asia/Kolkata (UTC+5:30)",
            "day": n.strftime("%A"), "weekday": n.weekday(), "is_weekend": n.weekday() >= 5,
            "is_holiday": is_nse_holiday(d), "is_trading_day": is_trading_day(d), "market_open": _is_market_hours()}

def _grade(score: float) -> str:
    if score >= 95: return "A+"
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 70: return "C"
    if score >= 60: return "D"
    return "F"

def _check(val, label, ok_if, warn_if=None):
    status = "ok" if ok_if(val) else ("warn" if warn_if and warn_if(val) else "fail")
    return {"check": label, "value": val, "status": status}

def build_health_report() -> dict:
    now = _ist_now(); today = now.date()
    report = {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S IST"), "version": VERSION,
              "is_trading_day": is_trading_day(today), "market_open": _is_market_hours(),
              "sections": {}, "overall_grade": "A", "issues": [], "warnings": []}
    checks_passed = 0; checks_total = 0

    def add_check(section, check):
        nonlocal checks_passed, checks_total
        report["sections"][section]["checks"].append(check); checks_total += 1
        if check["status"] == "ok": checks_passed += 1
        elif check["status"] == "warn":
            checks_passed += 0.5; report["warnings"].append(f"[{section}] {check['check']}: {check['value']}")
        else: report["issues"].append(f"[{section}] {check['check']}: {check['value']}")

    try:
        with get_conn() as conn, conn.cursor() as cur:
            report["sections"]["infrastructure"] = {"checks": [], "grade": "A"}
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))"); db_size = cur.fetchone()[0]
            add_check("infrastructure", _check(db_size, "DB size", lambda v: True))
            cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"); table_count = cur.fetchone()[0]
            add_check("infrastructure", _check(table_count, "Tables in DB", lambda v: v >= 40))

            report["sections"]["data_feeds"] = {"checks": [], "grade": "A"}
            for tbl, q, max_d, label in [
                ("raw_prices","SELECT MAX(price_date) FROM raw_prices",1,"EOD price data"),
                ("gvm_scores","SELECT MAX(score_date) FROM gvm_scores",1,"GVM scores"),
                ("v8_metrics","SELECT MAX(score_date) FROM v8_metrics",1,"V8 metrics"),
                ("v8_qualified","SELECT MAX(signal_date) FROM v8_qualified",1,"V8 signals"),
                ("global_indices","SELECT MAX(quote_date) FROM global_indices",2,"Global indices"),
                ("adr_daily","SELECT MAX(price_date) FROM adr_daily",1,"ADR daily"),
                ("pcr_daily","SELECT MAX(price_date) FROM pcr_daily",1,"PCR daily"),
            ]:
                try:
                    cur.execute(q); latest = cur.fetchone()[0]
                    if latest:
                        days_old = (today - latest).days
                        add_check("data_feeds", _check(f"{latest} ({days_old}d ago)", label,
                            lambda v, m=max_d, d=days_old: d <= m, lambda v, m=max_d, d=days_old: d <= m*3))
                    else: add_check("data_feeds", {"check": label, "value": "NO DATA", "status": "fail"})
                except Exception as e: add_check("data_feeds", {"check": label, "value": str(e), "status": "fail"})

            report["sections"]["content_refresh"] = {"checks": [], "grade": "A"}
            add_check("content_refresh", _check(_get_config("takeaway_refresh_due","false"), "Takeaway refresh due", lambda v: v=="false", lambda v: True))
            add_check("content_refresh", _check(_get_config("overview_refresh_due","false"), "Overview refresh due", lambda v: v=="false", lambda v: True))
            cur.execute("SELECT MIN(last_takeaway_updated), COUNT(*) FROM input_raw WHERE mcap_rank <= 500")
            r = cur.fetchone(); oldest = r[0]; count = r[1]
            days_since = (today - oldest).days if oldest else 999
            add_check("content_refresh", _check(f"oldest={oldest} ({days_since}d ago), count={count}", "Takeaway top500 freshness",
                lambda v: days_since <= 90, lambda v: days_since <= 120))

            report["sections"]["v8_engine"] = {"checks": [], "grade": "A"}
            cur.execute("SELECT COUNT(DISTINCT basket) FROM v8_qualified WHERE signal_date=(SELECT MAX(signal_date) FROM v8_qualified)"); active_baskets = cur.fetchone()[0]
            add_check("v8_engine", _check(f"{active_baskets}/5 baskets", "Active signal baskets", lambda v: active_baskets >= 3, lambda v: active_baskets >= 1))
            cur.execute("SELECT COUNT(*) FROM v8_paper_positions WHERE status='OPEN'"); paper_open = cur.fetchone()[0]
            add_check("v8_engine", _check(f"{paper_open} open", "Paper positions", lambda v: True))
            cur.execute("SELECT COUNT(*) FILTER (WHERE result='TARGET'), COUNT(*) FROM v8_paper_trades"); wins, total = cur.fetchone()
            win_rate = round(wins/total*100,1) if total else 0
            add_check("v8_engine", _check(f"{wins}W/{total}T ({win_rate}%)", "Paper win rate",
                lambda v: win_rate >= 60 or total < 5, lambda v: win_rate >= 40 or total < 5))

            report["sections"]["quant_baskets"] = {"checks": [], "grade": "A"}
            cur.execute("SELECT basket_name, COUNT(*) FILTER (WHERE status='open'), MAX(updated_at)::date FROM quant_paper_positions GROUP BY basket_name")
            baskets = cur.fetchall()
            add_check("quant_baskets", _check(f"{len(baskets)}/4 baskets", "Active baskets", lambda v: len(baskets) == 4, lambda v: len(baskets) >= 2))
            total_pos = sum(b[1] for b in baskets)
            add_check("quant_baskets", _check(f"{total_pos} open", "Total QB positions", lambda v: total_pos >= 60, lambda v: total_pos >= 40))

            report["sections"]["gvm_universe"] = {"checks": [], "grade": "A"}
            cur.execute("SELECT COUNT(*), ROUND(AVG(gvm_score)::numeric,2) FROM gvm_scores"); gvm_count, gvm_avg = cur.fetchone()
            add_check("gvm_universe", _check(f"{gvm_count} stocks scored", "GVM universe size", lambda v: gvm_count >= 1500, lambda v: gvm_count >= 1000))
            add_check("gvm_universe", _check(f"avg GVM = {gvm_avg}", "Average GVM score", lambda v: True))

    except Exception as e:
        report["issues"].append(f"[system] DB failed: {e}"); log.error(f"health_report failed: {e}")

    for sec_name, sec in report["sections"].items():
        sec_checks = sec.get("checks", [])
        if not sec_checks: sec["grade"] = "N/A"; continue
        sec_score = sum(1 if c["status"]=="ok" else (0.5 if c["status"]=="warn" else 0) for c in sec_checks)
        sec["grade"] = _grade(sec_score / len(sec_checks) * 100)

    overall_score = round(checks_passed/checks_total*100,1) if checks_total else 0
    report["overall_grade"] = _grade(overall_score); report["overall_score"] = overall_score
    report["checks_passed"] = int(checks_passed); report["checks_total"] = checks_total
    report["issues_count"] = len(report["issues"]); report["warnings_count"] = len(report["warnings"])
    return report

@app.get("/api/health/report")
def health_report(): return build_health_report()

def _build_digest_daily() -> dict:
    now = _ist_now(); result: Dict[str, Any] = {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S IST"), "version": VERSION, "sections": {}}
    def _r(v, d=2):
        try: return round(float(v), d) if v is not None else None
        except: return None
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT g.name, g.category, g.price, g.prev_close, g.chg_pct, g.quote_date::text
                FROM global_indices g
                JOIN (SELECT symbol, MAX(quote_date) AS md FROM global_indices GROUP BY symbol) m
                  ON g.symbol = m.symbol AND g.quote_date = m.md
                ORDER BY CASE g.category WHEN 'index' THEN 1 WHEN 'volatility' THEN 2 WHEN 'commodity' THEN 3 WHEN 'currency' THEN 4 ELSE 5 END, g.name
            """)
            cols = [d[0] for d in cur.description]
            global_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            result["sections"]["1_global_indices"] = {"label": "Global Indices", "quote_date": global_rows[0]["quote_date"] if global_rows else None, "data": global_rows}

            domestic = {}
            for sym in ("NIFTY50", "BANKNIFTY"):
                cur.execute("""
                    WITH d AS (SELECT price_date, open, high, low, close, ROW_NUMBER() OVER (ORDER BY price_date DESC) rn FROM raw_prices WHERE symbol = %s)
                    SELECT a.price_date::text, a.open, a.high, a.low, a.close, ROUND(((a.close-b.close)/NULLIF(b.close,0)*100)::numeric,2)
                    FROM d a JOIN d b ON b.rn=2 WHERE a.rn=1
                """, (sym,))
                r = cur.fetchone()
                if r: domestic[sym] = {"price_date": r[0], "open": _r(r[1]), "high": _r(r[2]), "low": _r(r[3]), "close": _r(r[4]), "chg_pct": _r(r[5])}

            cur.execute("SELECT price_date::text, advances, declines, unchanged, adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
            adr_row = cur.fetchone()
            adr = {"price_date": adr_row[0], "advances": adr_row[1], "declines": adr_row[2], "unchanged": adr_row[3], "adr": _r(adr_row[4])} if adr_row else None
            result["sections"]["2_domestic_indices"] = {"label": "Domestic Indices + ADR", "NIFTY50": domestic.get("NIFTY50"), "BANKNIFTY": domestic.get("BANKNIFTY"), "adr": adr}

            t_due = _get_config("takeaway_refresh_due", "false"); ov_due = _get_config("overview_refresh_due", "false")
            if t_due == "true" or ov_due == "true":
                tier = _get_config("takeaway_refresh_tier", "")
                result["refresh_alert"] = {"takeaway_due": t_due=="true", "overview_due": ov_due=="true", "tier": tier,
                    "action": f"Say 'run {'takeaway' if t_due=='true' else 'overview'} refresh{' top500' if tier=='top500' else ''}' in Claude chat"}

            pivots = {}
            for sym in ("NIFTY50", "BANKNIFTY"):
                cur.execute("SELECT AVG(high), AVG(low), AVG(close) FROM (SELECT high,low,close FROM raw_prices WHERE symbol=%s ORDER BY price_date DESC LIMIT 5) sub", (sym,))
                r = cur.fetchone()
                if r and r[0] is not None:
                    h, l, c = float(r[0]), float(r[1]), float(r[2]); pp = _r((h+l+c)/3)
                    pivots[sym] = {"pp": pp, "r1": _r(2*pp-l), "r2": _r(pp+(h-l)), "s1": _r(2*pp-h), "s2": _r(pp-(h-l))}

            result["sections"]["3_support_levels"] = {"label": "Support Levels (rolling-5d)",
                "NIFTY50": {"s1": pivots.get("NIFTY50",{}).get("s1"), "s2": pivots.get("NIFTY50",{}).get("s2")},
                "BANKNIFTY": {"s1": pivots.get("BANKNIFTY",{}).get("s1"), "s2": pivots.get("BANKNIFTY",{}).get("s2")}}
            result["sections"]["4_pivot_points"] = {"label": "Pivot Points (rolling-5d)", "NIFTY50": pivots.get("NIFTY50"), "BANKNIFTY": pivots.get("BANKNIFTY")}

            pcr_out = {}
            for und in ("NIFTY", "BANKNIFTY"):
                cur.execute("SELECT price_date::text, put_oi, call_oi, pcr FROM pcr_daily WHERE underlying=%s ORDER BY price_date DESC LIMIT 5", (und,))
                cols2 = [d[0] for d in cur.description]; pcr_out[und] = [dict(zip(cols2, row)) for row in cur.fetchall()]
            result["sections"]["5_pcr_trend"] = {"label": "PCR Trend (5-day rolling)", "NIFTY": pcr_out.get("NIFTY",[]), "BANKNIFTY": pcr_out.get("BANKNIFTY",[])}

    except Exception as e:
        log.error(f"_build_digest_daily failed: {e}"); result["error"] = str(e)
    return result

@app.get("/api/digest/daily")
def digest_daily(): return _build_digest_daily()

@app.get("/api/daily/adr")
def daily_adr(days: int = 5):
    days = min(max(days, 1), 30)
    rows = api_query("SELECT price_date::text, advances, declines, unchanged, adr, CASE WHEN adr>=1.0 THEN TRUE ELSE FALSE END AS pass FROM adr_daily ORDER BY price_date DESC LIMIT %s", (days,))
    return {"days": len(rows) if isinstance(rows, list) else 0, "data": rows if isinstance(rows, list) else []}

@app.get("/api/daily/pcr")
def daily_pcr(underlying: str = "NIFTY", days: int = 5):
    underlying = underlying.upper(); days = min(max(days, 1), 30)
    rows = api_query("SELECT price_date::text, underlying, put_oi, call_oi, pcr FROM pcr_daily WHERE underlying=%s ORDER BY price_date DESC LIMIT %s", (underlying, days))
    return {"underlying": underlying, "days": len(rows) if isinstance(rows, list) else 0, "data": rows if isinstance(rows, list) else []}

@app.post("/api/daily/compute_metrics")
def compute_daily_metrics_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with get_conn() as conn: return {"adr": _compute_and_store_adr(conn), "pcr": _compute_and_store_pcr(conn)}

@app.get("/api/admin/refresh_status")
def admin_refresh_status(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); return rt.get_refresh_status()

@app.post("/api/admin/mark_refresh_complete")
def mark_refresh_complete(field: str, tier: str, count: int, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); return rt.mark_refresh_complete(field, tier, count)

# ── Content Update Endpoint (v2.9.8) ──────────────────────────────────────────────
_ALLOWED_CONTENT_FIELDS = {"overview", "key_takeaway", "result_analysis"}
_TOP500_ONLY_FIELDS = {"key_takeaway", "result_analysis"}
_FIELD_TO_TS_COL = {
    "overview": "last_overview_updated",
    "key_takeaway": "last_takeaway_updated",
    "result_analysis": "last_result_analysis_updated",
}

@app.post("/api/admin/content_update")
def content_update(req_body: dict, x_admin_token: Optional[str] = Header(None)):
    """
    Manual content writer for input_raw.
    Body: { "symbol": "RELIANCE", "field": "overview|key_takeaway|result_analysis", "content": "..." }
    Rules:
      - field must be one of the 3 allowed values
      - key_takeaway + result_analysis blocked for mcap_rank > 500
      - always overwrites existing content
      - updates the corresponding last_*_updated timestamp
    """
    _check_admin(x_admin_token)

    symbol = (req_body.get("symbol") or "").strip().upper()
    field = (req_body.get("field") or "").strip().lower()
    content = req_body.get("content", "")

    if not symbol:
        raise HTTPException(400, "symbol is required")
    if field not in _ALLOWED_CONTENT_FIELDS:
        raise HTTPException(400, f"field must be one of: {sorted(_ALLOWED_CONTENT_FIELDS)}")
    if content is None or str(content).strip() == "":
        raise HTTPException(400, "content cannot be empty")

    content = str(content).strip()
    ts_col = _FIELD_TO_TS_COL[field]

    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, mcap_rank, company_name FROM input_raw WHERE nse_code = %s",
                (symbol,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, f"{symbol} not found in input_raw")

            row_id, mcap_rank, company_name = row[0], row[1], row[2]

            if field in _TOP500_ONLY_FIELDS:
                rank = mcap_rank if mcap_rank is not None else 9999
                if rank > 500:
                    raise HTTPException(400,
                        f"{symbol} has mcap_rank={rank} (>500). "
                        f"'{field}' is only allowed for mcap_rank <= 500. "
                        f"Use 'overview' instead — it covers all 1700+."
                    )

            cur.execute(
                f"UPDATE input_raw SET {field} = %s, {ts_col} = NOW() WHERE id = %s",
                (content, row_id)
            )
            conn.commit()

        return {
            "status": "ok",
            "symbol": symbol,
            "company_name": company_name,
            "field": field,
            "chars_written": len(content),
            "timestamp_col_updated": ts_col,
            "mcap_rank": mcap_rank,
        }

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"content_update failed for {symbol}: {e}")
        raise HTTPException(500, str(e))

# ── Refactor notes ─────────────────────────────────────────────────────────────
# QB endpoints -> qb_endpoints.py (file 1). GVM+market reads -> gvm_market_endpoints.py (file 2).
# Screener earnings + Drive loaders -> admin_data.py (file 3). Routers included above.

@app.get("/api/admin/env_check")
def env_check(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); keys = sorted(os.environ.keys())
    interesting = ["SCREENER_EMAIL","SCREENER_PASSWORD","GITHUB_TOKEN","GITHUB_REPO","ADMIN_TOKEN","DATABASE_URL","DEPLOY_GUARD","RAILWAY_PUBLIC_DOMAIN"]
    return {"version": VERSION, "all_keys_count": len(keys), "interesting": {k: {"present": k in os.environ, "len": len(os.environ.get(k,""))} for k in interesting}}

@app.post("/api/v8/build_cache")
def v8_build_cache(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with get_conn() as conn: return build_history_cache(conn)

@app.post("/api/v8/run_live")
def v8_run_live(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with get_conn() as conn: return run_live_tick(conn)

@app.post("/api/v8/run_signal_writer")
def v8_run_signal_writer(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with get_conn() as conn: return v8_signal_writer.run_live_signal_writer(conn)

@app.post("/api/momentum/run")
def momentum_run(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    import momentum_daily; return momentum_daily.compute_momentum()

@app.get("/api/health/feeds")
def health_feeds():
    out = []
    queries = [
        ("gvm_scores","SELECT MAX(score_date), COUNT(*) FROM gvm_scores"),
        ("raw_prices","SELECT MAX(price_date), COUNT(DISTINCT symbol) FROM raw_prices"),
        ("input_raw","SELECT NULL, COUNT(*) FROM input_raw"),
        ("screener_raw","SELECT NULL, COUNT(*) FROM screener_raw"),
        ("v8_metrics","SELECT MAX(score_date), COUNT(DISTINCT symbol) FROM v8_metrics"),
        ("v8_qualified","SELECT MAX(signal_date), COUNT(*) FROM v8_qualified"),
        ("v8_history_cache","SELECT MAX(cache_date), COUNT(*) FROM v8_history_cache"),
        ("global_indices","SELECT MAX(quote_date), COUNT(DISTINCT symbol) FROM global_indices"),
        ("adr_daily","SELECT MAX(price_date), COUNT(*) FROM adr_daily"),
        ("pcr_daily","SELECT MAX(price_date), COUNT(*) FROM pcr_daily"),
        ("quant_positions","SELECT MAX(updated_at)::date, COUNT(*) FROM quant_paper_positions WHERE status='open'"),
    ]
    try:
        with get_conn() as conn, conn.cursor() as cur:
            for name, q in queries:
                try:
                    cur.execute(q); r = cur.fetchone()
                    latest = str(r[0]) if r[0] else None; count = r[1] or 0
                    days_old = None; freshness = "n/a"
                    if latest and r[0]:
                        try: days_old = (date.today() - r[0]).days; freshness = "ok" if days_old < 7 else "stale"
                        except: pass
                    out.append({"source": name, "latest": latest, "records": count, "freshness": freshness, "days_old": days_old})
                except Exception as e: out.append({"source": name, "error": str(e)})
    except Exception as e: return {"error": str(e)}
    return {"checked_at": str(date.today()), "feeds": out}

def api_query(sql, params=None, single=False):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params or ()); cols = [d[0] for d in cur.description] if cur.description else []
            if single: r = cur.fetchone(); return dict(zip(cols, r)) if r else None
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"api_query error: {e}"); return {"error": str(e)}

@app.post("/api/v8/run")
async def v8_run(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with get_conn() as conn: return run_v8_engine(conn)

@app.post("/api/v8/run_for_date")
def v8_run_for_date(target_date: str, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    from datetime import date as _date; d = _date.fromisoformat(target_date)
    with get_conn() as conn: return run_v8_engine(conn, target_date=d)

@app.get("/api/v8/metrics/all")
def v8_metrics_all():
    return api_query("""
        SELECT symbol, score_date, gvm_score, dma_50, dma_200, dma_20, rsi_month, rsi_weekly, daily_rsi,
               month_return, week_return, year_return, prev_day_change, sector_day, sector_week,
               month_index, week_index_52, range_1d, range_3d, upper_bb, lower_bb, ma9_vs_ma21, vol_ratio
        FROM v8_metrics WHERE score_date=(SELECT MAX(score_date) FROM v8_metrics) ORDER BY symbol
    """)

@app.get("/api/v8/metrics/{symbol}")
def v8_metrics_single(symbol: str, score_date: Optional[str] = None):
    if not score_date: score_date = str(date.today())
    r = api_query("SELECT * FROM v8_metrics WHERE symbol=%s AND score_date=%s", (symbol.upper(), score_date), single=True)
    if not r: r = api_query("SELECT * FROM v8_metrics WHERE symbol=%s ORDER BY score_date DESC LIMIT 1", (symbol.upper(),), single=True)
    if not r: raise HTTPException(404, f"No metrics for {symbol}")
    return r

@app.get("/api/v8/live_metrics")
def v8_live_metrics():
    return api_query("""
        SELECT s.symbol, lc.close AS cmp, fc.open AS day_open,
            CASE WHEN fc.open>0 THEN ROUND(((lc.close/fc.open-1)*100)::numeric,2) END AS day_pct,
            hc.close AS hour_ago_close,
            CASE WHEN hc.close>0 THEN ROUND(((lc.close/hc.close-1)*100)::numeric,2) END AS hourly_pct
        FROM (SELECT symbol FROM futures_universe WHERE is_active=TRUE) s
        JOIN LATERAL (SELECT close FROM intraday_prices WHERE symbol=s.symbol AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 1) lc ON true
        JOIN LATERAL (SELECT open FROM intraday_prices WHERE symbol=s.symbol AND ts::date=CURRENT_DATE ORDER BY ts ASC LIMIT 1) fc ON true
        LEFT JOIN LATERAL (SELECT close FROM intraday_prices WHERE symbol=s.symbol AND ts>=NOW()-INTERVAL '65 minutes' ORDER BY ts ASC LIMIT 1) hc ON true
        ORDER BY s.symbol
    """)

@app.post("/api/admin/backfill_intraday")
async def backfill_intraday(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); futures = _get_futures_symbols()
    if not futures: return {"status":"warn","message":"No futures symbols"}
    total_candles, failed = 0, []
    for sym in futures:
        candles = await _fetch_intraday_yahoo(sym, range_str="7d")
        if candles: _insert_intraday(candles); total_candles += len(candles)
        else: failed.append(sym)
        await asyncio.sleep(0.25)
    _purge_intraday_old()
    return {"status":"ok","symbols_attempted":len(futures),"symbols_failed":len(failed),"total_candles":total_candles}

_LAG_MINUTES = 15; _HEAL_SLEEP = 0.8

def _yahoo_1m_today(symbol: str):
    ticker = _yahoo_ticker(symbol); now = int(time.time())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?interval=1m&period1={now-2*86400}&period2={now+3600}"
    for attempt in range(3):
        try:
            with httpx.Client(timeout=15, headers={"User-Agent":"Mozilla/5.0"}) as c:
                r = c.get(url); r.raise_for_status(); data = r.json()
            chart = (data.get("chart") or {}).get("result") or []
            if not chart:
                if attempt < 2: time.sleep(0.5+0.5*attempt); continue
                return []
            res = chart[0]; ts = res.get("timestamp") or []
            q = (res.get("indicators") or {}).get("quote",[{}])[0]
            o,h,l,c_,v = (q.get(k) or [] for k in ("open","high","low","close","volume"))
            out = []
            for i in range(len(ts)):
                op=o[i] if i<len(o) else None; hi=h[i] if i<len(h) else None
                lo=l[i] if i<len(l) else None; cl=c_[i] if i<len(c_) else None; vol=v[i] if i<len(v) else None
                if op is None or hi is None or lo is None or cl is None or not vol: continue
                dt = datetime.utcfromtimestamp(ts[i]) + timedelta(hours=5,minutes=30)
                out.append((dt,round(float(op),2),round(float(hi),2),round(float(lo),2),round(float(cl),2),int(vol)))
            return out
        except Exception as e:
            if attempt < 2: time.sleep(0.5+0.5*attempt); continue
            log.warning(f"yahoo_1m_today {symbol}: {e}"); return []
    return []

def _heal_morning_gaps(symbols=None):
    now = _ist_now(); today = now.date()
    open_dt = now.replace(hour=9,minute=15,second=0,microsecond=0); close_dt = now.replace(hour=15,minute=30,second=0,microsecond=0)
    heal_until = now - timedelta(minutes=_LAG_MINUTES)
    if heal_until > close_dt: heal_until = close_dt
    if heal_until <= open_dt: return {"status":"noop","reason":"before ~09:30 IST","today":str(today)}
    syms = symbols if symbols else _get_futures_symbols()
    syms = [s for s in syms if s not in ("NIFTY","BANKNIFTY","NIFTY50","FINNIFTY","MIDCPNIFTY","SENSEX","BANKEX")]
    healed,skipped,empties,errors,inserted = 0,0,0,[],0
    for sym in syms:
        try:
            row = api_query("SELECT MIN(ts) AS mn, COUNT(*) AS cnt FROM intraday_prices WHERE symbol=%s AND ts::date=%s AND timeframe='1m'", (sym,today), single=True)
            earliest = row.get("mn") if isinstance(row,dict) else None; cnt = row.get("cnt",0) if isinstance(row,dict) else 0
            if cnt==0: gap_from = open_dt.replace(tzinfo=None)
            elif earliest is not None and earliest>open_dt.replace(tzinfo=None)+timedelta(minutes=1): gap_from = open_dt.replace(tzinfo=None)
            else: skipped+=1; continue
            candles = _yahoo_1m_today(sym)
            if not candles: empties+=1; time.sleep(_HEAL_SLEEP); continue
            hu = heal_until.replace(tzinfo=None); rows = []
            for (ts,op,hi,lo,cl,vol) in candles:
                if ts.date()!=today or ts<gap_from or ts>hu: continue
                if earliest is not None and ts>=earliest: continue
                rows.append((sym,ts,op,hi,lo,cl,vol,"1m","yahoo"))
            if rows:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.executemany("INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (symbol,ts,timeframe) DO NOTHING", rows)
                    conn.commit()
                inserted+=len(rows); healed+=1
            else: skipped+=1
            time.sleep(_HEAL_SLEEP)
        except Exception as e: errors.append(f"{sym}: {str(e)[:60]}"); log.warning(f"heal {sym}: {e}")
    return {"status":"ok","today":str(today),"window":f"{open_dt.strftime('%H:%M')}-{heal_until.strftime('%H:%M')} IST",
            "symbols_checked":len(syms),"symbols_healed":healed,"bars_inserted":inserted,"skipped_complete":skipped,"empty_from_yahoo":empties,"errors":errors[:10]}

@app.post("/api/admin/heal_intraday")
async def heal_intraday(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); return await asyncio.to_thread(_heal_morning_gaps)

@app.post("/api/admin/run_yahoo_daily")
async def run_yahoo_daily_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    if _yahoo_daily_running: return {"status":"already_running"}
    asyncio.create_task(_bg_yahoo_daily()); return {"status":"started"}

@app.post("/api/admin/backfill_indices")
def backfill_indices_now(days: int = 7, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); return yahoo_index_backfill.backfill_indices(days=days)

@app.post("/api/paper/compute_pivots")
def paper_compute_pivots(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with get_conn() as conn: return v8_paper.compute_pivots(conn)

@app.post("/api/paper/tick")
def paper_tick_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); buy_slots = sell_slots = None
    try:
        with httpx.Client(timeout=30) as c:
            mood = c.get(f"{BASE_URL}/api/v8/market_mood").json()
            buy_slots,sell_slots = mood.get("buy_slots"),mood.get("sell_slots")
    except Exception: pass
    with get_conn() as conn: return v8_paper.paper_tick(conn, buy_slots=buy_slots, sell_slots=sell_slots)

@app.get("/api/paper/status")
def paper_status():
    return {"open_positions": api_query("SELECT symbol,side,basket,entry_price,entry_ts,target,stop_loss,qty,pivot_date FROM v8_paper_positions WHERE status='OPEN' ORDER BY entry_ts DESC"),
            "recent_trades": api_query("SELECT symbol,side,basket,entry_price,exit_price,pnl,return_pct,result,entry_ts,exit_ts FROM v8_paper_trades ORDER BY closed_at DESC LIMIT 100"),
            "missed": api_query("SELECT miss_date,symbol,side,basket,expected_entry,reason FROM v8_paper_missed ORDER BY ts DESC LIMIT 100"),
            "summary": api_query("SELECT COUNT(*) AS trades, COUNT(*) FILTER (WHERE result='TARGET') AS wins, COUNT(*) FILTER (WHERE result='SL') AS losses, ROUND(SUM(pnl)::numeric,2) AS total_pnl, ROUND(AVG(return_pct)::numeric,3) AS avg_ret FROM v8_paper_trades", single=True)}

@app.get("/api/paper/pivots")
def paper_pivots(limit: int = 250):
    return api_query("SELECT symbol,pp,r1,s1,r2,s2,pivot_date FROM v8_paper_pivots WHERE pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots) ORDER BY symbol LIMIT %s", (limit,))

@app.post("/api/admin/fetch_global")
async def fetch_global_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with global_indices.get_conn_from_env() as conn: return await global_indices.fetch_global_indices(conn)

@app.post("/api/admin/backfill_global")
async def backfill_global_now(years: int = 5, clean: bool = True, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with global_indices.get_conn_from_env() as conn: return await global_indices.backfill_global_indices(conn, years=years, clean=clean)

@app.post("/api/admin/fetch_global_intraday")
async def fetch_global_intraday_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with global_indices.get_conn_from_env() as conn:
        res = await global_indices.fetch_global_intraday(conn); global_indices.prune_global_intraday(conn, days=7); return res

GITHUB_API = "https://api.github.com"

def _gh_headers():
    if not GITHUB_TOKEN: raise HTTPException(500,"GITHUB_TOKEN not configured")
    return {"Authorization":f"Bearer {GITHUB_TOKEN}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}

def _check_admin(token):
    if not ADMIN_TOKEN: return True
    if token != ADMIN_TOKEN: raise HTTPException(403,"Invalid admin token")
    return True

def _check_deploy_guard():
    if not DEPLOY_GUARD: raise HTTPException(403,"DEPLOY_GUARD is off")

async def _gh_get_file(filepath):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=_gh_headers())
        if r.status_code == 404: return {"exists":False,"content":None,"sha":None,"size":0}
        r.raise_for_status(); data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return {"exists":True,"content":content,"sha":data["sha"],"size":data["size"]}

async def _gh_put_file(filepath, new_content, commit_message, sha=None):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    payload = {"message":commit_message,"content":base64.b64encode(new_content.encode("utf-8")).decode("ascii"),"branch":"main"}
    if sha: payload["sha"] = sha
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.put(url, headers=_gh_headers(), json=payload)
        if r.status_code not in (200,201): raise HTTPException(r.status_code, f"GitHub error: {r.text[:300]}")
        return r.json()

async def _gh_delete_file(filepath, commit_message, sha):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request("DELETE", url, headers=_gh_headers(), json={"message":commit_message,"sha":sha,"branch":"main"})
        if r.status_code != 200: raise HTTPException(r.status_code, f"GitHub delete error: {r.text[:300]}")
        return r.json()

async def _gh_list_tree(path_prefix=""):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path_prefix}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=_gh_headers()); r.raise_for_status(); data = r.json()
        if isinstance(data,dict): data = [data]
        return [{"name":x["name"],"path":x["path"],"type":x["type"],"size":x.get("size",0)} for x in data]

@app.get("/api/admin/github_read")
async def github_read(filepath: str, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    if not GITHUB_REPO: raise HTTPException(500,"GITHUB_REPO not configured")
    info = await _gh_get_file(filepath)
    if not info["exists"]: raise HTTPException(404,f"File not found: {filepath}")
    return {"filepath":filepath,"size":info["size"],"sha":info["sha"],"content":info["content"],"lines":info["content"].count("\n")+1}

@app.get("/api/admin/github_list")
async def github_list(path: str = "", x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    if not GITHUB_REPO: raise HTTPException(500,"GITHUB_REPO not configured")
    files = await _gh_list_tree(path)
    return {"path":path or "/","items":files,"count":len(files)}

@app.post("/api/admin/github_push")
async def github_push(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); _check_deploy_guard()
    if not GITHUB_REPO: raise HTTPException(500,"GITHUB_REPO not configured")
    body = await req.json()
    filepath = body.get("filepath"); new_content = body.get("new_content")
    commit_message = body.get("commit_message", f"chore: update {filepath}")
    create_if_missing = body.get("create_if_missing", True)
    if not filepath or new_content is None: raise HTTPException(400,"filepath and new_content required")
    existing = await _gh_get_file(filepath)
    if not existing["exists"] and not create_if_missing: raise HTTPException(404,f"File {filepath} does not exist")
    if existing["exists"] and existing["content"] == new_content:
        return {"status":"noop","message":"Content identical","filepath":filepath}
    sha = existing["sha"] if existing["exists"] else None
    result = await _gh_put_file(filepath, new_content, commit_message, sha)
    return {"status":"ok","filepath":filepath,"action":"updated" if existing["exists"] else "created",
            "commit_sha":result.get("commit",{}).get("sha"),"commit_url":result.get("commit",{}).get("html_url"),
            "old_size":existing["size"],"new_size":len(new_content)}

@app.post("/api/admin/github_delete")
async def github_delete(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); _check_deploy_guard()
    if not GITHUB_REPO: raise HTTPException(500,"GITHUB_REPO not configured")
    body = await req.json()
    filepath = body.get("filepath"); commit_message = body.get("commit_message",f"chore: delete {filepath}")
    if not filepath: raise HTTPException(400,"filepath required")
    existing = await _gh_get_file(filepath)
    if not existing["exists"]: raise HTTPException(404,f"File not found: {filepath}")
    result = await _gh_delete_file(filepath, commit_message, existing["sha"])
    return {"status":"ok","filepath":filepath,"action":"deleted","commit_sha":result.get("commit",{}).get("sha")}

_oauth_codes = {}; _oauth_tokens = {}

@app.get("/.well-known/oauth-authorization-server")
def oauth_metadata():
    return {"issuer":BASE_URL,"authorization_endpoint":f"{BASE_URL}/oauth/authorize","token_endpoint":f"{BASE_URL}/oauth/token",
            "registration_endpoint":f"{BASE_URL}/oauth/register","scopes_supported":["read","write"],
            "response_types_supported":["code"],"grant_types_supported":["authorization_code"],
            "code_challenge_methods_supported":["S256","plain"],"token_endpoint_auth_methods_supported":["none","client_secret_post"]}

@app.get("/.well-known/oauth-protected-resource")
def oauth_resource():
    return {"resource":BASE_URL,"authorization_servers":[BASE_URL],"scopes_supported":["read","write"]}

@app.post("/oauth/register")
async def oauth_register(req: Request):
    body = await req.json(); cid = secrets.token_urlsafe(16)
    return {"client_id":cid,"client_id_issued_at":int(time.time()),"redirect_uris":body.get("redirect_uris",[]),
            "token_endpoint_auth_method":"none","grant_types":["authorization_code"],"response_types":["code"]}

@app.get("/oauth/authorize")
def oauth_authorize(client_id: str, redirect_uri: str, response_type: str="code", state: str="", code_challenge: str="", code_challenge_method: str="", scope: str=""):
    code = secrets.token_urlsafe(24)
    _oauth_codes[code] = {"client_id":client_id,"redirect_uri":redirect_uri,"code_challenge":code_challenge,"created":time.time()}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}")

@app.post("/oauth/token")
async def oauth_token(req: Request):
    form = await req.form(); code = form.get("code")
    if code not in _oauth_codes: raise HTTPException(400,"Invalid code")
    info = _oauth_codes.pop(code); token = secrets.token_urlsafe(32)
    _oauth_tokens[token] = {"client_id":info["client_id"],"created":time.time()}
    return {"access_token":token,"token_type":"Bearer","expires_in":31536000,"scope":"read write"}

# ── MCP Tools ───────────────────────────────────────────────────────────────────
MCP_TOOLS = [
    {"name":"server_now","description":"Authoritative India time (Asia/Kolkata, UTC+5:30).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"health_report","description":"Full Scorr system health report card — sections with grades, content refresh status, issues list.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"digest_daily","description":"Daily Digest sections 1-5 baked from DB. Includes refresh_alert if takeaway/overview refresh is due.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v8_build_cache","description":"V8 LIVE: build v8_history_cache.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"v8_run_live","description":"V8 LIVE: run one live tick.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"run_momentum","description":"GVM: recompute daily momentum (M) for all stocks from raw_prices.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"gvm_recompute","description":"GVM: full recompute.","inputSchema":{"type":"object","properties":{"refresh_momentum":{"type":"boolean"}},"required":[]}},
    {"name":"gvm_history","description":"GVM: get the GVM score trend series for a stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"days":{"type":"integer"}},"required":["symbol"]}},
    {"name":"get_gvm","description":"Fetch full GVM score for a stock — includes overview, key_takeaway, instrument_type (futures/cash), cap_category (large/mid/small/micro), mcap_rank.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"get_top_stocks","description":"Get top N stocks by GVM.","inputSchema":{"type":"object","properties":{"n":{"type":"integer"},"verdict":{"type":"string"}},"required":["n"]}},
    {"name":"get_sector","description":"Get all stocks in a sector ordered by GVM.","inputSchema":{"type":"object","properties":{"sector":{"type":"string"}},"required":["sector"]}},
    {"name":"get_filter","description":"Filter stocks by GVM range.","inputSchema":{"type":"object","properties":{"min_gvm":{"type":"number"},"max_gvm":{"type":"number"}},"required":[]}},
    {"name":"get_sector_rating","description":"Get sector-level mcap-weighted GVM ratings.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"get_intraday","description":"Intraday OHLC for ANY stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"days":{"type":"integer"},"interval":{"type":"string"},"source":{"type":"string"}},"required":["symbol"]}},
    {"name":"get_cmp","description":"Get latest CMP for a stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"backfill_intraday","description":"MANUAL Yahoo fallback: fetch 7 days of 5-min OHLC for all futures.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"heal_intraday","description":"Fill TODAY's morning 1-min gap in intraday_prices for all active futures from Yahoo.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"run_yahoo_daily","description":"Trigger Yahoo daily OHLC update for raw_prices (background).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"backfill_indices","description":"Backfill NIFTY50 + BANKNIFTY 1-min OHLC into intraday_prices.","inputSchema":{"type":"object","properties":{"days":{"type":"integer"}},"required":[]}},
    {"name":"paper_compute_pivots","description":"PAPER: compute rolling-5-day pivots for all futures.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"paper_tick","description":"PAPER: run one paper-engine tick.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"paper_status","description":"PAPER: open positions + recent closed trades + summary.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"paper_pivots","description":"PAPER: latest rolling-5 pivot levels per stock.","inputSchema":{"type":"object","properties":{"limit":{"type":"integer"}},"required":[]}},
    {"name":"run_v8_engine","description":"Run the V8 EOD engine — compute metrics + write signals to DB.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"run_v8_for_date","description":"Backfill v8_metrics for a PAST date (YYYY-MM-DD).","inputSchema":{"type":"object","properties":{"target_date":{"type":"string"}},"required":["target_date"]}},
    {"name":"get_v8_metrics","description":"Get computed V8 metrics for one stock.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}},
    {"name":"get_v8_metrics_all","description":"Get all metrics for the full universe (latest date).","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"get_v8_live_metrics","description":"Get real-time CMP, day%, hourly gain for the universe.","inputSchema":{"type":"object","properties":{},"required":[]}},
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
    {"name":"get_global_intraday","description":"Gold/Silver 5-min intraday bars (7-day rolling).","inputSchema":{"type":"object","properties":{"name":{"type":"string"},"days":{"type":"integer"}},"required":["name"]}},
    {"name":"fetch_global_intraday","description":"Manually trigger Gold/Silver 5-min intraday fetch.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"qb_eod_check","description":"Quant Basket: run EOD stop-loss check + P&L mark for a basket.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"}},"required":[]}},
    {"name":"qb_positions","description":"Quant Basket: get open positions with P&L, stop prices.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"},"status":{"type":"string"}},"required":[]}},
    {"name":"qb_summary","description":"Quant Basket: portfolio summary — market value, unrealised P&L, realised P&L.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"}},"required":[]}},
    {"name":"qb_rebalance_log","description":"Quant Basket: rebalance + EOD check history.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"},"limit":{"type":"integer"}},"required":[]}},
    {"name":"qb_registry","description":"Quant Basket: registry of all baskets.","inputSchema":{"type":"object","properties":{"basket_name":{"type":"string"}},"required":[]}},
    {"name":"daily_adr","description":"ADR trend last N days from adr_daily.","inputSchema":{"type":"object","properties":{"days":{"type":"integer"}},"required":[]}},
    {"name":"daily_pcr","description":"PCR trend last N days from pcr_daily. underlying: NIFTY or BANKNIFTY.","inputSchema":{"type":"object","properties":{"underlying":{"type":"string"},"days":{"type":"integer"}},"required":[]}},
    {"name":"compute_daily_metrics","description":"Manually trigger ADR + PCR compute-and-store.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"refresh_status","description":"Show AI content refresh status — last run dates, next due dates, due flags. No API key needed.","inputSchema":{"type":"object","properties":{},"required":[]}},
    {"name":"content_update","description":"Manual content writer for input_raw. Updates overview, key_takeaway, or result_analysis for a single symbol. key_takeaway and result_analysis are blocked for mcap_rank > 500. Always overwrites.","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"field":{"type":"string","enum":["overview","key_takeaway","result_analysis"]},"content":{"type":"string"}},"required":["symbol","field","content"]}},
]

async def _call_tool(name, args):
    async with httpx.AsyncClient(timeout=600) as client:
        h = {"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}
        if name == "server_now": r = await client.get(f"{BASE_URL}/api/now"); return r.json()
        elif name == "health_report": r = await client.get(f"{BASE_URL}/api/health/report"); return r.json()
        elif name == "digest_daily": r = await client.get(f"{BASE_URL}/api/digest/daily"); return r.json()
        elif name == "v8_build_cache": r = await client.post(f"{BASE_URL}/api/v8/build_cache", headers=h); return r.json()
        elif name == "v8_run_live": r = await client.post(f"{BASE_URL}/api/v8/run_live", headers=h); return r.json()
        elif name == "run_momentum": r = await client.post(f"{BASE_URL}/api/momentum/run", headers=h); return r.json()
        elif name == "gvm_recompute": r = await client.post(f"{BASE_URL}/api/gvm/recompute", params={"refresh_momentum": args.get("refresh_momentum",True)}, headers=h); return r.json()
        elif name == "gvm_history": r = await client.get(f"{BASE_URL}/api/gvm/history/{args['symbol']}", params={"days": args.get("days",180)}); return r.json()
        elif name == "get_gvm": r = await client.get(f"{BASE_URL}/api/gvm/{args['symbol']}"); return r.json()
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
        elif name == "backfill_intraday": r = await client.post(f"{BASE_URL}/api/admin/backfill_intraday", headers=h); return r.json()
        elif name == "heal_intraday": r = await client.post(f"{BASE_URL}/api/admin/heal_intraday", headers=h); return r.json()
        elif name == "run_yahoo_daily": r = await client.post(f"{BASE_URL}/api/admin/run_yahoo_daily", headers=h); return r.json()
        elif name == "backfill_indices": r = await client.post(f"{BASE_URL}/api/admin/backfill_indices", params={"days": args.get("days",7)}, headers=h); return r.json()
        elif name == "paper_compute_pivots": r = await client.post(f"{BASE_URL}/api/paper/compute_pivots", headers=h); return r.json()
        elif name == "paper_tick": r = await client.post(f"{BASE_URL}/api/paper/tick", headers=h); return r.json()
        elif name == "paper_status": r = await client.get(f"{BASE_URL}/api/paper/status"); return r.json()
        elif name == "paper_pivots": r = await client.get(f"{BASE_URL}/api/paper/pivots", params={"limit": args.get("limit",250)}); return r.json()
        elif name == "run_v8_engine": r = await client.post(f"{BASE_URL}/api/v8/run", headers=h); return r.json()
        elif name == "run_v8_for_date": r = await client.post(f"{BASE_URL}/api/v8/run_for_date", params={"target_date": args["target_date"]}, headers=h); return r.json()
        elif name == "get_v8_metrics": r = await client.get(f"{BASE_URL}/api/v8/metrics/{args['symbol']}"); return r.json()
        elif name == "get_v8_metrics_all": r = await client.get(f"{BASE_URL}/api/v8/metrics/all"); return r.json()
        elif name == "get_v8_live_metrics": r = await client.get(f"{BASE_URL}/api/v8/live_metrics"); return r.json()
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
                json={"symbol": args["symbol"], "field": args["field"], "content": args["content"]},
                headers=h)
            return r.json()
        return {"error": f"Unknown tool: {name}"}

@app.post("/mcp")
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
