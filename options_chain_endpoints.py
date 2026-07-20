"""
options_chain_endpoints.py — cc#567 (STOCK_OPTIONS_OFF_WS_ONDEMAND_CHAIN_V1, session_log 6339)
===============================================================================================
On-demand option chain for ANY underlying, fetched LIVE from Fyers REST at request time.

Stock options left the 5-min WS feed entirely (cc#567): they are no longer subscribed, no longer
persisted to option_chain, and no longer carry a rolling ATM store. Instead, this endpoint powers
"show me the option chain for X" prompts by fetching the chain ephemerally on request:

  GET /api/options/chain?underlying=RELIANCE&n=10

  1. Live CMP from the Fyers quotes API.
  2. Real listed strikes from the Fyers public symbol master (never guessed — same rule as the
     backfill path), nearest 2N+1 to spot for the current monthly expiry.
  3. Per-contract ltp / oi / bid / ask from the Fyers depth API (quotes has NO OI), fetched
     concurrently (ThreadPoolExecutor).

Ephemeral: DISPLAY ONLY. Nothing here writes to option_chain or any rolling store — that is the
whole point of the cc#567 architecture (stop the inconsistent, unused stock-option persistence).

Mounted in main.py via: app.include_router(options_chain_router).
"""
import calendar
import logging
import os
import re
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dt_time, timedelta
from typing import Optional

import psycopg2
import requests
from fastapi import APIRouter, HTTPException

log = logging.getLogger("scorr.options_chain")
router = APIRouter(prefix="/api/options", tags=["options_chain"])

DATABASE_URL    = os.getenv("DATABASE_URL")
FYERS_CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "1A4STS8ZGD-100")
QUOTES_URL      = "https://api-t1.fyers.in/data/quotes"
DEPTH_URL       = "https://api-t1.fyers.in/data/depth"
SYM_MASTER_URL  = "https://public.fyers.in/sym_details/NSE_FO.csv"

DEFAULT_N     = 10          # ATM ± N strikes (each side); param-overridable
MAX_N         = 30          # cap so a single request can never fan out unbounded
WORKERS       = 10          # depth-fetch concurrency (same as the backfill path)
MKT_OPEN, MKT_CLOSE = dt_time(9, 15), dt_time(15, 30)
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Index underlyings quote via their -INDEX symbol; stocks via NSE:<SYM>-EQ. Mirrors the worker's
# INDEX_OPTION_UNDERLYINGS / SPECIAL_SYMBOLS mapping so an index chain resolves CMP correctly too.
_INDEX_QUOTE_SYM = {
    "NIFTY":     "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
    "FINNIFTY":  "NSE:FINNIFTY-INDEX",
    "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
}
_SPECIAL_EQ = {"M&M": "NSE:M&M-EQ"}

# Symbol-master cache: listed strikes don't change intraday, so cache the CSV for a while to avoid
# re-downloading a multi-MB file on every request. Guarded by a lock (concurrent requests).
_MASTER_TTL_SEC = 3600
_master_cache = {"text": None, "at": 0.0}
_master_lock = threading.Lock()


def _conn():
    return psycopg2.connect(DATABASE_URL)


def _hdr(token):
    return {"Authorization": f"{FYERS_CLIENT_ID}:{token}"}


def _load_token(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT access_token FROM fyers_tokens WHERE id=1")
        r = cur.fetchone()
    if not r or not r[0]:
        raise RuntimeError("No Fyers access_token in fyers_tokens (id=1)")
    return r[0]


def _quote_symbol(underlying: str) -> str:
    u = underlying.upper()
    if u in _INDEX_QUOTE_SYM:
        return _INDEX_QUOTE_SYM[u]
    if u in _SPECIAL_EQ:
        return _SPECIAL_EQ[u]
    return f"NSE:{u}-EQ"


def _live_cmp(token: str, underlying: str) -> Optional[float]:
    """Live CMP for the underlying from the Fyers quotes API (v.lp)."""
    try:
        r = requests.get(QUOTES_URL, params={"symbols": _quote_symbol(underlying)},
                         headers=_hdr(token), timeout=6)
        d = r.json()
        if d.get("s") != "ok":
            return None
        for item in d.get("d", []):
            lp = (item.get("v") or {}).get("lp")
            if lp:
                return float(lp)
    except Exception as e:
        log.warning(f"_live_cmp {underlying}: {e}")
    return None


def _last_tuesday(y, m):
    d = date(y, m, calendar.monthrange(y, m)[1])
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
    """Cached fetch of the Fyers public NSE_FO symbol master (real listed strikes)."""
    now = _time.time()
    with _master_lock:
        if _master_cache["text"] and (now - _master_cache["at"]) < _MASTER_TTL_SEC:
            return _master_cache["text"]
    r = requests.get(SYM_MASTER_URL, timeout=90)
    r.raise_for_status()
    with _master_lock:
        _master_cache["text"] = r.text
        _master_cache["at"] = now
    return r.text


def _resolve_strikes(master_text: str, underlying: str, spot: float, n: int, today: date):
    """Real listed strikes for the current monthly expiry from the symbol master, nearest 2N+1 to
    spot. Regex over raw lines (no column-order assumptions); re.escape handles digit/hyphen/&
    tickers (360ONE, BAJAJ-AUTO, M&M). Returns (expiry_code, expiry_date, sorted_strikes)."""
    pat = re.compile(r"NSE:" + re.escape(underlying.upper()) + r"(\d{2}[A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)\b")
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
    strikes = sorted(by_code[code], key=lambda s: abs(s - spot))[: 2 * n + 1]
    return code, _code_to_expiry(code), sorted(strikes)


def _fmt_strike(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else str(strike)


def _fetch_quote(token, underlying, code, strike, otype):
    """Live ltp/oi/bid/ask for one contract via the depth API (quotes has NO OI)."""
    sym = f"NSE:{underlying.upper()}{code}{_fmt_strike(strike)}{otype}"
    out = {"symbol": sym, "strike": strike, "option_type": otype,
           "ltp": None, "oi": None, "bid": None, "ask": None}
    try:
        r = requests.get(DEPTH_URL, params={"symbol": sym, "ohlcv_flag": 1},
                         headers=_hdr(token), timeout=8)
        body = (r.text or "").strip()
        if not body or r.status_code == 401:
            return out
        d = r.json()
        if d.get("s") != "ok":
            return out
        data_d = d.get("d")
        node = {}
        if isinstance(data_d, dict):
            node = data_d.get(sym) or (next(iter(data_d.values())) if data_d else {})
        elif isinstance(data_d, list) and data_d and isinstance(data_d[0], dict):
            node = data_d[0].get("v", data_d[0])
        if not isinstance(node, dict):
            return out
        out["ltp"] = _num(node.get("ltp"))
        out["oi"]  = _num(node.get("oi"))
        out["bid"] = _best(node.get("bids")) if node.get("bids") is not None else _num(node.get("bid"))
        out["ask"] = _best(node.get("ask"))  if node.get("ask")  is not None else _num(node.get("ask"))
    except Exception as e:
        log.warning(f"_fetch_quote {sym}: {e}")
    return out


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _best(book):
    """Best (top-of-book) price from a Fyers depth bids/ask array [{price,volume,ord}, ...]."""
    try:
        if isinstance(book, list) and book and isinstance(book[0], dict):
            return _num(book[0].get("price"))
    except Exception:
        pass
    return _num(book)


@router.get("/chain")
def options_chain(underlying: str, n: int = DEFAULT_N):
    """cc#567 on-demand option chain — LIVE from Fyers REST, ephemeral (NOT persisted).
    underlying = NSE symbol (RELIANCE, TCS, NIFTY, ...). n = ATM ± N strikes (default 10)."""
    underlying = (underlying or "").strip().upper()
    if not underlying:
        raise HTTPException(400, "underlying is required")
    if not re.fullmatch(r"[A-Z0-9&\-]+", underlying):
        raise HTTPException(400, "invalid underlying")
    n = max(1, min(int(n or DEFAULT_N), MAX_N))

    today = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()   # IST date for expiry math
    conn = _conn()
    try:
        token = _load_token(conn)
    finally:
        conn.close()

    cmp_px = _live_cmp(token, underlying)
    if cmp_px is None:
        raise HTTPException(502, f"no live CMP for {underlying} (Fyers quotes) — cannot build chain")

    master_text = _load_symbol_master()
    code, expiry, strikes = _resolve_strikes(master_text, underlying, cmp_px, n, today)
    if not strikes:
        raise HTTPException(404, f"no listed option strikes for {underlying} in the Fyers symbol master")

    atm = min(strikes, key=lambda s: abs(s - cmp_px))

    quotes = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(_fetch_quote, token, underlying, code, s, ot)
                for s in strikes for ot in ("CE", "PE")]
        for f in as_completed(futs, timeout=120):
            try:
                q = f.result(timeout=15)
                quotes[(q["strike"], q["option_type"])] = q
            except Exception as e:
                log.warning(f"chain {underlying}: contract fetch failed: {e}")

    def _leg(strike, ot):
        q = quotes.get((strike, ot)) or {}
        return {"symbol": q.get("symbol"), "ltp": q.get("ltp"), "oi": q.get("oi"),
                "bid": q.get("bid"), "ask": q.get("ask")}

    chain = [{"strike": s, "is_atm": (s == atm), "ce": _leg(s, "CE"), "pe": _leg(s, "PE")}
             for s in strikes]

    return {
        "underlying": underlying,
        "cmp": round(cmp_px, 2),
        "atm": atm,
        "expiry": str(expiry) if expiry else None,
        "expiry_code": code,
        "n": n,
        "strikes": chain,
        "source": "fyers_rest_live",
        "persisted": False,
        "generated_at": (datetime.utcnow() + timedelta(hours=5, minutes=30)).isoformat(),
        "note": "ephemeral display-only chain — not written to option_chain (cc#567)",
    }
