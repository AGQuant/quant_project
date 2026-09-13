"""chart_oneday_endpoints.py — cc#2043 UNIVERSAL RULE: anywhere in the app, a chart/C button's
ONE DAY view fetches on demand from Yahoo Finance — never Fyers (founder-explicit: minimize added
Fyers REST load, given worker/fyers_feed.py's own history of REST-throttling incidents), never a
WebSocket subscription, never a database write. ONE canonical function; every current and future
"1 day" chart affordance calls it rather than growing its own fetch.

Reuses yahoo_ondemand.fetch_intraday() (this project's existing, already-shipped, DB-write-free
Yahoo chart-API client — cmp_resolver.py/gvm_market_endpoints.py/yahoo_index_backfill.py already
depend on it) directly, NOT the general-purpose /api/intraday_ondemand/{symbol} route (that one
supports source='auto'/'fyers', which this universal rule explicitly forbids) and NOT
/api/bars/{symbol} (ondemand_bars.py — that one is "FYERS FIRST, YAHOO SECOND" by its own
docstring, the opposite of what this rule requires). A purpose-built, Yahoo-only entry point makes
it impossible for a future caller to accidentally reach Fyers through this function.
"""
import asyncio

from fastapi import APIRouter, HTTPException

import yahoo_ondemand

router = APIRouter()


def _one_day_bars_sync(symbol: str) -> dict:
    """The one canonical fetch. Pulls a few days of buffer from Yahoo (a bare 1-calendar-day
    window can be genuinely empty over a weekend/holiday) and returns only the LATEST actual
    trading session's bars — the same session-anchor discipline this codebase already applies
    elsewhere (e.g. mobile_home2.py's own MAX(ts)::date anchor: the axis is the last real session
    with data, never "today" blindly). Never fabricates a bar Yahoo did not return; an empty or
    short session is stated in `note`, not padded."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(400, "symbol required")
    try:
        bars = yahoo_ondemand.fetch_intraday(sym, days=5, interval="5m")
    except Exception as e:
        raise HTTPException(502, f"Yahoo fetch failed for {sym}: {e}")
    if not bars:
        return {"symbol": sym, "source": "yahoo", "session_date": None, "bars": [],
                "note": "Yahoo returned no intraday bars for this symbol."}
    latest_date = bars[-1]["ts"][:10]
    day_bars = [b for b in bars if b["ts"][:10] == latest_date]
    note = None if len(day_bars) >= 10 else f"only {len(day_bars)} bar(s) available for {latest_date}"
    return {"symbol": sym, "source": "yahoo", "session_date": latest_date, "bars": day_bars, "note": note}


@router.get("/api/chart/oneday/{symbol}")
async def chart_oneday(symbol: str):
    """GET /api/chart/oneday/{symbol} -> {symbol, source:'yahoo', session_date, bars:[{ts,open,
    high,low,close,volume}], note}. Request-scoped only — no DB write, no subscription, callable
    for ANY symbol regardless of futures_universe membership (that decision belongs to the caller,
    e.g. scorr_chart_card.js's own 1D-pill logic; this function itself never branches on it)."""
    return await asyncio.to_thread(_one_day_bars_sync, symbol)
