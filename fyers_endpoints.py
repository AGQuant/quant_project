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
from datetime import datetime, date
import os
import psycopg
import requests
import calendar

router = APIRouter(prefix="/api/fyers", tags=["fyers"])

FYERS_CLIENT_ID = os.getenv("FYERS_CLIENT_ID", "1A4STS8ZGD-100")
QUOTES_URL      = "https://api-t1.fyers.in/data/quotes"


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


def _futures_symbol(nse_code: str) -> str:
    """
    Build current-month futures Fyers symbol.
    e.g. SBIN -> NSE:SBIN26JUNFUT
    Rolls to next month after last Tuesday expiry.
    """
    today  = date.today()
    expiry = _last_tuesday(today.year, today.month)

    if today > expiry:
        if today.month == 12:
            expiry = _last_tuesday(today.year + 1, 1)
        else:
            expiry = _last_tuesday(today.year, today.month + 1)

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
def fyers_quote(symbol: str):
    """
    Fetch live futures quote for a symbol.
    Returns LTP, open, high, low, prev_close, day_change%, OI, volume.
    Used for trade card population — on-demand only, no storage.
    """
    symbol    = symbol.upper().strip()
    token     = _get_token()
    fyers_sym = _futures_symbol(symbol)

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
        "fetched_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
    }
