"""
Feed Health Monitor — GET /api/health/feed  (cc_task #85, P0 go-live blocker).

Public (no-auth) health check for the Fyers live feed. Reports GREEN / YELLOW /
RED from how many active stock-futures have written a 5-min bar in the last
GAP_THRESHOLD_MIN minutes during market hours. The index symbols
(NIFTY50 / BANKNIFTY / INDIAVIX) ride the 30s quotes poll, not the WS, so they
are checked separately under index_feed.

Status logic (spec session_log id=681):
  GREEN  — all futures have a bar within the last GAP_THRESHOLD_MIN.
  YELLOW — 1-19 symbols stale OR overall gap 10-20 min.
  RED    — 20+ symbols stale OR overall gap > 20 min OR the index feed is down.
  Outside market hours — always GREEN with note "market closed".

IMPORTANT: intraday_prices.ts is NAIVE IST. Postgres NOW() is UTC, so the
recency cutoff is computed in Python (IST) and passed as a parameter — never
NOW() (a NOW()-based window silently matches every bar of the day).
"""

import os
from datetime import datetime, timedelta, timezone, time as dt_time
import psycopg
from fastapi import APIRouter

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

GAP_THRESHOLD_MIN = 10                       # no bar in this window -> symbol is "stale"
INDEX_SYMBOLS = ["NIFTY50", "BANKNIFTY", "INDIAVIX"]
# Pseudo / index contracts to exclude from the stock-futures health set.
NON_FUTURES = INDEX_SYMBOLS + ["NIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)
STALE_LIST_CAP = 50                          # cap the stale_symbols list in the response
RED_STALE = 20                               # >= this many stale -> RED
RED_GAP_MIN = 20                             # overall gap > this -> RED


def _ist_now():
    return datetime.now(IST).replace(tzinfo=None)


def _market_open(now):
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


@router.get("/api/health/feed")
def feed_health():
    now = _ist_now()
    cutoff = now - timedelta(minutes=GAP_THRESHOLD_MIN)
    market_live = _market_open(now)

    resp = {
        "status": "GREEN",
        "summary": {"symbols_stale": 0, "total_symbols": 0,
                    "symbols_healthy": 0, "gap_threshold_minutes": GAP_THRESHOLD_MIN},
        "index_feed": {"status": "GREEN", "symbols": INDEX_SYMBOLS, "last_bar": None},
        "futures_feed": {"status": "GREEN", "gap_minutes": None, "stale_count": 0,
                         "stale_symbols": [], "last_bar_overall": None},
        "market_open": market_live,
        "last_updated": now.strftime("%Y-%m-%d %H:%M:%S IST"),
    }

    try:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT symbol FROM futures_universe
                WHERE is_active = TRUE AND symbol <> ALL(%s)
            """, (NON_FUTURES,))
            fut_syms = [r[0] for r in cur.fetchall()]
            total = len(fut_syms)

            recent = set()
            if fut_syms:
                cur.execute("""
                    SELECT DISTINCT symbol FROM intraday_prices
                    WHERE ts >= %s AND symbol = ANY(%s)
                """, (cutoff, fut_syms))
                recent = {r[0] for r in cur.fetchall()}
            stale_syms = sorted(s for s in fut_syms if s not in recent)
            healthy = total - len(stale_syms)

            cur.execute("""
                SELECT MAX(ts) FROM intraday_prices
                WHERE symbol = ANY(%s) AND ts::date = CURRENT_DATE
            """, (fut_syms or [""],))
            fut_last = (cur.fetchone() or [None])[0]
            gap_min = round((now - fut_last).total_seconds() / 60, 1) if fut_last else None

            cur.execute("""
                SELECT MAX(ts) FROM intraday_prices
                WHERE symbol = ANY(%s) AND ts::date = CURRENT_DATE
            """, (INDEX_SYMBOLS,))
            idx_last = (cur.fetchone() or [None])[0]

        idx_gap = (now - idx_last).total_seconds() / 60 if idx_last else None
        index_down = market_live and (idx_last is None or (idx_gap is not None and idx_gap > GAP_THRESHOLD_MIN))

        resp["summary"].update({"symbols_stale": len(stale_syms),
                                "total_symbols": total, "symbols_healthy": healthy})
        resp["futures_feed"].update({
            "gap_minutes": gap_min,
            "stale_count": len(stale_syms),
            "stale_symbols": stale_syms[:STALE_LIST_CAP],
            "last_bar_overall": fut_last.strftime("%Y-%m-%d %H:%M:%S") if fut_last else None,
        })
        resp["index_feed"].update({
            "last_bar": idx_last.strftime("%Y-%m-%d %H:%M:%S") if idx_last else None,
            "status": "RED" if index_down else "GREEN",
        })

        if not market_live:
            resp["status"] = "GREEN"
            resp["futures_feed"]["status"] = "GREEN"
            resp["index_feed"]["status"] = "GREEN"
            resp["note"] = "market closed"
            return resp

        n_stale = len(stale_syms)
        red = (n_stale >= RED_STALE) or (gap_min is not None and gap_min > RED_GAP_MIN) or index_down
        yellow = (1 <= n_stale < RED_STALE) or (gap_min is not None and GAP_THRESHOLD_MIN < gap_min <= RED_GAP_MIN)
        resp["futures_feed"]["status"] = "RED" if (n_stale >= RED_STALE or (gap_min is not None and gap_min > RED_GAP_MIN)) \
            else ("YELLOW" if yellow else "GREEN")
        resp["status"] = "RED" if red else ("YELLOW" if yellow else "GREEN")
        return resp
    except Exception as e:
        resp["status"] = "RED"
        resp["error"] = f"feed health check failed: {str(e)[:160]}"
        return resp
