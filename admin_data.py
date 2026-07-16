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

# cc#490: NSE's official corporate board-meetings feed — the canonical, machine-readable
# source for FORWARD-dated results. Screener.in's "upcoming-results" page only ever
# surfaced same-day rows (verified 16-Jul: 25 rows scraped, all ex_date=today, 0 future).
NSE_BASE = "https://www.nseindia.com"
NSE_BOARD_MEETINGS_URL = f"{NSE_BASE}/api/corporate-board-meetings?index=equities"


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


async def _fetch_nse_board_meetings():
    """cc#490: NSE's official corporate board-meetings feed, filtered to purpose containing
    "Financial Results" — official forward-dated results, unlike Screener's same-day-only
    upcoming-results page. Best-effort: NSE's anti-bot layer can block a datacenter IP, so
    any failure returns [] and the caller degrades gracefully rather than crashing (same
    cookie-seed pattern already proven working in global_indices.py's VIX fetch)."""
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{NSE_BASE}/companies-listing/corporate-filings-board-meetings"}
    try:
        async with httpx.AsyncClient(timeout=45, headers=hdrs, follow_redirects=True) as client:
            await client.get(NSE_BASE + "/")             # seed cookies (NSE anti-bot requirement)
            r = await client.get(NSE_BOARD_MEETINGS_URL)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning(f"NSE board-meetings fetch failed (best-effort, degrading gracefully): {e}")
        return []
    if not isinstance(data, list):
        log.warning(f"NSE board-meetings: unexpected response shape ({type(data).__name__}) — "
                    f"schema may have changed, sample: {str(data)[:300]}")
        return []
    rows_out = []
    for d in data:
        purpose = str(d.get("bm_purpose") or "")
        if "financial result" not in purpose.lower():
            continue
        ticker = str(d.get("bm_symbol") or "").strip().upper()
        ex_date = _parse_screener_date(d.get("bm_date"))
        if not ticker or not ex_date:
            continue
        rows_out.append({"company_name": str(d.get("sm_name") or ticker).strip(),
                          "ticker": ticker, "ex_date": ex_date, "record_date": None,
                          "event_type": "Financial Results"})
    return rows_out


def _upsert_earnings_row(cur, r, changed_at, verified):
    """cc#490: shared upsert/reschedule logic for one earnings_calendar row, factored out of
    refresh_earnings_calendar() so both the Screener scrape and the NSE board-meetings fetch
    go through identical accumulate/reschedule semantics (id=1770). Returns
    'inserted'/'updated'/'rescheduled'/'skipped'."""
    if r["ex_date"] is None:
        return "skipped"
    cur.execute("SELECT 1 FROM earnings_calendar WHERE ticker=%s AND ex_date=%s",
                (r["ticker"], r["ex_date"]))
    new_date_exists = cur.fetchone() is not None
    prior = None
    if not new_date_exists:
        # same ticker+event still 'upcoming' but at a DIFFERENT date => a reschedule
        cur.execute("""SELECT id, ex_date FROM earnings_calendar
                       WHERE ticker=%s AND event_type=%s AND status='upcoming'
                         AND ex_date <> %s ORDER BY ex_date DESC LIMIT 1""",
                    (r["ticker"], r["event_type"], r["ex_date"]))
        prior = cur.fetchone()
    if prior:
        prior_id, old_date = prior
        entry = {"old_date": str(old_date), "new_date": str(r["ex_date"]), "changed_at": changed_at}
        cur.execute("""UPDATE earnings_calendar
            SET ex_date=%s, event_type=%s, company_name=%s, verified=%s, last_updated=NOW(),
                reschedule_log = COALESCE(reschedule_log,'[]'::jsonb) || %s::jsonb
            WHERE id=%s""",
            (r["ex_date"], r["event_type"], r["company_name"], verified, json.dumps([entry]), prior_id))
        _oplog(cur, "alert", "earnings_reschedule", {"ticker": r["ticker"], **entry})
        return "rescheduled"
    cur.execute("""INSERT INTO earnings_calendar
        (company_name, ticker, ex_date, record_date, event_type, status, verified,
         first_seen, last_updated)
        VALUES (%(company_name)s,%(ticker)s,%(ex_date)s,%(record_date)s,%(event_type)s,
                'upcoming',%(verified)s,NOW(),NOW())
        ON CONFLICT (ticker, ex_date) DO UPDATE SET
            event_type=EXCLUDED.event_type,
            company_name=EXCLUDED.company_name,
            -- cc#490: NSE ('confirmed') is the canonical source — never let a later
            -- Screener re-scrape ('estimated') downgrade an already-confirmed row.
            verified=CASE WHEN earnings_calendar.verified='confirmed' THEN 'confirmed'
                          ELSE EXCLUDED.verified END,
            last_updated=NOW()
        RETURNING (xmax=0) AS was_insert""", {**r, "verified": verified})
    return "inserted" if cur.fetchone()[0] else "updated"


def _ensure_earnings_schema(cur):
    """cc#252 (spec 1770): idempotent V2 migration — unique key + lifecycle columns. Safe to
    run on every load (all IF-NOT-EXISTS / guarded). The unique (ticker, ex_date) key is what
    makes the upsert possible, so this MUST run before the loop."""
    cur.execute("ALTER TABLE earnings_calendar ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'upcoming'")
    cur.execute("ALTER TABLE earnings_calendar ADD COLUMN IF NOT EXISTS verified TEXT DEFAULT 'estimated'")
    cur.execute("ALTER TABLE earnings_calendar ADD COLUMN IF NOT EXISTS first_seen TIMESTAMP DEFAULT NOW()")
    cur.execute("ALTER TABLE earnings_calendar ADD COLUMN IF NOT EXISTS last_updated TIMESTAMP")
    cur.execute("ALTER TABLE earnings_calendar ADD COLUMN IF NOT EXISTS reschedule_log JSONB DEFAULT '[]'::jsonb")
    cur.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_ticker_exdate') THEN
            ALTER TABLE earnings_calendar ADD CONSTRAINT uq_ticker_exdate UNIQUE (ticker, ex_date);
        END IF;
    END $$;""")


def _oplog(cur, category, title, details):
    """Best-effort ops_log write (never fails the loader)."""
    try:
        cur.execute("INSERT INTO ops_log (category, title, details) VALUES (%s,%s,%s::jsonb)",
                    (category, title, json.dumps(details)))
    except Exception as e:
        log.warning(f"ops_log {title}: {e}")


def _earnings_lifecycle(cur):
    """cc#252/#420: after each scrape — age past-date 'upcoming' rows to 'reported'. cc#420: the
    purge is REMOVED — earnings_calendar is the permanent announcement-date archive from Jul-2026
    onward; 'reported'/'analyzed' rows are NEVER deleted (rescheduled rows keep their reschedule_log)."""
    cur.execute("""UPDATE earnings_calendar SET status='reported', last_updated=NOW()
                   WHERE status='upcoming' AND ex_date < CURRENT_DATE""")
    reported = cur.rowcount
    purged = 0   # cc#420: never purge reported/analyzed rows (permanent archive)
    return reported, purged


async def refresh_earnings_calendar():
    """cc#252 (spec 1770): scrape Screener.in upcoming results and ACCUMULATE into
    earnings_calendar via UPSERT — never wipe (the old DELETE+INSERT orphaned today's results
    and kept no history). Adds new rows, refreshes changed ones, logs reschedules, ages past
    events to 'reported', purges reported/analyzed >60d, and writes an ops_log
    (title=earnings_refresh) on EVERY run — success included. Shared by the admin endpoint AND
    the cc#225 daily 06:15 IST scheduler job.

    cc#490: ALSO pulls NSE's official board-meetings feed for FORWARD-dated results (Screener's
    upcoming-results page only ever had same-day rows). Both sources go through the same
    _upsert_earnings_row accumulate/reschedule semantics; NSE rows are tagged verified='confirmed'
    and never get downgraded by a later Screener re-scrape of the same ticker+date.

    GUARD: each source is fetched BEFORE any write for that source; a Screener failure still
    raises (existing behavior — table never wiped on failure), but the NSE fetch is fully
    best-effort (returns [] on any error) so an NSE outage/block never breaks the Screener path."""
    client = await _screener_login_session()
    try:
        screener_rows = await _scrape_upcoming_results(client)
    finally:
        await client.aclose()
    nse_rows = await _fetch_nse_board_meetings()

    if not screener_rows and not nse_rows:
        return {"status": "warn", "rows_scraped": 0}

    inserted = updated = rescheduled = skipped = 0
    changed_at = datetime.utcnow().isoformat() + "Z"
    with _conn() as conn, conn.cursor() as cur:
        _ensure_earnings_schema(cur)
        for r in screener_rows:
            outcome = _upsert_earnings_row(cur, r, changed_at, verified='estimated')
            if outcome == "skipped": skipped += 1
            elif outcome == "inserted": inserted += 1
            elif outcome == "updated": updated += 1
            elif outcome == "rescheduled": rescheduled += 1
        nse_inserted = nse_updated = nse_rescheduled = nse_skipped = 0
        for r in nse_rows:
            outcome = _upsert_earnings_row(cur, r, changed_at, verified='confirmed')
            if outcome == "skipped": nse_skipped += 1
            elif outcome == "inserted": nse_inserted += 1
            elif outcome == "updated": nse_updated += 1
            elif outcome == "rescheduled": nse_rescheduled += 1
        reported, purged = _earnings_lifecycle(cur)
        summary = {"scraped": len(screener_rows), "inserted": inserted, "updated": updated,
                   "rescheduled": rescheduled, "reported": reported, "purged": purged,
                   "skipped_no_date": skipped,
                   "nse_fetched": len(nse_rows), "nse_inserted": nse_inserted,
                   "nse_updated": nse_updated, "nse_rescheduled": nse_rescheduled,
                   "nse_skipped_no_date": nse_skipped}
        _oplog(cur, "info", "earnings_refresh", summary)
        conn.commit()
    return {"status": "ok", "rows_scraped": len(screener_rows), "rows_inserted": inserted,
            "rows_updated": updated, "rescheduled": rescheduled,
            "reported": reported, "purged": purged, "skipped_no_date": skipped,
            "nse_rows_fetched": len(nse_rows), "nse_rows_inserted": nse_inserted,
            "nse_rows_updated": nse_updated, "nse_rescheduled": nse_rescheduled}


@router.post("/api/admin/load_earnings_from_screener")
async def load_earnings_from_screener(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    return await refresh_earnings_calendar()
