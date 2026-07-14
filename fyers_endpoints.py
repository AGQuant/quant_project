"""
Fyers on-demand endpoints — quote fetcher for trade idea format.

On-demand only. No storage, no recurring calls.
Reads access_token from fyers_tokens table (written daily by fyers_feed.py auto-login).

Endpoints:
  GET /api/fyers/quote/{symbol}   — fetch live futures quote for a symbol
                                    symbol = NSE code e.g. SBIN, RELIANCE
                                    Builds futures ticker automatically for current month.

MCP tool: fyers_quote
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime, date, timedelta, time as dt_time
import os
import json
import time
import psycopg
import requests
import calendar

from nse_holidays import is_trading_day   # cc#193: market-hours gate for live quotes

CLAMP_MAX_DEV_PCT = 5.0   # cc#193: reject a live quote >5% off the latest DB session bar

router = APIRouter(prefix="/api/fyers", tags=["fyers"])

FYERS_CLIENT_ID = os.getenv("FYERS_CLIENT_ID", "1A4STS8ZGD-100")
QUOTES_URL      = "https://api-t1.fyers.in/data/quotes"
DEPTH_URL       = "https://api-t1.fyers.in/data/depth"
OPTION_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
FULL_CHAIN_MAX_STRIKES = 80   # ad hoc safety cap (both CE+PE combined) — respects Fyers rate limit
FULL_CHAIN_CALL_SPACING_SEC = 0.35


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _get_token() -> str:
    """Read today's access token from fyers_tokens table."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT access_token, access_created FROM fyers_tokens WHERE id = 1"
        )
        row = cur.fetchone()
    if not row or not row[0]:
        raise HTTPException(503, "Fyers token not found — worker may not have started yet")
    token, created = row[0], row[1]
    today = datetime.now().date()
    if created and created.date() != today:
        raise HTTPException(503, f"Fyers token is from {created.date()} — worker auto-login pending")
    return token


def _last_tuesday(y: int, m: int) -> date:
    """Last Tuesday of month y/m — NSE expiry since Sep 2025."""
    last_day = calendar.monthrange(y, m)[1]
    d = date(y, m, last_day)
    while d.weekday() != 1:   # 1 = Tuesday
        d = d.replace(day=d.day - 1)
    return d


def _current_expiry() -> date:
    """Current active monthly expiry (last Tuesday), rolling to next month after expiry."""
    today  = date.today()
    expiry = _last_tuesday(today.year, today.month)
    if today > expiry:
        if today.month == 12:
            expiry = _last_tuesday(today.year + 1, 1)
        else:
            expiry = _last_tuesday(today.year, today.month + 1)
    return expiry


def _futures_symbol(nse_code: str) -> str:
    """
    Build current-month futures Fyers symbol.
    e.g. SBIN -> NSE:SBIN26JUNFUT
    Rolls to next month after last Tuesday expiry.
    """
    expiry = _current_expiry()
    month_str = expiry.strftime("%b").upper()
    year_str  = expiry.strftime("%y")
    return f"NSE:{nse_code}{year_str}{month_str}FUT"


def _fetch_quote(fyers_symbol: str, token: str) -> dict:
    """Call Fyers quote API and return raw response."""
    r = requests.get(
        QUOTES_URL,
        params={"symbols": fyers_symbol},
        headers={"Authorization": f"{FYERS_CLIENT_ID}:{token}"},
        timeout=8,
    )
    d = r.json()
    if d.get("s") != "ok":
        raise HTTPException(502, f"Fyers API error: {d.get('message', d)}")
    items = d.get("d", [])
    if not items:
        raise HTTPException(404, f"No data returned for {fyers_symbol}")
    return items[0].get("v", {})


@router.get("/quote/{symbol}")
def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _market_open_ist() -> bool:
    """cc#193: True only during a real NSE session — trading day + 09:15-15:30 IST."""
    n = _ist_now()
    return is_trading_day(n.date()) and dt_time(9, 15) <= n.time() <= dt_time(15, 30)


def _latest_session_fut(symbol: str):
    """cc#193: latest TRADING-SESSION fyers_fut 5m bar (weekday + 09:15-15:30 IST)
    from intraday_prices. Returns (close, ts) or (None, None) — never a phantom
    off-hours tick."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT close, ts FROM intraday_prices
                WHERE symbol=%s AND source='fyers_fut' AND timeframe='5m'
                  AND EXTRACT(DOW FROM ts) BETWEEN 1 AND 5
                  AND ts::time >= TIME '09:15' AND ts::time < TIME '15:30'
                ORDER BY ts DESC LIMIT 1
            """, (symbol,))
            r = cur.fetchone()
            if r and r[0] is not None:
                return float(r[0]), r[1]
    except Exception:
        pass
    return None, None


def _log_quote_rejected(symbol, quote, db_val, dev_pct):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'quote_rejected', %s, %s::jsonb)""",
                        (f"{symbol} live quote rejected",
                         json.dumps({"symbol": symbol, "quote": quote,
                                     "db_bar_close": db_val, "deviation_pct": dev_pct})))
        conn.commit()
    except Exception:
        pass


def fyers_quote(symbol: str):
    """
    Live futures quote for a symbol — LTP, open, high, low, prev_close,
    day_change%, OI, volume. On-demand only, no storage.

    cc#193: (1) MARKET-HOURS GATE — outside a real NSE session we NEVER call the
    live quote (Fyers streams phantom garbage on non-trading days, e.g. Sat 04-Jul
    BANKNIFTY 64,043 vs the real 58,255); we serve the last futures session bar
    close from intraday_prices instead, with as_of = that bar's time. (2) SANITY
    CLAMP during market hours — a live quote deviating >5% from the latest DB
    session bar is rejected (garbage can spike any day), the DB bar is served, and
    the rejection is logged to ops_log(category=quote_rejected).
    """
    symbol    = symbol.upper().strip()
    fyers_sym = _futures_symbol(symbol)

    # (1) off-hours: never call live — serve the last futures session bar
    if not _market_open_ist():
        db_close, db_ts = _latest_session_fut(symbol)
        if db_close is not None:
            return {
                "symbol": symbol, "fyers_symbol": fyers_sym, "ltp": db_close,
                "open": None, "high": None, "low": None, "prev_close": None,
                "day_chg_pct": None, "volume": None, "oi": None,
                "source": "db_fut_bar", "is_live": False,
                "as_of": db_ts.strftime("%Y-%m-%d %H:%M:%S IST") if db_ts else None,
                "fetched_at": _ist_now().strftime("%Y-%m-%d %H:%M:%S IST"),
            }
        raise HTTPException(503, f"Market closed and no futures session bar for {symbol}")

    # market hours: fetch live, then sanity-clamp against the latest DB session bar
    token = _get_token()
    try:
        v = _fetch_quote(fyers_sym, token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Quote fetch failed: {e}")

    ltp        = v.get("lp") or v.get("ltp")
    open_p     = v.get("open_price")
    high_p     = v.get("high_price")
    low_p      = v.get("low_price")
    prev_close = v.get("prev_close_price")
    volume     = v.get("volume")
    oi         = v.get("oi")

    source, is_live, as_of = "fyers_live", True, None
    db_close, db_ts = _latest_session_fut(symbol)
    if ltp is not None and db_close and float(db_close) > 0:
        dev = abs(float(ltp) / float(db_close) - 1) * 100
        if dev > CLAMP_MAX_DEV_PCT:
            _log_quote_rejected(symbol, float(ltp), float(db_close), round(dev, 2))
            ltp = db_close
            source, is_live = "db_fut_bar_clamped", False
            as_of = db_ts.strftime("%Y-%m-%d %H:%M:%S IST") if db_ts else None

    day_chg_pct = None
    if ltp and prev_close and float(prev_close) > 0:
        day_chg_pct = round((float(ltp) - float(prev_close)) / float(prev_close) * 100, 2)

    return {
        "symbol":       symbol,
        "fyers_symbol": fyers_sym,
        "ltp":          ltp,
        "open":         open_p,
        "high":         high_p,
        "low":          low_p,
        "prev_close":   prev_close,
        "day_chg_pct":  day_chg_pct,
        "volume":       volume,
        "oi":           oi,
        "source":       source,
        "is_live":      is_live,
        "as_of":        as_of,
        "fetched_at":   _ist_now().strftime("%Y-%m-%d %H:%M:%S IST"),
    }


@router.get("/oi/stock_full/{symbol}")
def fyers_oi_stock_full(symbol: str):
    """cc#482 item 4: on-demand FULL stock option chain OI — NOT scheduled, callable when
    the ATM-only 5-min poll (worker) isn't enough. Fetches the live Fyers NSE_FO master to
    get the ACTUAL listed strikes for the current expiry (never guessed/synthesized), then
    depth-polls each via the shared main-app token (same fyers_tokens row the worker mints
    daily — no worker call needed). Rate-limit safe (paced, capped at
    FULL_CHAIN_MAX_STRIKES combined CE+PE) since this is an ad hoc tool, not a scheduled job."""
    symbol = symbol.upper().strip()
    token = _get_token()
    expiry = _current_expiry()

    try:
        r = requests.get(OPTION_MASTER_URL, timeout=30)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(502, f"NSE_FO master fetch failed: {e}")

    strikes = {"CE": [], "PE": []}
    prefix = f"NSE:{symbol}"
    for line in r.text.splitlines():
        parts = line.split(",")
        if len(parts) < 17:
            continue
        ticker = parts[9].strip()
        if not ticker.startswith(prefix):
            continue
        otype = parts[16].strip().upper()
        if otype not in ("CE", "PE"):
            continue
        try:
            exp = datetime.fromtimestamp(int(float(parts[8]))).date()
            if exp != expiry:
                continue
            strikes[otype].append((float(parts[15]), ticker))
        except Exception:
            continue

    if not strikes["CE"] and not strikes["PE"]:
        raise HTTPException(404, f"No listed option chain found for {symbol} expiry {expiry} "
                                  "(check the NSE code and that it has listed F&O options)")

    ladder = sorted(strikes["CE"]) + sorted(strikes["PE"])
    truncated = len(ladder) > FULL_CHAIN_MAX_STRIKES
    if truncated:
        ladder = sorted(strikes["CE"], key=lambda x: x[0])[:FULL_CHAIN_MAX_STRIKES // 2] \
               + sorted(strikes["PE"], key=lambda x: x[0])[:FULL_CHAIN_MAX_STRIKES // 2]

    headers = {"Authorization": f"{FYERS_CLIENT_ID}:{token}"}
    out, errors = [], 0
    for strike, ticker in ladder:
        otype = "CE" if ticker in [t for _, t in strikes["CE"]] else "PE"
        try:
            dr = requests.get(DEPTH_URL, params={"symbol": ticker, "ohlcv_flag": 1},
                               headers=headers, timeout=8)
            body = (dr.text or "").strip()
            if not body:
                out.append({"symbol": ticker, "strike": strike, "type": otype, "error": "empty response"})
                errors += 1
            else:
                dd = dr.json()
                node = {}
                data_d = dd.get("d")
                if isinstance(data_d, dict):
                    node = data_d.get(ticker) or (next(iter(data_d.values())) if data_d else {})
                elif isinstance(data_d, list) and data_d and isinstance(data_d[0], dict):
                    node = data_d[0].get("v", data_d[0])
                out.append({"symbol": ticker, "strike": strike, "type": otype,
                            "oi": node.get("oi"), "ltp": node.get("lp") or node.get("ltp"),
                            "bid": node.get("bid"), "ask": node.get("ask")})
        except Exception as e:
            out.append({"symbol": ticker, "strike": strike, "type": otype, "error": str(e)[:120]})
            errors += 1
        time.sleep(FULL_CHAIN_CALL_SPACING_SEC)

    return {
        "symbol": symbol, "expiry": str(expiry), "requested_strikes": len(ladder),
        "truncated": truncated, "errors": errors, "chain": out,
        "fetched_at": _ist_now().strftime("%Y-%m-%d %H:%M:%S IST"),
    }
