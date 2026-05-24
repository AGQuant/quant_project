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
import httpx
import pandas as pd

# ============================================================
# Scorr / Project Quant — main.py v1.2.3
# Adds: _parse_result_dates + earnings_calendar loader
#       GitHub read/write/list/delete endpoints + MCP tools
#       Last manual VS Code push — all future deploys from chat
# ============================================================

VERSION = "1.2.3"

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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# DB HELPERS
# ============================================================

def get_conn():
    return psycopg.connect(DATABASE_URL)

def create_tables():
    """Idempotent table creation on every cold start."""
    sql = """
    CREATE TABLE IF NOT EXISTS gvm_scores (
        symbol TEXT PRIMARY KEY,
        run_date DATE,
        g NUMERIC, v NUMERIC, m NUMERIC, gvm NUMERIC,
        verdict TEXT, sector TEXT, market_cap NUMERIC,
        full_data JSONB
    );
    CREATE TABLE IF NOT EXISTS raw_prices (
        symbol TEXT, date DATE, open NUMERIC, high NUMERIC,
        low NUMERIC, close NUMERIC, volume BIGINT,
        PRIMARY KEY (symbol, date)
    );
    CREATE TABLE IF NOT EXISTS input_raw (id SERIAL PRIMARY KEY, data JSONB);
    CREATE TABLE IF NOT EXISTS screener_raw (id SERIAL PRIMARY KEY, data JSONB);
    CREATE TABLE IF NOT EXISTS sector_ratings (
        sector TEXT PRIMARY KEY, run_date DATE,
        avg_gvm NUMERIC, mcap_weighted_gvm NUMERIC,
        stock_count INT, verdict TEXT
    );
    CREATE TABLE IF NOT EXISTS momentum_scores (
        symbol TEXT PRIMARY KEY, run_date DATE,
        return_1y NUMERIC, return_3y NUMERIC,
        dma50 NUMERIC, dma200 NUMERIC, vs_nifty NUMERIC,
        m_score NUMERIC
    );
    CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, data JSONB);
    CREATE TABLE IF NOT EXISTS signals (id SERIAL PRIMARY KEY, data JSONB);

    CREATE TABLE IF NOT EXISTS v5_signals (
        id SERIAL PRIMARY KEY,
        signal_type TEXT,
        timestamp TEXT,
        symbol TEXT,
        finkhoz_rating NUMERIC,
        record_price NUMERIC,
        cap_type TEXT,
        current_price NUMERIC,
        return_pct NUMERIC,
        hit_alert TEXT,
        analyst_verdict TEXT,
        alert_count INT,
        alert_types TEXT,
        event_date DATE,
        event_type TEXT,
        verdict_date DATE,
        loaded_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS v5_futures_open (
        id SERIAL PRIMARY KEY,
        symbol TEXT, open_date TEXT, type TEXT,
        qty NUMERIC, entry_price NUMERIC, current_price NUMERIC,
        net_pl_pct NUMERIC, profit_per_lot NUMERIC, value NUMERIC,
        remarks TEXT, status TEXT,
        loaded_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS v5_trades (
        id SERIAL PRIMARY KEY, data JSONB,
        loaded_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS v5_portfolio (
        id SERIAL PRIMARY KEY, data JSONB,
        loaded_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS v5_baskets (
        id SERIAL PRIMARY KEY, basket_name TEXT, data JSONB,
        loaded_at TIMESTAMP DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS earnings_calendar (
        id SERIAL PRIMARY KEY,
        company_name TEXT,
        ticker TEXT,
        ex_date TEXT,
        record_date TEXT,
        event_type TEXT,
        loaded_at TIMESTAMP DEFAULT NOW()
    );
    """
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()
        log.info("Tables ready")
    except Exception as e:
        log.error(f"create_tables failed: {e}")

# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():
    create_tables()
    log.info(f"Scorr API v{VERSION} started — DEPLOY_GUARD={DEPLOY_GUARD}")

# ============================================================
# ROOT / HEALTH
# ============================================================

@app.get("/")
def root():
    return {"service": "Scorr API", "version": VERSION, "status": "live"}

@app.get("/api/health")
def health():
    return {"status": "ok", "version": VERSION}

@app.get("/api/health/feeds")
def health_feeds():
    out = []
    queries = [
        ("gvm_scores", "SELECT MAX(run_date), COUNT(*) FROM gvm_scores"),
        ("raw_prices", "SELECT MAX(date), COUNT(DISTINCT symbol) FROM raw_prices"),
        ("screener_raw", "SELECT NULL, COUNT(*) FROM screener_raw"),
        ("input_raw", "SELECT NULL, COUNT(*) FROM input_raw"),
        ("sector_ratings", "SELECT MAX(run_date), COUNT(*) FROM sector_ratings"),
        ("momentum_scores", "SELECT MAX(run_date), COUNT(*) FROM momentum_scores"),
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
                if latest:
                    days_old = (date.today() - r[0]).days
                    freshness = "ok" if days_old < 7 else "stale"
                out.append({
                    "source": name, "latest": latest, "records": count,
                    "freshness": freshness, "days_old": days_old
                })
    except Exception as e:
        return {"error": str(e)}
    return {"checked_at": str(date.today()), "feeds": out}

# ============================================================
# DB QUERY HELPER
# ============================================================

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

# ============================================================
# GVM READ ENDPOINTS
# ============================================================

@app.get("/api/gvm/{symbol}")
def get_gvm(symbol: str):
    r = api_query("SELECT symbol, g, v, m, gvm, verdict, sector, market_cap, full_data FROM gvm_scores WHERE symbol = %s", (symbol.upper(),), single=True)
    if not r: raise HTTPException(404, f"{symbol} not found")
    return r

@app.get("/api/gvm/top/{n}")
def get_top(n: int, verdict: Optional[str] = None):
    n = min(max(n, 1), 100)
    if verdict:
        return api_query("SELECT symbol, g, v, m, gvm, verdict, sector, market_cap FROM gvm_scores WHERE verdict = %s ORDER BY gvm DESC LIMIT %s", (verdict, n))
    return api_query("SELECT symbol, g, v, m, gvm, verdict, sector, market_cap FROM gvm_scores ORDER BY gvm DESC LIMIT %s", (n,))

@app.get("/api/sector/{sector}")
def get_sector(sector: str):
    return api_query("SELECT symbol, g, v, m, gvm, verdict, market_cap FROM gvm_scores WHERE LOWER(sector) = LOWER(%s) ORDER BY gvm DESC", (sector,))

@app.get("/api/filter")
def get_filter(min_gvm: float = 0, max_gvm: float = 10):
    return api_query("SELECT symbol, g, v, m, gvm, verdict, sector, market_cap FROM gvm_scores WHERE gvm >= %s AND gvm <= %s ORDER BY gvm DESC", (min_gvm, max_gvm))

@app.get("/api/sectors")
def get_sectors():
    return api_query("SELECT sector, avg_gvm, mcap_weighted_gvm, stock_count, verdict FROM sector_ratings ORDER BY mcap_weighted_gvm DESC")

@app.get("/api/momentum/{symbol}")
def get_momentum(symbol: str):
    r = api_query("SELECT * FROM momentum_scores WHERE symbol = %s", (symbol.upper(),), single=True)
    if not r: raise HTTPException(404, f"{symbol} momentum not found")
    return r

# ============================================================
# DRIVE → DB LOADERS (existing)
# ============================================================

async def _drive_download(file_id: str) -> str:
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

# ============================================================
# V5 PARSERS
# ============================================================

def _v5_clean(v):
    if v is None: return ""
    s = str(v).strip()
    if s.lower() in ("nan", "none", "null", "#n/a", "#ref!"): return ""
    return s

def _v5_num(v):
    s = _v5_clean(v)
    if not s: return None
    s = s.replace(",", "").replace("%", "").replace("₹", "").strip()
    try:
        return float(s)
    except:
        return None

def _find_header(df, must_have_cols):
    for i in range(min(10, len(df))):
        vals = [str(v).strip() for v in df.iloc[i].values]
        if all(c in vals for c in must_have_cols):
            return i
    return None

def _parse_signal_tab(df, cap_type):
    rows = []
    hidx = _find_header(df, ["Timestamp", "Symbol"])
    if hidx is None: return rows
    df = df.copy()
    df.columns = [str(v).strip() for v in df.iloc[hidx].values]
    df = df.iloc[hidx+1:].reset_index(drop=True)
    for _, row in df.iterrows():
        sym = _v5_clean(row.get("Symbol"))
        if not sym or sym == "Symbol": continue
        rows.append({
            "signal_type":     "Alert",
            "timestamp":       _v5_clean(row.get("Timestamp")),
            "symbol":          sym,
            "finkhoz_rating":  _v5_num(row.get("Finkhoz Rating")),
            "record_price":    _v5_num(row.get("Record Price")),
            "cap_type":        cap_type,
            "current_price":   _v5_num(row.get("Current Price")),
            "return_pct":      _v5_num(row.get("Return %")),
            "hit_alert":       _v5_clean(row.get("Hit Alert")),
            "analyst_verdict": _v5_clean(row.get("Analyst Verdict")) or _v5_clean(row.get("Verdict")),
            "alert_count":     int(_v5_num(row.get("Alert Count")) or 0),
            "alert_types":     _v5_clean(row.get("Alert Type")),
            "event_date":      None,
            "event_type":      _v5_clean(row.get("Event Type")),
            "verdict_date":    None,
        })
    return rows

def _parse_futures_open(df):
    rows = []
    hidx = None
    for i, row in df.iterrows():
        vals = [str(v).strip() for v in row.values]
        if 'Open Date' in vals and 'Entry Price' in vals:
            hidx = i
            break
    if hidx is None: return rows
    df = df.copy()
    df.columns = [str(v).strip() for v in df.iloc[hidx].values]
    df = df.iloc[hidx+1:].reset_index(drop=True)
    for _, row in df.iterrows():
        sym = _v5_clean(str(row.iloc[1]))
        if not sym or sym.startswith('NIFTY') or sym in ('nan', '', 'Open positions in Future'):
            continue
        rows.append({
            "symbol":         sym,
            "open_date":      _v5_clean(row.get('Open Date')),
            "type":           _v5_clean(row.get('Type')),
            "qty":            _v5_num(row.get('Qty')),
            "entry_price":    _v5_num(row.get('Entry Price')),
            "current_price":  _v5_num(row.get('Current Price')),
            "net_pl_pct":     _v5_num(row.get('Net P/L%')),
            "profit_per_lot": _v5_num(row.get('Profit / Lot')),
            "value":          _v5_num(row.get('Value')),
            "remarks":        _v5_clean(row.get('Remarks')),
            "status":         _v5_clean(row.get('Status')),
        })
    return rows

def _parse_v5_screener(df, signal_type):
    """Parse Buy/Sell Reversal/Momentum tabs — screener matrix of NSE symbols."""
    rows = []
    seen = set()
    for _, row in df.iterrows():
        for val in row.values:
            s = str(val).strip()
            if s.startswith('NSE:') and '-EQ' in s:
                sym = s.replace('NSE:', '').replace('-EQ', '').strip()
                if sym and sym not in seen:
                    seen.add(sym)
                    rows.append({
                        "signal_type":     signal_type,
                        "timestamp":       str(date.today()),
                        "symbol":          sym,
                        "analyst_verdict": "Candidate",
                        "alert_count":     0,
                    })
    return rows

def _parse_trades(df):
    return [{"row": json.dumps(r, default=str)} for r in df.to_dict(orient="records")]

def _parse_portfolio(df):
    return [{"row": json.dumps(r, default=str)} for r in df.to_dict(orient="records")]

def _parse_result_dates(df):
    """Parse Result_Date tab → earnings_calendar.
       Columns: Company | TICKER | Ex Date | Record Date | Event Type"""
    rows = []
    for _, row in df.iterrows():
        ticker = _v5_clean(str(row.iloc[1]) if len(row) > 1 else '')
        if not ticker or ticker == 'TICKER': continue
        company = _v5_clean(str(row.iloc[0]) if len(row) > 0 else '')
        ex_raw = _v5_clean(str(row.iloc[2]) if len(row) > 2 else '')
        rec_raw = _v5_clean(str(row.iloc[3]) if len(row) > 3 else '')
        evt = _v5_clean(str(row.iloc[4]) if len(row) > 4 else '')
        if not ex_raw: continue
        rows.append({
            "company_name": company,
            "ticker":       ticker,
            "ex_date":      ex_raw,
            "record_date":  rec_raw,
            "event_type":   evt,
        })
    return rows

# ============================================================
# V5 BULK LOADER (Apps Script → Railway)
# ============================================================

@app.post("/api/admin/load_v5_from_drive")
async def load_v5_from_drive(req: Request):
    body = await req.json()
    file_ids = body.get("file_ids", {})
    if not file_ids:
        raise HTTPException(400, "file_ids required")

    FILE_MAP = {
        "v5_futures_open.csv":   ("v5_futures_open",    _parse_futures_open,                                       "1=1"),
        "v5_trades.csv":         ("v5_trades",          _parse_trades,                                              "1=1"),
        "v5_portfolio.csv":      ("v5_portfolio",       _parse_portfolio,                                           "1=1"),
        "v5_result_dates.csv":   ("earnings_calendar",  _parse_result_dates,                                        "1=1"),
        "v5_alerts_large.csv":   ("v5_signals",         lambda df: _parse_signal_tab(df, "Large Cap"),              "cap_type = 'Large Cap' AND signal_type = 'Alert'"),
        "v5_alerts_mid.csv":     ("v5_signals",         lambda df: _parse_signal_tab(df, "Mid Cap"),                "cap_type = 'Mid Cap' AND signal_type = 'Alert'"),
        "v5_alerts_small.csv":   ("v5_signals",         lambda df: _parse_signal_tab(df, "Small Cap"),              "cap_type = 'Small Cap' AND signal_type = 'Alert'"),
        "v5_buy_reversal.csv":   ("v5_signals",         lambda df: _parse_v5_screener(df, "Buy_Reversal"),          "signal_type = 'Buy_Reversal'"),
        "v5_buy_momentum.csv":   ("v5_signals",         lambda df: _parse_v5_screener(df, "Buy_Momentum"),          "signal_type = 'Buy_Momentum'"),
        "v5_sell_reversal.csv":  ("v5_signals",         lambda df: _parse_v5_screener(df, "Sell_Reversal"),         "signal_type = 'Sell_Reversal'"),
        "v5_sell_momentum.csv":  ("v5_signals",         lambda df: _parse_v5_screener(df, "Sell_Momentum"),         "signal_type = 'Sell_Momentum'"),
    }

    results = {}

    for fname, file_id in file_ids.items():
        if fname not in FILE_MAP:
            results[fname] = "skipped (no parser)"
            continue
        table, parser, where_clause = FILE_MAP[fname]
        try:
            csv_text = await _drive_download(file_id)
            df = pd.read_csv(io.StringIO(csv_text), header=None)
            rows = parser(df)
            if not rows:
                results[fname] = "0 rows parsed"
                continue
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute(f"DELETE FROM {table} WHERE {where_clause}")
                if table in ("v5_trades", "v5_portfolio"):
                    for r in rows:
                        cur.execute(f"INSERT INTO {table} (data) VALUES (%s::jsonb)", (r["row"],))
                elif table == "v5_signals":
                    for r in rows:
                        cur.execute("""
                            INSERT INTO v5_signals
                            (signal_type, timestamp, symbol, finkhoz_rating, record_price,
                             cap_type, current_price, return_pct, hit_alert, analyst_verdict,
                             alert_count, alert_types, event_date, event_type, verdict_date)
                            VALUES (%(signal_type)s, %(timestamp)s, %(symbol)s, %(finkhoz_rating)s, %(record_price)s,
                                    %(cap_type)s, %(current_price)s, %(return_pct)s, %(hit_alert)s, %(analyst_verdict)s,
                                    %(alert_count)s, %(alert_types)s, %(event_date)s, %(event_type)s, %(verdict_date)s)
                        """, {
                            "signal_type":     r.get("signal_type"),
                            "timestamp":       r.get("timestamp"),
                            "symbol":          r.get("symbol"),
                            "finkhoz_rating":  r.get("finkhoz_rating"),
                            "record_price":    r.get("record_price"),
                            "cap_type":        r.get("cap_type"),
                            "current_price":   r.get("current_price"),
                            "return_pct":      r.get("return_pct"),
                            "hit_alert":       r.get("hit_alert"),
                            "analyst_verdict": r.get("analyst_verdict"),
                            "alert_count":     r.get("alert_count"),
                            "alert_types":     r.get("alert_types"),
                            "event_date":      r.get("event_date"),
                            "event_type":      r.get("event_type"),
                            "verdict_date":    r.get("verdict_date"),
                        })
                elif table == "v5_futures_open":
                    for r in rows:
                        cur.execute("""
                            INSERT INTO v5_futures_open
                            (symbol, open_date, type, qty, entry_price, current_price,
                             net_pl_pct, profit_per_lot, value, remarks, status)
                            VALUES (%(symbol)s, %(open_date)s, %(type)s, %(qty)s, %(entry_price)s, %(current_price)s,
                                    %(net_pl_pct)s, %(profit_per_lot)s, %(value)s, %(remarks)s, %(status)s)
                        """, r)
                elif table == "earnings_calendar":
                    for r in rows:
                        cur.execute("""
                            INSERT INTO earnings_calendar
                            (company_name, ticker, ex_date, record_date, event_type)
                            VALUES (%(company_name)s, %(ticker)s, %(ex_date)s, %(record_date)s, %(event_type)s)
                        """, r)
                conn.commit()
            results[fname] = f"{len(rows)} rows → {table}"
        except Exception as e:
            results[fname] = f"error: {str(e)[:200]}"

    return {"status": "ok", "results": results}

# ============================================================
# GITHUB AUTO-DEPLOY (v1.2.3 new)
# ============================================================
# These endpoints let Claude read/write any file in the GitHub repo from chat.
# Authentication: X-Admin-Token header must match ADMIN_TOKEN env.
# Safety: DEPLOY_GUARD must be 'true' for any write operation.
# Recovery: All actions create commits — any change reversible via git revert.

GITHUB_API = "https://api.github.com"

def _gh_headers():
    if not GITHUB_TOKEN:
        raise HTTPException(500, "GITHUB_TOKEN not configured")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def _check_admin(token: Optional[str]):
    if not ADMIN_TOKEN:
        # If no admin token set, allow MCP-side calls only (MCP itself is OAuth-protected).
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True

def _check_deploy_guard():
    if not DEPLOY_GUARD:
        raise HTTPException(403, "DEPLOY_GUARD is off — writes disabled. Set DEPLOY_GUARD=true in Railway to enable.")

async def _gh_get_file(filepath: str) -> Dict[str, Any]:
    """Fetch a file's content + SHA from GitHub. Returns {content, sha, size, exists}."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=_gh_headers())
        if r.status_code == 404:
            return {"exists": False, "content": None, "sha": None, "size": 0}
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return {
            "exists": True,
            "content": content,
            "sha": data["sha"],
            "size": data["size"],
            "html_url": data.get("html_url"),
        }

async def _gh_put_file(filepath: str, new_content: str, commit_message: str, sha: Optional[str] = None) -> Dict[str, Any]:
    """Create or update a file via GitHub Contents API."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    payload = {
        "message": commit_message,
        "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.put(url, headers=_gh_headers(), json=payload)
        if r.status_code not in (200, 201):
            raise HTTPException(r.status_code, f"GitHub error: {r.text[:300]}")
        return r.json()

async def _gh_delete_file(filepath: str, commit_message: str, sha: str) -> Dict[str, Any]:
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    payload = {"message": commit_message, "sha": sha, "branch": "main"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request("DELETE", url, headers=_gh_headers(), json=payload)
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"GitHub delete error: {r.text[:300]}")
        return r.json()

async def _gh_list_tree(path_prefix: str = "") -> List[Dict[str, Any]]:
    """List files in the repo (root or under a path)."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path_prefix}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=_gh_headers())
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):  # single file response
            data = [data]
        return [{"name": x["name"], "path": x["path"], "type": x["type"], "size": x.get("size", 0)} for x in data]

# Read endpoint — no DEPLOY_GUARD check, reads are always safe
@app.get("/api/admin/github_read")
async def github_read(filepath: str, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    if not GITHUB_REPO:
        raise HTTPException(500, "GITHUB_REPO not configured")
    info = await _gh_get_file(filepath)
    if not info["exists"]:
        raise HTTPException(404, f"File not found: {filepath}")
    return {
        "filepath": filepath,
        "size": info["size"],
        "sha": info["sha"],
        "content": info["content"],
        "lines": info["content"].count("\n") + 1,
    }

@app.get("/api/admin/github_list")
async def github_list(path: str = "", x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    if not GITHUB_REPO:
        raise HTTPException(500, "GITHUB_REPO not configured")
    files = await _gh_list_tree(path)
    return {"path": path or "/", "items": files, "count": len(files)}

@app.post("/api/admin/github_push")
async def github_push(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    _check_deploy_guard()
    if not GITHUB_REPO:
        raise HTTPException(500, "GITHUB_REPO not configured")
    body = await req.json()
    filepath = body.get("filepath")
    new_content = body.get("new_content")
    commit_message = body.get("commit_message", f"chore: update {filepath}")
    create_if_missing = body.get("create_if_missing", True)
    if not filepath or new_content is None:
        raise HTTPException(400, "filepath and new_content required")

    existing = await _gh_get_file(filepath)
    if not existing["exists"] and not create_if_missing:
        raise HTTPException(404, f"File {filepath} does not exist and create_if_missing=false")
    if existing["exists"] and existing["content"] == new_content:
        return {"status": "noop", "message": "Content identical, no commit made", "filepath": filepath}

    sha = existing["sha"] if existing["exists"] else None
    result = await _gh_put_file(filepath, new_content, commit_message, sha)
    return {
        "status": "ok",
        "filepath": filepath,
        "action": "updated" if existing["exists"] else "created",
        "commit_sha": result.get("commit", {}).get("sha"),
        "commit_url": result.get("commit", {}).get("html_url"),
        "old_size": existing["size"],
        "new_size": len(new_content),
    }

@app.post("/api/admin/github_delete")
async def github_delete(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    _check_deploy_guard()
    if not GITHUB_REPO:
        raise HTTPException(500, "GITHUB_REPO not configured")
    body = await req.json()
    filepath = body.get("filepath")
    commit_message = body.get("commit_message", f"chore: delete {filepath}")
    if not filepath:
        raise HTTPException(400, "filepath required")
    existing = await _gh_get_file(filepath)
    if not existing["exists"]:
        raise HTTPException(404, f"File not found: {filepath}")
    result = await _gh_delete_file(filepath, commit_message, existing["sha"])
    return {
        "status": "ok",
        "filepath": filepath,
        "action": "deleted",
        "commit_sha": result.get("commit", {}).get("sha"),
    }

# ============================================================
# OAUTH (for Claude MCP client)
# ============================================================

_oauth_codes: Dict[str, Dict] = {}
_oauth_tokens: Dict[str, Dict] = {}

@app.get("/.well-known/oauth-authorization-server")
def oauth_metadata():
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "scopes_supported": ["read", "write"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
    }

@app.get("/.well-known/oauth-protected-resource")
def oauth_resource():
    return {
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL],
        "scopes_supported": ["read", "write"],
    }

@app.post("/oauth/register")
async def oauth_register(req: Request):
    body = await req.json()
    cid = secrets.token_urlsafe(16)
    return {
        "client_id": cid,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": body.get("redirect_uris", []),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }

@app.get("/oauth/authorize")
def oauth_authorize(client_id: str, redirect_uri: str, response_type: str = "code",
                    state: str = "", code_challenge: str = "", code_challenge_method: str = "",
                    scope: str = ""):
    code = secrets.token_urlsafe(24)
    _oauth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "created": time.time(),
    }
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}")

@app.post("/oauth/token")
async def oauth_token(req: Request):
    form = await req.form()
    code = form.get("code")
    if code not in _oauth_codes:
        raise HTTPException(400, "Invalid code")
    info = _oauth_codes.pop(code)
    token = secrets.token_urlsafe(32)
    _oauth_tokens[token] = {"client_id": info["client_id"], "created": time.time()}
    return {"access_token": token, "token_type": "Bearer", "expires_in": 31536000, "scope": "read write"}

# ============================================================
# MCP JSON-RPC SERVER
# ============================================================

MCP_TOOLS = [
    {"name": "get_gvm", "description": "Fetch full GVM score for a stock.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_top_stocks", "description": "Get top N stocks by GVM. Optional verdict filter.",
     "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}, "verdict": {"type": "string"}}, "required": ["n"]}},
    {"name": "get_sector", "description": "Get all stocks in a sector ordered by GVM.",
     "inputSchema": {"type": "object", "properties": {"sector": {"type": "string"}}, "required": ["sector"]}},
    {"name": "get_filter", "description": "Filter stocks by GVM range.",
     "inputSchema": {"type": "object", "properties": {"min_gvm": {"type": "number"}, "max_gvm": {"type": "number"}}, "required": []}},
    {"name": "get_sector_rating", "description": "Get sector-level mcap-weighted GVM ratings.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_momentum", "description": "Get momentum scores for a stock.",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "health_feeds", "description": "Status dashboard for all data feeds.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "run_sql", "description": "Run any SQL query on Railway PostgreSQL.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "load_input_from_drive", "description": "Reload input_raw table from a Google Drive CSV file ID.",
     "inputSchema": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}},
    {"name": "load_screener_from_drive", "description": "Reload screener_raw table from a Google Drive CSV file ID.",
     "inputSchema": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}},
    {"name": "get_v5_signals", "description": "Query V5 signals (Alert, Buy/Sell Reversal/Momentum). Optional filters by signal_type, cap_type, analyst_verdict.",
     "inputSchema": {"type": "object", "properties": {
         "signal_type": {"type": "string"}, "cap_type": {"type": "string"},
         "verdict": {"type": "string"}, "limit": {"type": "integer"}
     }, "required": []}},
    {"name": "check_blackout", "description": "Check if a symbol is in earnings blackout (today + T+1).",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_v5_portfolio", "description": "Get current V5 portfolio holdings.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "github_read", "description": "Read any file from the GitHub repo. Returns content + line count + size.",
     "inputSchema": {"type": "object", "properties": {"filepath": {"type": "string"}}, "required": ["filepath"]}},
    {"name": "github_list", "description": "List files in the repo (root or a folder). Empty path = root.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}},
    {"name": "github_push", "description": "Create or update a file in the repo. Triggers Railway redeploy automatically. Requires DEPLOY_GUARD=true.",
     "inputSchema": {"type": "object", "properties": {
         "filepath": {"type": "string"},
         "new_content": {"type": "string"},
         "commit_message": {"type": "string"},
         "create_if_missing": {"type": "boolean"}
     }, "required": ["filepath", "new_content", "commit_message"]}},
    {"name": "github_delete", "description": "Delete a file from the repo. Requires DEPLOY_GUARD=true.",
     "inputSchema": {"type": "object", "properties": {
         "filepath": {"type": "string"},
         "commit_message": {"type": "string"}
     }, "required": ["filepath"]}},
]

async def _call_tool(name: str, args: dict) -> Any:
    async with httpx.AsyncClient(timeout=60) as client:
        if name == "get_gvm":
            r = await client.get(f"{BASE_URL}/api/gvm/{args['symbol']}")
            return r.json()
        elif name == "get_top_stocks":
            params = {}
            if args.get("verdict"): params["verdict"] = args["verdict"]
            r = await client.get(f"{BASE_URL}/api/gvm/top/{args['n']}", params=params)
            return r.json()
        elif name == "get_sector":
            r = await client.get(f"{BASE_URL}/api/sector/{args['sector']}")
            return r.json()
        elif name == "get_filter":
            r = await client.get(f"{BASE_URL}/api/filter", params={"min_gvm": args.get("min_gvm", 0), "max_gvm": args.get("max_gvm", 10)})
            return r.json()
        elif name == "get_sector_rating":
            r = await client.get(f"{BASE_URL}/api/sectors")
            return r.json()
        elif name == "get_momentum":
            r = await client.get(f"{BASE_URL}/api/momentum/{args['symbol']}")
            return r.json()
        elif name == "health_feeds":
            r = await client.get(f"{BASE_URL}/api/health/feeds")
            return r.json()
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
            r = await client.post(f"{BASE_URL}/api/admin/load_input_from_drive", json={"file_id": args["file_id"]})
            return r.json()
        elif name == "load_screener_from_drive":
            r = await client.post(f"{BASE_URL}/api/admin/load_screener_from_drive", json={"file_id": args["file_id"]})
            return r.json()
        elif name == "get_v5_signals":
            conds = []
            vals = []
            if args.get("signal_type"):
                conds.append("signal_type = %s"); vals.append(args["signal_type"])
            if args.get("cap_type"):
                conds.append("cap_type = %s"); vals.append(args["cap_type"])
            if args.get("verdict"):
                conds.append("analyst_verdict = %s"); vals.append(args["verdict"])
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
            return {"symbol": sym, "events": [{"ex_date": r[1], "event_type": r[2]} for r in rows]}
        elif name == "get_v5_portfolio":
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT data FROM v5_portfolio ORDER BY id LIMIT 200")
                return {"rows": [r[0] for r in cur.fetchall()]}
        elif name == "github_read":
            r = await client.get(f"{BASE_URL}/api/admin/github_read", params={"filepath": args["filepath"]},
                                  headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {})
            return r.json()
        elif name == "github_list":
            r = await client.get(f"{BASE_URL}/api/admin/github_list", params={"path": args.get("path", "")},
                                  headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {})
            return r.json()
        elif name == "github_push":
            r = await client.post(f"{BASE_URL}/api/admin/github_push", json=args,
                                   headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {})
            return r.json()
        elif name == "github_delete":
            r = await client.post(f"{BASE_URL}/api/admin/github_delete", json=args,
                                   headers={"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {})
            return r.json()
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
            "serverInfo": {"name": "Scorr", "version": VERSION},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        try:
            result = await _call_tool(name, args)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}]
            }}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(e)}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return Response(status_code=204)
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}