"""
fundamentals_scraper.py — cc#361 GVM_HISTORY_BACKFILL_V1, PHASE 1 (SCRAPE) ONLY.
================================================================================
Founder-ordered one-time scrape of Screener.in LOGGED-OUT PUBLIC company pages to reconstruct
5yr point-in-time fundamentals for the whole ~1,766-stock universe. Raw capture only — G/V field
selection + backward-GVM compute (Phases 2-4) are GATED and live elsewhere; this file NEVER computes
GVM and NEVER writes gvm_scores/gvm_history.

DESIGN (per spec id=361):
  * Public pages only (www.screener.in/company/<CODE>/[consolidated/]) — never any account session.
  * Consolidated preferred when the page carries consolidated financials, else standalone; flag stored.
  * Store EVERYTHING raw: one row per (symbol, section, period) with a jsonb of every metric Screener
    exposes in that table — quarters / profit-loss / balance-sheet / cash-flow / ratios. Field
    selection happens at compute time (tomorrow), so nothing is dropped now.
  * Throttle 2-3 s/page, exponential backoff on 429/403, checkpoint-resume per symbol (a re-run skips
    already-ok symbols; writes are idempotent upserts so repeats are harmless).
  * Runs as a background daemon ON RAILWAY (boot-flag trigger / admin route) — NOT inside a CC session.

Trigger (app_config key 'fundamentals_scrape'):
    'test'    -> scrape only the 3 founder spot-check symbols (RELIANCE, KPITTECH, HDFCBANK), then stop
                 (lets the parser be verified before the full run).
    'run'/'pending' -> full universe (screener_raw.nse_code), resumable in ~590-symbol stages.
Status: GET /api/admin/fundamentals_scrape_status.  Manual kick: POST /api/admin/run_fundamentals_scrape.

DEPTH NOTE (verified 11-Jul on RELIANCE/HDFCBANK/KPITTECH test scrape): logged-out public pages
expose ~13 trailing quarters + ~12 annual years — NOT the ~40 quarters the spec anticipated. Annual
history (12y) is ample for the annual-stepped G/V components; quarterly QoQ inputs are capped at
~3.25y. Phase 2 compute should lean on annual G/V + daily-M and treat QoQ as short-history.
"""

import logging
import os
import time
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, Header, HTTPException
from typing import Optional

log = logging.getLogger("scorr.fundamentals_scraper")
router = APIRouter(prefix="/api/admin", tags=["fundamentals_scraper"])
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

BASE = "https://www.screener.in/company/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0 Safari/537.36")
THROTTLE = 2.5          # seconds between company pages (spec: 2-3 s)
STAGE_SIZE = 590        # ~1766 / 3 -> per-stage summary boundary
SPOT_CHECK = ["RELIANCE", "KPITTECH", "HDFCBANK"]
SCRAPE_FLAG = "fundamentals_scrape"
SCRAPE_1B_FLAG = "fundamentals_scrape_1b"   # cc#391 shareholding phase

# screener section id -> period_type stored in fundamentals_history
SECTIONS = {"quarters": "quarter", "profit-loss": "annual", "balance-sheet": "annual",
            "cash-flow": "annual", "ratios": "annual"}
_MONTHS = {"jan": 31, "feb": 28, "mar": 31, "apr": 30, "may": 31, "jun": 30,
           "jul": 31, "aug": 31, "sep": 30, "oct": 31, "nov": 30, "dec": 31}


def _check_admin(token):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")


# ── schema (idempotent) ──────────────────────────────────────────────────────────

def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fundamentals_history (
                id            BIGSERIAL PRIMARY KEY,
                symbol        TEXT NOT NULL,
                consolidated  BOOLEAN,
                section       TEXT NOT NULL,
                period_type   TEXT,
                period_label  TEXT NOT NULL,
                period_end    DATE,
                metrics       JSONB,
                scraped_at    TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (symbol, section, period_label, consolidated)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_fh_symbol ON fundamentals_history(symbol)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fundamentals_scrape_status (
                symbol       TEXT PRIMARY KEY,
                status       TEXT,          -- ok / failed / skipped
                rows_written INTEGER,
                consolidated BOOLEAN,
                error        TEXT,
                scraped_at   TIMESTAMPTZ DEFAULT NOW()
            )""")
        # cc#391: independent Phase-1B (shareholding) progress on the SAME status table
        cur.execute("ALTER TABLE fundamentals_scrape_status ADD COLUMN IF NOT EXISTS phase1b_status TEXT")
        cur.execute("ALTER TABLE fundamentals_scrape_status ADD COLUMN IF NOT EXISTS phase1b_rows INTEGER")
        cur.execute("ALTER TABLE fundamentals_scrape_status ADD COLUMN IF NOT EXISTS phase1b_scraped_at TIMESTAMPTZ")
    conn.commit()


# ── parsing ──────────────────────────────────────────────────────────────────────

def _period_end(label):
    """'Mar 2024' -> 2024-03-31; 'TTM'/unknown -> None."""
    try:
        parts = label.strip().split()
        if len(parts) != 2:
            return None
        mon = parts[0][:3].lower()
        yr = int(parts[1])
        if mon not in _MONTHS:
            return None
        day = 29 if (mon == "feb" and yr % 4 == 0 and (yr % 100 != 0 or yr % 400 == 0)) else _MONTHS[mon]
        return date(yr, list(_MONTHS).index(mon) + 1, day)
    except Exception:
        return None


def _parse_section(soup, section_id, ptype):
    """Extract one Screener data-table into per-period metric dicts. Returns list of period rows."""
    sec = soup.find("section", id=section_id)
    if not sec:
        return []
    table = sec.find("table", class_="data-table")
    if not table or not table.find("thead") or not table.find("tbody"):
        return []
    heads = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
    periods = heads[1:]                       # first header cell is the row-label column (empty)
    if not periods:
        return []
    per = {p: {} for p in periods}
    for tr in table.find("tbody").find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        label = tds[0].get_text(" ", strip=True).rstrip("+").strip()
        if not label:
            continue
        for i, td in enumerate(tds[1:]):
            if i >= len(periods):
                break
            val = td.get_text(strip=True)
            if val != "":
                per[periods[i]][label] = val
    rows = []
    for p, metrics in per.items():
        if metrics:
            rows.append({"section": section_id, "period_type": ptype, "period_label": p,
                         "period_end": _period_end(p), "metrics": metrics})
    return rows


def _fetch(url):
    """GET with browser UA + exponential backoff on 429/403. Returns Response or None."""
    delay = 5
    for attempt in range(4):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                             timeout=25)
        except Exception as e:
            log.warning(f"fetch {url} attempt {attempt}: {e}")
            time.sleep(delay); delay *= 2; continue
        if r.status_code in (429, 403):
            log.warning(f"fetch {url} -> {r.status_code}, backoff {delay}s")
            time.sleep(delay); delay *= 2; continue
        return r
    return None


def _fetch_soup(symbol):
    """Fetch a company page (consolidated preferred, else standalone) and return (soup, cons) or
    (None, None). A valid page carries a profit-loss data-table."""
    sym = symbol.strip().upper()
    for cons, path in ((True, f"{BASE}{sym}/consolidated/"), (False, f"{BASE}{sym}/")):
        r = _fetch(path)
        if r is None or r.status_code != 200 or "data-table" not in r.text:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        if soup.find("section", id="profit-loss"):
            return soup, cons
    return None, None


def fetch_company(symbol):
    """Financials sections (quarters/profit-loss/balance-sheet/cash-flow/ratios).
    Returns (rows, consolidated_flag) or ([], None)."""
    soup, cons = _fetch_soup(symbol)
    if soup is None:
        return [], None
    rows = []
    for sid, ptype in SECTIONS.items():
        rows.extend(_parse_section(soup, sid, ptype))
    return (rows, cons) if rows else ([], None)


def fetch_shareholding(symbol):
    """cc#391 Phase 1B: the shareholding quarterly table only (Promoters/FIIs/DIIs/Government/Public/
    No. of Shareholders per quarter). Returns (rows, consolidated_flag) or ([], None)."""
    soup, cons = _fetch_soup(symbol)
    if soup is None:
        return [], None
    rows = _parse_section(soup, "shareholding", "quarter")
    return (rows, cons) if rows else ([], None)


# ── writing + status ─────────────────────────────────────────────────────────────

def _write_symbol(conn, symbol, rows, cons):
    import json
    with conn.cursor() as cur:
        for row in rows:
            cur.execute("""
                INSERT INTO fundamentals_history
                    (symbol, consolidated, section, period_type, period_label, period_end, metrics, scraped_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,NOW())
                ON CONFLICT (symbol, section, period_label, consolidated)
                DO UPDATE SET metrics=EXCLUDED.metrics, period_end=EXCLUDED.period_end, scraped_at=NOW()
            """, (symbol, cons, row["section"], row["period_type"], row["period_label"],
                  row["period_end"], json.dumps(row["metrics"])))
    conn.commit()


def _set_status(conn, symbol, status, rows_written=0, cons=None, error=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fundamentals_scrape_status (symbol, status, rows_written, consolidated, error, scraped_at)
            VALUES (%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (symbol) DO UPDATE SET status=EXCLUDED.status, rows_written=EXCLUDED.rows_written,
                consolidated=EXCLUDED.consolidated, error=EXCLUDED.error, scraped_at=NOW()
        """, (symbol, status, rows_written, cons, (error or "")[:400]))
    conn.commit()


def _slog(conn, category, title, details):
    import json
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO session_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                        (category, title, json.dumps(details, default=str)))
        conn.commit()
    except Exception as e:
        log.warning(f"session_log write ({title}): {e}")


# ── runner ───────────────────────────────────────────────────────────────────────

def run_scrape(mode="run") -> dict:
    """Phase-1 scrape. mode='test' -> 3 spot-check symbols only; else full universe (resumable).
    Per symbol: fetch + parse + upsert + status; throttle THROTTLE s; failures logged and skipped
    (never abort). Stage summary (scraped/failed/skipped) every STAGE_SIZE symbols. On completion
    writes session_log GVM_HISTORY_SCRAPE_COMPLETE and STOPS — no compute (Phases 2-4 gated)."""
    import fyers_feed
    conn = fyers_feed.get_db()
    started = time.time()
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            if mode == "test":
                symbols = list(SPOT_CHECK)
                already = set()
            else:
                cur.execute("SELECT DISTINCT UPPER(nse_code) FROM screener_raw "
                            "WHERE nse_code IS NOT NULL AND nse_code<>'' ORDER BY 1")
                symbols = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT symbol FROM fundamentals_scrape_status WHERE status='ok'")
                already = {r[0] for r in cur.fetchall()}
        todo = [s for s in symbols if s not in already]
        _slog(conn, "backfill", "GVM_HISTORY_SCRAPE_START",
              {"mode": mode, "universe": len(symbols), "already_ok": len(already), "to_do": len(todo)})
        ok = failed = skipped = rows_total = 0
        failures = []
        for i, sym in enumerate(todo, 1):
            try:
                rows, cons = fetch_company(sym)
                if rows:
                    _write_symbol(conn, sym, rows, cons)
                    _set_status(conn, sym, "ok", len(rows), cons)
                    ok += 1; rows_total += len(rows)
                else:
                    _set_status(conn, sym, "failed", 0, None, "no financial tables parsed")
                    failed += 1; failures.append(sym)
            except Exception as e:
                try:
                    _set_status(conn, sym, "failed", 0, None, str(e))
                except Exception:
                    pass
                failed += 1; failures.append(sym)
                log.error(f"scrape {sym} failed: {e}")
            if i % STAGE_SIZE == 0:
                _slog(conn, "backfill", "GVM_HISTORY_SCRAPE_STAGE",
                      {"mode": mode, "done": i, "of": len(todo), "ok": ok, "failed": failed,
                       "rows": rows_total, "elapsed_min": round((time.time()-started)/60, 1),
                       "last": sym})
            time.sleep(THROTTLE)
        # coverage: symbols with < 8 quarters flagged (spec)
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*), COALESCE(SUM(rows_written),0) FROM fundamentals_scrape_status WHERE status='ok'""")
            tot = cur.fetchone()
            cur.execute("""SELECT COUNT(*) FROM (
                SELECT symbol FROM fundamentals_history WHERE section='quarters'
                GROUP BY symbol HAVING COUNT(*) < 8) z""")
            thin = cur.fetchone()
        summary = {"mode": mode, "ok": ok, "failed": failed, "skipped_already": len(already),
                   "rows_written_this_run": rows_total, "elapsed_min": round((time.time()-started)/60, 1),
                   "symbols_ok_total": int(tot[0] or 0), "rows_total": int(tot[1] or 0),
                   "symbols_under_8_quarters": int(thin[0] or 0), "failures_sample": failures[:50]}
        _slog(conn, "data_audit", "GVM_HISTORY_SCRAPE_COMPLETE", summary)
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s", (SCRAPE_FLAG,))
            conn.commit()
        except Exception:
            pass
        log.info(f"run_scrape COMPLETE: {summary}")
        return summary
    finally:
        conn.close()


def _set_status_1b(conn, symbol, status, rows_written=0):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fundamentals_scrape_status (symbol, phase1b_status, phase1b_rows, phase1b_scraped_at)
            VALUES (%s,%s,%s,NOW())
            ON CONFLICT (symbol) DO UPDATE SET phase1b_status=EXCLUDED.phase1b_status,
                phase1b_rows=EXCLUDED.phase1b_rows, phase1b_scraped_at=NOW()
        """, (symbol, status, rows_written))
    conn.commit()


def run_shareholding_scrape() -> dict:
    """cc#391 Phase 1B: scrape ONLY the shareholding quarterly table for the full universe into
    fundamentals_history (section='shareholding'), tracked independently via phase1b_status. Same
    politeness pacing + checkpoint-resume (skip phase1b_status='ok'). Completion -> session_log
    PHASE_1B_SHAREHOLDING_COMPLETE. Never computes GVM (Phases 2-4 gated)."""
    import fyers_feed
    conn = fyers_feed.get_db()
    started = time.time()
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT UPPER(nse_code) FROM screener_raw "
                        "WHERE nse_code IS NOT NULL AND nse_code<>'' ORDER BY 1")
            symbols = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT symbol FROM fundamentals_scrape_status WHERE phase1b_status='ok'")
            already = {r[0] for r in cur.fetchall()}
        todo = [s for s in symbols if s not in already]
        _slog(conn, "backfill", "PHASE_1B_SHAREHOLDING_START",
              {"universe": len(symbols), "already_ok": len(already), "to_do": len(todo)})
        ok = failed = rows_total = 0
        failures = []
        for i, sym in enumerate(todo, 1):
            try:
                rows, cons = fetch_shareholding(sym)
                if rows:
                    _write_symbol(conn, sym, rows, cons)
                    _set_status_1b(conn, sym, "ok", len(rows))
                    ok += 1; rows_total += len(rows)
                else:
                    _set_status_1b(conn, sym, "failed", 0)
                    failed += 1; failures.append(sym)
            except Exception as e:
                try:
                    _set_status_1b(conn, sym, "failed", 0)
                except Exception:
                    pass
                failed += 1; failures.append(sym)
                log.error(f"shareholding scrape {sym} failed: {e}")
            if i % STAGE_SIZE == 0:
                _slog(conn, "backfill", "PHASE_1B_SHAREHOLDING_STAGE",
                      {"done": i, "of": len(todo), "ok": ok, "failed": failed, "rows": rows_total,
                       "elapsed_min": round((time.time()-started)/60, 1), "last": sym})
            time.sleep(THROTTLE)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM fundamentals_scrape_status WHERE phase1b_status='ok'")
            tot = cur.fetchone()
            cur.execute("SELECT COUNT(DISTINCT symbol) FROM fundamentals_history WHERE section='shareholding'")
            covered = cur.fetchone()
        summary = {"ok": ok, "failed": failed, "skipped_already": len(already),
                   "rows_written_this_run": rows_total, "symbols_ok_total": int(tot[0] or 0),
                   "symbols_with_shareholding": int(covered[0] or 0),
                   "elapsed_min": round((time.time()-started)/60, 1), "failures_sample": failures[:50]}
        _slog(conn, "data_audit", "PHASE_1B_SHAREHOLDING_COMPLETE", summary)
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s", (SCRAPE_1B_FLAG,))
            conn.commit()
        except Exception:
            pass
        log.info(f"run_shareholding_scrape COMPLETE: {summary}")
        return summary
    finally:
        conn.close()


def _claim_scrape_flag():
    """Consume app_config['fundamentals_scrape'] in ('test','run','pending'); return the mode or None.
    Marks 'claimed:<mode>' so it never re-fires on the next boot."""
    import fyers_feed
    try:
        conn = fyers_feed.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key=%s FOR UPDATE", (SCRAPE_FLAG,))
                r = cur.fetchone()
                val = (r[0] or "").strip().lower() if r else ""
                if val not in ("test", "run", "pending"):
                    conn.commit(); return None
                cur.execute("UPDATE app_config SET value=%s, updated_at=NOW() WHERE key=%s",
                            (f"claimed:{val}", SCRAPE_FLAG))
            conn.commit()
        finally:
            conn.close()
        return "test" if val == "test" else "run"
    except Exception as e:
        log.error(f"scrape flag claim failed: {e}")
        return None


def _claim_1b_flag():
    """Consume app_config['fundamentals_scrape_1b'] in ('run','pending') -> claimed. Returns True once."""
    import fyers_feed
    try:
        conn = fyers_feed.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key=%s FOR UPDATE", (SCRAPE_1B_FLAG,))
                r = cur.fetchone()
                val = (r[0] or "").strip().lower() if r else ""
                if val not in ("run", "pending"):
                    conn.commit(); return False
                cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s", (SCRAPE_1B_FLAG,))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        log.error(f"scrape_1b flag claim failed: {e}")
        return False


@router.on_event("startup")
async def _scrape_startup_trigger():
    import threading
    mode = _claim_scrape_flag()
    if mode:
        log.info(f"cc#361: fundamentals_scrape flag claimed (mode={mode}) — starting in background")
        threading.Thread(target=run_scrape, args=(mode,), name="cc361-scrape", daemon=True).start()
    if _claim_1b_flag():   # cc#391
        log.info("cc#391: fundamentals_scrape_1b flag claimed — starting shareholding scrape in background")
        threading.Thread(target=run_shareholding_scrape, name="cc391-scrape1b", daemon=True).start()


@router.get("/fundamentals_scrape_status")
async def fundamentals_scrape_status():
    """cc#361 status: per-status counts + rows + a rough ETA for the remaining universe."""
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.fundamentals_scrape_status')")
            if cur.fetchone()[0] is None:
                return {"status": "not_started", "note": "tables not yet created"}
            cur.execute("SELECT status, COUNT(*) FROM fundamentals_scrape_status GROUP BY status")
            counts = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute("SELECT COALESCE(SUM(rows_written),0) FROM fundamentals_scrape_status WHERE status='ok'")
            rows = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT phase1b_status, COUNT(*) FROM fundamentals_scrape_status WHERE phase1b_status IS NOT NULL GROUP BY phase1b_status")
            phase1b = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute("SELECT COUNT(DISTINCT UPPER(nse_code)) FROM screener_raw WHERE nse_code IS NOT NULL AND nse_code<>''")
            universe = int(cur.fetchone()[0] or 0)
        done = sum(counts.values())
        remaining = max(0, universe - counts.get("ok", 0))
        return {"universe": universe, "counts": counts, "rows_written": rows,
                "phase1b": phase1b, "remaining_est": remaining,
                "eta_min_est": round(remaining * THROTTLE / 60, 1),
                "at": datetime.utcnow().isoformat()}
    finally:
        conn.close()


@router.post("/run_fundamentals_scrape")
async def run_fundamentals_scrape(mode: str = "run", x_admin_token: Optional[str] = Header(None)):
    """cc#361 Phase 1: kick the Screener scrape in a background daemon. mode='test' (3 spot-check
    symbols) or 'run' (full universe, resumable). Phases 2-4 (compute) remain gated."""
    _check_admin(x_admin_token)
    import threading
    m = "test" if (mode or "").lower() == "test" else "run"
    threading.Thread(target=run_scrape, args=(m,), name="cc361-scrape-manual", daemon=True).start()
    return {"status": "started", "mode": m,
            "note": "Scrape running in background; poll /api/admin/fundamentals_scrape_status."}


@router.post("/run_shareholding_scrape")
async def run_shareholding_scrape_now(x_admin_token: Optional[str] = Header(None)):
    """cc#391 Phase 1B: kick the shareholding scrape (Promoters/FIIs/DIIs/... per quarter) in a
    background daemon — resumable, independent of Phase 1. Phases 2-4 remain gated."""
    _check_admin(x_admin_token)
    import threading
    threading.Thread(target=run_shareholding_scrape, name="cc391-scrape1b-manual", daemon=True).start()
    return {"status": "started",
            "note": "Shareholding scrape running; poll /api/admin/fundamentals_scrape_status (phase1b_* fields)."}
