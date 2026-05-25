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

from v5_engine import (
    V5_SCHEMA_SQL, seed_default_filters, run_v5_engine,
    compute_metrics_for_symbol, load_filters, store_metrics
)
from v6_backtest import V6_BACKTEST_SCHEMA, run_full_optimization
from v6_engine import V6_SCHEMA_SQL, run_v6_engine, compare_v5_v6

# ============================================================
# Scorr / Project Quant — main.py v1.6.4
# v1.6.4: Restore /api/admin/load_v5_from_drive (original V5 Apps Script endpoint)
# v1.6.3: /api/admin/load_v5_signals_csv individual file loader
# v1.6.2: /api/v5/live_metrics + /api/v5/metrics/all for V5 Apps Script wiring
# v1.6.1: Fix cmp_prices (yfinance batch), wire raw_prices EOD scheduler
# v1.6.0: V6 shadow engine (optimized filters, v5↔v6 paper-trade compare)
# ============================================================

VERSION = "1.6.4"

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

def get_conn():
    return psycopg.connect(DATABASE_URL)

def create_tables():
    sql = """
    CREATE TABLE IF NOT EXISTS input_raw (id SERIAL PRIMARY KEY, data JSONB);
    CREATE TABLE IF NOT EXISTS screener_raw (id SERIAL PRIMARY KEY, data JSONB);
    CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, data JSONB);
    CREATE TABLE IF NOT EXISTS signals (id SERIAL PRIMARY KEY, data JSONB);
    CREATE TABLE IF NOT EXISTS v5_signals (
        id SERIAL PRIMARY KEY, signal_type TEXT, timestamp TEXT, symbol TEXT,
        finkhoz_rating NUMERIC, record_price NUMERIC, cap_type TEXT,
        current_price NUMERIC, return_pct NUMERIC, hit_alert TEXT,
        analyst_verdict TEXT, alert_count INT, alert_types TEXT,
        event_date DATE, event_type TEXT, verdict_date DATE,
        loaded_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS v5_futures_open (
        id SERIAL PRIMARY KEY, symbol TEXT, open_date TEXT, type TEXT,
        qty NUMERIC, entry_price NUMERIC, current_price NUMERIC,
        net_pl_pct NUMERIC, profit_per_lot NUMERIC, value NUMERIC,
        remarks TEXT, status TEXT, loaded_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS v5_trades (id SERIAL PRIMARY KEY, data JSONB, loaded_at TIMESTAMP DEFAULT NOW());
    CREATE TABLE IF NOT EXISTS v5_portfolio (id SERIAL PRIMARY KEY, data JSONB, loaded_at TIMESTAMP DEFAULT NOW());
    CREATE TABLE IF NOT EXISTS v5_baskets (id SERIAL PRIMARY KEY, basket_name TEXT, data JSONB, loaded_at TIMESTAMP DEFAULT NOW());
    CREATE TABLE IF NOT EXISTS earnings_calendar (
        id SERIAL PRIMARY KEY, company_name TEXT, ticker TEXT,
        ex_date DATE, record_date DATE, event_type TEXT,
        loaded_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS intraday_prices (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, ts TIMESTAMP NOT NULL,
        open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC, volume BIGINT,
        UNIQUE(symbol, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_intraday_symbol_ts ON intraday_prices(symbol, ts DESC);
    CREATE TABLE IF NOT EXISTS cmp_prices (
        symbol TEXT PRIMARY KEY, cmp NUMERIC, updated_at TIMESTAMP DEFAULT NOW()
    );
    """ + V5_SCHEMA_SQL + V6_BACKTEST_SCHEMA + V6_SCHEMA_SQL
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()
        log.info("Tables ready")
        with get_conn() as conn:
            seed_default_filters(conn)
    except Exception as e:
        log.error(f"create_tables failed: {e}")

def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def _is_market_hours() -> bool:
    now = _ist_now()
    if now.weekday() >= 5: return False
    return now.replace(hour=9, minute=15, second=0, microsecond=0) <= now <= now.replace(hour=15, minute=30, second=0, microsecond=0)

def _is_eod_window() -> bool:
    now = _ist_now()
    if now.weekday() >= 5: return False
    return now.replace(hour=15, minute=45, second=0, microsecond=0) <= now <= now.replace(hour=16, minute=0, second=0, microsecond=0)

def _get_futures_symbols() -> List[str]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM v5_signals ORDER BY symbol")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"_get_futures_symbols failed: {e}")
        return []

def _get_all_gvm_symbols() -> List[str]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM gvm_scores ORDER BY symbol")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"_get_all_gvm_symbols failed: {e}")
        return []

def _yahoo_ticker(symbol: str) -> str:
    return f"{symbol}.NS"

async def _fetch_cmp_yahoo(symbols: List[str]) -> Dict[str, float]:
    results = {}
    batch_size = 150

    def _yf_batch(batch: List[str]) -> Dict[str, float]:
        try:
            tickers = [f"{s}.NS" for s in batch]
            data = yf.download(tickers, period="2d", progress=False,
                               auto_adjust=True, threads=False)
            if data is None or data.empty:
                return {}
            close = data["Close"] if "Close" in data.columns else None
            if close is None:
                return {}
            last = close.iloc[-1]
            out = {}
            if isinstance(last, pd.Series):
                for ticker, price in last.items():
                    if not pd.isna(price):
                        sym = str(ticker).replace(".NS", "").strip()
                        out[sym] = float(price)
            elif len(batch) == 1 and not pd.isna(last):
                out[batch[0]] = float(last)
            return out
        except Exception as e:
            log.warning(f"CMP yfinance batch error: {e}")
            return {}

    loop = asyncio.get_event_loop()
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        res = await loop.run_in_executor(None, _yf_batch, batch)
        results.update(res)
        await asyncio.sleep(1.5)
    return results

async def _fetch_intraday_yahoo(symbol: str, range_str: str = "1d") -> List[dict]:
    ticker = _yahoo_ticker(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?interval=5m&range={range_str}"
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        chart = data.get("chart", {}).get("result", [])
        if not chart: return []
        result = chart[0]
        timestamps = result.get("timestamp", [])
        indicators = result.get("indicators", {}).get("quote", [{}])[0]
        opens, highs, lows, closes, volumes = (indicators.get(k, []) for k in ("open","high","low","close","volume"))
        candles = []
        for j, ts in enumerate(timestamps):
            c_val = closes[j] if j < len(closes) else None
            if c_val is None: continue
            dt = datetime.utcfromtimestamp(ts) + timedelta(hours=5, minutes=30)
            candles.append({"symbol": symbol, "ts": dt,
                "open": opens[j] if j < len(opens) else None,
                "high": highs[j] if j < len(highs) else None,
                "low":  lows[j]  if j < len(lows)  else None,
                "close": c_val,
                "volume": volumes[j] if j < len(volumes) else None})
        return candles
    except Exception as e:
        log.warning(f"intraday fetch {symbol} range={range_str}: {e}")
        return []

def _upsert_cmp(cmp_map):
    if not cmp_map: return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            for sym, price in cmp_map.items():
                cur.execute("INSERT INTO cmp_prices (symbol, cmp, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (symbol) DO UPDATE SET cmp = EXCLUDED.cmp, updated_at = NOW()", (sym, price))
            conn.commit()
        log.info(f"CMP upserted: {len(cmp_map)} symbols")
    except Exception as e:
        log.error(f"_upsert_cmp failed: {e}")

def _insert_intraday(candles):
    if not candles: return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            for c in candles:
                cur.execute("INSERT INTO intraday_prices (symbol, ts, open, high, low, close, volume) VALUES (%(symbol)s, %(ts)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s) ON CONFLICT (symbol, ts) DO NOTHING", c)
            conn.commit()
    except Exception as e:
        log.error(f"_insert_intraday failed: {e}")

def _purge_intraday_old():
    cutoff = _ist_now() - timedelta(days=15)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s", (cutoff,))
            conn.commit()
    except Exception as e:
        log.error(f"_purge_intraday_old failed: {e}")

_intraday_fetched_today: Optional[date] = None
_raw_prices_updated_today: Optional[date] = None

async def _task_refresh_cmp():
    futures_set = set(_get_futures_symbols())
    all_symbols = _get_all_gvm_symbols()
    non_futures = [s for s in all_symbols if s not in futures_set]
    if not non_futures: return
    cmp_map = await _fetch_cmp_yahoo(non_futures)
    _upsert_cmp(cmp_map)

async def _task_fetch_intraday():
    global _intraday_fetched_today
    today = _ist_now().date()
    if _intraday_fetched_today == today: return
    futures = _get_futures_symbols()
    if not futures: return
    total = 0
    for sym in futures:
        candles = await _fetch_intraday_yahoo(sym, range_str="1d")
        if candles:
            _insert_intraday(candles)
            total += len(candles)
        await asyncio.sleep(0.2)
    _purge_intraday_old()
    _intraday_fetched_today = today

async def _task_update_raw_prices():
    global _raw_prices_updated_today
    today = _ist_now().date()
    if _raw_prices_updated_today == today: return
    log.info("EOD: Starting raw_prices daily update")
    loop = asyncio.get_event_loop()
    try:
        import yahoo_daily_update as ydu
        await loop.run_in_executor(None, ydu.main)
        _raw_prices_updated_today = today
        log.info("EOD: raw_prices update complete")
    except Exception as e:
        log.error(f"raw_prices EOD update failed: {e}")

async def _scheduler():
    log.info("Scheduler started")
    while True:
        try:
            if _is_market_hours(): await _task_refresh_cmp()
            if _is_eod_window():
                await _task_fetch_intraday()
                await _task_update_raw_prices()
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        await asyncio.sleep(300)

@app.on_event("startup")
async def startup():
    create_tables()
    asyncio.create_task(_scheduler())
    log.info(f"Scorr API v{VERSION} started — DEPLOY_GUARD={DEPLOY_GUARD}")

@app.get("/")
def root():
    return {"service": "Scorr API", "version": VERSION, "status": "live"}

@app.get("/api/health")
def health():
    return {"status": "ok", "version": VERSION}

@app.get("/api/admin/env_check")
def env_check(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    keys = sorted(os.environ.keys())
    interesting = ["SCREENER_EMAIL", "SCREENER_PASSWORD", "GITHUB_TOKEN", "GITHUB_REPO",
                   "ADMIN_TOKEN", "DATABASE_URL", "DEPLOY_GUARD", "RAILWAY_PUBLIC_DOMAIN"]
    return {"version": VERSION, "all_keys_count": len(keys),
            "interesting": {k: {"present": k in os.environ, "len": len(os.environ.get(k, ""))} for k in interesting}}

@app.get("/api/health/feeds")
def health_feeds():
    out = []
    queries = [
        ("gvm_scores", "SELECT MAX(score_date), COUNT(*) FROM gvm_scores"),
        ("raw_prices", "SELECT MAX(price_date), COUNT(DISTINCT symbol) FROM raw_prices"),
        ("screener_raw", "SELECT NULL, COUNT(*) FROM screener_raw"),
        ("input_raw", "SELECT NULL, COUNT(*) FROM input_raw"),
        ("sector_ratings", "SELECT MAX(score_date), COUNT(*) FROM sector_ratings"),
        ("momentum_scores", "SELECT MAX(score_date), COUNT(*) FROM momentum_scores"),
        ("earnings_calendar", "SELECT MAX(loaded_at)::date, COUNT(*) FROM earnings_calendar"),
        ("intraday_prices", "SELECT MAX(ts)::date, COUNT(DISTINCT symbol) FROM intraday_prices"),
        ("cmp_prices", "SELECT MAX(updated_at)::date, COUNT(*) FROM cmp_prices"),
        ("v5_metrics", "SELECT MAX(score_date), COUNT(DISTINCT symbol) FROM v5_metrics"),
        ("v5_qualified", "SELECT MAX(score_date), COUNT(*) FROM v5_qualified"),
        ("v6_qualified", "SELECT MAX(score_date), COUNT(*) FROM v6_qualified"),
        ("v6_backtest_metrics", "SELECT MAX(score_date), COUNT(*) FROM v6_backtest_metrics"),
        ("v6_backtest_results", "SELECT MAX(created_at)::date, COUNT(DISTINCT run_id) FROM v6_backtest_results"),
    ]
    try:
        with get_conn() as conn, conn.cursor() as cur:
            for name, q in queries:
                cur.execute(q)
                r = cur.fetchone()
                latest = str(r[0]) if r[0] else None
                count = r[1] or 0
                days_old = None
                freshness = "n/a"
                if latest and r[0]:
                    try:
                        days_old = (date.today() - r[0]).days
                        freshness = "ok" if days_old < 7 else "stale"
                    except Exception: pass
                out.append({"source": name, "latest": latest, "records": count, "freshness": freshness, "days_old": days_old})
    except Exception as e:
        return {"error": str(e)}
    return {"checked_at": str(date.today()), "feeds": out}

def api_query(sql, params=None, single=False):
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description] if cur.description else []
            if single:
                r = cur.fetchone()
                return dict(zip(cols, r)) if r else None
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"api_query error: {e}")
        return {"error": str(e)}

@app.get("/api/gvm/{symbol}")
def get_gvm(symbol: str):
    r = api_query("SELECT symbol, company_name, segment, price, g_score, v_score, m_score, gvm_score, verdict, punchline, market_cap FROM gvm_scores WHERE symbol = %s", (symbol.upper(),), single=True)
    if not r: raise HTTPException(404, f"{symbol} not found")
    return r

@app.get("/api/gvm/top/{n}")
def get_top(n: int, verdict: Optional[str] = None):
    n = min(max(n, 1), 100)
    if verdict:
        return api_query("SELECT symbol, company_name, segment, g_score, v_score, m_score, gvm_score, verdict, market_cap FROM gvm_scores WHERE verdict = %s ORDER BY gvm_score DESC LIMIT %s", (verdict, n))
    return api_query("SELECT symbol, company_name, segment, g_score, v_score, m_score, gvm_score, verdict, market_cap FROM gvm_scores ORDER BY gvm_score DESC LIMIT %s", (n,))

@app.get("/api/sector/{sector}")
def get_sector(sector: str):
    return api_query("SELECT symbol, company_name, g_score, v_score, m_score, gvm_score, verdict, market_cap FROM gvm_scores WHERE LOWER(segment) = LOWER(%s) ORDER BY gvm_score DESC", (sector,))

@app.get("/api/filter")
def get_filter(min_gvm: float = 0, max_gvm: float = 10):
    return api_query("SELECT symbol, company_name, segment, g_score, v_score, m_score, gvm_score, verdict, market_cap FROM gvm_scores WHERE gvm_score >= %s AND gvm_score <= %s ORDER BY gvm_score DESC", (min_gvm, max_gvm))

@app.get("/api/sectors")
def get_sectors():
    return api_query("SELECT segment, simple_avg_gvm AS avg_gvm, mcap_weighted_gvm, stocks_count AS stock_count, verdict, top_stock, top_stock_gvm FROM sector_ratings ORDER BY mcap_weighted_gvm DESC")

@app.get("/api/momentum/{symbol}")
def get_momentum(symbol: str):
    r = api_query("SELECT * FROM momentum_scores WHERE symbol = %s", (symbol.upper(),), single=True)
    if not r: raise HTTPException(404, f"{symbol} momentum not found")
    return r

@app.get("/api/intraday/{symbol}")
def get_intraday(symbol: str, days: int = 1):
    days = min(max(days, 1), 15)
    cutoff = _ist_now() - timedelta(days=days)
    return api_query("SELECT symbol, ts, open, high, low, close, volume FROM intraday_prices WHERE symbol = %s AND ts >= %s ORDER BY ts ASC", (symbol.upper(), cutoff))

@app.get("/api/cmp/{symbol}")
def get_cmp(symbol: str):
    r = api_query("SELECT symbol, cmp, updated_at FROM cmp_prices WHERE symbol = %s", (symbol.upper(),), single=True)
    if not r: raise HTTPException(404, f"{symbol} CMP not found")
    return r

@app.get("/api/cmp")
def get_all_cmp():
    return api_query("SELECT symbol, cmp, updated_at FROM cmp_prices ORDER BY symbol")

# V5 ENGINE
@app.post("/api/v5/run")
async def v5_run(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with get_conn() as conn:
        results = run_v5_engine(conn)
    return results

@app.get("/api/v5/qualified")
def v5_qualified(signal_type: Optional[str] = None, score_date: Optional[str] = None):
    if not score_date: score_date = str(date.today())
    if signal_type:
        return api_query("SELECT symbol, signal_type, gvm_score, cmp, metrics, qualified_at FROM v5_qualified WHERE signal_type = %s AND score_date = %s ORDER BY gvm_score DESC", (signal_type, score_date))
    return api_query("SELECT symbol, signal_type, gvm_score, cmp, metrics, qualified_at FROM v5_qualified WHERE score_date = %s ORDER BY signal_type, gvm_score DESC", (score_date,))

@app.get("/api/v5/metrics/{symbol}")
def v5_metrics_single(symbol: str, score_date: Optional[str] = None):
    if not score_date: score_date = str(date.today())
    r = api_query("SELECT * FROM v5_metrics WHERE symbol = %s AND score_date = %s", (symbol.upper(), score_date), single=True)
    if not r: raise HTTPException(404, f"No metrics for {symbol} on {score_date}")
    return r

@app.get("/api/v5/metrics/all")
def v5_metrics_all():
    return api_query("""
        SELECT symbol, score_date, gvm_score, dma_50, dma_200, dma_20,
               rsi_month, rsi_weekly, daily_rsi,
               month_return, week_return, year_return, prev_day_change,
               sector_day, sector_week, month_index, week_index_52,
               range_1d, range_3d, upper_bb, lower_bb
        FROM v5_metrics
        WHERE score_date = (SELECT MAX(score_date) FROM v5_metrics)
        ORDER BY symbol
    """)

@app.get("/api/v5/live_metrics")
def v5_live_metrics():
    return api_query("""
        SELECT s.symbol,
            lc.close AS cmp,
            fc.open AS day_open,
            CASE WHEN fc.open > 0 THEN ROUND(((lc.close / fc.open - 1) * 100)::numeric, 2) END AS day_pct,
            hc.close AS hour_ago_close,
            CASE WHEN hc.close > 0 THEN ROUND(((lc.close / hc.close - 1) * 100)::numeric, 2) END AS hourly_pct
        FROM (SELECT DISTINCT symbol FROM v5_signals) s
        JOIN LATERAL (
            SELECT close FROM intraday_prices WHERE symbol = s.symbol
            AND ts::date = CURRENT_DATE ORDER BY ts DESC LIMIT 1
        ) lc ON true
        JOIN LATERAL (
            SELECT open FROM intraday_prices WHERE symbol = s.symbol
            AND ts::date = CURRENT_DATE ORDER BY ts ASC LIMIT 1
        ) fc ON true
        LEFT JOIN LATERAL (
            SELECT close FROM intraday_prices WHERE symbol = s.symbol
            AND ts >= NOW() - INTERVAL '65 minutes' ORDER BY ts ASC LIMIT 1
        ) hc ON true
        ORDER BY s.symbol
    """)

@app.get("/api/v5/filters")
def v5_filters():
    return api_query("SELECT signal_type, metric, min_val, max_val, updated_at FROM v5_filters ORDER BY signal_type, metric")

@app.post("/api/v5/filter/update")
async def v5_filter_update(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    body = await req.json()
    sig = body.get("signal_type"); metric = body.get("metric")
    mn = body.get("min_val"); mx = body.get("max_val")
    if not sig or not metric: raise HTTPException(400, "signal_type and metric required")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO v5_filters (signal_type, metric, min_val, max_val) VALUES (%s, %s, %s, %s) ON CONFLICT (signal_type, metric) DO UPDATE SET min_val = EXCLUDED.min_val, max_val = EXCLUDED.max_val, updated_at = NOW()", (sig, metric, mn, mx))
        conn.commit()
    return {"status": "ok", "signal_type": sig, "metric": metric, "min_val": mn, "max_val": mx}

# V6 SHADOW ENGINE
@app.post("/api/v6/run")
async def v6_run(x_admin_token: Optional[str] = Header(None), recompute: bool = False):
    _check_admin(x_admin_token)
    with get_conn() as conn:
        return run_v6_engine(conn, recompute=recompute)

@app.get("/api/v6/qualified")
def v6_qualified_get(signal_type: Optional[str] = None, score_date: Optional[str] = None):
    if not score_date: score_date = str(date.today())
    if signal_type:
        return api_query("SELECT symbol, signal_type, gvm_score, cmp, metrics, qualified_at FROM v6_qualified WHERE signal_type = %s AND score_date = %s ORDER BY gvm_score DESC", (signal_type, score_date))
    return api_query("SELECT symbol, signal_type, gvm_score, cmp, metrics, qualified_at FROM v6_qualified WHERE score_date = %s ORDER BY signal_type, gvm_score DESC", (score_date,))

@app.get("/api/v6/filters")
def v6_filters_get():
    return api_query("SELECT signal_type, metric, min_val, max_val, updated_at FROM v6_filters ORDER BY signal_type, metric")

@app.get("/api/v6/compare")
def v6_compare(score_date: Optional[str] = None):
    target = date.fromisoformat(score_date) if score_date else date.today()
    with get_conn() as conn:
        return compare_v5_v6(conn, target)

@app.post("/api/v6/backtest/run")
async def v6_backtest_run(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with get_conn() as conn:
        results = run_full_optimization(conn)
    return results

@app.get("/api/v6/backtest/results")
def v6_backtest_results(run_id: Optional[str] = None, signal_type: Optional[str] = None):
    if run_id and signal_type:
        return api_query("SELECT * FROM v6_backtest_results WHERE run_id = %s AND signal_type = %s ORDER BY composite_score DESC", (run_id, signal_type))
    if run_id:
        return api_query("SELECT signal_type, variant_label, num_signals, win_rate, avg_return, composite_score, filter_config FROM v6_backtest_results WHERE run_id = %s ORDER BY signal_type, composite_score DESC", (run_id,))
    return api_query("SELECT run_id, MAX(created_at) as latest, COUNT(*) as rows FROM v6_backtest_results GROUP BY run_id ORDER BY latest DESC LIMIT 10")

# ADMIN — DATA FEEDS
@app.post("/api/admin/backfill_intraday")
async def backfill_intraday(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    futures = _get_futures_symbols()
    if not futures: return {"status": "warn", "message": "No futures symbols"}
    total_candles, failed = 0, []
    for sym in futures:
        candles = await _fetch_intraday_yahoo(sym, range_str="15d")
        if candles:
            _insert_intraday(candles)
            total_candles += len(candles)
        else:
            failed.append(sym)
        await asyncio.sleep(0.25)
    _purge_intraday_old()
    return {"status": "ok", "symbols_attempted": len(futures), "symbols_failed": len(failed), "failed_symbols": failed[:20], "total_candles": total_candles}

@app.post("/api/admin/fetch_intraday_now")
async def fetch_intraday_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    global _intraday_fetched_today
    _intraday_fetched_today = None
    futures = _get_futures_symbols()
    if not futures: return {"status": "warn", "message": "No futures symbols"}
    total = 0
    for sym in futures:
        candles = await _fetch_intraday_yahoo(sym, range_str="1d")
        if candles:
            _insert_intraday(candles)
            total += len(candles)
        await asyncio.sleep(0.2)
    _purge_intraday_old()
    _intraday_fetched_today = _ist_now().date()
    return {"status": "ok", "symbols": len(futures), "candles": total}

@app.post("/api/admin/refresh_cmp_now")
async def refresh_cmp_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    futures_set = set(_get_futures_symbols())
    all_symbols = _get_all_gvm_symbols()
    non_futures = [s for s in all_symbols if s not in futures_set]
    if not non_futures: return {"status": "warn", "message": "No non-futures symbols"}
    cmp_map = await _fetch_cmp_yahoo(non_futures)
    _upsert_cmp(cmp_map)
    return {"status": "ok", "symbols_fetched": len(cmp_map), "symbols_expected": len(non_futures)}

@app.post("/api/admin/run_yahoo_daily")
async def run_yahoo_daily_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    global _raw_prices_updated_today
    _raw_prices_updated_today = None
    loop = asyncio.get_event_loop()
    try:
        import yahoo_daily_update as ydu
        await loop.run_in_executor(None, ydu.main)
        _raw_prices_updated_today = _ist_now().date()
        return {"status": "ok", "message": "raw_prices updated"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

async def _drive_download(file_id):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text

@app.post("/api/admin/load_input_from_drive")
async def load_input(req: Request):
    body = await req.json()
    file_id = body.get("file_id")
    if not file_id: raise HTTPException(400, "file_id required")
    csv_text = await _drive_download(file_id)
    df = pd.read_csv(io.StringIO(csv_text))
    rows = df.to_dict(orient="records")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM input_raw")
        for row in rows:
            cur.execute("INSERT INTO input_raw (data) VALUES (%s::jsonb)", (json.dumps(row, default=str),))
        conn.commit()
    return {"status": "ok", "rows": len(rows)}

@app.post("/api/admin/load_screener_from_drive")
async def load_screener(req: Request):
    body = await req.json()
    file_id = body.get("file_id")
    if not file_id: raise HTTPException(400, "file_id required")
    csv_text = await _drive_download(file_id)
    df = pd.read_csv(io.StringIO(csv_text))
    rows = df.to_dict(orient="records")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM screener_raw")
        for row in rows:
            cur.execute("INSERT INTO screener_raw (data) VALUES (%s::jsonb)", (json.dumps(row, default=str),))
        conn.commit()
    return {"status": "ok", "rows": len(rows)}

async def _parse_and_store_signals(csv_text: str, signal_type: str, replace: bool = True) -> dict:
    """Parse V5 alerts CSV and store into v5_signals. Shared by load_v5_signals_csv and load_v5_from_drive."""
    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception as e:
        return {"status": "error", "error": f"CSV parse failed: {e}"}

    df.columns = [str(c).strip() for c in df.columns]
    sym_col = next((c for c in df.columns if "symbol" in c.lower()), None)
    if not sym_col:
        return {"status": "error", "error": "No Symbol column found"}

    df = df[df[sym_col].notna() & (df[sym_col].astype(str).str.strip().str.len() > 0)]
    if df.empty:
        return {"status": "warn", "rows": 0}

    def _sf(v):
        try: return float(v) if pd.notna(v) else None
        except: return None
    def _si(v):
        try: return int(float(v)) if pd.notna(v) else None
        except: return None
    def _ss(v):
        s = str(v).strip() if pd.notna(v) else None
        return None if not s or s.lower() in ("nan", "none") else s
    def _sd(v):
        try:
            s = str(v).strip()
            if not s or s.lower() in ("nan", "none", ""): return None
            s = re.sub(r"\([^)]+\)", "", s).strip()
            dt = pd.to_datetime(s, errors="coerce")
            return dt.date() if pd.notna(dt) else None
        except: return None
    def _gc(*names):
        for n in names:
            match = next((c for c in df.columns if n.lower() in c.lower()), None)
            if match: return match
        return None

    inserted, skipped = 0, 0
    with get_conn() as conn, conn.cursor() as cur:
        if replace:
            cur.execute("DELETE FROM v5_signals WHERE signal_type = %s", (signal_type,))
            conn.commit()
        for _, row in df.iterrows():
            sym = _ss(row.get(sym_col))
            if not sym: continue
            try:
                cur.execute("""
                    INSERT INTO v5_signals
                    (signal_type, timestamp, symbol, finkhoz_rating, record_price, cap_type,
                     current_price, return_pct, hit_alert, analyst_verdict, alert_count,
                     alert_types, event_date, event_type, verdict_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    signal_type,
                    _ss(row.get(_gc("timestamp", "time"))),
                    sym,
                    _sf(row.get(_gc("finkhoz", "rating"))),
                    _sf(row.get(_gc("record price", "record_price"))),
                    _ss(row.get(_gc("type", "cap"))),
                    _sf(row.get(_gc("current price", "current_price"))),
                    _sf(row.get(_gc("return"))),
                    _ss(row.get(_gc("hit alert", "hit_alert"))),
                    _ss(row.get(_gc("analyst verdict", "verdict"))),
                    _si(row.get(_gc("alert count", "count"))),
                    _ss(row.get(_gc("alert type", "alert_type", "alert_types"))),
                    _sd(row.get(_gc("^date$", "event_date", "date"))),
                    _ss(row.get(_gc("event type", "event_type"))),
                    _sd(row.get(_gc("verdict_date", "date of verdict"))),
                ))
                inserted += 1
            except Exception as e:
                skipped += 1
                log.warning(f"v5_signals row skip ({sym}): {e}")
        conn.commit()

    return {"status": "ok", "signal_type": signal_type, "rows_read": len(df), "rows_inserted": inserted, "rows_skipped": skipped}

@app.post("/api/admin/load_v5_signals_csv")
async def load_v5_signals_csv(req: Request, x_admin_token: Optional[str] = Header(None)):
    """Load v5_signals from a single Drive CSV. Body: {file_id, signal_type, replace}"""
    _check_admin(x_admin_token)
    body = await req.json()
    file_id = body.get("file_id")
    signal_type = body.get("signal_type", "Alert")
    replace = body.get("replace", False)
    if not file_id: raise HTTPException(400, "file_id required")
    csv_text = await _drive_download(file_id)
    result = await _parse_and_store_signals(csv_text, signal_type, replace)
    result["file_id"] = file_id
    return result

@app.post("/api/admin/load_v5_from_drive")
async def load_v5_from_drive(req: Request):
    """
    Original V5 Apps Script endpoint — called by exportV5Tabs() every 15 min.
    Routes each exported CSV filename to the correct Railway table.
    Body: {file_ids: {"v5_alerts_large.csv": "fileId1", "v5_portfolio.csv": "fileId2", ...}}
    """
    body = await req.json()
    file_ids = body.get("file_ids", {})
    results = {}

    ALERTS_MAP = {
        "v5_alerts_large.csv": "Alert_Large",
        "v5_alerts_mid.csv":   "Alert_Mid",
        "v5_alerts_small.csv": "Alert_Small",
    }

    for filename, file_id in file_ids.items():
        fn = filename.lower().strip()
        try:
            if fn in ALERTS_MAP:
                csv_text = await _drive_download(file_id)
                results[filename] = await _parse_and_store_signals(csv_text, ALERTS_MAP[fn], replace=True)

            elif "portfolio" in fn or "in_position" in fn:
                csv_text = await _drive_download(file_id)
                df = pd.read_csv(io.StringIO(csv_text))
                rows = df.to_dict(orient="records")
                with get_conn() as conn, conn.cursor() as cur:
                    if "portfolio" in fn:
                        cur.execute("DELETE FROM v5_portfolio")
                    for row in rows:
                        cur.execute("INSERT INTO v5_portfolio (data) VALUES (%s::jsonb)", (json.dumps(row, default=str),))
                    conn.commit()
                results[filename] = {"status": "ok", "rows": len(rows)}

            elif "trade" in fn:
                csv_text = await _drive_download(file_id)
                df = pd.read_csv(io.StringIO(csv_text))
                rows = df.to_dict(orient="records")
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute("DELETE FROM v5_trades")
                    for row in rows:
                        cur.execute("INSERT INTO v5_trades (data) VALUES (%s::jsonb)", (json.dumps(row, default=str),))
                    conn.commit()
                results[filename] = {"status": "ok", "rows": len(rows)}

            else:
                results[filename] = {"status": "skipped"}

        except Exception as e:
            results[filename] = {"status": "error", "error": str(e)[:100]}
            log.warning(f"load_v5_from_drive {filename}: {e}")

    loaded  = sum(1 for r in results.values() if r.get("status") == "ok")
    skipped = sum(1 for r in results.values() if r.get("status") == "skipped")
    errors  = sum(1 for r in results.values() if r.get("status") == "error")
    log.info(f"load_v5_from_drive: {loaded} loaded, {skipped} skipped, {errors} errors")
    return {"status": "ok", "files": len(file_ids), "loaded": loaded, "skipped": skipped, "errors": errors, "results": results}

SCREENER_BASE = "https://www.screener.in"
SCREENER_LOGIN_URL = f"{SCREENER_BASE}/login/"
SCREENER_UPCOMING_URL = f"{SCREENER_BASE}/upcoming-results/"

def _parse_screener_date(s):
    if not s: return None
    s = str(s).strip()
    if not s or s.lower() in ("nan", "none", "-", "n/a"): return None
    formats = ["%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b", "%d %B"]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=date.today().year)
                if dt.date() < date.today() - timedelta(days=30):
                    dt = dt.replace(year=date.today().year + 1)
            return dt.date()
        except ValueError: continue
    return None

async def _screener_login_session():
    email = os.getenv("SCREENER_EMAIL", "").strip()
    password = os.getenv("SCREENER_PASSWORD", "").strip()
    if not email or not password: raise HTTPException(500, "SCREENER creds missing")
    client = httpx.AsyncClient(follow_redirects=True, timeout=60,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "en-US,en;q=0.9"})
    r = await client.get(SCREENER_LOGIN_URL)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if not csrf_input:
        await client.aclose()
        raise HTTPException(500, "CSRF token not found")
    r = await client.post(SCREENER_LOGIN_URL, data={"csrfmiddlewaretoken": csrf_input.get("value"), "username": email, "password": password, "next": ""}, headers={"Referer": SCREENER_LOGIN_URL})
    if "sessionid" not in client.cookies:
        await client.aclose()
        raise HTTPException(401, "Screener login failed")
    return client

async def _scrape_upcoming_results(client):
    r = await client.get(SCREENER_UPCOMING_URL)
    r.raise_for_status()
    html = r.text
    if "/login/" in str(r.url) or "Login to your account" in html:
        raise HTTPException(401, "Screener session expired")
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables: raise HTTPException(500, "No table found")
    rows_out = []
    seen_tickers = set()
    for tbl in tables:
        try: dfs = pd.read_html(io.StringIO(str(tbl)))
        except Exception: continue
        for df in dfs:
            if df.empty or len(df.columns) < 2: continue
            cols_lower = {str(c).strip().lower(): c for c in df.columns}
            name_col = date_col = event_col = None
            for key, orig in cols_lower.items():
                if name_col is None and ("name" in key or "company" in key): name_col = orig
                if date_col is None and ("date" in key or "result" in key): date_col = orig
                if event_col is None and ("type" in key or "purpose" in key): event_col = orig
            if name_col is None or date_col is None: continue
            ticker_map = {}
            for tr in tbl.find_all("tr"):
                a = tr.find("a", href=re.compile(r"/company/[^/]+/"))
                if a:
                    m = re.search(r"/company/([^/]+)/", a.get("href", ""))
                    if m: ticker_map[a.get_text(strip=True)] = m.group(1)
            for _, row in df.iterrows():
                name = str(row[name_col]).strip()
                if not name or name.lower() in ("nan", "name", "company"): continue
                ticker = ticker_map.get(name, "") or re.sub(r"[^A-Z0-9&]", "", name.upper())[:20]
                if ticker in seen_tickers: continue
                seen_tickers.add(ticker)
                ex_date = _parse_screener_date(str(row[date_col]))
                event_type = str(row[event_col]).strip() if event_col else "Quarterly Result"
                rows_out.append({"company_name": name, "ticker": ticker, "ex_date": ex_date, "record_date": None, "event_type": event_type})
    return rows_out

@app.post("/api/admin/load_earnings_from_screener")
async def load_earnings_from_screener(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    client = await _screener_login_session()
    try: rows = await _scrape_upcoming_results(client)
    finally: await client.aclose()
    if not rows: return {"status": "warn", "rows_scraped": 0}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM earnings_calendar")
        inserted = 0
        for r in rows:
            try:
                cur.execute("INSERT INTO earnings_calendar (company_name, ticker, ex_date, record_date, event_type) VALUES (%(company_name)s, %(ticker)s, %(ex_date)s, %(record_date)s, %(event_type)s)", r)
                inserted += 1
            except Exception as e:
                log.warning(f"row skip: {e}")
        conn.commit()
    return {"status": "ok", "rows_scraped": len(rows), "rows_inserted": inserted}

GITHUB_API = "https://api.github.com"

def _gh_headers():
    if not GITHUB_TOKEN: raise HTTPException(500, "GITHUB_TOKEN not configured")
    return {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

def _check_admin(token):
    if not ADMIN_TOKEN: return True
    if token != ADMIN_TOKEN: raise HTTPException(403, "Invalid admin token")
    return True

def _check_deploy_guard():
    if not DEPLOY_GUARD: raise HTTPException(403, "DEPLOY_GUARD is off")

async def _gh_get_file(filepath):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=_gh_headers())
        if r.status_code == 404: return {"exists": False, "content": None, "sha": None, "size": 0}
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return {"exists": True, "content": content, "sha": data["sha"], "size": data["size"]}

async def _gh_put_file(filepath, new_content, commit_message, sha=None):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    payload = {"message": commit_message, "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"), "branch": "main"}
    if sha: payload["sha"] = sha
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.put(url, headers=_gh_headers(), json=payload)
        if r.status_code not in (200, 201):
            raise HTTPException(r.status_code, f"GitHub error: {r.text[:300]}")
        return r.json()

async def _gh_delete_file(filepath, commit_message, sha):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request("DELETE", url, headers=_gh_headers(), json={"message": commit_message, "sha": sha, "branch": "main"})
        if r.status_code != 200: raise HTTPException(r.status_code, f"GitHub delete error: {r.text[:300]}")
        return r.json()

async def _gh_list_tree(path_prefix=""):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path_prefix}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=_gh_headers())
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict): data = [data]
        return [{"name": x["name"], "path": x["path"], "type": x["type"], "size": x.get("size", 0)} for x in data]

@app.get("/api/admin/github_read")
async def github_read(filepath: str, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    if not GITHUB_REPO: raise HTTPException(500, "GITHUB_REPO not configured")
    info = await _gh_get_file(filepath)
    if not info["exists"]: raise HTTPException(404, f"File not found: {filepath}")
    return {"filepath": filepath, "size": info["size"], "sha": info["sha"], "content": info["content"], "lines": info["content"].count("\n") + 1}

@app.get("/api/admin/github_list")
async def github_list(path: str = "", x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    if not GITHUB_REPO: raise HTTPException(500, "GITHUB_REPO not configured")
    files = await _gh_list_tree(path)
    return {"path": path or "/", "items": files, "count": len(files)}

@app.post("/api/admin/github_push")
async def github_push(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    _check_deploy_guard()
    if not GITHUB_REPO: raise HTTPException(500, "GITHUB_REPO not configured")
    body = await req.json()
    filepath = body.get("filepath")
    new_content = body.get("new_content")
    commit_message = body.get("commit_message", f"chore: update {filepath}")
    create_if_missing = body.get("create_if_missing", True)
    if not filepath or new_content is None: raise HTTPException(400, "filepath and new_content required")
    existing = await _gh_get_file(filepath)
    if not existing["exists"] and not create_if_missing: raise HTTPException(404, f"File {filepath} does not exist")
    if existing["exists"] and existing["content"] == new_content:
        return {"status": "noop", "message": "Content identical", "filepath": filepath}
    sha = existing["sha"] if existing["exists"] else None
    result = await _gh_put_file(filepath, new_content, commit_message, sha)
    return {"status": "ok", "filepath": filepath, "action": "updated" if existing["exists"] else "created",
            "commit_sha": result.get("commit", {}).get("sha"), "commit_url": result.get("commit", {}).get("html_url"),
            "old_size": existing["size"], "new_size": len(new_content)}

@app.post("/api/admin/github_delete")
async def github_delete(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    _check_deploy_guard()
    if not GITHUB_REPO: raise HTTPException(500, "GITHUB_REPO not configured")
    body = await req.json()
    filepath = body.get("filepath")
    commit_message = body.get("commit_message", f"chore: delete {filepath}")
    if not filepath: raise HTTPException(400, "filepath required")
    existing = await _gh_get_file(filepath)
    if not existing["exists"]: raise HTTPException(404, f"File not found: {filepath}")
    result = await _gh_delete_file(filepath, commit_message, existing["sha"])
    return {"status": "ok", "filepath": filepath, "action": "deleted", "commit_sha": result.get("commit", {}).get("sha")}

_oauth_codes = {}
_oauth_tokens = {}

@app.get("/.well-known/oauth-authorization-server")
def oauth_metadata():
    return {"issuer": BASE_URL, "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
            "token_endpoint": f"{BASE_URL}/oauth/token", "registration_endpoint": f"{BASE_URL}/oauth/register",
            "scopes_supported": ["read", "write"], "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"], "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"]}

@app.get("/.well-known/oauth-protected-resource")
def oauth_resource():
    return {"resource": BASE_URL, "authorization_servers": [BASE_URL], "scopes_supported": ["read", "write"]}

@app.post("/oauth/register")
async def oauth_register(req: Request):
    body = await req.json()
    cid = secrets.token_urlsafe(16)
    return {"client_id": cid, "client_id_issued_at": int(time.time()), "redirect_uris": body.get("redirect_uris", []),
            "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"], "response_types": ["code"]}

@app.get("/oauth/authorize")
def oauth_authorize(client_id: str, redirect_uri: str, response_type: str = "code",
                    state: str = "", code_challenge: str = "", code_challenge_method: str = "", scope: str = ""):
    code = secrets.token_urlsafe(24)
    _oauth_codes[code] = {"client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": code_challenge, "created": time.time()}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}")

@app.post("/oauth/token")
async def oauth_token(req: Request):
    form = await req.form()
    code = form.get("code")
    if code not in _oauth_codes: raise HTTPException(400, "Invalid code")
    info = _oauth_codes.pop(code)
    token = secrets.token_urlsafe(32)
    _oauth_tokens[token] = {"client_id": info["client_id"], "created": time.time()}
    return {"access_token": token, "token_type": "Bearer", "expires_in": 31536000, "scope": "read write"}

MCP_TOOLS = [
    {"name": "get_gvm", "description": "Fetch full GVM score for a stock.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_top_stocks", "description": "Get top N stocks by GVM.",
     "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}, "verdict": {"type": "string"}}, "required": ["n"]}},
    {"name": "get_sector", "description": "Get all stocks in a sector ordered by GVM.",
     "inputSchema": {"type": "object", "properties": {"sector": {"type": "string"}}, "required": ["sector"]}},
    {"name": "get_filter", "description": "Filter stocks by GVM range.",
     "inputSchema": {"type": "object", "properties": {"min_gvm": {"type": "number"}, "max_gvm": {"type": "number"}}, "required": []}},
    {"name": "get_sector_rating", "description": "Get sector-level mcap-weighted GVM ratings.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_momentum", "description": "Get momentum scores for a stock.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_intraday", "description": "Get 5-min OHLC candles for a futures stock.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}, "days": {"type": "integer"}}, "required": ["symbol"]}},
    {"name": "get_cmp", "description": "Get latest CMP for a non-futures stock.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "backfill_intraday", "description": "Fetch 15 days of 5-min OHLC for all 290 futures stocks.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "run_yahoo_daily", "description": "Trigger Yahoo daily OHLC update for raw_prices (last 7 days, 1,720 symbols). Takes 10-20 min.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "run_v5_engine", "description": "Run V5 filter engine: compute metrics + AND-gate for all 290 futures stocks.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_v5_qualified", "description": "Get stocks qualified by V5 AND-gate today.",
     "inputSchema": {"type": "object", "properties": {"signal_type": {"type": "string"}}, "required": []}},
    {"name": "get_v5_metrics", "description": "Get computed V5 metrics for one stock.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_v5_metrics_all", "description": "Get all 19 static metrics for all 290 stocks (latest date).",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_v5_live_metrics", "description": "Get real-time CMP, day%, and hourly gain for all 290 futures. Single call for 1-min refresh.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_v5_filters", "description": "Get current V5 filter thresholds.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "run_v6_backtest", "description": "Run V6 backtest optimizer.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_v6_backtest_results", "description": "Get V6 backtest results.",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "signal_type": {"type": "string"}}, "required": []}},
    {"name": "run_v6_engine", "description": "Run V6 filter engine: backtest-optimized AND-gate against v6_filters.",
     "inputSchema": {"type": "object", "properties": {"recompute": {"type": "boolean"}}, "required": []}},
    {"name": "get_v6_qualified", "description": "Get stocks qualified by V6 (optimized) AND-gate today.",
     "inputSchema": {"type": "object", "properties": {"signal_type": {"type": "string"}, "score_date": {"type": "string"}}, "required": []}},
    {"name": "get_v6_filters", "description": "Get current V6 filter thresholds (backtest-optimized).",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "compare_v5_v6", "description": "Paper-trade comparison: V5 vs V6 qualified stocks per signal type.",
     "inputSchema": {"type": "object", "properties": {"score_date": {"type": "string"}}, "required": []}},
    {"name": "health_feeds", "description": "Status dashboard for all data feeds.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "env_check", "description": "Diagnostic: which env vars are visible.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "run_sql", "description": "Run any SQL query on Railway PostgreSQL.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "load_input_from_drive", "description": "Reload input_raw table from a Google Drive CSV file ID.",
     "inputSchema": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}},
    {"name": "load_screener_from_drive", "description": "Reload screener_raw table from a Google Drive CSV file ID.",
     "inputSchema": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}},
    {"name": "load_v5_signals_csv", "description": "Load v5_signals from a single Drive CSV. Body: file_id, signal_type, replace.",
     "inputSchema": {"type": "object", "properties": {"file_id": {"type": "string"}, "signal_type": {"type": "string"}, "replace": {"type": "boolean"}}, "required": ["file_id"]}},
    {"name": "load_earnings_from_screener", "description": "Scrape Screener.in and refresh earnings_calendar.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_v5_signals", "description": "Query V5 signals.",
     "inputSchema": {"type": "object", "properties": {"signal_type": {"type": "string"}, "cap_type": {"type": "string"}, "verdict": {"type": "string"}, "limit": {"type": "integer"}}, "required": []}},
    {"name": "check_blackout", "description": "Check if a symbol is in earnings blackout.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_v5_portfolio", "description": "Get current V5 portfolio holdings.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "github_read", "description": "Read any file from the GitHub repo.",
     "inputSchema": {"type": "object", "properties": {"filepath": {"type": "string"}}, "required": ["filepath"]}},
    {"name": "github_list", "description": "List files in the repo.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}},
    {"name": "github_push", "description": "Create or update a file.",
     "inputSchema": {"type": "object", "properties": {"filepath": {"type": "string"}, "new_content": {"type": "string"}, "commit_message": {"type": "string"}, "create_if_missing": {"type": "boolean"}}, "required": ["filepath", "new_content", "commit_message"]}},
    {"name": "github_delete", "description": "Delete a file.",
     "inputSchema": {"type": "object", "properties": {"filepath": {"type": "string"}, "commit_message": {"type": "string"}}, "required": ["filepath"]}},
]

async def _call_tool(name, args):
    async with httpx.AsyncClient(timeout=600) as client:
        if name == "get_gvm":
            r = await client.get(f"{BASE_URL}/api/gvm/{args['symbol']}"); return r.json()
        elif name == "get_top_stocks":
            params = {}
            if args.get("verdict"): params["verdict"] = args["verdict"]
            r = await client.get(f"{BASE_URL}/api/gvm/top/{args['n']}", params=params); return r.json()
        elif name == "get_sector":
            r = await client.get(f"{BASE_URL}/api/sector/{args['sector']}"); return r.json()
        elif name == "get_filter":
            r = await client.get(f"{BASE_URL}/api/filter", params={"min_gvm": args.get("min_gvm", 0), "max_gvm": args.get("max_gvm", 10)}); return r.json()
        elif name == "get_sector_rating":
            r = await client.get(f"{BASE_URL}/api/sectors"); return r.json()
        elif name == "get_momentum":
            r = await client.get(f"{BASE_URL}/api/momentum/{args['symbol']}"); return r.json()
        elif name == "get_intraday":
            r = await client.get(f"{BASE_URL}/api/intraday/{args['symbol']}", params={"days": args.get("days", 1)}); return r.json()
        elif name == "get_cmp":
            r = await client.get(f"{BASE_URL}/api/cmp/{args['symbol']}"); return r.json()
        elif name == "backfill_intraday":
            r = await client.post(f"{BASE_URL}/api/admin/backfill_intraday", headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "run_yahoo_daily":
            r = await client.post(f"{BASE_URL}/api/admin/run_yahoo_daily", headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "run_v5_engine":
            r = await client.post(f"{BASE_URL}/api/v5/run", headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "get_v5_qualified":
            params = {}
            if args.get("signal_type"): params["signal_type"] = args["signal_type"]
            r = await client.get(f"{BASE_URL}/api/v5/qualified", params=params); return r.json()
        elif name == "get_v5_metrics":
            r = await client.get(f"{BASE_URL}/api/v5/metrics/{args['symbol']}"); return r.json()
        elif name == "get_v5_metrics_all":
            r = await client.get(f"{BASE_URL}/api/v5/metrics/all"); return r.json()
        elif name == "get_v5_live_metrics":
            r = await client.get(f"{BASE_URL}/api/v5/live_metrics"); return r.json()
        elif name == "get_v5_filters":
            r = await client.get(f"{BASE_URL}/api/v5/filters"); return r.json()
        elif name == "run_v6_backtest":
            r = await client.post(f"{BASE_URL}/api/v6/backtest/run", headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "get_v6_backtest_results":
            params = {}
            if args.get("run_id"): params["run_id"] = args["run_id"]
            if args.get("signal_type"): params["signal_type"] = args["signal_type"]
            r = await client.get(f"{BASE_URL}/api/v6/backtest/results", params=params); return r.json()
        elif name == "run_v6_engine":
            params = {}
            if args.get("recompute"): params["recompute"] = "true"
            r = await client.post(f"{BASE_URL}/api/v6/run", params=params, headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "get_v6_qualified":
            params = {}
            if args.get("signal_type"): params["signal_type"] = args["signal_type"]
            if args.get("score_date"): params["score_date"] = args["score_date"]
            r = await client.get(f"{BASE_URL}/api/v6/qualified", params=params); return r.json()
        elif name == "get_v6_filters":
            r = await client.get(f"{BASE_URL}/api/v6/filters"); return r.json()
        elif name == "compare_v5_v6":
            params = {}
            if args.get("score_date"): params["score_date"] = args["score_date"]
            r = await client.get(f"{BASE_URL}/api/v6/compare", params=params); return r.json()
        elif name == "health_feeds":
            r = await client.get(f"{BASE_URL}/api/health/feeds"); return r.json()
        elif name == "env_check":
            r = await client.get(f"{BASE_URL}/api/admin/env_check", headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "run_sql":
            q = args["query"]
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(q)
                    if cur.description:
                        cols = [d[0] for d in cur.description]
                        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                        conn.commit()
                        return {"rows": rows, "count": len(rows)}
                    conn.commit()
                    return {"status": "ok", "rowcount": cur.rowcount}
            except Exception as e:
                return {"error": str(e)}
        elif name == "load_input_from_drive":
            r = await client.post(f"{BASE_URL}/api/admin/load_input_from_drive", json={"file_id": args["file_id"]}); return r.json()
        elif name == "load_screener_from_drive":
            r = await client.post(f"{BASE_URL}/api/admin/load_screener_from_drive", json={"file_id": args["file_id"]}); return r.json()
        elif name == "load_v5_signals_csv":
            r = await client.post(f"{BASE_URL}/api/admin/load_v5_signals_csv",
                json={"file_id": args["file_id"], "signal_type": args.get("signal_type","Alert"), "replace": args.get("replace", False)},
                headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "load_earnings_from_screener":
            r = await client.post(f"{BASE_URL}/api/admin/load_earnings_from_screener", headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "get_v5_signals":
            conds, vals = [], []
            if args.get("signal_type"): conds.append("signal_type = %s"); vals.append(args["signal_type"])
            if args.get("cap_type"): conds.append("cap_type = %s"); vals.append(args["cap_type"])
            if args.get("verdict"): conds.append("analyst_verdict = %s"); vals.append(args["verdict"])
            where = " AND ".join(conds) if conds else "1=1"
            limit = int(args.get("limit", 50))
            q = f"SELECT signal_type, symbol, cap_type, analyst_verdict, finkhoz_rating, current_price, return_pct, alert_types FROM v5_signals WHERE {where} ORDER BY id DESC LIMIT {limit}"
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(q, vals)
                cols = [d[0] for d in cur.description]
                return {"rows": [dict(zip(cols, r)) for r in cur.fetchall()]}
        elif name == "check_blackout":
            sym = args["symbol"].upper()
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT ticker, ex_date, event_type FROM earnings_calendar WHERE UPPER(ticker) = %s ORDER BY id DESC LIMIT 5", (sym,))
                rows = cur.fetchall()
            return {"symbol": sym, "events": [{"ex_date": str(r[1]), "event_type": r[2]} for r in rows]}
        elif name == "get_v5_portfolio":
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT data FROM v5_portfolio ORDER BY id LIMIT 200")
                return {"rows": [r[0] for r in cur.fetchall()]}
        elif name == "github_read":
            r = await client.get(f"{BASE_URL}/api/admin/github_read", params={"filepath": args["filepath"]}, headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "github_list":
            r = await client.get(f"{BASE_URL}/api/admin/github_list", params={"path": args.get("path", "")}, headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "github_push":
            r = await client.post(f"{BASE_URL}/api/admin/github_push", json=args, headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        elif name == "github_delete":
            r = await client.post(f"{BASE_URL}/api/admin/github_delete", json=args, headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}); return r.json()
        return {"error": f"Unknown tool: {name}"}

@app.post("/mcp")
async def mcp_endpoint(req: Request):
    body = await req.json()
    method = body.get("method")
    params = body.get("params", {})
    msg_id = body.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "Scorr", "version": VERSION}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        try:
            result = await _call_tool(name, args)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(e)}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return Response(status_code=204)
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
