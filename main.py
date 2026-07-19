from fastapi import FastAPI, HTTPException, Request, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse, HTMLResponse
from pydantic import BaseModel
import os
import sys
# cc#416: worker-runtime modules moved to worker/ for deploy isolation (watch path worker/**). The app
# still imports a few of them (fyers_hist_backfill router; fundamentals_scraper / fyers_range_backfill
# lazy-import fyers_feed/fyers_backfill). Add worker/ to sys.path once here so every `import fyers_*`
# below and in downstream modules resolves; root-staying modules (fyers_backfill, nse_holidays) keep
# resolving via the repo root the app already runs from.
_WORKER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker")
if _WORKER_DIR not in sys.path:
    sys.path.append(_WORKER_DIR)
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
from gvm_report_endpoints import router as gvm_report_router
from gvm_market_endpoints import router as gvm_market_router
from gvm_universe_pivots import router as gvm_universe_pivots_router
from admin_data import router as admin_data_router
from fyers_endpoints import router as fyers_router
from diagnosis import router as diagnosis_router
from v9_endpoints import router as v9_router
from v10_endpoints import router as v10_router
from v14_endpoints import router as v14_router   # cc#442: V14 intraday engine
from bt6_endpoints import router as bt6_router   # cc#544: V6 BT playground (read-mostly wrapper)
from pcr_endpoints import router as pcr_router
from v8_replay_endpoints import router as v8_replay_router
from v8_intra_backtest_endpoints import router as backtest_router
from v8_backfill_endpoints import router as v8_backfill_router
from nse_holidays import is_trading_day, is_nse_holiday
from gvm_nightly import router as gvm_nightly_router, recompute_gvm, _sql_clean_replace_screener
from mcp_dispatch import router as mcp_router
from anthropic_endpoints import router as anthropic_router
from scorr_endpoints import router as scorr_router
from scorr_chat_endpoint import router as scorr_chat_router
from trade_check_v34_endpoints import router as trade_check_v34_router
from tc_v4_endpoints import router as tc_v4_router
from tc_v4_dual import router as tc_v4_dual_router   # cc#386: dual-style v4 engine (spec id=2926)
from tc_v4_scan import router as tc_v4_scan_router   # cc#387: dual-style v4 batch scanner
from check_endpoint import router as check_router
from sector_endpoints import router as sector_router
from sector_brief_endpoints import router as sector_brief_router, _batch_job as _sector_brief_batch
from ops_metrics_pipeline import router as ops_metrics_router   # cc#523: sector KPI registry + concall pipeline
from scheduler_master import router as scheduler_master_router   # cc#525: scheduled-job registry + drift audit
from scorr_auth import router as auth_router, _is_authed, PROTECTED
from scorr_authset_probe import router as authset_probe_router
from pwa_endpoints import router as pwa_router
from investment_check import router as investment_check_router
from scanner_endpoints import router as scanner_router
from intraday_scanner_endpoints import router as intraday_scanner_router  # cc#481: restored (cc#476 kill reversed)
from tc_scanner_endpoints import router as tc_scanner_router  # cc#464: TC Scanner (13-check binary engine, id=399/400)
from structure_endpoints import structure_router
from performance_endpoints import router as performance_router
from scheduler_health_endpoints import router as scheduler_health_router
from news_endpoints import router as news_router
from position_news_endpoints import router as position_news_router  # cc#207
from admin_index_backfill import router as idx_backfill_router
from feed_health_endpoints import router as feed_health_router
from v12_endpoints import router as v12_router
from v12_backtest import router as v12_backtest_router   # cc#394 V12 Basket Builder backtest walker
from test_cio_endpoints import router as test_cio_router
from fyers_range_backfill_endpoints import router as fyers_range_backfill_router
from smartgain_daily_m2m import router as smartgain_daily_m2m_router
from smartgain_reconcile import router as smartgain_reconcile_router
from stock_options_backfill import router as stock_options_backfill_router
from fyers_hist_backfill import router as fyers_hist_backfill_router   # cc#377 Phase B
from fundamentals_scraper import router as fundamentals_scraper_router   # cc#361 Phase 1 scrape
from v13_presets_endpoints import router as v13_presets_router
from mf_pipeline import router as mf_pipeline_router   # cc#466: V15 MF Intelligence data layer
from galaxy_endpoints import router as galaxy_router
from hr_endpoints import router as hr_router   # cc#398 Portfolio Health Report (M1 ingest)
from hr_report import router as hr_report_router   # cc#398 Portfolio Health Report (M2 report engine)
import yahoo_ondemand
import yahoo_index_backfill
import v8_paper
import global_indices
import v8_signal_writer
import qb_eod_checker
import refresh_takeaways as rt
import scheduler
from scheduler import _compute_and_store_adr, _compute_and_store_pcr

# ============================================================
# Scorr / Project Quant — main.py v2.9.60
# v2.9.60: v13_presets router (cc#182 saveable filter themes) + live_metrics as-of fallback.
# v2.9.59: PWA injection for /screener /intraday /structure /performance /ask (cc#176).
# v2.9.58: stock_options_backfill router (cc#175 weekend options data).
# v2.9.57: smartgain_daily_m2m router moved from scorr_endpoints nesting to explicit main.py wiring (cc#173).
# v2.9.56: GET /holdings route + SmartGain M2M page (cc_task #94).
# v2.9.55: Wire admin_index_backfill router — SENSEX/FINNIFTY/MIDCAPNIFTY backfill endpoint.
# v2.9.54: Added /quant-basket route (Quant Basket dashboard).
# v2.9.53: Removed intraday_router (intraday_endpoints.py + intraday_engine.py retired).
#   /api/intraday/* now served by trade_check_v34_router -> tci.intraday_dashboard().
# v2.9.52: intraday paper engine wired. v2.9.51: /fpc. v2.9.50: v8_backfill.
# ============================================================

VERSION = "2.9.66"

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

_LOGOUT_BTN = (
    # cc#433: sit BELOW the 46px sticky navbar (was top:12 -> overlapped the last nav tabs) +
    # semi-transparent idle (0.45) that goes solid on hover, so it never obstructs nav/content.
    b"<style>#scorr-lo{position:fixed;top:64px;right:14px;z-index:9999;opacity:.45;transition:opacity .15s;}"
    b"#scorr-lo:hover{opacity:1;}"
    b"#scorr-lo a{display:inline-flex;align-items:center;gap:5px;padding:5px 11px;"
    b"background:rgba(15,22,35,0.88);border:1px solid #2a3548;border-radius:7px;"
    b"color:#5a6781;font-size:10.5px;font-weight:600;text-decoration:none;"
    b"font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif;"
    b"backdrop-filter:blur(8px);transition:all .15s;}"
    b"#scorr-lo a:hover{color:#b45309!important;border-color:#b45309!important;}"
    # cc#363: light-theme override — dark pill was unreadable on the light header.
    b":root[data-theme=\"light\"] #scorr-lo a{background:rgba(255,255,255,.92);"
    b"border-color:rgba(20,35,80,.14);color:#5B6B94;}</style>"
    b'<div id="scorr-lo"><a href="/logout">&#x23CF; Logout</a></div>'
)

# cc#348: ONE global theme switch, fixed top-right just BELOW the logout pill (founder 09-Jul).
# Sets scorr_theme + reloads so EVERY page — CSS-var pages, the React GVM, and the older
# hardcoded pages — re-renders in the chosen theme (guaranteed consistency, no per-page drift).
_THEME_BTN = (
    b"<style>#scorr-th{position:fixed;top:102px;right:14px;z-index:9999;opacity:.45;transition:opacity .15s}"   # cc#433: below navbar + logout, semi-transparent idle
    b"#scorr-th:hover{opacity:1}"
    b"#scorr-th button{display:inline-flex;align-items:center;gap:5px;padding:5px 11px;"
    b"border-radius:7px;border:1px solid #2a3548;background:rgba(15,22,35,.88);color:#5a6781;"
    b"font-size:10.5px;font-weight:600;cursor:pointer;backdrop-filter:blur(8px);"
    b"font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif}"
    b"#scorr-th button:hover{color:#4D7CFE;border-color:#4D7CFE}"
    # cc#363: light-theme override — the dark pill on the light header was the "broken" look.
    b":root[data-theme=\"light\"] #scorr-th button{background:rgba(255,255,255,.92);"
    b"border-color:rgba(20,35,80,.14);color:#5B6B94}"
    b":root[data-theme=\"light\"] #scorr-th button:hover{color:#3D6BEC;border-color:#3D6BEC}</style>"
    b'<div id="scorr-th"><button id="scorr-th-b" type="button" title="Toggle light / dark"></button></div>'
    b"<script>(function(){var b=document.getElementById('scorr-th-b');if(!b)return;"
    b"function cur(){try{return localStorage.getItem('scorr_theme')||'light';}catch(e){return 'light';}}"
    b"b.innerHTML=cur()==='light'?'\\u2600 Light':'\\u263e Dark';"
    b"b.onclick=function(){var t=cur()==='light'?'dark':'light';"
    b"try{localStorage.setItem('scorr_theme',t);}catch(e){}location.reload();};})();</script>"
)

def _is_embedded(request: Request) -> bool:
    if request.query_params.get("embed") == "1":
        return True
    if request.headers.get("sec-fetch-dest", "").lower() == "iframe":
        return True
    return False

# cc#176: /screener /intraday /structure /performance /ask were missing -- those
# pages never got the PWA bootstrap (no mobile bottom-nav / manifest / SW).
_PWA_INJECT_PATHS = {"/app", "/cio", "/cio2", "/check", "/scanners", "/news", "/v10", "/v9", "/v14",
                     "/dashboard", "/sector", "/fpc", "/quant-basket", "/holdings", "/filters",
                     "/intraday", "/structure", "/performance", "/ask",
                     "/v13", "/v12", "/health", "/v15", "/scheduler-master"}   # cc#392/394/398/426/442/467/525: no-store + theme/logout pills
# cc#407: /screener retired -> 301 /v13 (V13 is the single screening surface). Not injected/protected.
PROTECTED.add("/v13"); PROTECTED.add("/v12"); PROTECTED.add("/health"); PROTECTED.add("/v9"); PROTECTED.add("/v14"); PROTECTED.add("/v15")   # cc#392/394/398/426/442/467: gate + no-store
PROTECTED.add("/scheduler-master")   # cc#525: gate + no-store
# cc#399: /v4scan retired as a page — now a 301 -> /check (TC v4 merged into Check). Not injected/protected.
_PWA_TAG = b'<script src="/pwa.js" defer></script>'

# cc#327 MOBILE_UX_REDEFINE_V1 P1/10: canonical Sora font + shared mobile.css,
# injected into <head> on every protected/app page via the same gate as the PWA
# bootstrap, so no page is missed and the design system is defined in ONE place.
_MOBILE_HEAD = (
    # cc#345/348: set the saved theme SYNCHRONOUSLY before first paint (no flash).
    # cc#348: DEFAULT is now LIGHT (founder 09-Jul) — no saved pick => light.
    b"<script>(function(){try{var t=localStorage.getItem('scorr_theme')||'light';"
    b"document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>"
    b'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    b'<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700&display=swap" rel="stylesheet">'
    b'<link rel="stylesheet" href="/static/mobile.css">'
    b'<script src="/mobile_tables.js" defer></script>'   # cc#330 P4: shared table helper
)

@app.middleware("http")
async def auth_gate(request: Request, call_next):
    if request.url.path in PROTECTED and not _is_authed(request):
        from fastapi.responses import RedirectResponse as _RR
        return _RR(url="/login")
    response = await call_next(request)
    path = request.url.path
    do_logout = path in PROTECTED
    do_pwa = path in _PWA_INJECT_PATHS
    if (do_logout or do_pwa) and "text/html" in response.headers.get("content-type", ""):
        is_embed = _is_embedded(request)
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        if not is_embed:
            if do_logout:
                body = body.replace(b"</body>", _LOGOUT_BTN + b"</body>", 1)
            if (do_logout or do_pwa) and b'id="scorr-th"' not in body:   # cc#348: global theme switch
                body = body.replace(b"</body>", _THEME_BTN + b"</body>", 1)
            if do_pwa and b'src="/pwa.js"' not in body:
                body = body.replace(b"</body>", _PWA_TAG + b"</body>", 1)
            # cc#327: shared mobile design system into <head> (fallback: before </body>)
            if b'href="/static/mobile.css"' not in body:
                if b"</head>" in body:
                    body = body.replace(b"</head>", _MOBILE_HEAD + b"</head>", 1)
                else:
                    body = body.replace(b"</body>", _MOBILE_HEAD + b"</body>", 1)
        headers = dict(response.headers)
        headers["content-length"] = str(len(body))
        headers["cache-control"] = "no-store, no-cache, must-revalidate"
        headers["pragma"] = "no-cache"
        return Response(content=body, status_code=response.status_code,
                        headers=headers, media_type="text/html")
    return response

app.include_router(auth_router)
app.include_router(authset_probe_router)
app.include_router(v8_router)
app.include_router(v8_futures_router)
app.include_router(qb_router)
app.include_router(gvm_nightly_router)
app.include_router(gvm_report_router)
app.include_router(gvm_market_router)
app.include_router(gvm_universe_pivots_router)
app.include_router(admin_data_router)
app.include_router(fyers_router)
app.include_router(diagnosis_router)
app.include_router(v9_router)
app.include_router(v10_router)
app.include_router(v14_router)   # cc#442
app.include_router(bt6_router)   # cc#544: /api/bt6/* V6 BT playground
app.include_router(pcr_router)
app.include_router(v8_replay_router)
app.include_router(backtest_router)
app.include_router(v8_backfill_router)
app.include_router(mcp_router)
app.include_router(anthropic_router)
app.include_router(scorr_router)
app.include_router(scorr_chat_router)
app.include_router(trade_check_v34_router)
app.include_router(tc_v4_router)
app.include_router(tc_v4_dual_router)   # cc#386
app.include_router(tc_v4_scan_router)   # cc#387
app.include_router(check_router)
app.include_router(sector_router)
app.include_router(sector_brief_router)
app.include_router(ops_metrics_router)   # cc#523
app.include_router(scheduler_master_router)   # cc#525
app.include_router(investment_check_router)
app.include_router(scanner_router)
app.include_router(intraday_scanner_router)   # cc#481: restored
app.include_router(tc_scanner_router)         # cc#464: TC Scanner
app.include_router(structure_router)
from deriv_metrics import deriv_router          # cc#346: DERIVATIVE COCKPIT data layer
app.include_router(deriv_router)
from nse_eod_ingest import nse_eod_router       # cc#517: NSE EOD ingest suite (delivery/FII-DII/participant OI/F&O ban)
app.include_router(nse_eod_router)
app.include_router(performance_router)
app.include_router(scheduler_health_router)
app.include_router(news_router)
app.include_router(position_news_router)  # cc#207: Position News quarantine tab
app.include_router(pwa_router)
app.include_router(idx_backfill_router)
app.include_router(feed_health_router)
app.include_router(v12_router)
app.include_router(v12_backtest_router)   # cc#394
app.include_router(test_cio_router)
app.include_router(fyers_range_backfill_router)
app.include_router(smartgain_daily_m2m_router)
app.include_router(smartgain_reconcile_router)
app.include_router(stock_options_backfill_router)
app.include_router(fyers_hist_backfill_router)   # cc#377 Phase B
app.include_router(fundamentals_scraper_router)   # cc#361 Phase 1 scrape
app.include_router(v13_presets_router)
app.include_router(mf_pipeline_router)   # cc#466: /api/v15/mf/*
app.include_router(galaxy_router)
app.include_router(hr_router)   # cc#398 Portfolio Health Report (ingest)
app.include_router(hr_report_router)   # cc#398 Portfolio Health Report (report engine)

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
        UNIQUE(symbol, ts, timeframe, source)
    );
    CREATE INDEX IF NOT EXISTS idx_intraday_symbol_ts ON intraday_prices(symbol, ts DESC);
    ALTER TABLE intraday_prices ADD COLUMN IF NOT EXISTS timeframe TEXT DEFAULT '1m';
    ALTER TABLE intraday_prices ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'fyers';
    CREATE TABLE IF NOT EXISTS cmp_prices (
        symbol TEXT PRIMARY KEY, cmp NUMERIC, updated_at TIMESTAMP DEFAULT NOW()
    );
    ALTER TABLE cmp_prices ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'fyers';
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
    -- cc#394 V12 Quant Basket Builder (P1): one basket definition JSONB, two executors (bt + paper).
    CREATE TABLE IF NOT EXISTS v12_universes (
        id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, definition JSONB NOT NULL,
        created_by TEXT, created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS v12_baskets (
        id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, definition JSONB NOT NULL,
        status TEXT DEFAULT 'draft', created_by TEXT,
        created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS v12_backtests (
        id BIGSERIAL PRIMARY KEY, basket_id BIGINT, params_hash TEXT UNIQUE,
        status TEXT DEFAULT 'pending', result JSONB, error TEXT,
        created_at TIMESTAMP DEFAULT NOW(), finished_at TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_v12_backtests_hash ON v12_backtests(params_hash);
    CREATE INDEX IF NOT EXISTS idx_v12_baskets_status ON v12_baskets(status);
    -- cc#398 Portfolio Health Report (spec id=2994): uploaded holdings -> Scorr-native report.
    CREATE TABLE IF NOT EXISTS hr_portfolios (
        id BIGSERIAL PRIMARY KEY, name TEXT, created_by TEXT, source TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS hr_holdings (
        id BIGSERIAL PRIMARY KEY, portfolio_id BIGINT REFERENCES hr_portfolios(id) ON DELETE CASCADE,
        symbol TEXT, company_name TEXT, qty NUMERIC, avg_price NUMERIC,
        resolved BOOLEAN DEFAULT FALSE, raw_input JSONB, created_at TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_hr_holdings_portfolio ON hr_holdings(portfolio_id);
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
    CREATE TABLE IF NOT EXISTS futures_basis (
        id            SERIAL PRIMARY KEY,
        symbol        TEXT      NOT NULL,
        ts            TIMESTAMP NOT NULL,
        spot_close    NUMERIC,
        futures_close NUMERIC,
        basis         NUMERIC,
        basis_pct     NUMERIC,
        UNIQUE(symbol, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_futures_basis_symbol_ts ON futures_basis(symbol, ts DESC);
    CREATE TABLE IF NOT EXISTS gvm_cache (
        symbol VARCHAR(10) PRIMARY KEY,
        gvm_score DECIMAL(5, 2),
        growth DECIMAL(5, 2),
        value DECIMAL(5, 2),
        momentum DECIMAL(5, 2),
        segment VARCHAR(50),
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS peer_averages (
        segment VARCHAR(50) PRIMARY KEY,
        avg_gvm DECIMAL(5, 2),
        avg_growth DECIMAL(5, 2),
        avg_value DECIMAL(5, 2),
        avg_momentum DECIMAL(5, 2),
        stock_count INT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS cache_metadata (
        key VARCHAR(50) PRIMARY KEY,
        last_sync TIMESTAMP,
        stock_count INT,
        status VARCHAR(20)
    );
    INSERT INTO cache_metadata (key, status)
    VALUES ('gvm_cache', 'pending_first_load')
    ON CONFLICT (key) DO NOTHING;
    CREATE TABLE IF NOT EXISTS sector_briefs (
        id SERIAL PRIMARY KEY,
        segment TEXT NOT NULL UNIQUE,
        what_is_it TEXT,
        growth_drivers JSONB,
        application_type TEXT,
        business_model TEXT,
        key_risks JSONB,
        generated_at TIMESTAMP DEFAULT NOW(),
        model TEXT DEFAULT 'claude-haiku-4-5-20251001'
    );
    """ + V8_SCHEMA_SQL
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql); conn.commit()
        log.info("Tables ready (v2.9.56)")
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

def _get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
            r = cur.fetchone(); return r[0] if r else default
    except Exception as e:
        log.error(f"_get_config {key} failed: {e}"); return default

def _yahoo_ticker(symbol: str) -> str:
    return {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK"}.get(symbol, f"{symbol}.NS")

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

def _insert_intraday(candles):
    if not candles: return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            for c in candles:
                cur.execute("INSERT INTO intraday_prices (symbol, ts, open, high, low, close, volume) VALUES (%(symbol)s, %(ts)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s) ON CONFLICT (symbol, ts, timeframe, source) DO NOTHING", c)
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

_BG_TASKS: set = set()

@app.on_event("startup")
async def startup():
    async def _init_tables():
        try: await asyncio.to_thread(create_tables)
        except Exception as e: log.error(f"create_tables (bg) failed: {e}")

    async def _auto_fill_briefs():
        await asyncio.sleep(15)
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM sector_briefs")
                cached = cur.fetchone()[0]
                cur.execute("SELECT COUNT(DISTINCT segment) FROM sector_ratings WHERE score_date=(SELECT MAX(score_date) FROM sector_ratings)")
                total = cur.fetchone()[0]
            if cached < total:
                log.info(f"[startup] sector_briefs: {cached}/{total} cached — launching batch generation")
                await _sector_brief_batch(refresh=False)
            else:
                log.info(f"[startup] sector_briefs: all {cached}/{total} cached — skipping")
        except Exception as e:
            log.error(f"[startup] sector_brief auto-fill failed: {e}")

    async def _v8_paper_rebuild_cutover():
        # cc#504 V8 SUITE REBUILD (18-Jul-2026): one-time flatten of every OPEN paper position at
        # live CMP (result='SUITE_REBUILD'), zero-schema-change era split via app_config -- see
        # v8_paper.rebuild_cutover() docstring. Idempotent (app_config key = the guard), so every
        # redeploy after the first successful run is a no-op single SELECT.
        await asyncio.sleep(20)
        try:
            with get_conn() as conn:
                result = await asyncio.to_thread(v8_paper.rebuild_cutover, conn)
            if result.get("already_done"):
                log.info(f"[startup] v8_paper_rebuild_cutover: already done at {result.get('cutover_ts')}")
            else:
                log.info(f"[startup] v8_paper_rebuild_cutover: flattened "
                         f"{len(result.get('flattened', []))} position(s) at {result.get('cutover_ts')}")
        except Exception as e:
            log.error(f"[startup] v8_paper_rebuild_cutover failed: {e}")

    t0 = asyncio.create_task(_init_tables())
    _BG_TASKS.add(t0); t0.add_done_callback(_BG_TASKS.discard)
    t1 = asyncio.create_task(_auto_fill_briefs())
    _BG_TASKS.add(t1); t1.add_done_callback(_BG_TASKS.discard)
    t2 = asyncio.create_task(_v8_paper_rebuild_cutover())
    _BG_TASKS.add(t2); t2.add_done_callback(_BG_TASKS.discard)
    scheduler.start_background(app, BASE_URL, ADMIN_TOKEN)
    log.info(f"Scorr API v{VERSION} started — DEPLOY_GUARD={DEPLOY_GUARD}")

@app.get("/", response_class=HTMLResponse)
def home():
    with open("scorr_home.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/status")
def status(): return {"service": "Scorr API", "version": VERSION, "status": "live"}

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    with open("v8_dashboard.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/cio", response_class=HTMLResponse)
def cio():
    with open("scorr_cockpit.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/cio2", response_class=HTMLResponse)
def cio2():
    with open("scorr_cio_dashboard.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/ask", response_class=HTMLResponse)
def ask():
    with open("scorr_ask.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/check", response_class=HTMLResponse)
def check():
    with open("scorr_check.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/intraday", response_class=HTMLResponse)   # cc#481: restored (cc#476 kill reversed)
def intraday():
    with open("scorr_intraday.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/sector", response_class=HTMLResponse)
def sector():
    with open("scorr_sector.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/fpc", response_class=HTMLResponse)
def fpc():
    with open("fpc_v11.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/scanners", response_class=HTMLResponse)
def scanners():
    with open("scorr_scanners.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/filters")
def filters_page():
    """cc#393: the Unified Screener is folded into /v13 (registry + live screener). Permanent
    redirect keeps old bookmarks working. scorr_filters.html kept one release for rollback."""
    return RedirectResponse(url="/v13", status_code=301)

@app.get("/structure", response_class=HTMLResponse)
def structure_page():
    with open("scorr_structure.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/performance", response_class=HTMLResponse)
def performance():
    with open("scorr_performance.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/quant-basket", response_class=HTMLResponse)
def quant_basket():
    with open("quant_basket.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/news", response_class=HTMLResponse)
def news_page():
    with open("scorr_news.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/v10", response_class=HTMLResponse)
def v10_dashboard_page():
    with open("v10_dashboard.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/v9", response_class=HTMLResponse)
def v9_pairs_page():
    """cc#426: V9 · Pairs — sector-neutral long-short concept, extracted from the V8 dashboard tab
    into its own page (same renderer + /api/v8/v9_pairs_sectors pool)."""
    with open("scorr_v9.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/v14", response_class=HTMLResponse)
def v14_intraday_page():
    """cc#442: V14 intraday engine (paper) — live open positions with tag chips, closed-trade log,
    per-tag day summary. Data from /api/v14/*."""
    with open("scorr_v14.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/v15", response_class=HTMLResponse)
def v15_mf_page():
    """cc#467: V15 MF Intelligence skeleton — curated screener + fund deep-dive (look-through holdings
    scored on GVM, NAV-derived returns, external ratings). MQS scoring next session. Data from /api/v15/mf/*."""
    with open("scorr_v15.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/holdings", response_class=HTMLResponse)
def holdings_page():
    """SmartGain MHK40 holdings — gated by single password (scorr_auth PROTECTED set)."""
    with open("scorr_holdings.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/scheduler-master", response_class=HTMLResponse)
def scheduler_master_page():
    """cc#525: Master Scheduler Registry -- every scheduled job (AST-enumerated from
    scheduler.py, not hand-maintained docs), last run/status, drift-audited daily. Reads
    /api/scheduler/master (scheduler_master.py)."""
    with open("scorr_scheduler_master.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/v13", response_class=HTMLResponse)
def v13_filter_registry_page():
    """cc#384: V13 filter registry — reality-verified inventory of every platform metric."""
    with open("scorr_v13.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/v4scan")
def tc_v4_scan_page():
    """cc#399: TC v4 merged into /check (Future Scans). /v4scan now 301-redirects to /check."""
    return RedirectResponse(url="/check", status_code=301)

@app.get("/v12", response_class=HTMLResponse)
def v12_builder_page():
    """cc#394: V12 Quant Basket Builder — 5-step wizard (universe/entry/exit/backtest/deploy)."""
    with open("scorr_v12.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/health", response_class=HTMLResponse)
def health_report_page():
    """cc#398: Portfolio Health Report — upload holdings -> Scorr-native 13-section report."""
    with open("scorr_health.html", "r", encoding="utf-8") as f: return f.read()

# ── NAV_REGISTRY (cc#397, rule id=2987) — every GET-HTML route -> nav label -> status ──────────────
# STATUS: nav = in cockpit web nav + cio dashboard nav + mobile launcher; redirect = 301s away;
# INTERNAL = deliberately not in nav (test/dev). Keep this in sync when adding a page (nav-complete
# shipping rule: a page is not "done" until it is routed + in BOTH navs, collision-free, cache-safe).
NAV_REGISTRY = {
    "/":             ("Home",                 "nav"),
    "/dashboard":    ("V8",                   "nav"),
    "/cio":          ("Max (AI CIO)",         "nav"),
    "/cio2":         ("GVM (?model=gvm)",     "nav"),
    "/ask":          ("(removed from nav — superseded by Max)", "typed-url"),   # cc#435
    "/check":        ("Check",                "nav"),
    "/intraday":     ("TC Scanner",           "nav"),   # cc#464: same-slot rename (id=2987)
    "/sector":       ("Sector",               "nav"),
    "/fpc":          ("FPC",                  "nav"),
    "/scanners":     ("(removed from nav — superseded by V12/V13/Check)", "typed-url"),   # cc#441
    "/structure":    ("(removed from nav — superseded)", "typed-url"),   # cc#437
    "/performance":  ("(removed from nav — superseded)", "typed-url"),   # cc#437
    "/quant-basket": ("QB (curated Quant Basket)", "nav"),
    "/news":         ("News (Intelligence)",  "nav"),
    "/v10":          ("(-> /dashboard#index · Index Intel tab; standalone retired)", "typed-url"),   # cc#542
    "/v9":           ("V9 · Pairs",           "nav"),        # cc#426 rule id=2987 (extracted from V8 tab)
    "/v14":          ("(-> /dashboard#v14 · V14 Intraday tab; standalone retired)", "typed-url"),   # cc#543
    "/dashboard#index": ("Index Intel (V8 tab)", "nav"),     # cc#542 rule id=2987 (folded into V8)
    "/dashboard#v14":   ("V14 · Intraday (V8 tab)", "nav"),  # cc#543 rule id=2987 (folded into V8)
    "/dashboard#bt":    ("V6 BT (V8 tab · backtest playground)", "nav"),  # cc#544 rule id=2987 (folded into V8)
    "/v15":          ("V15 · MF",             "nav"),        # cc#467 rule id=2987 (MF intelligence)
    "/scheduler-master": ("Scheduler Master",  "nav"),        # cc#525: scheduled-job registry + drift audit
    "/holdings":     ("Holdings",             "nav"),
    "/v13":          ("V13 · Registry & Screener", "nav"),
    "/v4scan":       ("(-> /check · Future Scans)", "redirect"),   # cc#399 301
    "/v12":          ("V12 · Quant Basket Builder", "nav"),
    "/screener":     ("(-> /v13 · RETIRED)",  "redirect"),   # cc#407 301 (V13 = single screener)
    "/health":       ("Health Report",        "nav"),        # cc#398 rule id=2987
    "/filters":      ("(-> /v13)",            "redirect"),   # cc#393 301
    "/test-cio":     ("(test harness)",       "INTERNAL"),   # test_cio_endpoints, dev-only
}

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
                ("futures_basis","SELECT MAX(ts)::date FROM futures_basis",1,"Futures basis"),
            ]:
                try:
                    cur.execute(q); latest = cur.fetchone()[0]
                    if latest:
                        days_old = (today - latest).days
                        add_check("data_feeds", _check(f"{latest} ({days_old}d ago)", label,
                            lambda v, m=max_d, d=days_old: d <= m, lambda v, m=max_d, d=days_old: d <= m*3))
                    else: add_check("data_feeds", {"check": label, "value": "NO DATA", "status": "fail"})
                except Exception as e: add_check("data_feeds", {"check": label, "value": str(e), "status": "fail"})

            # cc#406: gvm_cache staleness — the Max/query cache must track gvm_scores (>2d warn, >7d fail)
            try:
                cur.execute("SELECT last_sync, status FROM cache_metadata WHERE key='gvm_cache'")
                cm = cur.fetchone()
                if cm and cm[0]:
                    cache_age = (now - cm[0]).days if hasattr(cm[0], "date") else (today - cm[0]).days
                    add_check("data_feeds", _check(f"{cm[0]} ({cache_age}d ago, {cm[1]})", "GVM cache sync",
                        lambda v, a=cache_age: a <= 2, lambda v, a=cache_age: a <= 7))
                else:
                    add_check("data_feeds", {"check": "GVM cache sync", "value": "never synced", "status": "fail"})
            except Exception as e:
                add_check("data_feeds", {"check": "GVM cache sync", "value": str(e), "status": "fail"})

            # cc#420: earnings_calendar freshness — scrape runs daily incl weekends (>36h warn, >72h fail)
            try:
                cur.execute("SELECT MAX(last_updated) FROM earnings_calendar")
                em = cur.fetchone()
                if em and em[0]:
                    ec_hrs = round((now - em[0]).total_seconds() / 3600, 1)
                    add_check("data_feeds", _check(f"{em[0]} ({ec_hrs}h ago)", "Earnings calendar",
                        lambda v, a=ec_hrs: a <= 36, lambda v, a=ec_hrs: a <= 72))
                else:
                    add_check("data_feeds", {"check": "Earnings calendar", "value": "no rows", "status": "fail"})
            except Exception as e:
                add_check("data_feeds", {"check": "Earnings calendar", "value": str(e), "status": "fail"})

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

def _digest_domestic_live(cur, sym):
    def _r(v, d=2):
        try: return round(float(v), d) if v is not None else None
        except: return None
    cur.execute("SELECT close FROM raw_prices WHERE symbol=%s AND price_date < CURRENT_DATE ORDER BY price_date DESC LIMIT 1", (sym,))
    pc = cur.fetchone()
    prev_close = float(pc[0]) if pc and pc[0] is not None else None
    cur.execute("""
        SELECT
            (SELECT open  FROM intraday_prices WHERE symbol=%s AND ts::date=CURRENT_DATE ORDER BY ts ASC  LIMIT 1) AS o,
            MAX(high) AS h, MIN(low) AS l,
            (SELECT close FROM intraday_prices WHERE symbol=%s AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 1) AS c
        FROM intraday_prices WHERE symbol=%s AND ts::date=CURRENT_DATE
    """, (sym, sym, sym))
    r = cur.fetchone()
    if r and r[3] is not None and prev_close:
        chg = round((float(r[3]) / prev_close - 1) * 100, 2)
        return {"price_date": str(date.today()), "open": _r(r[0]), "high": _r(r[1]),
                "low": _r(r[2]), "close": _r(r[3]), "prev_close": _r(prev_close),
                "chg_pct": chg, "source": "live_intraday"}
    cur.execute("""
        WITH d AS (SELECT price_date, open, high, low, close, ROW_NUMBER() OVER (ORDER BY price_date DESC) rn FROM raw_prices WHERE symbol = %s)
        SELECT a.price_date::text, a.open, a.high, a.low, a.close, ROUND(((a.close-b.close)/NULLIF(b.close,0)*100)::numeric,2)
        FROM d a JOIN d b ON b.rn=2 WHERE a.rn=1
    """, (sym,))
    e = cur.fetchone()
    if e:
        return {"price_date": e[0], "open": _r(e[1]), "high": _r(e[2]), "low": _r(e[3]),
                "close": _r(e[4]), "chg_pct": _r(e[5]), "source": "eod_fallback"}
    return None

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
                domestic[sym] = _digest_domestic_live(cur, sym)
            cur.execute("SELECT price_date::text, advances, declines, unchanged, adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
            adr_row = cur.fetchone()
            adr = {"price_date": adr_row[0], "advances": adr_row[1], "declines": adr_row[2], "unchanged": adr_row[3], "adr": _r(adr_row[4])} if adr_row else None
            result["sections"]["2_domestic_indices"] = {"label": "Domestic Indices + ADR", "NIFTY50": domestic.get("NIFTY50"), "BANKNIFTY": domestic.get("BANKNIFTY"), "adr": adr}
            t_due = _get_config("takeaway_refresh_due", "false"); ov_due = _get_config("overview_refresh_due", "false")
            if t_due == "true" or ov_due == "true":
                tier = _get_config("takeaway_refresh_tier", "")
                result["refresh_alert"] = {"takeaway_due": t_due=="true", "overview_due": ov_due=="true", "tier": tier}
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
            # cc#517: FII/DII cash + FII index-futures positioning (participant-wise OI). Computed
            # in nse_eod_ingest.py (single source, no recompute here); a blank section before the
            # first nightly run is expected, not an error.
            try:
                from nse_eod_ingest import fii_dii_streak_and_5d, fii_index_futures_positioning
                fii = fii_dii_streak_and_5d(cur, "FII")
                dii = fii_dii_streak_and_5d(cur, "DII")
                positioning = fii_index_futures_positioning(cur)
                result["sections"]["6_market_positioning"] = {
                    "label": "FII/DII + Positioning (NSE EOD)",
                    "fii_dii": {"FII": fii, "DII": dii}, "fii_index_futures": positioning,
                }
            except Exception as e:
                log.warning(f"digest market_positioning section: {e}")
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

_ALLOWED_CONTENT_FIELDS = {"overview", "key_takeaway", "result_analysis"}
_TOP500_ONLY_FIELDS = {"key_takeaway", "result_analysis"}
_FIELD_TO_TS_COL = {
    "overview": "last_overview_updated",
    "key_takeaway": "last_takeaway_updated",
    "result_analysis": "last_result_analysis_updated",
}

@app.post("/api/admin/content_update")
def content_update(req_body: dict, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    symbol = (req_body.get("symbol") or "").strip().upper()
    field = (req_body.get("field") or "").strip().lower()
    content = req_body.get("content", "")
    if not symbol: raise HTTPException(400, "symbol is required")
    if field not in _ALLOWED_CONTENT_FIELDS: raise HTTPException(400, f"field must be one of: {sorted(_ALLOWED_CONTENT_FIELDS)}")
    if content is None or str(content).strip() == "": raise HTTPException(400, "content cannot be empty")
    content = str(content).strip(); ts_col = _FIELD_TO_TS_COL[field]
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, mcap_rank, company_name FROM input_raw WHERE nse_code = %s", (symbol,))
            row = cur.fetchone()
            if not row: raise HTTPException(404, f"{symbol} not found in input_raw")
            row_id, mcap_rank, company_name = row[0], row[1], row[2]
            if field in _TOP500_ONLY_FIELDS:
                rank = mcap_rank if mcap_rank is not None else 9999
                if rank > 500: raise HTTPException(400, f"{symbol} has mcap_rank={rank} (>500).")
            cur.execute(f"UPDATE input_raw SET {field} = %s, {ts_col} = NOW() WHERE id = %s", (content, row_id))
            conn.commit()
        return {"status": "ok", "symbol": symbol, "company_name": company_name, "field": field,
                "chars_written": len(content), "timestamp_col_updated": ts_col, "mcap_rank": mcap_rank}
    except HTTPException: raise
    except Exception as e: log.error(f"content_update failed for {symbol}: {e}"); raise HTTPException(500, str(e))

@app.get("/api/admin/env_check")
def env_check(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); keys = sorted(os.environ.keys())
    interesting = ["SCREENER_EMAIL","SCREENER_PASSWORD","GITHUB_TOKEN","GITHUB_REPO","ADMIN_TOKEN","DATABASE_URL","DEPLOY_GUARD","RAILWAY_PUBLIC_DOMAIN","SCORR_PASSWORD"]
    return {"version": VERSION, "all_keys_count": len(keys), "interesting": {k: {"present": k in os.environ, "len": len(os.environ.get(k,""))} for k in interesting}}

@app.post("/api/v8/run_signal_writer")
def v8_run_signal_writer(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    # cc#230 hotfix: capture the traceback instead of letting it escape as a non-JSON 500
    # (writer dead since 03-Jul with an unhandled exception; Railway logs not queryable).
    try:
        with get_conn() as conn: return v8_signal_writer.run_live_signal_writer(conn)
    except Exception as e:
        import traceback as _tb
        tb = _tb.format_exc()
        try:
            with get_conn() as _c, _c.cursor() as _cur:
                _cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                             "VALUES (CURRENT_DATE, NOW(), 'alert', 'signal_writer_crash', %s::jsonb)",
                             (json.dumps({"error": str(e), "tb": tb.splitlines()[-12:]}),))
                _c.commit()
        except Exception:
            pass
        return {"error": str(e), "traceback": tb.splitlines()[-12:]}

@app.post("/api/v8/bt7_run")          # cc#218: BT7 parity harness — walk a day into the sandbox
def v8_bt7_run(date: str, label: str, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    import bt7_harness; return bt7_harness.run_bt7(date, label)

@app.get("/api/v8/bt7_diff")          # cc#218: zero-diff report between two runs (or vs golden_YYYYMMDD)
def v8_bt7_diff(label_a: str, label_b: str, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    import bt7_harness; return bt7_harness.bt7_diff(label_a, label_b)

@app.get("/api/v8/bt7_status")        # cc#220: poll a run (status running/ok/error + summary)
def v8_bt7_status(label: str, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    import bt7_harness; return bt7_harness.bt7_status(label)

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
        ("futures_basis","SELECT MAX(ts)::date, COUNT(*) FROM futures_basis"),
        ("option_chain","SELECT MAX(ts)::date, COUNT(*) FROM option_chain"),
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
               month_return, week_return, year_return, mom_2d, day_1d, eod_chg,
               month_index, week_index_52, ma9_vs_ma21, vol_ratio,
               sector_week, sector_month
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
    # cc#182: anchor to the last available 5m trading day instead of CURRENT_DATE so
    # CMP / Day Change / Hourly keep serving Friday's values on weekends & holidays.
    # Hourly is anchored to the latest bar (lc.ts) rather than NOW(): on a live day
    # that IS "~65 min ago"; off-hours it becomes the last 65-min window of that day.
    # cc#424: anchor to the last day that actually has fyers_eq bars. Without the source
    # filter, a stray phantom fyers_hist bar (e.g. a Sat backfill row) wins MAX(ts::date)
    # while the LATERAL joins below read source='fyers_eq' only -> 0 rows -> blank funnels.
    as_of = api_query("SELECT MAX(ts::date) AS d FROM intraday_prices WHERE timeframe='5m' AND source='fyers_eq'", single=True)
    as_of_date = (as_of or {}).get("d")
    rows = api_query("""
        WITH asof AS (SELECT %s::date AS d)
        SELECT s.symbol, lc.close AS cmp, fc.open AS day_open,
            CASE WHEN fc.open>0 THEN ROUND(((lc.close/fc.open-1)*100)::numeric,2) END AS day_pct,
            hc.close AS hour_ago_close,
            CASE WHEN hc.close>0 THEN ROUND(((lc.close/hc.close-1)*100)::numeric,2) END AS hourly_pct
        FROM (SELECT symbol FROM futures_universe WHERE is_active=TRUE) s
        JOIN LATERAL (SELECT close, ts FROM intraday_prices WHERE symbol=s.symbol AND ts::date=(SELECT d FROM asof) AND source='fyers_eq' ORDER BY ts DESC LIMIT 1) lc ON true
        JOIN LATERAL (SELECT open FROM intraday_prices WHERE symbol=s.symbol AND ts::date=(SELECT d FROM asof) AND source='fyers_eq' ORDER BY ts ASC LIMIT 1) fc ON true
        LEFT JOIN LATERAL (SELECT close FROM intraday_prices WHERE symbol=s.symbol AND ts::date=(SELECT d FROM asof) AND source='fyers_eq' AND ts <= lc.ts - INTERVAL '65 minutes' ORDER BY ts DESC LIMIT 1) hc ON true
        ORDER BY s.symbol
    """, (str(as_of_date) if as_of_date else None,))
    return {"as_of": str(as_of_date) if as_of_date else None, "rows": rows}

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

def _resample_1m_to_5m(candles):
    """cc#229: aggregate yahoo 1-min OHLCV -> native 5-min buckets (5m system, spec id=167;
    1-min deprecated). O=first, H=max, L=min, C=last, V=sum per 5-min bucket. candles are
    (ts, o, h, l, c, v)."""
    buckets = {}
    for (ts, o, h, l, c, v) in candles:
        b = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
        bk = buckets.get(b)
        if bk is None:
            buckets[b] = {"o": o, "h": h, "l": l, "c": c, "v": v or 0, "first": ts, "last": ts}
        else:
            bk["h"] = max(bk["h"], h); bk["l"] = min(bk["l"], l); bk["v"] += (v or 0)
            if ts < bk["first"]: bk["first"] = ts; bk["o"] = o
            if ts > bk["last"]:  bk["last"]  = ts; bk["c"] = c
    return [(b, buckets[b]["o"], buckets[b]["h"], buckets[b]["l"], buckets[b]["c"], buckets[b]["v"])
            for b in sorted(buckets)]

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
            # cc#238 (Branch B, addendum 1652): detect ANY missing 5-min tick across the FULL
            # 09:15-15:30 session (was leading-gap-only). One LAG query flags leading/interior/
            # trailing gaps; heal ONLY when a real gap exists so a clean session makes zero
            # Yahoo calls. Reuses the same Yahoo-1m->5m->fyers_eq point-in-time pattern — this
            # is data-completion, never a v8_qualified re-score (GVM stays last-frozen).
            row = api_query("""SELECT COUNT(*) AS cnt, MIN(ts) AS mn, MAX(ts) AS mx,
                       COALESCE(MAX(EXTRACT(EPOCH FROM (ts - prev_ts))/60), 0) AS max_gap_min
                FROM (SELECT ts, LAG(ts) OVER (ORDER BY ts) AS prev_ts FROM intraday_prices
                      WHERE symbol=%s AND ts::date=%s AND timeframe='5m' AND source='fyers_eq') x""",
                (sym, today), single=True)
            cnt = row.get("cnt",0) if isinstance(row,dict) else 0
            mn = row.get("mn") if isinstance(row,dict) else None
            mx = row.get("mx") if isinstance(row,dict) else None
            max_gap = float(row.get("max_gap_min") or 0) if isinstance(row,dict) else 0
            od = open_dt.replace(tzinfo=None); hu = heal_until.replace(tzinfo=None)
            last_expected = hu - timedelta(minutes=5)   # last definitely-closed 5m bar
            has_gap = (cnt == 0
                       or (mn is not None and mn > od + timedelta(minutes=6))              # leading gap
                       or max_gap > 6.0                                                     # interior gap
                       or (mx is not None and mx < last_expected - timedelta(minutes=1)))   # trailing gap
            if not has_gap: skipped+=1; continue
            gap_from = od
            candles = _yahoo_1m_today(sym)
            if not candles: empties+=1; time.sleep(_HEAL_SLEEP); continue
            # resample the full session window; ON CONFLICT DO NOTHING fills ONLY the missing 5m
            # slots (never clobbers a real WS bar), so interior gaps heal without re-scoring.
            windowed = [(ts,op,hi,lo,cl,vol) for (ts,op,hi,lo,cl,vol) in candles
                        if ts.date()==today and gap_from<=ts<=hu]
            # write as source='fyers_eq' 5m so the V8 engine (fyers_eq-only, cc#228) actually
            # reads the healed gap; ON CONFLICT DO NOTHING never clobbers real WS bars.
            rows = [(sym,b,o,h,l,c,v,"5m","fyers_eq") for (b,o,h,l,c,v) in _resample_1m_to_5m(windowed)]
            if rows:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.executemany("INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (symbol,ts,timeframe,source) DO NOTHING", rows)
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
    if scheduler._yahoo_daily_running: return {"status":"already_running"}
    asyncio.create_task(scheduler._bg_yahoo_daily()); return {"status":"started"}

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
    # cc#367: CMP must be the SPOT equity bar — the old lateral had NO source filter, so a symbol's
    # latest bar could be a fyers_fut (futures) bar at the same 5-min ts, putting a basis-off price
    # in the CMP column. Excluding fyers_fut pins CMP to spot. prev_close lets the dashboard compute
    # DAY% = CMP / prev_close - 1 (one consistent, hand-verifiable pair) instead of v8_metrics.day_1d.
    # cc#373: prev_close base is the latest raw close STRICTLY BEFORE THE CMP'S OWN SESSION
    # (lp.ts::date), NOT before CURRENT_DATE. Off-market the CMP is the last (e.g. Friday) tick, so a
    # "< today" base returned that same Friday session -> DAY% compared Friday against itself (~0.0x%).
    # Anchoring to lp.ts::date gives Thu-close base for a Fri CMP, and Fri-close base for a Mon live CMP.
    # cc#504: fresh-book-only default — recent_trades/summary filter to entry_ts >= the SUITE_REBUILD
    # cutover (app_config.v8_paper_rebuild_cutover_ts, zero-schema-change era split). Pre-rebuild
    # trades stay in v8_paper_trades (kept, never deleted) but drop out of the default view; era=NULL
    # (cutover hasn't run yet, e.g. first boot before the startup task lands) -> no filter, full
    # history shown rather than an empty dashboard. open_positions needs no filter in practice (every
    # pre-cutover OPEN row was closed by the cutover itself) but carries the same guard for safety.
    cutover = api_query("SELECT value FROM app_config WHERE key='v8_paper_rebuild_cutover_ts'", single=True)
    cutover_ts = (cutover or {}).get("value")
    era_clause = "entry_ts >= %s::timestamp" if cutover_ts else "TRUE"
    era_params = (cutover_ts,) if cutover_ts else ()

    open_positions = api_query(f"""
        SELECT p.symbol, p.side, p.basket, p.entry_price, p.entry_ts,
            p.target, p.stop_loss, p.qty, p.pivot_date,
            COALESCE(lp.cmp, p.entry_price) AS cmp,
            ROUND(CASE p.side WHEN 'LONG' THEN (COALESCE(lp.cmp, p.entry_price) - p.entry_price) * p.qty
                WHEN 'SHORT' THEN (p.entry_price - COALESCE(lp.cmp, p.entry_price)) * p.qty ELSE 0 END::numeric, 2) AS unrealised_pnl,
            lp.ts AS cmp_updated_at, pc.prev_close
        FROM v8_paper_positions p
        LEFT JOIN LATERAL (
            SELECT close AS cmp, ts FROM intraday_prices
            WHERE symbol = p.symbol AND source <> 'fyers_fut' ORDER BY ts DESC LIMIT 1
        ) lp ON true
        LEFT JOIN LATERAL (
            SELECT close AS prev_close FROM raw_prices
            WHERE symbol = p.symbol
              AND price_date < COALESCE(lp.ts::date, (NOW() AT TIME ZONE 'Asia/Kolkata')::date)
            ORDER BY price_date DESC LIMIT 1
        ) pc ON true
        WHERE p.status = 'OPEN' AND {era_clause} ORDER BY p.entry_ts DESC
    """, era_params)
    return {
        "open_positions": open_positions,
        "recent_trades": api_query(f"SELECT symbol,side,basket,entry_price,exit_price,pnl,return_pct,result,entry_ts,exit_ts FROM v8_paper_trades WHERE {era_clause} ORDER BY closed_at DESC LIMIT 100", era_params),
        "missed": api_query("SELECT miss_date,symbol,side,basket,expected_entry,reason FROM v8_paper_missed ORDER BY ts DESC LIMIT 100"),
        "summary": api_query(f"SELECT COUNT(*) AS trades, COUNT(*) FILTER (WHERE result='TARGET') AS wins, COUNT(*) FILTER (WHERE result='SL') AS losses, ROUND(SUM(pnl)::numeric,2) AS total_pnl, ROUND(AVG(return_pct)::numeric,3) AS avg_ret FROM v8_paper_trades WHERE {era_clause}", era_params, single=True),
        "rebuild_cutover_ts": cutover_ts,
    }

@app.get("/api/paper/pivots")
def paper_pivots(limit: int = 250, symbol: Optional[str] = None):
    # cc#547: return the LATEST pivot row PER symbol (v8_paper_pivots holds history).
    # The old query filtered on a single global MAX(pivot_date) and then ORDER BY symbol
    # LIMIT — index symbols like NIFTY50 sit deep in the alphabetical order (~1085th of
    # ~1750 rows) and were silently truncated by the LIMIT, showing "pivots pending".
    # An optional comma-separated `symbol` filter fetches specific symbols un-truncated.
    if symbol:
        syms = [s.strip().upper() for s in symbol.split(",") if s.strip()]
        return api_query("SELECT DISTINCT ON (symbol) symbol,pp,r1,s1,r2,s2,pivot_date FROM v8_paper_pivots WHERE UPPER(symbol)=ANY(%s) ORDER BY symbol, pivot_date DESC", (syms,))
    return api_query("SELECT DISTINCT ON (symbol) symbol,pp,r1,s1,r2,s2,pivot_date FROM v8_paper_pivots ORDER BY symbol, pivot_date DESC LIMIT %s", (limit,))

@app.post("/api/paper/rebuild_cutover")
def paper_rebuild_cutover(x_admin_token: Optional[str] = Header(None)):
    # cc#504: manual re-arm/visibility for the startup cutover task (idempotent — a second call
    # after the first successful run just returns already_done). Lets the flatten be checked or
    # (re)triggered without waiting for the next deploy.
    _check_admin(x_admin_token)
    with get_conn() as conn: return v8_paper.rebuild_cutover(conn)

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
