"""
index_tape.py — cc#1054 INDEX 100-BAR TAPE
==========================================
Serves the rolling 100-bar 5-min CASH tape for NIFTY 50 and BANK NIFTY, plus the
session-boundary positions the renderer draws its day-breakers from.

WHY THIS IS ITS OWN ENDPOINT rather than a wider window on /api/v10/candles: that route
reads nifty_5m_test_data / banknifty_5m_test_data, which are the V10 engine's own append
tables — they only carry what the V10 appender has run for. This tape has to be the price
record itself, so it reads intraday_prices directly under the cc#1053 convention.

THE CASH FILTER IS THE POINT (INDEX_SYMBOL_CONVENTION_V1, session_log 23247).
BANKNIFTY carries its index FUTURES under the same symbol as its cash index, split only by
`source`. An unfiltered tape would interleave two instruments ~200 pts apart at alternating
timestamps and draw a sawtooth that is not a price series. The exclusion list is imported
from price_sources.py — never retyped here, and an EXCLUSION rather than an allow-list so a
new cash source joins the tape automatically instead of silently vanishing from it.

THE WINDOW IS COUNTED IN BARS THAT EXIST, NEVER IN BARS THAT SHOULD EXIST.
A full session is 75-76 five-minute bars, so "the prior session starts 75 bars back" is the
obvious shortcut and it is wrong. Measured: 06-Aug-2026 carries 17 bars and 07-Aug carries
37, both feed-gap days. Counting backwards would put the day-breaker in the middle of a
session. So the window is the last N DISTINCT timestamps that actually carry a cash bar, and
the breakers come from ts::date transitions inside the returned rows. On a gap day the tape
legitimately spans three sessions and draws two breakers — that is correct output, not a bug.

NO CURRENT_DATE ANYWHERE. The window ends at the last bar that exists, so a Sunday or a
holiday renders the previous session's tail rather than an empty chart (cc#1032 pattern).

DAY % IS NOT COMPUTED HERE, DELIBERATELY. The printed day-change number keeps coming from
whatever each surface already used (/api/v8/live_metrics, with domestic_live as fallback).
This endpoint reports WHERE the current session starts in the array; the caller colours from
that index onward and dims what is behind it. Lengthening the tape must not change what the
number means, and the surest way to guarantee that is to not touch the number.
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, HTTPException

from price_sources import not_fut   # cc#1053 INDEX_SYMBOL_CONVENTION_V1

log = logging.getLogger("scorr.index_tape")
router = APIRouter(prefix="/api/index", tags=["index"])

IST = ZoneInfo("Asia/Kolkata")

# cc#1053: cash symbol per index. NIFTY50 and BANKNIFTY are BOTH the cash names — the Nifty
# FUTURES leg lives under the separate symbol `NIFTY` (the documented Nifty exception), and
# the BankNifty futures leg lives under BANKNIFTY itself. Neither is reachable from here:
# the source filter excludes both futures legs regardless of which symbol carries them.
INDEX_TAPE = [
    {"symbol": "NIFTY50",   "label": "NIFTY 50"},
    {"symbol": "BANKNIFTY", "label": "BANK NIFTY"},
]

DEFAULT_BARS = 100
MAX_BARS = 400
TIMEFRAME = "5m"


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


@router.get("/tape")
def index_tape(bars: int = DEFAULT_BARS):
    """Rolling last-N 5-min cash bars for both indices on ONE shared timestamp window."""
    try:
        n = max(2, min(int(bars), MAX_BARS))
    except (TypeError, ValueError):
        raise HTTPException(400, "bars must be an integer")

    syms = [c["symbol"] for c in INDEX_TAPE]
    nf = not_fut()

    with _conn() as conn, conn.cursor() as cur:
        # The window: the last n timestamps that carry a cash bar for EITHER index. Shared
        # across both so the two tapes span the same wall clock and their day-breakers land
        # on the same x — two charts on different windows cannot be read side by side.
        cur.execute("""
            SELECT ts FROM (
                SELECT DISTINCT ts FROM intraday_prices
                WHERE symbol = ANY(%s) AND timeframe = %s
                  AND close IS NOT NULL
                  AND COALESCE(source,'') <> ALL(%s)
                ORDER BY ts DESC LIMIT %s
            ) w ORDER BY ts ASC
        """, (syms, TIMEFRAME, nf, n))
        window = [r[0] for r in cur.fetchall()]
        if not window:
            return {"status": "empty", "bars": 0, "indices": {},
                    "note": "no cash index bars in intraday_prices"}

        # DISTINCT ON guards against a future cash source ever writing a second row at a
        # timestamp another cash source already holds. There are none today (verified
        # 16-Aug-2026 over 01-Aug onward), and a duplicate would draw a vertical line.
        cur.execute("""
            SELECT DISTINCT ON (symbol, ts) symbol, ts, close
            FROM intraday_prices
            WHERE symbol = ANY(%s) AND timeframe = %s
              AND ts >= %s AND ts <= %s
              AND close IS NOT NULL
              AND COALESCE(source,'') <> ALL(%s)
            ORDER BY symbol, ts, source
        """, (syms, TIMEFRAME, window[0], window[-1], nf))
        by_sym = {}
        for sym, ts, close in cur.fetchall():
            by_sym.setdefault(sym, []).append({"ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
                                               "close": float(close)})

    out = {}
    for cfg in INDEX_TAPE:
        rows = by_sym.get(cfg["symbol"]) or []
        dates = [r["ts"][:10] for r in rows]
        # Breakers are the POSITIONS of the first bar of each new session inside the rows we
        # are actually returning. Derived from the data, never from a bar count.
        breaks = [i for i in range(1, len(dates)) if dates[i] != dates[i - 1]]
        session_date = dates[-1] if dates else None
        # Where the CURRENT (latest) session begins. The caller draws everything from here
        # in the day-change colour and dims what is behind it.
        session_start = 0
        for i in range(len(dates) - 1, -1, -1):
            if dates[i] != session_date:
                session_start = i + 1
                break
        out[cfg["symbol"]] = {
            "label": cfg["label"],
            "rows": rows,
            "breaks": breaks,
            "session_date": session_date,
            "session_start": session_start if dates else 0,
            "sessions": sorted(set(dates)),
        }

    return {
        "status": "ok",
        "bars": len(window),
        "requested": n,
        "window": {"from": window[0].strftime("%Y-%m-%d %H:%M:%S"),
                   "to": window[-1].strftime("%Y-%m-%d %H:%M:%S")},
        "timeframe": TIMEFRAME,
        "basis": "cash leg only — INDEX_SYMBOL_CONVENTION_V1 (session_log 23247)",
        "as_of": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "indices": out,
    }
