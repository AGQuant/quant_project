"""
yahoo_index_backfill.py — Scorr
================================
One-shot / repeatable backfill of INDEX 1-min OHLC into intraday_prices,
fetched from Yahoo (interim source until the Fyers feed subscribes to the
index symbols — see session_log id 38).

Indices: NIFTY50 (^NSEI), BANKNIFTY (^NSEBANK).
Yahoo 1-min history reaches back ~7 days, so this fills the trailing week —
exactly the window the paper-engine replay sim needs for the intraday gate.

Writes to intraday_prices with:
    symbol    = 'NIFTY50' | 'BANKNIFTY'   (NOT the futures-stock universe)
    ts        = naive IST (matches the Fyers futures rows — read RAW, no TZ math)
    timeframe = '1m'
    source    = 'yahoo'
ON CONFLICT (symbol, ts, timeframe) DO NOTHING — safe to re-run.

These index rows are intentionally OUTSIDE futures_universe, so they do NOT
leak into /api/v8/raw, Filter_Scan, or the paper signal universe (those JOIN
futures_universe). They are read ONLY by the market-mood gate (Nifty D/W/M);
ADR has its own breadth calc.
"""

import logging
import os
import psycopg
import yahoo_ondemand

log = logging.getLogger("scorr.idxbackfill")

# Yahoo tickers for the NSE indices
INDEX_YSYM = {
    "NIFTY50":   "^NSEI",
    "BANKNIFTY": "^NSEBANK",
}

_INSERT_SQL = """
INSERT INTO intraday_prices (symbol, ts, open, high, low, close, volume, timeframe, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, '1m', 'yahoo')
ON CONFLICT (symbol, ts, timeframe) DO NOTHING
"""


def _fetch_one(symbol: str, days: int = 7):
    """Fetch index 1-min candles from Yahoo via yahoo_ondemand (no DB write here).
    yahoo_ondemand.fetch_intraday appends '.NS' by default, so we register the
    caret tickers in its override map for this call."""
    yahoo_ondemand.YSYM_OVERRIDE[symbol] = INDEX_YSYM[symbol]
    return yahoo_ondemand.fetch_intraday(symbol, days=days, interval="1m", exchange="NS")


def backfill_indices(days: int = 7, symbols=None) -> dict:
    """Backfill the given index symbols (default both) into intraday_prices.
    Returns per-symbol counts. UPSERT-safe / idempotent."""
    dburl = os.environ.get("DATABASE_URL")
    if not dburl:
        return {"status": "error", "msg": "DATABASE_URL not set"}

    symbols = symbols or list(INDEX_YSYM.keys())
    out = {}
    with psycopg.connect(dburl) as conn:
        for sym in symbols:
            if sym not in INDEX_YSYM:
                out[sym] = {"status": "skip", "msg": "unknown index symbol"}
                continue
            try:
                candles = _fetch_one(sym, days=days)
            except Exception as e:
                out[sym] = {"status": "fetch_error", "msg": str(e)[:120]}
                log.warning(f"index backfill fetch {sym}: {e}")
                continue

            written = 0
            with conn.cursor() as cur:
                for c in candles:
                    cur.execute(_INSERT_SQL, (
                        sym, c["ts"], c["open"], c["high"], c["low"],
                        c["close"], c.get("volume", 0),
                    ))
                    written += cur.rowcount
                conn.commit()

            first_ts = candles[0]["ts"] if candles else None
            last_ts  = candles[-1]["ts"] if candles else None
            out[sym] = {"status": "ok", "fetched": len(candles),
                        "inserted_new": written, "first_ts": first_ts, "last_ts": last_ts}
            log.info(f"index backfill {sym}: fetched {len(candles)}, new {written}")
    return {"status": "done", "days": days, "results": out}


if __name__ == "__main__":
    import json
    print(json.dumps(backfill_indices(), indent=2, default=str))
