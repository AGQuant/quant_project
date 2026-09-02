from fastapi import FastAPI, HTTPException, Request, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware   # cc#712: response compression
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
# from qsr_endpoints import router as qsr_router   # cc#1442: QSR RETIRED (founder order, session_log 33844) — unmounted, not deleted
from bt6_endpoints import router as bt6_router   # cc#544: V6 BT playground (read-mostly wrapper)
from results_endpoints import router as results_router   # cc#572: Results "R" card backend (id=6438)
from pcr_endpoints import router as pcr_router
from pcr_mood import router as pcr_mood_router   # cc#1568: /api/pcr/mood composer (session_log 36200)
from v8_replay_endpoints import router as v8_replay_router
from v8_intra_backtest_endpoints import router as backtest_router
from v8_backfill_endpoints import router as v8_backfill_router
from v8_metrics_gapfill import router as v8_gapfill_router   # cc#1048 full-universe v8_metrics gapfill
from index_tape import router as index_tape_router   # cc#1054 index 100-bar cash tape
from v10_page_endpoints import router as v10_page_router   # cc#1069 GET /m/v10
from volume_flow_endpoints import router as volume_flow_router   # cc#1368 GET /api/volume-flow
from trade_alerts_endpoints import router as trade_alerts_router  # cc#1503 /api/alerts/*
from github_ops import router as github_ops_router        # cc#1249: was never imported
from mobile_cards_endpoints import router as mobile_cards_router   # cc#1090 P0: GET /m/cards
from gvm_twopager import router as gvm_twopager_router   # cc#1085 R6-P1: GET /gvm/2pager/{symbol}
from price_sources import NOT_FUT_SQL   # cc#1056 / cc#1053 source registry — one list, never retyped
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
from tc_scanner_config import router as tc_scanner_config_router   # cc#1222: TC_SCANNER_GATED_CONFIG_V1.1 — one config, served
from check_endpoint import router as check_router
from tc_sim_endpoints import router as tc_sim_router   # cc#748: TC outcome sim
from client_index_endpoints import router as client_index_router   # cc#758: client_index credential security
from sector_endpoints import router as sector_router
from sector_brief_endpoints import router as sector_brief_router, _batch_job as _sector_brief_batch
from ops_metrics_pipeline import router as ops_metrics_router   # cc#523: sector KPI registry + concall pipeline
from ops_peer_benchmark import router as ops_peer_benchmark_router   # cc#593: ops-metrics peer-benchmark compute
from result_corner import router as result_corner_router   # cc#602: news-vs-calendar result coverage
from result_corner import page_router as result_corner_page_router   # cc#603: /api/result-corner page API
from engine_watchdog import router as engine_watchdog_router   # cc#599: engine watchdog outcome audit
from result_analysis_gen import router as result_analysis_gen_router   # cc#602: result_analysis regeneration
from scheduler_master import router as scheduler_master_router   # cc#525: scheduled-job registry + drift audit
from scorr_auth import router as auth_router, _is_authed, PROTECTED
from scorr_authset_probe import router as authset_probe_router
from pwa_endpoints import router as pwa_router
from investment_check import router as investment_check_router
from invest_check_v2 import router as invest_check_v2_router   # cc#1174: Investment Check V2 (27979)
from scanner_endpoints import router as scanner_router
from intraday_scanner_endpoints import router as intraday_scanner_router  # cc#481: restored (cc#476 kill reversed)
from tc_scanner_endpoints import router as tc_scanner_router  # cc#464: TC Scanner (13-check binary engine, id=399/400)
from structure_endpoints import structure_router
from performance_endpoints import router as performance_router
from scheduler_health_endpoints import router as scheduler_health_router
from news_endpoints import router as news_router
# cc#847: position_news_endpoints REMOVED — Position News retired (tab + fetcher + endpoints).
from stock_views_funnel import router as stock_views_funnel_router  # cc#787: FUNNEL 2 (Stock Views)
from admin_index_backfill import router as idx_backfill_router
from feed_health_endpoints import router as feed_health_router
from v12_endpoints import router as v12_router
from v12_backtest import router as v12_backtest_router   # cc#394 V12 Basket Builder backtest walker
from test_cio_endpoints import router as test_cio_router
from fyers_range_backfill_endpoints import router as fyers_range_backfill_router
from smartgain_daily_m2m import router as smartgain_daily_m2m_router
from smartgain_reconcile import router as smartgain_reconcile_router
from stock_options_backfill import router as stock_options_backfill_router
from fy_end_backfill import router as fy_end_backfill_router   # cc#703 FY-end price backfill 2015-2021
from fyers_hist_backfill import router as fyers_hist_backfill_router   # cc#377 Phase B
from fundamentals_scraper import router as fundamentals_scraper_router   # cc#361 Phase 1 scrape
from screeners_endpoints import router as screeners_router   # cc#824 predefined screeners (read-only)
from max_ivr_endpoints import router as max_ivr_router   # cc#836 Max IVR guided CIO tree + telemetry
from max_native_cards import router as max_cards_router   # cc#836 phase B: native card templates
from v8_pivot_star import router as pivot_star_router   # cc#856 pivot-star marker (read-only)
from preview_endpoints import router as preview_router   # cc#866 preview screens (Claude.ai pushes previews/)
from mobile_endpoints import router as mobile_router     # cc#874 promoted mobile screens (/m/*)
from v8_futures_book import router as v8_futures_book_router   # cc#885 /api/v8/futures_book
from mobile_endpoints import wants_mobile_home                 # cc#886 mobile entry decision
# cc#893 item 3: these two were mounted from a tail import inside preview_endpoints.py because
# Fable cannot edit main.py. They belong here, with every other router.
from mobile_home2 import router as mobile_home2_router         # cc#889 Home (market-first rebuild)
from v8_book_canon import router as v8_book_canon_router     # cc#970 V8_PNL_CANON_V1 (rule 13)
from v8_era import router as v8_era_router                    # cc#1604 V8_ERA_CUTOVER_ONLY_V1: /api/v8/era
from v8_daylog_extras import router as v8_daylog_extras_router   # cc#1561 /api/v8/daylog/series
from mobile_ext import router as mobile_ext_router             # cc#892 breadth + cc#893 depth
from trade_wall_endpoints import router as trade_wall_router    # cc#991 Wall of Trades
from model_launcher import router as model_launcher_router   # cc#860 model launcher (read-only)
from global_heatstrip import router as heatstrip_router   # cc#842 global day/week heat strip (read-only)
from chart_peers import router as chart_peers_router   # cc#845 chart card Peers tab (read-only)
from digest_v3 import router as digest_v3_router   # cc#846 Daily Digest V3 (read-only render)
from v13_presets_endpoints import router as v13_presets_router
from mf_pipeline import router as mf_pipeline_router   # cc#466: V15 MF Intelligence data layer
from galaxy_endpoints import router as galaxy_router
from hr_endpoints import router as hr_router   # cc#398 Portfolio Health Report (M1 ingest)
from hr_report import router as hr_report_router   # cc#398 Portfolio Health Report (M2 report engine)
from hr_report_pdf import router as hr_report_pdf_router   # cc#652 Portfolio Health Report white-label PDF
import yahoo_ondemand
import yahoo_index_backfill
from yahoo_symbol_resolver import router as yahoo_resolver_router   # cc#938: Yahoo ticker resolver + price-feed exclusion register
from room_endpoints import router as room_router   # cc#1086: /room + /api/room/feed (read-only Fable Room viewer)
from ondemand_bars import router as ondemand_bars_router   # cc#1103: /api/bars/{symbol} on-demand 5-min pull
from tc_screener_v2 import router as tc_screener_v2_router   # cc#1172: four-bucket screener (tc_screener_v2)
from tc_position_stars_v2 import router as tc_position_stars_v2_router   # cc#1172: four-bucket position stars
from tc_score_replay_endpoints import router as tc_score_replay_router   # cc#1211: TC score entry replay
from basket_rebalance_endpoints import router as basket_rebalance_router  # cc#1273: per-client basket subscriptions + repair
from inv_scanner_universe import router as inv_scanner_router  # cc#1283: investment scanner universe (engine 1/3)
from inv_scanner_scoring import router as inv_scanner_scoring_router  # cc#1284: two-track scoring (engine 2/3)
from inv_scanner_rules import router as inv_scanner_rules_router  # cc#1285: entry/exit rules (engine 3/3)
from inv_scanner_endpoints import router as inv_scanner_page_router  # cc#1286: /inv-scanner page + board feed
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
    b"background:rgba(15,22,35,0.88);border:1px solid var(--edge, #2a3548);border-radius:7px;"
    b"color:var(--muted, #5a6781);font-size:10.5px;font-weight:600;text-decoration:none;"
    b"font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif;"
    b"backdrop-filter:blur(8px);transition:all .15s;}"
    b"#scorr-lo a:hover{color:var(--amber, #b45309)!important;border-color:var(--amber, #b45309)!important;}"
    # cc#363: light-theme override — dark pill was unreadable on the light header.
    b":root[data-theme=\"light\"] #scorr-lo a{background:rgba(255,255,255,.92);"
    b"border-color:rgba(20,35,80,.14);color:var(--muted, #5B6B94);}</style>"
    b'<div id="scorr-lo"><a href="/logout">&#x23CF; Logout</a></div>'
)

# cc#348: ONE global theme switch, fixed top-right just BELOW the logout pill (founder 09-Jul).
# Sets scorr_theme + reloads so EVERY page — CSS-var pages, the React GVM, and the older
# hardcoded pages — re-renders in the chosen theme (guaranteed consistency, no per-page drift).
_THEME_BTN = (
    b"<style>#scorr-th{position:fixed;top:102px;right:14px;z-index:9999;opacity:.45;transition:opacity .15s}"   # cc#433: below navbar + logout, semi-transparent idle
    b"#scorr-th:hover{opacity:1}"
    b"#scorr-th button{display:inline-flex;align-items:center;gap:5px;padding:5px 11px;"
    b"border-radius:7px;border:1px solid var(--edge, #2a3548);background:rgba(15,22,35,.88);color:var(--muted, #5a6781);"
    b"font-size:10.5px;font-weight:600;cursor:pointer;backdrop-filter:blur(8px);"
    b"font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif}"
    b"#scorr-th button:hover{color:var(--pulse, #4D7CFE);border-color:var(--pulse, #4D7CFE)}"
    # cc#363: light-theme override — the dark pill on the light header was the "broken" look.
    b":root[data-theme=\"light\"] #scorr-th button{background:rgba(255,255,255,.92);"
    b"border-color:rgba(20,35,80,.14);color:var(--muted, #5B6B94)}"
    b":root[data-theme=\"light\"] #scorr-th button:hover{color:var(--pulse, #3D6BEC);border-color:var(--pulse, #3D6BEC)}</style>"
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
                     "/v13", "/v12", "/health", "/v15", "/scheduler-master", "/result-corner",
                     "/screeners",   # cc#824
                     "/inv-scanner", # cc#1286: Investment Scanner tab
                     "/digest",      # cc#846
                     "/trades",      # cc#991: Wall of Trades (web renderer)
                     "/alerts",      # cc#1536: Alerts (web renderer, the approve surface)
                     "/adaptive",   # cc#392/394/398/426/442/467/525/603/651: no-store + theme/logout pills
                     "/room"}       # cc#1086: Fable Room viewer
# cc#407: /screener retired -> 301 /v13 (V13 is the single screening surface). Not injected/protected.
PROTECTED.add("/v13"); PROTECTED.add("/v12"); PROTECTED.add("/health"); PROTECTED.add("/v9"); PROTECTED.add("/v14"); PROTECTED.add("/v15")   # cc#392/394/398/426/442/467: gate + no-store
PROTECTED.add("/scheduler-master")   # cc#525: gate + no-store
PROTECTED.add("/adaptive")   # cc#651: Adaptive Dashboard (client report shelf) — gate + no-store
PROTECTED.add("/result-corner")   # cc#603: gate + no-store
PROTECTED.add("/screeners")   # cc#824: gate + no-store
PROTECTED.add("/inv-scanner")   # cc#1286: gate + no-store
PROTECTED.add("/digest")   # cc#846: gate + no-store
PROTECTED.add("/trades")   # cc#991: Wall of Trades, web — gate + no-store
PROTECTED.add("/alerts")   # cc#1536: Alerts, web — gate + no-store
# cc#1086: the room carries internal engineering discussion and unreleased spec detail. Gated for
# that reason, not by habit — a logged-out request must reach login, never the thread.
PROTECTED.add("/room")
# cc#874: promoted mobile screens are login-gated like every other page. Added to PROTECTED
# only — deliberately NOT to _PWA_INJECT_PATHS: pwa.js injects the DESKTOP navbar into
# #scorr-nav, and these screens carry their own 5-slot bottom nav per 15913 (no tab rows,
# one nav). Injecting both would put two navigations on one screen.
PROTECTED.add("/m/intel"); PROTECTED.add("/m/positions")   # cc#874
PROTECTED.add("/m/qb"); PROTECTED.add("/m/gvm")            # cc#874
PROTECTED.add("/m/v8"); PROTECTED.add("/m/check"); PROTECTED.add("/m/home")   # cc#874
PROTECTED.add("/m/digest"); PROTECTED.add("/m/results")   # cc#874 (final three)
PROTECTED.add("/m/trades")   # cc#991: Wall of Trades, app screen
PROTECTED.add("/m/v10")      # cc#1069: V10 signal view — gated like every other /m/ screen
# cc#1506: Alerts feed. PROTECTED only, deliberately NOT _PWA_INJECT_PATHS — the cc#874 rule
# above: /m/ screens carry their own 5-slot bottom nav, and injecting pwa.js's would put two
# navigations on one screen. (The card's checklist named both sets; this file's own doctrine
# for every sibling /m/ screen is PROTECTED-only, and that is what is followed.)
PROTECTED.add("/m/alerts")
PROTECTED.add("/m/models")   # cc#886 slot 5
# /m/login is DELIBERATELY NOT PROTECTED (cc#874 item 7). Putting the login page behind the login
# gate is a lockout with no way back in. It posts to the existing /login in scorr_auth.py and
# duplicates no auth logic of its own.
# cc#399: /v4scan retired as a page — now a 301 -> /check (TC v4 merged into Check). Not injected/protected.
_PWA_TAG = b'<script src="/pwa.js" defer></script>'

# cc#327 MOBILE_UX_REDEFINE_V1 P1/10: canonical Sora font + shared mobile.css,
# injected into <head> on every protected/app page via the same gate as the PWA
# bootstrap, so no page is missed and the design system is defined in ONE place.
# cc#792: one build stamp for every cache-busted asset URL. Same source the service worker uses for
# its cache name (pwa_endpoints.BUILD_ID), so the two can never drift apart within a deploy.
#
# cc#821: this is now IMPORTED from pwa_endpoints rather than recomputed. The comment above already
# claimed the two shared a source; they did not. Both read RAILWAY_GIT_COMMIT_SHA then APP_VERSION,
# but the final fallback differed — pwa used a process-start token, main.py used the VERSION
# constant. On any deploy where Railway does not expose the SHA to the runtime, main.py's asset
# stamps would freeze at a constant across every deploy (so max-age=86400 assets never bust) while
# the service-worker cache name still rotated. Same intent, opposite behaviour, and only visible in
# production. Importing removes the possibility rather than re-stating the rule in two places.
from pwa_endpoints import BUILD_ID as _BUILD_ID   # noqa: E402  (single source, see above)
from pwa_endpoints import APP_THEME_RESOLVE_JS as _THEME_RESOLVE_JS   # noqa: E402  cc#1185 P10
_BUILD_B = _BUILD_ID.encode()

_MOBILE_HEAD = (
    # cc#345/348: set the saved theme SYNCHRONOUSLY before first paint (no flash).
    # cc#348: DEFAULT is now LIGHT (founder 09-Jul) — no saved pick => light.
    b"<script>(function(){try{var t=localStorage.getItem('scorr_theme')||'light';"
    b"document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>"
    b'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    b'<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700&display=swap" rel="stylesheet">'
    # cc#816: BOTH of these are served with Cache-Control max-age=86400 (_CACHE_1D in
    # pwa_endpoints), so without a changing URL a returning client keeps yesterday's copy for a
    # full day. That is exactly the failure cc#792 stamped the shared JS tags for — it just missed
    # these two, because they sit above the block it edited. The visible symptom the founder hit:
    # the theme button correctly reports Light while the page renders dark, because the cached
    # mobile.css predates the :root[data-theme=light] override it depends on.
    # mobile_tables.js has the identical cache header and the identical exposure, so it is stamped
    # in the same change rather than waiting to be found the same way.
    # The two Google Fonts links above are deliberately NOT stamped: they are third-party URLs on
    # a different origin with their own cache policy, and appending ?v= to them would fingerprint
    # our build into an external request without busting anything we control.
    + b'<link rel="stylesheet" href="/static/mobile.css?v=' + _BUILD_B + b'">'
    # cc#1064 TELEMETRY DROP. The token layer goes AFTER mobile.css deliberately: it re-points the
    # legacy token names (--bg/--panel/--txt/--grn/--red/--blu …) at the R5 palette, and those names
    # are what all 5,138 var() references across the app already resolve to. One link, every page,
    # no per-page edit. It is gated :not([data-theme="light"]) inside the file, so mobile.css's light
    # palette is untouched — Telemetry Drop IS the dark theme, not a replacement for both.
    # Stamped, because cc#1060 was exactly the cost of shipping an unstamped max-age=86400 asset.
    + b'<link rel="stylesheet" href="/static/scorr_theme_r5.css?v=' + _BUILD_B + b'">'
    # R5's three faces. Third-party origin, so deliberately NOT stamped — same reasoning as the
    # Sora link above.
    + b'<link href="https://fonts.googleapis.com/css2?family=Archivo+Black'
      b'&family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@400;600;800'
      b'&display=swap" rel="stylesheet">'
    + b'<script src="/mobile_tables.js?v=' + _BUILD_B + b'" defer></script>'   # cc#330 P4: shared table helper
    # cc#792: every shared JS tag carries the deploy build stamp, so a push busts the browser cache
    # automatically. These are served with Cache-Control max-age=86400, so without a changing URL a
    # returning client could serve yesterday's bundle for a full day — which is exactly how the
    # founder saw the pre-cc#779 chart card hours after it deployed. Stamping the URL is the fix that
    # does not depend on anyone remembering anything at deploy time.
    # cc#805 DEPENDENCY ORDER — scorr_card_common.js FIRST. It owns the primitives every other card
    # file binds to (num/sign/getJSON/newsEsc, the volume tiles, the heat cells, the sparkline), and
    # both scorr_analysis_card.js and scorr_cockpit_card.js bail out with a console warning if
    # window.ScorrCardCommon is not already there. Do not reorder these tags.
    # cc#859 Part A added scorr_mobile_cards.js — it is self-contained (no ScorrCardCommon
    # dependency), so its position is not load-bearing, but it must precede its consumers.
    + b'<script src="/scorr_card_common.js?v=' + _BUILD_B + b'" defer></script>'  # cc#805: shared card primitives (must precede every consumer)
    + b'<script src="/scorr_mobile_cards.js?v=' + _BUILD_B + b'" defer></script>'  # cc#859 Part A: shared mobile section card (cc#862/#863 import it, never redefine it)
    + b'<script src="/scorr_card_strip.js?v=' + _BUILD_B + b'" defer></script>'   # cc#789: shared C·A·R·D strip, load before its consumers
    + b'<script src="/scorr_segment_results.js?v=' + _BUILD_B + b'" defer></script>'  # cc#1191: SEGMENT RESULTS popout — AFTER the strip, which it calls per row
    + b'<script src="/results_card.js?v=' + _BUILD_B + b'" defer></script>'       # cc#573: shared Results R-pill + card
    + b'<script src="/scorr_chart_card.js?v=' + _BUILD_B + b'" defer></script>'   # cc#706: shared V8-type price chart card (letter C)
    + b'<script src="/scorr_analysis_card.js?v=' + _BUILD_B + b'" defer></script>'  # cc#805: shared Analysis modal (letter A)
    + b'<script src="/scorr_cockpit_card.js?v=' + _BUILD_B + b'" defer></script>'   # cc#805: shared Derivative Cockpit (letter D)
    # cc#1390: the SAME BUILD_ID every stamped URL above already carries (RAILWAY_GIT_COMMIT_SHA,
    # pwa_endpoints.BUILD_ID, one source), now also exposed as a plain global so a page's own JS can
    # compare "what I was served as" against "what the server is running right now" (read from a
    # live, no-store endpoint's own response) and self-heal with a cache-busted reload if a layer
    # this app does not control served it a stale document despite every no-store/cache-bust
    # mechanism already in place here. Inlined, not a separate asset — nothing to cache-bust about
    # a value that changes every time this very response is generated. App-wide (every /m/* page
    # picks this up for free via _MOBILE_HEAD), even though the comparison+reload LOGIC that reads
    # it is wired up on Home only for now (cc#1390's own scope) — see mobile/home.html's own use of
    # it for the actual check.
    + b"<script>window.__SCORR_BUILD='" + _BUILD_B + b"';</script>"
)

# cc#1064: the mobile app is DARK-ONLY — mobile_endpoints' mobile_app.css defines the dark tokens
# and carries ZERO :root[data-theme="light"] overrides, so an /m/* page renders dark whatever the
# attribute says. _MOBILE_HEAD nonetheless stamped data-theme from localStorage, defaulting to
# 'light' (cc#348, a WEB decision). The attribute was simply describing those pages wrongly, and it
# stayed invisible because nothing read it there — until the R5 layer, which is gated on exactly
# that attribute and would have skipped the entire mobile app. Fixed at the stamp rather than
# worked around in the CSS: this runs after _MOBILE_HEAD's script, so it wins.
# cc#1203 push 3: the web theme boot, INLINED rather than linked. It must stamp
# html[data-theme] before the first paint, and both a <script src> round trip and the deferred
# siblings below run too late — the page would paint dark and snap to light. Read once at import
# from scorr_theme_boot.js, which stays the one place the logic is edited. No build stamp is
# needed precisely BECAUSE it is inlined: there is no cached asset to bust (the cc#1060 trap does
# not apply to bytes that ship inside the page).
#
# The read is guarded, but NOT silently. A bare `except: pass` here would turn a typo or a missing
# file into "every web page ships an empty <script>" — a failure that renders as plain dark and so
# looks exactly like success. The log line is the difference between a bug you find in the deploy
# log and one you find in a screenshot a week later.
_THEME_BOOT_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scorr_theme_boot.js")
try:
    with open(_THEME_BOOT_SRC, "r", encoding="utf-8") as _tbf:
        _THEME_BOOT = b"<script>" + _tbf.read().encode("utf-8") + b"</script>"
    if len(_THEME_BOOT) < 200:          # the file is ~2.5KB; anything tiny means a truncated read
        raise ValueError("scorr_theme_boot.js read back too small: %d bytes" % len(_THEME_BOOT))
except Exception as _tbe:
    _THEME_BOOT = b""   # absent boot = pages keep whatever theme their own CSS defaults to
    print("[cc#1203] theme boot NOT inlined (%s): %s" % (_THEME_BOOT_SRC, _tbe), flush=True)


_MOBILE_APP_DARK = (
    b"<script>(function(){try{"
    b"document.documentElement.setAttribute('data-theme','dark');"
    b"}catch(e){}})();</script>"
)

# cc#1193 SHARED_CSS_RULE_V1 (session_log 29017): the APP-ONLY R5 rules, linked on /m/* and
# /preview/* only. Build-stamped like every other asset in _MOBILE_HEAD so a deploy busts the
# 1-day cache — a new stylesheet served max-age=86400 with no stamp is the cc#1060 outage.
# cc#1203: the web token contract. Build-stamped like every other linked asset — this one IS a
# fetched file, so the cc#1060 cache rule applies to it even though the boot beside it is inlined.
_WEB_TOKENS_LINK = (
    b'<link rel="stylesheet" href="/static/scorr_web_tokens.css?v=' + _BUILD_B + b'">'
)

# cc#1185 P10: theme resolution, injected at the END of <head> so it runs after each page's own
# inline boot and is the authority. The rule itself lives in ONE place (pwa_endpoints), not in nine
# copies across eight pages and the appshell. It no-ops on any page whose <body> carries no
# data-theme attribute, which is what keeps it off the web surfaces.
_APP_THEME_RESOLVE = b"<script>" + _THEME_RESOLVE_JS.encode("utf-8") + b"</script>"

_THEME_MOBILE_LINK = (
    b'<link rel="stylesheet" href="/static/theme_mobile.css?v=' + _BUILD_B + b'">'
)


def _find_outside_comments(hay: bytes, needle: bytes) -> int:
    """cc#821: index of `needle` in `hay`, ignoring occurrences inside an HTML comment.

    Injecting shared assets by substring-matching a tag name is only safe if prose can never look
    like markup — and it can. A cc#805 comment in v8_dashboard.html that mentioned a closing-head
    tag caused the entire shared-asset block to be injected inside that comment, disabling the theme
    and every C·A·R·D card in production. Returns -1 when every occurrence is commented out."""
    pos = 0
    while True:
        i = hay.find(needle, pos)
        if i < 0:
            return -1
        c_open = hay.rfind(b"<!--", 0, i)
        if c_open < 0:
            return i                      # no comment opens before it -> real markup
        c_close = hay.find(b"-->", c_open)
        if c_close < 0 or c_close > i:
            pos = i + 1                   # sits inside that comment -> keep looking
            continue
        return i                          # the comment closed before it -> real markup


_INERT_SCRIPT = re.compile(rb"<script\b[^>]*>.*?</script>", re.S | re.I)
_INERT_COMMENT = re.compile(rb"<!--.*?-->", re.S)


def _present_in_markup(body: bytes, needle: bytes) -> bool:
    """cc#914: is `needle` REAL MARKUP in `body`, rather than page prose or page code?

    cc#821 fixed this class once, for </head>, by skipping HTML comments. It bit again today from
    the other side: a JAVASCRIPT comment inside <script> on mobile/home.html explained the
    injection rule and quoted the mobile.css link tag verbatim. The guard below is a bare
    substring test, so it matched that explanation, concluded the page already had the shared
    stylesheet, and skipped the ENTIRE _MOBILE_HEAD block — which is how every C·A·R·D component
    silently vanished from /m/home while the page itself looked fine.

    An asset-presence test must not be fooled by a document TALKING about the asset. Script
    bodies and HTML comments are both blanked before the search, so neither prose nor code can
    disable the shared-asset layer again.

    The cheap substring test runs first and the strip only happens to CONFIRM a hit, so the
    common case (page does not have the tag) costs exactly what it did before."""
    if needle not in body:
        return False
    stripped = _INERT_COMMENT.sub(b"", _INERT_SCRIPT.sub(b"<script></script>", body))
    return needle in stripped


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    # cc#866: PROTECTED/_PWA_INJECT_PATHS are EXACT-match sets, but /preview/{name} is dynamic —
    # adding the literal '/preview' would gate the index and leave every screen ungated. The
    # prefix test below is what actually makes /preview/v8 redirect to /login when logged out.
    _is_preview = request.url.path == '/preview' or request.url.path.startswith('/preview/')
    if (request.url.path in PROTECTED or _is_preview) and not _is_authed(request):
        from fastapi.responses import RedirectResponse as _RR
        return _RR(url="/login")
    response = await call_next(request)
    path = request.url.path
    _prev = path == '/preview' or path.startswith('/preview/')   # cc#866
    # cc#1203: WEB vs APP, decided once. /m/* is the retail app and /preview/* is its review
    # surface; both are the black-and-gold contract and are pinned dark. Everything else is the
    # premium web site, which is what the token contract, the theme boot and the Theme pill are
    # for. Three separate injections below key off this one name so they cannot drift apart —
    # the failure mode being a page that gets the pill but not the boot, or neither control.
    _web = not (path.startswith("/m/") or _prev)
    do_logout = path in PROTECTED or _prev
    do_pwa = path in _PWA_INJECT_PATHS or _prev
    if (do_logout or do_pwa) and "text/html" in response.headers.get("content-type", ""):
        is_embed = _is_embedded(request)
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        if not is_embed:
            if do_logout:
                body = body.replace(b"</body>", _LOGOUT_BTN + b"</body>", 1)
            # cc#348: global theme switch. cc#1203 push 4: NOT on web pages any more — the shared
            # Theme pill now sits in the canonical top-nav, and shipping both would put two theme
            # controls on one screen, disagreeing about their labels (this one reads its own
            # localStorage with its own 'light' default; the pill reads ScorrTheme, which defaults
            # dark). The condition is the SAME one that decides whether the boot ships, written the
            # same way, so the two can never drift into a page with neither control.
            if (do_logout or do_pwa) and not _web and b'id="scorr-th"' not in body:
                body = body.replace(b"</body>", _THEME_BTN + b"</body>", 1)
            if do_pwa and b'src="/pwa.js"' not in body:
                body = body.replace(b"</body>", _PWA_TAG + b"</body>", 1)
            # cc#805: a page may carry its OWN early <script src="/scorr_card_common.js"> tag when it
            # needs the shared primitives at PARSE time — v8_dashboard.html does, because the injected
            # tags below are `defer` and (that document having no </head>) land at the END of <body>,
            # after its inline scripts have already called loadAll()/loadNews()/loadDataIntegrity().
            # Stamp that hardcoded tag with the build id here, so it can never serve a day-stale
            # cached bundle the way an unstamped max-age=86400 URL would.
            # cc#918: this stamped scorr_card_common.js ONLY, and five mobile templates
            # (v8, check, holdings, positions, screeners) hardcode the other five card files with a
            # plain src as well. Those are served max-age=86400, so a phone could hold a day-old
            # copy — and worse than merely stale: the hardcoded tags are BLOCKING and run before the
            # deferred injected ones, while every card file guards itself against double-init
            # (`if (window.ScorrCardStrip) return`). A cached copy therefore WINS and the fresh
            # injected one becomes a no-op. Stamping all six makes each hardcoded URL identical to
            # its injected twin, so it is both cache-busted and de-duplicated to one fetch.
            # cc#1021: v8_ladder_v2.js joins the list for the same reason — v8_dashboard.html
            # hardcodes it as a blocking tag, and an unstamped max-age=86400 URL would serve a
            # day-stale ladder after a deploy.
            # cc#1060 P0: index_tape_card.js joins the list, and it is here because leaving it out
            # broke the Index Intel tab in production. cc#1054 shipped the file with a HARDCODED
            # blocking tag in v8_dashboard.html and v10_dashboard.html but never added it here, so
            # the URL never changed while the response carried max-age=86400. Browsers that had
            # loaded the page between the cc#1054 and cc#1058 deploys kept serving the OLD module,
            # whose surface had no placeholder()/mountAll(). rIdxSpark() called placeholder() on it,
            # threw TypeError inside the template string, and render() died BEFORE assigning
            # #idx-app.innerHTML — so the pane sat on "Loading Index Intelligence…" forever. This is
            # the identical failure the cc#1021 note above predicts, one file later.
        # cc#1282: everything from HERE runs for EMBEDDED pages too. The embed guard above now
        # covers only the CHROME (logout pill, theme button, pwa.js nav) — an iframed page must
        # not carry a second navbar, but it absolutely needs the token stylesheets and the theme
        # boot, which this block injects. Before this change embed=1 stripped the ENTIRE shared
        # head, so every embedded surface (the cc#740 TC Scanner frame since day one) rendered
        # token-less — var(--bg)/var(--panel) unresolved, pages passing on browser defaults and
        # inline fallbacks alone. Found while embedding /digest and /trades, whose palettes live
        # entirely in the injected contract and would have arrived blank.
        for _js in (b"scorr_card_common.js", b"scorr_card_strip.js", b"scorr_chart_card.js",
                    b"scorr_analysis_card.js", b"results_card.js", b"scorr_cockpit_card.js",
                    b"v8_ladder_v2.js", b"index_tape_card.js",
                    # cc#1061: added WITH their script tags, in the same commit, so the
                    # cc#1060 failure cannot repeat for these two.
                    b"scrub_layer.js", b"pcr_trend_card.js",
                    # cc#1129: the shared news row. Hardcoded as a blocking tag in BOTH
                    # mobile/home.html and scorr_digest_mobile.html and served max-age=86400,
                    # so it is added here in the SAME change as those tags — the cc#1060
                    # lesson, applied at the time rather than after the outage.
                    b"scorr_news_row.js",
                    # cc#1191: added WITH its script tag in the same commit. This file is
                    # served max-age=86400 like its neighbours, so without the stamp a reader
                    # who loaded a page before this deploy keeps the old bundle for a day —
                    # which for a brand-new file means the segment chip does nothing when
                    # clicked, with no error to explain it. The cc#1060 failure, pre-empted.
                    b"scorr_segment_results.js"):
            body = body.replace(b'src="/' + _js + b'"',
                                b'src="/' + _js + b'?v=' + _BUILD_B + b'"')
        # APP_QA_R4 P2: mobile/home.html hardcodes the theme token layer as a <link href>,
        # and the loop above only rewrites src= attributes. /static/scorr_themes.css is
        # served max-age=86400, so without a stamp a returning phone could hold yesterday's
        # palette for a full day — the cc#1060 failure exactly, one asset later.
        for _css in (b"scorr_themes.css", b"scorr_appshell.css"):
            body = body.replace(b'href="/static/' + _css + b'"',
                                b'href="/static/' + _css + b'?v=' + _BUILD_B + b'"')
        # cc#1066 · THE SAME HOLE, ON THE JS SIDE OF /static. The loop above stamps
        # /static/*.css and the loop before it stamps root-relative /*.js — but NOTHING stamped
        # /static/*.js, and scorr_appshell.js is served with max-age=86400 from an unchanging
        # URL. That is the cc#1060 failure exactly: a returning phone keeps yesterday's file for
        # a full day after a deploy. It is hardcoded as a blocking tag in three templates
        # (scorr_digest_mobile, scorr_v10_signal, scorr_gvm_fightcard) and it is the file that
        # carries cc#1119's theme switcher and the back control, so a stale copy means the
        # founder deploys a theme row and does not see one.
        # This is a REAL cause of "deploys do not reach my phone" and it is NOT the service
        # worker — see the cc#1066 result for why the SW was already correct.
        for _sjs in (b"scorr_appshell.js",):
            body = body.replace(b'src="/static/' + _sjs + b'"',
                                b'src="/static/' + _sjs + b'?v=' + _BUILD_B + b'"')
        # cc#327: shared mobile design system into <head> (fallback: end of document)
        # cc#821 P0 — this used a bare `b"</head>" in body` substring test. v8_dashboard.html has
        # no closing-head tag, but a cc#805 COMMENT explaining that fact contained the literal
        # string. The test matched it, and all six shared tags plus the mobile.css link were
        # injected INSIDE that comment, where they are inert. One cause, all three reported
        # symptoms: no mobile.css so the light theme never applied; no scorr_card_strip/chart/
        # analysis/cockpit so C fell back to the page-local overlay while A/R/D opened nothing
        # (their local definitions having moved out in cc#805); and it is why cc#816's stamp
        # changed nothing — a commented-out tag cannot be cache-busted.
        # Matches inside HTML comments are now skipped, so no future comment can disable the
        # entire shared-asset layer by mentioning a tag name.
        # cc#914: was a bare `not in body` test — see _present_in_markup for what that cost.
        if not _present_in_markup(body, b'href="/static/mobile.css"'):
            _at = _find_outside_comments(body, b"</head>")
            if _at < 0:
                _at = _find_outside_comments(body, b"</body>")
            if _at >= 0:
                _head = _MOBILE_HEAD
                # cc#1203: the WEB token contract + the theme boot, on WEB PATHS ONLY.
                # /m/* and /preview/* are the app's black-and-gold contract (cc#1193) and are
                # pinned dark by _MOBILE_APP_DARK below — giving them a light-capable boot
                # would be two rules fighting over one attribute on every app page load.
                #
                # ORDER MATTERS TWICE OVER. The stylesheet goes in BEFORE the boot so the
                # tokens exist when the attribute lands, and both go in before the page's own
                # <style>, so a page can still override a token locally while it is being
                # migrated in pushes 5-14 and simply stops needing to.
                if _web:
                    _head = _head + _WEB_TOKENS_LINK + _THEME_BOOT
                # cc#1193 SHARED_CSS_RULE_V1: the app-only half of the R5 theme, injected on
                # APP SURFACES ONLY. It must come AFTER _MOBILE_HEAD, which is where
                # scorr_theme_r5.css is linked — same rules, same specificity, later sheet.
                #
                # /preview/* is included alongside /m/* deliberately. Previews are live mobile
                # review screens served by preview_endpoints and given this same PWA injection
                # a few lines up (`do_pwa = path in _PWA_INJECT_PATHS or _prev`), and two of
                # the moved selectors — .chd and .ix — render there. Injecting only on /m/
                # would have left previews/home_v2.html and previews/digest.html unstyled,
                # which is the one visible way this change could have gone wrong.
                if path.startswith("/m/") or _prev:
                    _head = _head + _THEME_MOBILE_LINK + _APP_THEME_RESOLVE
                if path.startswith("/m/"):
                    _head = _head + _MOBILE_APP_DARK   # cc#1064: dark-only surface, stamped honestly
                body = body[:_at] + _head + body[_at:]
        headers = dict(response.headers)
        headers["content-length"] = str(len(body))
        headers["cache-control"] = "no-store, no-cache, must-revalidate"
        headers["pragma"] = "no-cache"
        return Response(content=body, status_code=response.status_code,
                        headers=headers, media_type="text/html")
    return response

# cc#712: GZip added AFTER auth_gate so it is the OUTERMOST middleware — it compresses the FINAL
# response body (incl. the auth_gate logout/theme/pwa injection) and all >1KB API JSON. HTML pages
# keep their no-store cache-control (set in auth_gate); gzip only touches content-encoding/length.
app.add_middleware(GZipMiddleware, minimum_size=1000)

@app.middleware("http")
async def perf_request_log_middleware(request: Request, call_next):
    """cc#1272 scope 1: log request performance metrics (path, method, status, response time).
    Async logging via asyncio.create_task() to avoid blocking response times.
    Table: perf_request_log(id, path, method, status_code, response_time_ms, user_agent, created_at)."""
    start_time = time.time()
    path = request.url.path
    method = request.method
    user_agent = request.headers.get("user-agent", "")
    response = await call_next(request)
    duration_ms = (time.time() - start_time) * 1000
    status_code = response.status_code
    asyncio.create_task(_log_perf(path, method, status_code, duration_ms, user_agent))
    return response

async def _log_perf(path, method, status_code, duration_ms, user_agent):
    """Non-blocking database insert for performance metrics. Errors logged but never raised."""
    try:
        with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO perf_request_log (path, method, status_code, response_time_ms, user_agent) VALUES (%s, %s, %s, %s, %s)",
                    (path, method, status_code, duration_ms, user_agent)
                )
            conn.commit()
    except Exception as e:
        logging.warning(f"Failed to log perf data: {e}")

# cc#712: serve HTML pages from an in-memory cache — read each file once, then from the dict. A new
# deploy is a fresh process, so it naturally reloads (no TTL needed). Removes per-request disk reads.
_HTML_CACHE = {}
def _page(filename):
    html = _HTML_CACHE.get(filename)
    if html is None:
        with open(filename, "r", encoding="utf-8") as f:
            html = f.read()
        _HTML_CACHE[filename] = html
    return html

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
# app.include_router(qsr_router)   # cc#1442: QSR RETIRED — every /api/qsr/* route now 404s. Code + qsr_* tables preserved intact (unmount, not delete)
app.include_router(bt6_router)   # cc#544: /api/bt6/* V6 BT playground
app.include_router(results_router)   # cc#572: /api/results/card (Results R-card backend)
app.include_router(pcr_router)
app.include_router(pcr_mood_router)   # cc#1568
app.include_router(v8_replay_router)
app.include_router(backtest_router)
app.include_router(v8_backfill_router)
app.include_router(v8_gapfill_router)   # cc#1048: POST /api/v8/backfill/metrics_gapfill
app.include_router(index_tape_router)   # cc#1054: GET /api/index/tape
app.include_router(v10_page_router)     # cc#1069: GET /m/v10 (mobile V10 signal view)
app.include_router(volume_flow_router)  # cc#1368: GET /api/volume-flow (green vs red bar volume)
app.include_router(trade_alerts_router)  # cc#1503: /api/alerts/create|list|dismiss (manual trade alerts)
app.include_router(github_ops_router)   # cc#1249: /api/admin/github_* — the extraction from
                                       # File 5/5 that was never actually wired
app.include_router(mobile_cards_router)  # cc#1090 P0: card depth prototype (own router, wiring only)
app.include_router(gvm_twopager_router)   # cc#1085 R6-P1: GVM 2-Pager print route (own router, wiring only)
app.include_router(mcp_router)
app.include_router(anthropic_router)
app.include_router(scorr_router)
app.include_router(scorr_chat_router)
app.include_router(trade_check_v34_router)
app.include_router(tc_v4_router)
app.include_router(tc_v4_dual_router)   # cc#386
app.include_router(tc_v4_scan_router)   # cc#387
app.include_router(tc_scanner_config_router)   # cc#1222
app.include_router(check_router)
app.include_router(tc_sim_router)   # cc#748: /api/tc-sim/*
app.include_router(client_index_router)   # cc#758: /api/admin/client-index/*
app.include_router(sector_router)
app.include_router(sector_brief_router)
app.include_router(ops_metrics_router)   # cc#523
app.include_router(ops_peer_benchmark_router)   # cc#593: /api/ops-peer/* peer-benchmark compute
app.include_router(result_corner_router)   # cc#602: /api/admin/result_corner/* news-vs-calendar
app.include_router(engine_watchdog_router)   # cc#599: /api/watchdog/gaps engine watchdog
app.include_router(result_corner_page_router)   # cc#603: /api/result-corner page API
app.include_router(result_analysis_gen_router)   # cc#602: /api/admin/result_analysis/regenerate
app.include_router(scheduler_master_router)   # cc#525
app.include_router(investment_check_router)
app.include_router(invest_check_v2_router)   # cc#1174: /api/investment-check-v2 (+/batch, /weights)
app.include_router(scanner_router)
app.include_router(intraday_scanner_router)   # cc#481: restored
app.include_router(tc_scanner_router)         # cc#464: TC Scanner
app.include_router(structure_router)
from deriv_metrics import deriv_router          # cc#346: DERIVATIVE COCKPIT data layer
app.include_router(deriv_router)
from nse_eod_ingest import nse_eod_router       # cc#517: NSE EOD ingest suite (delivery/FII-DII/participant OI/F&O ban)
app.include_router(nse_eod_router)
from nse_fo_eod import fo_eod_router            # cc#682: NSE F&O bhavcopy -> EOD open-interest ingest (permanent OI fallback)
app.include_router(fo_eod_router)
from ops_control_plane import control_plane_router   # cc#693: ops control plane (registries + job_runs spine + diagnosis)
app.include_router(control_plane_router)
app.include_router(performance_router)
app.include_router(scheduler_health_router)
app.include_router(news_router)
app.include_router(stock_views_funnel_router)  # cc#787: FUNNEL 2 — /api/news/stock_views/feed
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
app.include_router(fy_end_backfill_router)   # cc#703 FY-end price backfill 2015-2021
app.include_router(fyers_hist_backfill_router)   # cc#377 Phase B
app.include_router(fundamentals_scraper_router)   # cc#361 Phase 1 scrape
app.include_router(screeners_router)   # cc#824 predefined screeners
app.include_router(v13_presets_router)
app.include_router(mf_pipeline_router)   # cc#466: /api/v15/mf/*
app.include_router(galaxy_router)
app.include_router(hr_router)   # cc#398 Portfolio Health Report (ingest)
app.include_router(hr_report_router)   # cc#398 Portfolio Health Report (report engine)
app.include_router(hr_report_pdf_router)   # cc#652 Portfolio Health Report white-label PDF
app.include_router(max_ivr_router)   # cc#836: /api/max/ivr/* (guided CIO tree, config-driven)
app.include_router(max_cards_router)   # cc#836 phase B: /api/max/card/{intent} (native, $0)
app.include_router(pivot_star_router)   # cc#856: /api/v8/pivot_star
app.include_router(preview_router)   # cc#866: /preview + /preview/{name}
app.include_router(mobile_router)    # cc#874: promoted mobile screens — all logic in mobile_endpoints.py
# cc#885: futures-basis open book. Its own file and its own route ON PURPOSE — /api/paper/status
# is untouched, so the Equity path stays byte-identical and issues no extra query.
app.include_router(v8_futures_book_router)
# cc#893 item 3: relocated out of the preview_endpoints tail shim. Order matters only in that
# these must be included after mobile_router, which owns /static/mobile_app.css and the shared
# helpers both modules import.
app.include_router(mobile_home2_router)
app.include_router(v8_book_canon_router)
app.include_router(v8_era_router)   # cc#1604: era caption + suspension flag, one source
app.include_router(v8_daylog_extras_router)   # cc#1561: Day Log P&L series + return facts
app.include_router(mobile_ext_router)
app.include_router(trade_wall_router)   # cc#991: /api/tradewall + /m/trades + /trades
app.include_router(model_launcher_router)   # cc#860: /api/models/status
app.include_router(heatstrip_router)   # cc#842: /api/global/heatstrip* · cc#849: /api/global/chart/{sym}?tf=
app.include_router(chart_peers_router)   # cc#845: /api/chart/peers/{symbol}
app.include_router(digest_v3_router)   # cc#846: /digest + /api/digest/v3
app.include_router(yahoo_resolver_router)   # cc#938: /api/admin/yahoo/resolve · /api/feeds/price-excluded · /api/feeds/symbol-map
app.include_router(room_router)   # cc#1086: /room + /api/room/feed
app.include_router(ondemand_bars_router)   # cc#1103: /api/bars/{symbol} + /api/bars/_cache/stats
app.include_router(tc_screener_v2_router)   # cc#1172: /api/trade-check/screen-v2 + /api/admin/run-tc-screener-v2
app.include_router(tc_position_stars_v2_router)   # cc#1172: /api/trade-check/position-stars-v2 + admin run
app.include_router(tc_score_replay_router)   # cc#1211: /api/tc/replay/* + one-shot /api/admin/run-tc-replay
app.include_router(basket_rebalance_router)  # cc#1273: /api/adaptive/baskets/* (available/subscribe/repair)
app.include_router(inv_scanner_router)  # cc#1283: /api/inv-scanner/universe + admin one-shot
app.include_router(inv_scanner_scoring_router)  # cc#1284: /api/inv-scanner/scores + admin one-shot
app.include_router(inv_scanner_rules_router)  # cc#1285: /api/inv-scanner/signals + /state + admin one-shot
app.include_router(inv_scanner_page_router)  # cc#1286: /inv-scanner page + /api/inv-scanner/board

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
    ALTER TABLE hr_portfolios ADD COLUMN IF NOT EXISTS alpha_start_date DATE;   -- cc#653: per-portfolio alpha window start
    CREATE TABLE IF NOT EXISTS hr_holdings (
        id BIGSERIAL PRIMARY KEY, portfolio_id BIGINT REFERENCES hr_portfolios(id) ON DELETE CASCADE,
        symbol TEXT, company_name TEXT, qty NUMERIC, avg_price NUMERIC,
        resolved BOOLEAN DEFAULT FALSE, raw_input JSONB, created_at TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_hr_holdings_portfolio ON hr_holdings(portfolio_id);
    -- cc#1013: portfolio-level invested/cash/tracking-date meta (see migrations/hr_portfolio_meta.sql).
    -- Headline P&L basis when per-holding buy prices are unknown; CREATE only, never ALTER hr_*.
    CREATE TABLE IF NOT EXISTS hr_portfolio_meta (
        portfolio_id INTEGER PRIMARY KEY REFERENCES hr_portfolios(id) ON DELETE CASCADE,
        invested_amount NUMERIC, cash NUMERIC DEFAULT 0, tracking_date DATE,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    -- cc#1338: broker/account id per portfolio, for the /adaptive shelf's (i) button. Same
    -- side-table shape as hr_portfolio_meta just above; CREATE only, never ALTER hr_*.
    CREATE TABLE IF NOT EXISTS hr_portfolio_broker (
        portfolio_id INTEGER PRIMARY KEY REFERENCES hr_portfolios(id) ON DELETE CASCADE,
        broker TEXT, account_id TEXT, updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    -- cc#654: realised (sold) lots for a portfolio -> realised P&L in the Health Report. Populated
    -- externally (Claude web); CREATE IF NOT EXISTS here so build_report's SUM never fails on a fresh DB.
    CREATE TABLE IF NOT EXISTS hr_realised (
        id BIGSERIAL PRIMARY KEY, portfolio_id BIGINT, fullname TEXT, qty NUMERIC,
        buy_rate NUMERIC, sell_rate NUMERIC, buy_date DATE, sell_date DATE,
        purchase_value NUMERIC, sale_value NUMERIC, gainloss NUMERIC, st_gain NUMERIC, lt_gain NUMERIC
    );
    CREATE INDEX IF NOT EXISTS idx_hr_realised_portfolio ON hr_realised(portfolio_id);
    -- cc#658 CA_WATCHDOG: corporate actions (split/bonus/demerger/rights/special-dividend) per symbol.
    -- Cross-checked against raw_prices cliffs so a >33% single-day drop WITH a matching ex_date is a
    -- feed-adjustment artefact (auto-restate via cc#657) vs a genuine crash (review-only).
    CREATE TABLE IF NOT EXISTS corporate_actions (
        id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, action_type TEXT NOT NULL,
        ex_date DATE, ratio_text TEXT, source TEXT, detected_at TIMESTAMP DEFAULT NOW(),
        UNIQUE(symbol, action_type, ex_date)
    );
    CREATE INDEX IF NOT EXISTS idx_corporate_actions_exdate ON corporate_actions(ex_date);
    CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol ON corporate_actions(symbol);
    -- cc#592: v8_history_cache DROPPED — one-time 05-Jun-2026 build, never refreshed, zero live
    -- readers (superseded by universe_technicals cc#154 + fyers_hist cc#377; 52W-breakout reads
    -- universe_technicals). CREATE removed so the weekend Railway-console DROP is not recreated.
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
    # cc#841 part_2: startup one-shots DO fire on every deploy, but nothing recorded them, so they
    # showed "never run" in scheduler_master forever — an instrumentation gap masquerading as four
    # dead jobs. _recorded() wraps each one so the registry sees the truth. Never raises: a recorder
    # failure must not become a startup failure.
    def _recorded(job_name, coro_fn):
        async def _wrapped():
            import time as _t
            t0 = _t.time()
            status, err = "ok", None
            try:
                await coro_fn()
            except Exception as e:
                status, err = "error", str(e)[:400]
                raise
            finally:
                try:
                    import scheduler_master
                    scheduler_master.record_run(job_name, status, error=err,
                                                duration_ms=int((_t.time() - t0) * 1000))
                except Exception as _re:
                    log.warning(f"record_run({job_name}) failed: {_re}")
        return _wrapped

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

    t0 = asyncio.create_task(_recorded("init_tables", _init_tables)())
    _BG_TASKS.add(t0); t0.add_done_callback(_BG_TASKS.discard)
    t1 = asyncio.create_task(_recorded("auto_fill_briefs", _auto_fill_briefs)())
    _BG_TASKS.add(t1); t1.add_done_callback(_BG_TASKS.discard)
    t2 = asyncio.create_task(_recorded("v8_paper_rebuild_cutover", _v8_paper_rebuild_cutover)())
    _BG_TASKS.add(t2); t2.add_done_callback(_BG_TASKS.discard)
    scheduler.start_background(app, BASE_URL, ADMIN_TOKEN)
    # cc#745: ensure the pcr_daily quality columns exist + backfill the marker across history on boot
    # (so the digest §5 read never errors on a missing column and the 27-Jul false 0.002 is flagged
    # immediately, without waiting for the next PCR compute).
    try:
        import pcr_backfill, psycopg
        with psycopg.connect(os.getenv("DATABASE_URL", "")) as _pc:
            res = pcr_backfill.mark_pcr_quality(_pc)
        log.info(f"cc#745 pcr quality backfill: {res}")
    except Exception as _pe:
        log.warning(f"cc#745 pcr quality backfill on startup failed: {_pe}")
    # cc#758: idempotently encrypt client_index credentials in place, IF CLIENT_INDEX_ENC_KEY is set.
    # No-op (single SELECT per column) once everything is already enc:-prefixed; silently skips when the
    # key isn't configured yet (nothing is exposed meanwhile — the scrub_row denylist + no readers).
    try:
        import client_index_security, psycopg
        if client_index_security.key_present():
            with psycopg.connect(os.getenv("DATABASE_URL", "")) as _cc:
                res = client_index_security.migrate_encrypt(_cc)
            log.info(f"cc#758 client_index encrypt-migrate: {res}")
    except Exception as _ce:
        log.warning(f"cc#758 client_index encrypt-migrate on startup failed: {_ce}")
    log.info(f"Scorr API v{VERSION} started — DEPLOY_GUARD={DEPLOY_GUARD}")

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # cc#886: the front door. cc#874 wired eleven /m/* screens and no card ever repointed the
    # entry, so the installed app still opened this desktop page. The decision lives in
    # mobile_endpoints.wants_mobile_home (phone UA + top-level navigation only); this route stays
    # wiring. Desktop is untouched — no UA match, no redirect.
    if wants_mobile_home(request):
        return RedirectResponse("/m/home", status_code=302)
    return _page("scorr_home.html")

@app.get("/status")
def status(): return {"service": "Scorr API", "version": VERSION, "status": "live"}

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return _page("v8_dashboard.html")

@app.get("/cio", response_class=HTMLResponse)
def cio():
    return _page("scorr_cockpit.html")

@app.get("/cio2", response_class=HTMLResponse)
def cio2():
    return _page("scorr_cio_dashboard.html")

@app.get("/ask", response_class=HTMLResponse)
def ask():
    return _page("scorr_ask.html")

@app.get("/check", response_class=HTMLResponse)
def check():
    return _page("scorr_check.html")

@app.get("/intraday", response_class=HTMLResponse)   # cc#481: restored (cc#476 kill reversed)
def intraday():
    return _page("scorr_intraday.html")

@app.get("/sector", response_class=HTMLResponse)
def sector():
    return _page("scorr_sector.html")

@app.get("/fpc", response_class=HTMLResponse)
def fpc():
    return _page("fpc_v11.html")

@app.get("/scanners", response_class=HTMLResponse)
def scanners():
    return _page("scorr_scanners.html")

@app.get("/filters")
def filters_page():
    """cc#393: the Unified Screener is folded into /v13 (registry + live screener). Permanent
    redirect keeps old bookmarks working. scorr_filters.html kept one release for rollback."""
    return RedirectResponse(url="/v13", status_code=301)

@app.get("/structure", response_class=HTMLResponse)
def structure_page():
    return _page("scorr_structure.html")

@app.get("/performance", response_class=HTMLResponse)
def performance():
    return _page("scorr_performance.html")

@app.get("/quant-basket", response_class=HTMLResponse)
def quant_basket():
    return _page("quant_basket.html")

@app.get("/news", response_class=HTMLResponse)
def news_page():
    return _page("scorr_news.html")

@app.get("/v10", response_class=HTMLResponse)
def v10_dashboard_page():
    return _page("v10_dashboard.html")

@app.get("/v9", response_class=HTMLResponse)
def v9_pairs_page():
    """cc#426: V9 · Pairs — sector-neutral long-short concept, extracted from the V8 dashboard tab
    into its own page (same renderer + /api/v8/v9_pairs_sectors pool)."""
    return _page("scorr_v9.html")

@app.get("/v14", response_class=HTMLResponse)
def v14_intraday_page():
    """cc#442: V14 intraday engine (paper) — live open positions with tag chips, closed-trade log,
    per-tag day summary. Data from /api/v14/*."""
    return _page("scorr_v14.html")

@app.get("/v15", response_class=HTMLResponse)
def v15_mf_page():
    """cc#467: V15 MF Intelligence skeleton — curated screener + fund deep-dive (look-through holdings
    scored on GVM, NAV-derived returns, external ratings). MQS scoring next session. Data from /api/v15/mf/*."""
    return _page("scorr_v15.html")

@app.get("/holdings", response_class=HTMLResponse)
def holdings_page():
    """SmartGain MHK40 holdings — gated by single password (scorr_auth PROTECTED set)."""
    return _page("scorr_holdings.html")

@app.get("/result-corner", response_class=HTMLResponse)
def result_corner_page():
    """cc#603: Result Corner — reported companies (newest first) with mcap-tier filter, GVM verdict,
    and a result snapshot. Reads /api/result-corner (result_corner.py)."""
    return _page("scorr_result_corner.html")

@app.get("/scheduler-master", response_class=HTMLResponse)
def scheduler_master_page():
    """cc#525: Master Scheduler Registry -- every scheduled job (AST-enumerated from
    scheduler.py, not hand-maintained docs), last run/status, drift-audited daily. Reads
    /api/scheduler/master (scheduler_master.py)."""
    return _page("scorr_scheduler_master.html")

@app.get("/screeners", response_class=HTMLResponse)
def screeners_page():
    """cc#824: predefined screens as a standing destination — EOD-computed member tables, no Run
    buttons. The screens themselves live ONLY here; /v13 stays the filter registry + ad-hoc
    screener (founder 02-Aug)."""
    return _page("scorr_screeners.html")

@app.get("/v13", response_class=HTMLResponse)
def v13_filter_registry_page():
    """cc#384: V13 filter registry — reality-verified inventory of every platform metric."""
    return _page("scorr_v13.html")

@app.get("/v4scan")
def tc_v4_scan_page():
    """cc#399: TC v4 merged into /check (Future Scans). /v4scan now 301-redirects to /check."""
    return RedirectResponse(url="/check", status_code=301)

@app.get("/v12", response_class=HTMLResponse)
def v12_builder_page():
    """cc#394: V12 Quant Basket Builder — 5-step wizard (universe/entry/exit/backtest/deploy)."""
    return _page("scorr_v12.html")

@app.get("/health", response_class=HTMLResponse)
def health_report_page():
    """cc#398: Portfolio Health Report — upload holdings -> Scorr-native 13-section report."""
    return _page("scorr_health.html")

@app.get("/adaptive", response_class=HTMLResponse)
def adaptive_dashboard_page():
    """cc#651: Adaptive Dashboard — client-facing shelf of saved Portfolio Health Reports."""
    return _page("scorr_adaptive.html")

# ── NAV_REGISTRY (cc#397, rule id=2987) — every GET-HTML route -> nav label -> status ──────────────
# STATUS: nav = in cockpit web nav + cio dashboard nav + mobile launcher; redirect = 301s away;
# INTERNAL = deliberately not in nav (test/dev). Keep this in sync when adding a page (nav-complete
# shipping rule: a page is not "done" until it is routed + in BOTH navs, collision-free, cache-safe).
NAV_REGISTRY = {
    # cc#995 (founder 10-Aug): Previews DE-LISTED from the nav (removed from the NAV array). The
    # /preview route + preview_endpoints.py stay \u2014 reachable by typed URL only, like /holdings.
    "/preview":      ("Previews \u2014 de-listed, reachable by typed URL", "typed-url"),
    # cc#874: promoted mobile screens. Mirrored here so the registry cannot drift from the live
    # NAV array (rule 2987). One entry per WIRED screen only — an unwired screen must never
    # appear in the registry, or the registry starts claiming pages that do not exist.
    # cc#882 item 5: the status now states the FORM FACTOR too. These carry the 'm' flag in the
    # NAV array, so they render in the mobile More sheet and are filtered OUT of the desktop top
    # bar — a retail screen must never sit on the professional web nav (16915). "nav-mobile" is
    # the registry saying which bar an entry actually appears on, so the registry cannot claim a
    # desktop destination that the desktop never renders.
    "/m/intel":      ("Intel (mobile)",       "nav-mobile"),
    "/m/positions":  ("Open Book (mobile)",   "nav-mobile"),
    "/m/qb":         ("Baskets (mobile)",     "nav-mobile"),
    "/m/gvm":        ("GVM (mobile)",         "nav-mobile"),
    "/m/v8":         ("V8 (mobile)",          "nav-mobile"),
    "/m/check":      ("Trade Check (mobile)", "nav-mobile"),
    "/m/home":       ("Home (mobile)",        "nav-mobile"),
    "/m/digest":     ("Daily Digest (mobile)", "nav-mobile"),
    # APP_QA_R4 P11: mirrors the NAV array entry added in pwa_endpoints.py (rule 2987).
    "/m/v10":        ("Index Intel (mobile)",  "nav-mobile"),
    # cc#1506 gave Alerts the app bar's 4th slot; cc#1535 (founder 31-Aug) moved it to the home
    # grid tile 1 (the approve surface leads the grid) and gave the slot to WoT. Still in the
    # More sheet for injected pages — nothing stranded (rule 2987).
    "/m/alerts":     ("Alerts (mobile)",       "grid+more-sheet"),
    "/m/results":    ("Results (mobile)",      "nav-mobile"),
    "/m/models":     ("Models — de-listed, reachable by typed URL", "typed-url"),   # cc#995: removed from nav (route + ScorrModels stay)
    # cc#991: Wall of Trades, TWO routes, one endpoint. cc#1535/cc#1536 (founder 31-Aug) ended
    # the original off-nav placement: the app screen took the bottom-nav WoT slot (Alerts moved
    # to the home grid tile 1), and the web page joined the desktop top nav next to the new
    # /alerts page (Alerts leads, WoT adjacent). Both remain injected + PROTECTED above.
    "/m/trades":     ("Wall of Trades (mobile)", "bottom-nav"),   # cc#1535: WoT slot
    "/trades":       ("Wall of Trades (web)",    "nav"),          # cc#1536: adjacent to /alerts
    "/alerts":       ("Alerts (web)",            "nav"),          # cc#1536: the approve surface
    "/inv-scanner":  ("Invest Scan",              "nav"),   # cc#1286
    # /m/login is a page, not a destination — it is reached by being logged out, never by tapping
    # a nav item, so it carries no NAV entry and is not PROTECTED. Recorded here so the registry
    # accounts for every /m/ route rather than only the navigable ones.
    "/m/login":      ("Login (mobile)",        "typed-url"),
    # cc#1086: mirrors the NAV array entry in pwa_endpoints.py (rule 2987).
    "/room":         ("Fable Room",           "nav"),
    "/":             ("Home",                 "nav"),
    "/dashboard":    ("V8",                   "nav"),
    "/cio":          ("Max (AI CIO)",         "nav"),
    "/cio2":         ("GVM (?model=gvm)",     "nav"),
    # cc#853: /digest is reachable from the V8 TAB ROW (after V6 BT), not the site nav — founder
    # 04-Aug. Still injected + PROTECTED above; only its discovery point moved.
    "/digest":       ("Daily Digest · V8 tab row (after V6 BT)", "v8-tab"),   # cc#846 -> cc#853
    "/ask":          ("(removed from nav — superseded by Max)", "typed-url"),   # cc#435
    "/check":        ("Check",                "nav"),
    "/screeners":    ("Screeners",            "nav"),   # cc#824
    "/intraday":     ("(-> /dashboard#tcscan · TC Scanner tab; page kept for the tab's iframe embed)", "typed-url"),   # cc#740
    "/dashboard#tcscan": ("TC Scanner · V8 tab — reachable via the V8 tab bar / deep link", "typed-url"),   # cc#740; cc#822 removed from nav
    "/sector":       ("Sector",               "nav"),
    "/fpc":          ("FPC",                  "nav"),
    "/scanners":     ("(removed from nav — superseded by V12/V13/Check)", "typed-url"),   # cc#441
    "/structure":    ("(removed from nav — superseded)", "typed-url"),   # cc#437
    "/performance":  ("(removed from nav — superseded)", "typed-url"),   # cc#437
    "/quant-basket": ("QB (curated Quant Basket)", "nav"),
    # cc#1523: Intel de-listed from the site nav — now a V8 tab-row embed pane (the cc#853 Digest
    # placement). Route unchanged, still PROTECTED + injected; /m/intel app entry untouched.
    "/news":         ("Intel · V8 tab row (after Wall of Trades)", "v8-tab"),
    "/dashboard#intel": ("Intel (V8 tab)", "tab"),   # cc#1523 rule id=2987
    "/v10":          ("(-> /dashboard#index · Index Intel tab; standalone retired)", "typed-url"),   # cc#542
    "/v9":           ("V9 · Pairs",           "nav"),        # cc#426 rule id=2987 (extracted from V8 tab)
    "/v14":          ("(-> /dashboard#v14 · V14 Intraday tab; standalone retired)", "typed-url"),   # cc#543
    # cc#851: /dashboard#digest retired — the V8 Digest pane is gone and the hash now
    # redirects to /digest, which is the single Digest entry (see NAV above).
    "/dashboard#index": ("Index Intel (V8 tab)", "nav"),     # cc#542 rule id=2987 (folded into V8)
    # cc#1207: hidden from the nav — the engine is silent, so the tab is revealed only by a
    # direct #v14 link. Route and pane both still work; it is off the menu, not retired.
    "/dashboard#v14":   ("V14 · Intraday (V8 tab, hidden — direct link only)", "typed-url"),  # cc#543/#1207
    "/dashboard#bt":    ("V6 BT — V8 tab-only deep-link (removed from top nav)", "tab"),  # cc#551: dropped from NAV, reachable via the V8 tab bar only
    "/v15":          ("V15 · MF",             "nav"),        # cc#467 rule id=2987 (MF intelligence)
    "/scheduler-master": ("Scheduler Master",  "nav"),        # cc#525: scheduled-job registry + drift audit
    "/result-corner":    ("Result Corner",     "nav"),        # cc#603: reported-companies tier board
    "/holdings":     ("Holdings — route unchanged, typed URL only", "typed-url"),   # cc#822 removed from nav
    "/v13":          ("V13 · Registry & Screener", "nav"),
    "/v4scan":       ("(-> /check · Future Scans)", "redirect"),   # cc#399 301
    "/v12":          ("V12 · Quant Basket Builder — QB-page button (removed from top nav)", "tab"),  # cc#557: folded into /quant-basket
    "/screener":     ("(-> /v13 · RETIRED)",  "redirect"),   # cc#407 301 (V13 = single screener)
    "/health":       ("Health Report — route unchanged, typed URL + Adaptive masthead button only", "typed-url"),   # cc#1520 removed from nav (cc#822 pattern)
    "/adaptive":     ("Adaptive Dashboard",   "nav"),        # cc#651 rule id=2987
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

            # cc#658 part_6: data-integrity card — CA watchdog / cliff status from the daily notes.
            report["sections"]["data_integrity"] = {"checks": [], "grade": "A"}
            try:
                import ca_watchdog
                di = ca_watchdog.data_integrity_status()
                add_check("data_integrity", _check(di.get("ca_headline") or "no note yet", "CA daily note",
                          lambda v: ("ALL CLEAR" in str(v)) or ("no note" in str(v))))
                add_check("data_integrity", _check(di.get("genuine_flags", 0), "Genuine-crash flags",
                          lambda v: (v or 0) == 0))
                add_check("data_integrity", _check(di.get("master_headline") or "no note yet", "Master watchdog",
                          lambda v: ("ALL CLEAR" in str(v)) or ("no note" in str(v))))
            except Exception as e:
                add_check("data_integrity", _check(f"err:{str(e)[:60]}", "CA watchdog", lambda v: False))

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

# cc#851: the v2.3 digest renderer (_digest_domestic_live, _build_digest_daily) and its route
# /api/digest/daily are DELETED. Daily Digest V3 (digest_v3.py) is now the only digest — one
# renderer, one data path, served at /digest + /api/digest/v3. This also clears a long-standing
# rule-4 violation: ~160 lines of section-building logic living in main.py, which is wiring only.

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

_ALLOWED_CONTENT_FIELDS = {"overview", "key_takeaway", "result_analysis", "fy27_outlook"}   # cc#572
_TOP500_ONLY_FIELDS = {"key_takeaway", "result_analysis"}
_FIELD_TO_TS_COL = {
    "overview": "last_overview_updated",
    "key_takeaway": "last_takeaway_updated",
    "result_analysis": "last_result_analysis_updated",
    "fy27_outlook": "last_fy27_outlook_updated",   # cc#572: manual override for the FY27 outlook
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
        # cc#592: v8_history_cache removed from the feed-health monitor (dead table, false 46d-stale alert)
        ("global_indices","SELECT MAX(quote_date), COUNT(DISTINCT symbol) FROM global_indices"),
        ("adr_daily","SELECT MAX(price_date), COUNT(*) FROM adr_daily"),
        ("pcr_daily","SELECT MAX(price_date), COUNT(*) FROM pcr_daily"),
        ("quant_positions","SELECT MAX(updated_at)::date, COUNT(*) FROM quant_paper_positions WHERE status='open'"),
        ("futures_basis","SELECT MAX(ts)::date, COUNT(*) FROM futures_basis"),
        ("option_chain","SELECT MAX(ts)::date, COUNT(*) FROM option_chain"),
        # cc#1194 scope 4: the two live intraday tables that were BOTH at zero rows all of
        # 21-Aug while every other row on this page looked healthy. Two list entries, no logic —
        # main.py stays wiring.
        #
        # The count is scoped to the LATEST DAY, not the lifetime total every row above uses.
        # That is the whole point: adr_intraday holds ~2,000 rows of history, so a lifetime
        # count reads reassuringly large on a day that produced nothing. Latest-day count next
        # to latest date says "2026-08-20, 70 rows" on a Friday and the gap is visible at a
        # glance instead of being averaged away by history.
        ("adr_intraday","SELECT MAX(ts)::date, COUNT(*) FROM adr_intraday "
                        "WHERE ts::date = (SELECT MAX(ts)::date FROM adr_intraday)"),
        ("pcr_intraday","SELECT MAX(ts)::date, COUNT(*) FROM pcr_intraday "
                        "WHERE ts::date = (SELECT MAX(ts)::date FROM pcr_intraday)"),
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
    # cc#580 fault_4: surface the LIVE v8_metrics compute age (minutes since MAX(computed_at)) — not
    # just the day-level score_date above — and flag stale if >10 min during market hours (09:15-15:30
    # IST). This is the exact signal the writer watchdog uses, so a silent writer freeze (20-Jul,
    # frozen ~5h) now shows RED on the feeds health page instead of looking fresh.
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td, time as _tt
        with get_conn() as conn, conn.cursor() as cur:
            # cc#1194 scope 5: BOTH SIDES IN IST. computed_at is a naive `timestamp` holding IST
            # wall-clock; bare NOW() is timestamptz on a UTC Railway session, and subtracting the
            # two made Postgres read the stored IST clock AS UTC. The age came out exactly 330
            # minutes short — measured on the live table: 1753.7 as shipped against 2083.7 correct.
            # The threshold below is 10 minutes, so a writer had to be frozen for 340 minutes
            # before this could say stale — longer than the 09:15-15:30 session itself. The check
            # cc#580 built for a five-hour freeze could not report one. feed_guardian already
            # converts to IST for the same reason (cc#1022); this was the last raw subtraction.
            cur.execute("SELECT EXTRACT(EPOCH FROM "
                        "((NOW() AT TIME ZONE 'Asia/Kolkata') - MAX(computed_at)))/60.0 "
                        "FROM v8_metrics")
            r = cur.fetchone()
        age_min = round(float(r[0]), 1) if r and r[0] is not None else None
        now_ist = _dt.now(_tz(_td(hours=5, minutes=30))).replace(tzinfo=None)
        mkt = now_ist.weekday() < 5 and _tt(9, 15) <= now_ist.time() <= _tt(15, 30)
        stale = mkt and (age_min is None or age_min > 10)
        out.append({"source": "v8_metrics_compute", "latest": None, "records": None,
                    "compute_age_min": age_min, "market_hours": mkt,
                    "freshness": "stale" if stale else "ok", "days_old": None,
                    "note": "live 5-min tick freshness (MAX computed_at); >10min in market hours = writer stalled"})
    except Exception as e:
        out.append({"source": "v8_metrics_compute", "error": str(e)})
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

# /api/v8/live_metrics moved to v8_endpoints.py (cc#1565) — rule 4, main.py is wiring only.

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

@app.post("/api/admin/restate_symbols")
async def restate_symbols_now(symbols: str = "", lookback: str = "5y", detect: bool = False,
                              x_admin_token: Optional[str] = Header(None)):
    """cc#657: full 5y adjusted re-pull for corporate-action-polluted symbols (one/sec). Pass an explicit
    comma/space list, or detect=true to auto-select the current cliff backlog. Synchronous — returns the
    per-symbol report (bars / dates / residual_cliffs)."""
    _check_admin(x_admin_token)
    import yahoo_daily_update as ydu
    syms = [s.strip().upper() for s in (symbols or "").replace(",", " ").split() if s.strip()]
    if detect and not syms:
        syms = await asyncio.to_thread(ydu.detect_cliff_symbols)
    if not syms:
        return {"error": "provide symbols (comma/space separated) or detect=true"}
    report = await asyncio.to_thread(ydu.restate_symbol_history, syms, lookback)
    return {"count": len(report), "restated": len([r for r in report if r.get("status") == "restated"]),
            "report": report}

@app.get("/api/data-integrity/status")
def data_integrity_status():
    """cc#658 part_6: compact data-integrity status (latest CA note + master note headlines) for the
    /health card and the V8 dashboard strip. Red when any unresolved anomaly."""
    import ca_watchdog
    return ca_watchdog.data_integrity_status()

@app.post("/api/admin/ca_run")
async def ca_run(action: str = "daily_note", x_admin_token: Optional[str] = Header(None)):
    """cc#658: run a CA-watchdog job on demand — action=daily_note|master_note|weekly_scan|forward_heal."""
    _check_admin(x_admin_token)
    import ca_watchdog
    fn = {"daily_note": ca_watchdog.ca_daily_note, "master_note": ca_watchdog.master_watchdog_note,
          "weekly_scan": ca_watchdog.weekly_distortion_scan, "forward_heal": ca_watchdog.forward_heal,
          "sweep_daily": ca_watchdog.run_ca_sweep_daily,
          "sweep_full": ca_watchdog.run_ca_sweep}.get(action)
    if not fn:
        return {"error": "action must be daily_note|master_note|weekly_scan|forward_heal|sweep_daily|sweep_full"}
    return await asyncio.to_thread(fn)

@app.post("/api/admin/ca_sweep")
async def ca_sweep_symbols(symbols: str = "", x_admin_token: Optional[str] = Header(None)):
    """cc#658 part_1: scrape corporate actions for an explicit symbol list (comma/space) — used to
    verify known events (LICI/VEDL/TRENT/TRIVENI) populate corporate_actions."""
    _check_admin(x_admin_token)
    import ca_watchdog
    syms = [s.strip().upper() for s in (symbols or "").replace(",", " ").split() if s.strip()]
    if not syms:
        return {"error": "provide symbols"}
    return await asyncio.to_thread(ca_watchdog.run_ca_sweep, syms)

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

def _canon_retired():
    """The retired-basket registry (cc#970). Wiring only — the list lives in app_config."""
    from v8_book_canon import _conn as _c, retired_baskets
    with _c() as _cn, _cn.cursor() as _cu:
        names, _ = retired_baskets(_cu)
    return names


def _canon_summary(era):
    """Trade Log / Master Dashboard summary, shaped as the dashboard already expects, with every
    figure taken from v8_book_canon. No formula here (rule 13: one formula, one place)."""
    from v8_book_canon import _conn as _c, book_canon
    with _c() as _cn:
        b = book_canon(_cn, era=era)
    return {"trades": b["trades"], "wins": b["wins"], "losses": b["losses"],
            "total_pnl": b["realised"], "gross_pnl": b["gross"], "brokerage": b["brokerage"],
            "decided": b["decided"], "win_rate": b["win_rate"],
            "era": b["era"], "canon": b["canon"], "retired_baskets": b["retired_baskets"]}


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
    # cc#1604: era gate, read once per request from app_config (v8_era is the one place that reads it)
    from v8_era import full_ledger_allowed as _fla, suspended_payload as _sp, era_block as _eb
    from v8_book_canon import _conn as _era_conn
    with _era_conn() as _ec, _ec.cursor() as _ecur:
        _ledger_ok = _fla(_ecur)
        _era_suspended = _sp(_ecur)
        _era_block = _eb(_ecur)
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
            WHERE symbol = p.symbol AND {NOT_FUT_SQL} ORDER BY ts DESC LIMIT 1
        ) lp ON true
        LEFT JOIN LATERAL (
            SELECT close AS prev_close FROM raw_prices
            WHERE symbol = p.symbol
              AND price_date < COALESCE(lp.ts::date, (NOW() AT TIME ZONE 'Asia/Kolkata')::date)
            ORDER BY price_date DESC LIMIT 1
        ) pc ON true
        WHERE p.status = 'OPEN' AND {era_clause} AND NOT (p.basket = ANY(%s))
        ORDER BY p.entry_ts DESC
    """, era_params + (_canon_retired(),))
    # cc#751: the Trade Log tab is the COMPLETE trade ledger (all baskets, all time) — the era filter
    # (cc#504 fresh-book) applies only to the headline paper P&L / open positions above, NOT to the
    # Trade Log. all_trades/all_summary are the un-era'd full history the Trade Log renders from, so a
    # basket that stopped running pre-cutover (e.g. sell_momentum, 11 closed trades) still shows with a
    # P&L that matches its own list (both computed from the SAME full-history set). recent_trades/summary
    # (era) are kept unchanged for the fresh-book headline consumers.
    return {
        "open_positions": open_positions,
        "recent_trades": api_query(
            f"SELECT symbol,side,basket,entry_price,exit_price,pnl,return_pct,result,entry_ts,exit_ts "
            f"FROM v8_paper_trades WHERE {era_clause} AND NOT (basket = ANY(%s)) "
            f"ORDER BY closed_at DESC LIMIT 100", era_params + (_canon_retired(),)),
        # cc#970 (rule 13): retired baskets vanish from every P&L display INCLUDING history, so the
        # all-era trade list is filtered too — buy_s1_bounce's 10 rows (+67,154) used to sit in here
        # and in all_summary below, which is why the Trade Log tab and the Master Dashboard
        # disagreed on 09-Aug.
        # cc#1604 V8_ERA_CUTOVER_ONLY_V1: while the full ledger is suspended, all_trades is the
        # cutover-era list (no LIMIT window, cc#1583 finding) and all_summary is the suspended
        # payload — the pre-cutover aggregate is never computed. Flip app_config
        # v8_full_ledger_suspended=false to restore the old keys; no deploy.
        "all_trades": (api_query(
            "SELECT symbol,side,basket,entry_price,exit_price,pnl,return_pct,result,entry_ts,exit_ts "
            "FROM v8_paper_trades WHERE NOT (basket = ANY(%s)) ORDER BY closed_at DESC LIMIT 500",
            (_canon_retired(),)) if _ledger_ok else api_query(
            f"SELECT symbol,side,basket,entry_price,exit_price,pnl,return_pct,result,entry_ts,exit_ts "
            f"FROM v8_paper_trades WHERE {era_clause} AND NOT (basket = ANY(%s)) ORDER BY closed_at DESC",
            era_params + (_canon_retired(),))),
        "missed": api_query("SELECT miss_date,symbol,side,basket,expected_entry,reason FROM v8_paper_missed ORDER BY ts DESC LIMIT 100"),
        # cc#970: BOTH summaries are the canon now. They used to be two hand-written aggregates
        # here — the fresh one shipped total_pnl GROSS (no brokerage) and neither excluded a
        # retired basket. `summary` is the canonical fresh book; `all_summary` is history, still
        # with retired baskets removed, and carries era='all' so the tab can label it.
        "summary": _canon_summary("fresh"),
        "all_summary": (_canon_summary("all") if _ledger_ok else _era_suspended),
        "rebuild_cutover_ts": cutover_ts,
        "era_block": _era_block,             # cc#1604: caption "Since 18-Jul-2026", served not typed
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

# ── cc#1249 · THE GITHUB ENDPOINTS MOVED OUT, AND THEY SHOULD HAVE BEEN OUT ALREADY ────────────
# github_ops.py opens with "Extracted from main.py (File 5/5 split)". The extraction was DONE and
# the wiring never happened: main.py kept its own copy of all four handlers and github_ops was
# never imported, so for months there were two implementations and the dead one was the one being
# improved. cc#1185 P8 wired the theme gate into github_ops.github_push and reported it as live on
# "the one push path this app owns" — it was not live at all, because that function was never
# reachable. That claim was wrong and this is where it gets corrected.
# The router is included with the others; the duplicates below are deleted rather than left to rot,
# because a second copy is exactly how this happened. _check_admin STAYS here — it is used by ~30
# other admin endpoints in this file and has nothing to do with GitHub.
def _check_admin(token):
    if not ADMIN_TOKEN: return True
    if token != ADMIN_TOKEN: raise HTTPException(403,"Invalid admin token")
    return True


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
