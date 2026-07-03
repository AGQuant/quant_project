"""
stock_options_backfill.py — cc#175 (URGENT WEEKEND, 03-Jul-2026)
==================================================================
2-trading-day stock-options backfill into option_chain for the weekend
dashboard build: ALL active futures_universe stocks (excl. index symbols),
ATM+-3 strikes x CE/PE, last 2 trading days, 5-min bars with OI.

Reuses production-validated patterns verbatim:
  - Fyers History API with oi_flag=1 + market-hours bar filter + ON CONFLICT
    (symbol, ts) upsert onto option_chain: pcr_backfill.py
  - ThreadPoolExecutor(WORKERS=10) concurrency: fyers_options_feed.py
  - Headless token from fyers_tokens (fyers_autologin TOTP daily): pcr_backfill

STRIKES ARE NEVER GUESSED (spec hard rule): real listed strikes come from the
Fyers public symbol master (public.fyers.in/sym_details/NSE_FO.csv), matched by
regex on option tickers, so per-stock strike intervals (and digit/hyphen/&
tickers like 360ONE, BAJAJ-AUTO, M&M — cc#148) resolve correctly. ATM+-3 =
the 7 listed strikes nearest spot (latest raw_prices close).

TRIGGER: no HTTP path exists from the CC sandbox, so the run self-starts on
app boot when app_config key 'cc175_options_backfill' = 'pending' (claimed
atomically -> runs once, restart-safe). Validation gate per spec: RELIANCE
fetched FIRST, strikes + row counts logged to ops_log (category
cc175_backfill); the full run proceeds only if RELIANCE passes sanity
(>=10 contracts matched, >=100 bars). Manual re-trigger:
POST /api/admin/backfill_stock_options (admin token).
Progress: ops_log category cc175_backfill (poll via run_sql).
"""
import calendar
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dt_time, timedelta
from typing import Optional

import psycopg2
import requests
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("scorr.cc175_options_backfill")
router = APIRouter(prefix="/api/admin", tags=["stock_options_backfill"])

DATABASE_URL    = os.getenv("DATABASE_URL")
ADMIN_TOKEN     = os.getenv("ADMIN_TOKEN", "")
FYERS_CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "1A4STS8ZGD-100")
HISTORY_URL     = "https://api-t1.fyers.in/data/history"
SYM_MASTER_URL  = "https://public.fyers.in/sym_details/NSE_FO.csv"

WORKERS      = 10          # fyers_options_feed.py production value, same endpoint
ATM_EACH_SIDE = 3          # ATM +- 3 -> 7 strikes
TRADING_DAYS  = 2          # default; override via flag value 'pending:<days>'
FLAG_KEY     = "cc175_options_backfill"
MKT_OPEN, MKT_CLOSE = dt_time(9, 15), dt_time(15, 30)
INDEX_SKIP   = {"NIFTY", "NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

_running = False


def _conn():
    return psycopg2.connect(DATABASE_URL)


def _hdr(token):
    return {"Authorization": f"{FYERS_CLIENT_ID}:{token}"}


def _log_progress(title: str, details: dict, alert: bool = False):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                        ("alert" if alert else "cc175_backfill", title, json.dumps(details, default=str)))
            conn.commit()
    except Exception as e:
        log.error(f"cc175 progress log failed: {e}")


def _load_token(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT access_token FROM fyers_tokens WHERE id=1")
        r = cur.fetchone()
    if not r or not r[0]:
        raise RuntimeError("No Fyers access_token in fyers_tokens (id=1)")
    return r[0]


def _last_tuesday(y, m):
    last_day = calendar.monthrange(y, m)[1]
    d = date(y, m, last_day)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


def _current_expiry(ref: date) -> date:
    exp = _last_tuesday(ref.year, ref.month)
    if ref > exp:
        exp = _last_tuesday(ref.year + 1, 1) if ref.month == 12 else _last_tuesday(ref.year, ref.month + 1)
    return exp


def _expiry_code(exp: date) -> str:
    return f"{exp.strftime('%y')}{exp.strftime('%b').upper()}"


def _code_to_expiry(code: str) -> Optional[date]:
    m = re.fullmatch(r"(\d{2})([A-Z]{3})", code)
    if not m or m.group(2) not in MONTHS:
        return None
    return _last_tuesday(2000 + int(m.group(1)), MONTHS.index(m.group(2)) + 1)


def _load_symbol_master() -> str:
    r = requests.get(SYM_MASTER_URL, timeout=90)
    r.raise_for_status()
    return r.text


def _resolve_strikes(master_text: str, underlying: str, spot: float, today: date):
    """Real listed strikes for the current monthly expiry from the symbol master.
    Regex over raw lines (no column-order assumptions); re.escape handles
    digit/hyphen/& tickers. Returns (expiry_code, expiry_date, [7 strikes nearest spot])."""
    pat = re.compile(r"NSE:" + re.escape(underlying) + r"(\d{2}[A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)\b")
    by_code = {}
    for m in pat.finditer(master_text):
        code, strike = m.group(1), float(m.group(2))
        exp = _code_to_expiry(code)
        if exp and exp >= today:
            by_code.setdefault(code, set()).add(strike)
    if not by_code:
        return None, None, []
    primary = _expiry_code(_current_expiry(today))
    code = primary if primary in by_code else min(by_code, key=lambda c: _code_to_expiry(c))
    strikes = sorted(by_code[code], key=lambda s: abs(s - spot))[: 2 * ATM_EACH_SIDE + 1]
    return code, _code_to_expiry(code), sorted(strikes)


def _fetch_contract(token, underlying, code, exp, strike, otype, start, end):
    sym = f"NSE:{underlying}{code}{int(strike) if float(strike).is_integer() else strike}{otype}"
    r = requests.get(HISTORY_URL, params={
        "symbol": sym, "resolution": "5", "date_format": "1",
        "range_from": start, "range_to": end,
        "cont_flag": "1", "oi_flag": "1",
    }, headers=_hdr(token), timeout=15)
    d = r.json()
    if d.get("s") != "ok":
        return sym, []
    rows = []
    for c in d.get("candles", []):
        if len(c) < 7:
            continue
        ts = datetime.utcfromtimestamp(c[0]) + timedelta(hours=5, minutes=30)
        if not (MKT_OPEN <= ts.time() <= MKT_CLOSE):
            continue
        rows.append((sym, underlying, strike, otype, exp, c[4],
                     int(c[6]) if c[6] is not None else None,
                     int(c[5]) if c[5] is not None else None, ts))
    return sym, rows


def _upsert_rows(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO option_chain
                (symbol, underlying, strike, option_type, expiry, ltp, oi, volume, bid, ask, ts)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,%s)
            ON CONFLICT (symbol, ts) DO UPDATE SET
                oi = EXCLUDED.oi,
                ltp = COALESCE(option_chain.ltp, EXCLUDED.ltp),
                volume = COALESCE(option_chain.volume, EXCLUDED.volume)
        """, rows)
    conn.commit()


def _backfill_underlying(conn, token, master_text, underlying, spot, start, end, today):
    code, exp, strikes = _resolve_strikes(master_text, underlying, spot, today)
    if not strikes:
        return {"underlying": underlying, "error": "no strikes in symbol master", "rows": 0, "contracts": 0}
    total, ok_contracts = 0, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(_fetch_contract, token, underlying, code, exp, s, ot, start, end)
                for s in strikes for ot in ("CE", "PE")]
        for f in as_completed(futs, timeout=300):
            try:
                _sym, rows = f.result(timeout=20)
                if rows:
                    _upsert_rows(conn, rows)
                    total += len(rows)
                    ok_contracts += 1
            except Exception as e:
                log.warning(f"cc175 {underlying}: contract fetch failed: {e}")
    return {"underlying": underlying, "expiry": str(exp), "strikes": strikes,
            "contracts": ok_contracts, "rows": total}


def _last_n_trading_days(conn, n):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT price_date FROM raw_prices ORDER BY price_date DESC LIMIT %s", (n,))
        days = [r[0] for r in cur.fetchall()]
    return min(days), max(days)


def run_backfill(days: int = TRADING_DAYS) -> dict:
    global _running
    today = date.today() + timedelta(hours=0)   # container UTC date is fine for expiry math
    conn = _conn()
    try:
        token = _load_token(conn)
        start_d, end_d = _last_n_trading_days(conn, days)
        start, end = start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d")

        with conn.cursor() as cur:
            cur.execute("""SELECT fu.symbol, rp.close FROM futures_universe fu
                           JOIN LATERAL (SELECT close FROM raw_prices
                                         WHERE symbol=fu.symbol ORDER BY price_date DESC LIMIT 1) rp ON true
                           WHERE fu.is_active=TRUE ORDER BY fu.symbol""")
            spots = {r[0]: float(r[1]) for r in cur.fetchall() if r[0] not in INDEX_SKIP and r[1]}

        _log_progress("cc175 backfill starting", {
            "window": [start, end], "stocks": len(spots), "workers": WORKERS})
        master_text = _load_symbol_master()

        # ---- step 1: RELIANCE validation gate (spec) ----
        rel = _backfill_underlying(conn, token, master_text, "RELIANCE",
                                   spots.get("RELIANCE", 0), start, end, today)
        _log_progress("cc175 RELIANCE validation", rel)
        if rel.get("contracts", 0) < 10 or rel.get("rows", 0) < 100:
            _log_progress("cc175 ABORTED - RELIANCE validation failed", rel, alert=True)
            return {"status": "aborted_validation", "reliance": rel}

        # ---- step 2: full run ----
        summary = {"stocks_done": 1, "rows_total": rel["rows"], "errors": []}
        for i, (sym, spot) in enumerate(sorted(spots.items()), 1):
            if sym == "RELIANCE":
                continue
            try:
                res = _backfill_underlying(conn, token, master_text, sym, spot, start, end, today)
                summary["rows_total"] += res.get("rows", 0)
                summary["stocks_done"] += 1
                if res.get("error"):
                    summary["errors"].append(f"{sym}: {res['error']}")
            except Exception as e:
                summary["errors"].append(f"{sym}: {str(e)[:60]}")
            if i % 25 == 0:
                _log_progress(f"cc175 progress {i}/{len(spots)}",
                              {"stocks_done": summary["stocks_done"], "rows_total": summary["rows_total"],
                               "errors": len(summary["errors"])})
        _log_progress("cc175 backfill COMPLETE", {
            "window": [start, end], "stocks_done": summary["stocks_done"],
            "rows_total": summary["rows_total"], "errors": summary["errors"][:20],
            "error_count": len(summary["errors"])},
            alert=len(summary["errors"]) > len(spots) * 0.2)
        return {"status": "complete", **summary}
    except Exception as e:
        _log_progress("cc175 backfill CRASHED", {"error": str(e)}, alert=True)
        raise
    finally:
        _running = False
        conn.close()


def _claim_flag() -> int:
    """Atomically consume the pending flag so restarts never double-run.
    Flag value 'pending' = default window; 'pending:<n>' = n trading days.
    Returns the claimed day-count, or 0 if nothing was pending."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            # read old value + claim in one transaction (FOR UPDATE = atomic vs a
            # second booting replica; a RETURNING subquery would see the NEW value)
            cur.execute("SELECT value FROM app_config WHERE key=%s AND value LIKE 'pending%%' FOR UPDATE",
                        (FLAG_KEY,))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s",
                            (FLAG_KEY,))
            conn.commit()
        if r is None:
            return 0
        val = r[0] or "pending"
        if ":" in val:
            try:
                return max(1, min(int(val.split(":", 1)[1]), 7))
            except ValueError:
                return TRADING_DAYS
        return TRADING_DAYS
    except Exception as e:
        log.error(f"cc175 flag claim failed: {e}")
        return 0


def _maybe_start(source: str, days: int = TRADING_DAYS):
    global _running
    if _running:
        return False
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    if now_ist.weekday() < 5 and MKT_OPEN <= now_ist.time() <= MKT_CLOSE:
        log.warning("cc175: market hours -- historical backfill deferred")
        return False
    _running = True
    threading.Thread(target=run_backfill, args=(days,),
                     name=f"cc175-backfill-{source}", daemon=True).start()
    return True


@router.on_event("startup")
async def _startup_trigger():
    # CC sandbox has no HTTP path to prod; a DB flag set via MCP run_sql +
    # this hook = deploy-time self-trigger (runs once, atomic flag claim).
    days = _claim_flag()
    if days:
        log.info(f"cc175: pending flag claimed -- starting stock options backfill ({days} trading days)")
        _maybe_start("startup", days)


@router.post("/backfill_stock_options")
def backfill_stock_options_now(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    if _running:
        return {"status": "already_running"}
    started = _maybe_start("manual")
    return {"status": "started" if started else "deferred_market_hours",
            "progress": "SELECT * FROM ops_log WHERE category='cc175_backfill' ORDER BY id DESC"}
