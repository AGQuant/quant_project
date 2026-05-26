"""
yahoo_daily_update.py — v2.0
Chart API async update — replaces slow yf.Ticker().history()
Fetches last 10 days OHLC for all symbols in raw_prices (~1720 stocks).
Uses httpx async with semaphore=8. ~3 min for full universe.
Safe to re-run: UPSERT on (symbol, price_date).
"""

import asyncio
import os
import datetime
import psycopg
import httpx
import urllib.parse
import logging

log = logging.getLogger("yahoo_daily")

DB_URL = os.environ.get("DATABASE_URL")
LOOKBACK = "10d"
SEMAPHORE = 8

UPSERT_SQL = """
INSERT INTO raw_prices (symbol, price_date, open, high, low, close, adjusted_close, volume)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (symbol, price_date) DO UPDATE SET
    open           = EXCLUDED.open,
    high           = EXCLUDED.high,
    low            = EXCLUDED.low,
    close          = EXCLUDED.close,
    adjusted_close = EXCLUDED.adjusted_close,
    volume         = EXCLUDED.volume;
"""

INDICES = {
    "NIFTY50":   "^NSEI",
    "BANKNIFTY": "^NSEBANK",
}

def _to_yahoo_ticker(symbol: str) -> str:
    return INDICES.get(symbol, symbol + ".NS")


async def _fetch_symbol(client: httpx.AsyncClient, sem: asyncio.Semaphore, symbol: str):
    ticker = _to_yahoo_ticker(symbol)
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(ticker)}?interval=1d&range={LOOKBACK}"
    )
    async with sem:
        try:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            chart = data.get("chart", {}).get("result", [])
            if not chart:
                return symbol, []
            result   = chart[0]
            tss      = result.get("timestamp", [])
            q        = result.get("indicators", {}).get("quote", [{}])[0]
            adj_list = result.get("indicators", {}).get("adjclose", [])
            adj_closes = adj_list[0].get("adjclose", []) if adj_list else []

            opens   = q.get("open",   [])
            highs   = q.get("high",   [])
            lows    = q.get("low",    [])
            closes  = q.get("close",  [])
            volumes = q.get("volume", [])

            rows = []
            for i, ts in enumerate(tss):
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                o  = opens[i]   if i < len(opens)   else None
                h  = highs[i]   if i < len(highs)   else None
                l  = lows[i]    if i < len(lows)    else None
                v  = volumes[i] if i < len(volumes)  else None
                ac = adj_closes[i] if i < len(adj_closes) else c
                dt = datetime.datetime.utcfromtimestamp(ts).date()
                rows.append((
                    symbol, dt,
                    round(float(o), 2) if o is not None else None,
                    round(float(h), 2) if h is not None else None,
                    round(float(l), 2) if l is not None else None,
                    round(float(c), 2),
                    round(float(ac), 2) if ac is not None else None,
                    int(v) if v is not None else 0,
                ))
            return symbol, rows
        except Exception as e:
            log.warning(f"yahoo_daily {symbol}: {e}")
            return symbol, []
        finally:
            await asyncio.sleep(0.05)


async def run_async(symbols=None, lookback=None):
    """
    Main async entry point. If symbols is None, fetches all from raw_prices.
    Returns dict with stats.
    """
    global LOOKBACK
    if lookback:
        LOOKBACK = lookback

    if symbols is None:
        with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM raw_prices ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]

    if not symbols:
        return {"updated": 0, "failed": 0, "rows": 0}

    sem = asyncio.Semaphore(SEMAPHORE)
    failed = []
    total_rows = 0

    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    ) as client:
        tasks   = [_fetch_symbol(client, sem, s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        for symbol, rows in results:
            if not rows:
                failed.append(symbol)
                continue
            cur.executemany(UPSERT_SQL, rows)
            total_rows += len(rows)
        conn.commit()

    summary = {
        "symbols_attempted": len(symbols),
        "updated":           len(symbols) - len(failed),
        "failed":            len(failed),
        "rows_upserted":     total_rows,
        "failed_symbols":    failed[:20],
    }
    log.info(f"yahoo_daily done: {summary}")
    return summary


def main():
    """Sync wrapper — called by legacy code or manual CLI run."""
    import sys
    if "--test" in sys.argv:
        with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM raw_prices")
            print(f"OK — raw_prices has {cur.fetchone()[0]:,} rows")
        return
    result = asyncio.run(run_async())
    print(f"Done: {result}")


if __name__ == "__main__":
    main()
