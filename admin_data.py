"""
Admin data-loader endpoints — Screener earnings scraper + Google Drive CSV loaders.

Extracted from main.py (refactor file 3/5, 04-Jun-2026). All endpoints here are
HTTP-only (never called in-process by the scheduler — the scheduler's earnings
job calls /api/admin/load_earnings_from_screener over HTTP, not these functions
directly). Self-contained: own _conn, _check_admin. Imports _sql_clean_replace_screener
from gvm_nightly (same module main.py uses).

GitHub helpers and ADR/PCR compute intentionally NOT moved — GitHub helpers are the
deploy lifeline; ADR/PCR compute is on the scheduler's direct call path. Both move
in file 5 with the scheduler.

Endpoints:
  POST /api/admin/load_input_from_drive      — reload input_raw from Drive CSV
  POST /api/admin/load_screener_from_drive   — reload screener_raw (WIDE) from Drive CSV
  POST /api/admin/load_earnings_from_screener — scrape Screener.in upcoming results
"""

from fastapi import APIRouter, HTTPException, Request, Header
from datetime import datetime, date, timedelta
from typing import Optional
import os
import io
import re
import json
import logging
import psycopg
import httpx
import pandas as pd
from bs4 import BeautifulSoup

from gvm_nightly import _sql_clean_replace_screener

log = logging.getLogger("scorr.admin_data")

router = APIRouter(tags=["admin_data"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

SCREENER_BASE = "https://www.screener.in"
SCREENER_LOGIN_URL = f"{SCREENER_BASE}/login/"
SCREENER_UPCOMING_URL = f"{SCREENER_BASE}/upcoming-results/"


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


# ── Google Drive CSV loaders ────────────────────────────────────────────────────

async def _drive_download(file_id):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
        r = await c.get(url); r.raise_for_status(); return r.text


@router.post("/api/admin/load_input_from_drive")
async def load_input(req: Request):
    body = await req.json(); file_id = body.get("file_id")
    if not file_id: raise HTTPException(400, "file_id required")
    csv_text = await _drive_download(file_id); df = pd.read_csv(io.StringIO(csv_text)); rows = df.to_dict(orient="records")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM input_raw")
        for row in rows: cur.execute("INSERT INTO input_raw (data) VALUES (%s::jsonb)", (json.dumps(row, default=str),))
        conn.commit()
    return {"status": "ok", "rows": len(rows)}


@router.post("/api/admin/load_screener_from_drive")
async def load_screener(req: Request):
    body = await req.json(); file_id = body.get("file_id")
    if not file_id: raise HTTPException(400, "file_id required")
    csv_text = await _drive_download(file_id); df = pd.read_csv(io.StringIO(csv_text))
    n = _sql_clean_replace_screener(df.to_dict(orient="records"))
    return {"status": "ok", "action": "clean_replace_wide", "rows_loaded": n}


# ── Screener.in earnings scraper ─────────────────────────────────────────────────

def _parse_screener_date(s):
    if not s: return None
    s = str(s).strip()
    if not s or s.lower() in ("nan", "none", "-", "n/a"): return None
    for fmt in ["%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b", "%d %B"]:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=date.today().year)
                if dt.date() < date.today() - timedelta(days=30): dt = dt.replace(year=date.today().year + 1)
            return dt.date()
        except ValueError:
            continue
    return None


async def _screener_login_session():
    email = os.getenv("SCREENER_EMAIL", "").strip(); password = os.getenv("SCREENER_PASSWORD", "").strip()
    if not email or not password: raise HTTPException(500, "SCREENER creds missing")
    client = httpx.AsyncClient(follow_redirects=True, timeout=60, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "en-US,en;q=0.9"})
    r = await client.get(SCREENER_LOGIN_URL); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser"); csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if not csrf_input: await client.aclose(); raise HTTPException(500, "CSRF token not found")
    r = await client.post(SCREENER_LOGIN_URL, data={"csrfmiddlewaretoken": csrf_input.get("value"), "username": email, "password": password, "next": ""}, headers={"Referer": SCREENER_LOGIN_URL})
    if "sessionid" not in client.cookies: await client.aclose(); raise HTTPException(401, "Screener login failed")
    return client


async def _scrape_upcoming_results(client):
    r = await client.get(SCREENER_UPCOMING_URL); r.raise_for_status(); html = r.text
    if "/login/" in str(r.url) or "Login to your account" in html: raise HTTPException(401, "Screener session expired")
    soup = BeautifulSoup(html, "html.parser"); tables = soup.find_all("table")
    if not tables: raise HTTPException(500, "No table found")
    rows_out = []; seen_tickers = set()
    for tbl in tables:
        try:
            dfs = pd.read_html(io.StringIO(str(tbl)))
        except Exception:
            continue
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


@router.post("/api/admin/load_earnings_from_screener")
async def load_earnings_from_screener(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); client = await _screener_login_session()
    try:
        rows = await _scrape_upcoming_results(client)
    finally:
        await client.aclose()
    if not rows: return {"status": "warn", "rows_scraped": 0}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM earnings_calendar"); inserted = 0
        for r in rows:
            try:
                cur.execute("INSERT INTO earnings_calendar (company_name,ticker,ex_date,record_date,event_type) VALUES (%(company_name)s,%(ticker)s,%(ex_date)s,%(record_date)s,%(event_type)s)", r); inserted += 1
            except Exception as e:
                log.warning(f"row skip: {e}")
        conn.commit()
    return {"status": "ok", "rows_scraped": len(rows), "rows_inserted": inserted}
