"""
ondemand_bars.py — cc#1103 ON-DEMAND 5-MIN BARS FOR SYMBOLS THAT LEFT THE LIVE FEED
==================================================================================
    GET /api/bars/{symbol}          -> 5-min candles, newest last, 600 by default
    GET /api/bars/_cache/stats      -> what the cache is holding right now

WHY THIS EXISTS. The live WebSocket carried an extended equity leg — 500 symbols today, 1,408 at
its widest — writing roughly 37,000 bars a day for names no engine reads, against a broker
subscription ceiling that has already cost one 94-minute outage. Founder ruled 19-Aug: the live
feed keeps the 208 active futures, their equity legs, the indices and three benchmark ETFs.
Everything else is PULLED WHEN SOMEONE ACTUALLY LOOKS AT IT. This is that pull.

FYERS FIRST, YAHOO SECOND, AND THE SOURCE IS ALWAYS NAMED. Fyers is the same REST history endpoint
the backfill uses, so an on-demand chart and a backfilled one come from one place. Yahoo is the
fallback, and every response says which one answered — a chart that silently changes provider is a
chart whose gaps nobody can explain.

NOTHING IS WRITTEN TO intraday_prices. This module is READ-ONLY against the database. The feed
worker owns that table; a page view must never be able to inject bars into it, because the moment
it can, a chart open at 09:20 becomes a data source the engines read. That was the whole shape of
the wrong-price paper entry that _assert_not_market_hours exists to prevent in fyers_backfill.

THE CACHE, and the TTL choice stated rather than assumed. A page that opens twenty symbols must not
fire twenty cold pulls every time it is refreshed, so results are held in-process:

    LIVE  (during the session)      60s   — one 5-min bar is 300s wide, so a 60s TTL can never
                                            show a bar more than one fifth of a bar out of date,
                                            while collapsing a burst of page loads to one fetch.
    CLOSED (outside market hours)  900s   — the last bar is final and cannot change until the next
                                            open, so a short TTL here would be pure API cost. It is
                                            15 minutes rather than "until the open" so that a
                                            holiday or a late session correction still heals itself.

The cache is per-process and bounded. It is a burst absorber, not a store: a restart loses it and
nothing is worse for that.
"""

import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
import psycopg2
from fastapi import APIRouter, HTTPException, Query

log = logging.getLogger("scorr.ondemand")
router = APIRouter(tags=["bars"])

_DB = os.getenv("DATABASE_URL", "")
_FYERS_CLIENT_ID = os.getenv("FYERS_CLIENT_ID", "")
_HISTORY_URL = "https://api-t1.fyers.in/data/history"

DEFAULT_BARS = 600
MAX_BARS = 2000

TTL_LIVE_SEC = 60
TTL_CLOSED_SEC = 900

# A hard ceiling on the in-process cache. Bounded because this is a burst absorber, not a store —
# an unbounded dict on a long-lived web process is a memory leak with a friendly name.
CACHE_MAX_ENTRIES = 400

# cc#1103 item 5, the CONFIG CEILING. Incident 8 was a limit of 0 meaning "no limit", which asked
# the broker for 3,574 symbols instead of ~650 and got the whole batch force-closed for 94 minutes.
# So in this module 0 NEVER means unlimited: a non-positive, missing or unparseable bar count falls
# back to DEFAULT_BARS, and anything above MAX_BARS is clamped DOWN. There is no code path that
# turns a bad number into a bigger request.
def _safe_bars(n) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return DEFAULT_BARS
    if n <= 0:
        return DEFAULT_BARS
    return min(n, MAX_BARS)


_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "fyers": 0, "yahoo": 0, "empty": 0}


def _market_is_open() -> bool:
    """Rough IST session test. Deliberately rough: it only picks a CACHE TTL, never gates data."""
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    return now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 35)


def _ttl() -> int:
    return TTL_LIVE_SEC if _market_is_open() else TTL_CLOSED_SEC


def _cache_get(key: str):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (time.time() - hit["at"]) < hit["ttl"]:
            _stats["hits"] += 1
            return hit
        _stats["misses"] += 1
        return None


def _cache_put(key: str, payload: dict, ttl: int):
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            # Evict the oldest. A cheap policy on purpose — a smarter one would need its own
            # bookkeeping, and the thing being protected here is a burst, not a working set.
            for k in sorted(_cache, key=lambda k: _cache[k]["at"])[:CACHE_MAX_ENTRIES // 4]:
                _cache.pop(k, None)
        _cache[key] = {"at": time.time(), "ttl": ttl, **payload}


def _fyers_token() -> Optional[str]:
    try:
        with psycopg2.connect(_DB) as conn, conn.cursor() as cur:
            cur.execute("SELECT access_token FROM fyers_tokens WHERE id = 1")
            row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        log.warning("ondemand: token read failed (%s) — Yahoo only this call", e)
        return None


def _fyers_symbol(symbol: str) -> str:
    return {"M&M": "NSE:M&M-EQ"}.get(symbol, f"NSE:{symbol}-EQ")


def _from_fyers(symbol: str, bars: int) -> List[dict]:
    token = _fyers_token()
    if not token:
        return []
    # 5-min bars: ~75 a session. Ask for enough CALENDAR days to cover `bars` trading sessions with
    # room for weekends and holidays, then trim to `bars` at the end — asking by day count and
    # trimming by bar count is the only way to get exactly N bars from a date-ranged API.
    days = max(7, int(bars / 75 * 1.6) + 5)
    to_d = date.today()
    params = {"symbol": _fyers_symbol(symbol), "resolution": "5", "date_format": "1",
              "range_from": (to_d - timedelta(days=days)).strftime("%Y-%m-%d"),
              "range_to": to_d.strftime("%Y-%m-%d"), "cont_flag": "1"}
    try:
        r = httpx.get(_HISTORY_URL, params=params,
                      headers={"Authorization": f"{_FYERS_CLIENT_ID}:{token}"}, timeout=12)
        d = r.json()
    except Exception as e:
        log.warning("ondemand fyers %s: %s", symbol, e)
        return []
    candles = d.get("candles") if isinstance(d, dict) else None
    if not candles:
        log.warning("ondemand fyers EMPTY %s: %s", symbol, str(d)[:200])
        return []
    out = []
    for c in candles:
        ts = datetime.utcfromtimestamp(c[0]) + timedelta(hours=5, minutes=30)
        out.append({"ts": ts.isoformat(), "open": c[1], "high": c[2],
                    "low": c[3], "close": c[4], "volume": int(c[5])})
    return out[-bars:]


def _from_yahoo(symbol: str, bars: int) -> List[dict]:
    """The Yahoo leg, delegated to yahoo_ondemand.fetch_intraday.

    NOT re-implemented here. yahoo_ondemand has been the project's Yahoo intraday fetcher since the
    futures-only era — it owns the ticker overrides, the per-interval day caps, the null-padding
    skip and the precise window trim. A second Yahoo client in this file would have looked shorter
    and been wrong in a different way on the first symbol whose ticker needs an override.
    """
    try:
        import yahoo_ondemand
        # Ask by DAYS because that is Yahoo's unit, then trim to `bars`. ~75 five-minute bars a
        # session, padded for weekends and holidays; fetch_intraday clamps to Yahoo's own 60-day
        # cap for 5m, so an over-large ask degrades to the cap rather than erroring.
        days = max(7, int(bars / 75 * 1.6) + 5)
        rows = yahoo_ondemand.fetch_intraday(symbol, days=days, interval="5m") or []
    except Exception as e:
        log.warning("ondemand yahoo %s: %s", symbol, e)
        return []
    return [{"ts": r["ts"], "open": r.get("open"), "high": r.get("high"),
             "low": r.get("low"), "close": r.get("close"), "volume": r.get("volume")}
            for r in rows][-bars:]


@router.get("/api/bars/_cache/stats")
def cache_stats():
    """What the cache is actually doing. Without this the TTL choice is an assertion, not a measurement."""
    with _cache_lock:
        entries = len(_cache)
        oldest = min((v["at"] for v in _cache.values()), default=None)
    return {"entries": entries, "max_entries": CACHE_MAX_ENTRIES,
            "ttl_now_sec": _ttl(), "market_open": _market_is_open(),
            "ttl_live_sec": TTL_LIVE_SEC, "ttl_closed_sec": TTL_CLOSED_SEC,
            "oldest_entry_age_sec": round(time.time() - oldest, 1) if oldest else None,
            **_stats}


@router.get("/api/bars/{symbol}")
def bars(symbol: str, count: int = Query(DEFAULT_BARS), refresh: bool = Query(False)):
    """5-min candles for one symbol, pulled on demand. Fyers first, Yahoo fallback.

    `refresh=true` bypasses the cache for one call. It does NOT clear the entry for everyone else —
    a debug switch that empties a shared cache is a way to turn one person's investigation into
    everybody's latency.
    """
    sym = (symbol or "").strip().upper()
    if not sym or len(sym) > 32:
        raise HTTPException(400, "bad symbol")
    n = _safe_bars(count)
    key = f"{sym}|{n}"

    if not refresh:
        hit = _cache_get(key)
        if hit:
            return {"symbol": sym, "count": len(hit["candles"]), "candles": hit["candles"],
                    "source": hit["source"], "cached": True,
                    "age_sec": round(time.time() - hit["at"], 1), "ttl_sec": hit["ttl"]}

    t0 = time.time()
    candles, source = _from_fyers(sym, n), "fyers_rest"
    if not candles:
        candles, source = _from_yahoo(sym, n), "yahoo"
    if not candles:
        _stats["empty"] += 1
        # No bars is stated, never served as an empty chart that looks like a flat day.
        raise HTTPException(404, f"no 5-min bars available for {sym} from Fyers or Yahoo")
    _stats["fyers" if source == "fyers_rest" else "yahoo"] += 1

    ttl = _ttl()
    _cache_put(key, {"candles": candles, "source": source}, ttl)
    return {"symbol": sym, "count": len(candles), "candles": candles, "source": source,
            "cached": False, "elapsed_ms": round((time.time() - t0) * 1000, 1), "ttl_sec": ttl}
