"""
Global Indices — Scorr V8 / Daily Digest gap #1
================================================
Fetches daily close + prev close for global indices, commodities, currency
via Yahoo CHART API one-at-a-time (proven reliable; batch yfinance is BROKEN).

Feeds Daily Digest section: OVERNIGHT SCORECARD.
Table: global_indices (UPSERT on symbol, quote_date).

Wired into main.py: daily 07:00 IST scheduler + /api/global + get_global MCP tool.
Standalone:  py -3.11 -c "import asyncio,global_indices as g; print(asyncio.run(g.fetch_global_indices(g.get_conn_from_env())))"
"""
import os
import asyncio
import logging
import urllib.parse
from datetime import datetime, timedelta

import httpx
import psycopg

log = logging.getLogger("scorr.global_indices")

GLOBAL_TICKERS = [
    ("^DJI", "Dow", "index"),
    ("^GSPC", "S&P 500", "index"),
    ("^IXIC", "Nasdaq", "index"),
    ("^VIX", "US VIX", "volatility"),
    ("^N225", "Nikkei", "index"),
    ("^FTSE", "FTSE", "index"),
    ("^GDAXI", "DAX", "index"),
    ("000001.SS", "Shanghai", "index"),
    ("BZ=F", "Brent", "commodity"),
    ("CL=F", "WTI", "commodity"),
    ("GC=F", "Gold", "commodity"),
    ("SI=F", "Silver", "commodity"),
    ("INR=X", "USDINR", "currency"),
    ("DX-Y.NYB", "DXY", "currency"),
]

DDL = """
CREATE TABLE IF NOT EXISTS global_indices (
    id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL,
    category TEXT NOT NULL, price NUMERIC, prev_close NUMERIC, chg_pct NUMERIC,
    quote_date DATE NOT NULL, updated_at TIMESTAMP DEFAULT NOW(), source TEXT DEFAULT 'yahoo',
    UNIQUE(symbol, quote_date)
);
CREATE INDEX IF NOT EXISTS idx_global_sym_date ON global_indices(symbol, quote_date DESC);
"""


def get_conn_from_env():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


async def fetch_global_indices(conn) -> dict:
    """Yahoo chart API, one symbol at a time. UPSERT into global_indices.
    Returns {stored, errors, total}."""
    ensure_table(conn)
    rows, errors = [], 0
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for symbol, name, cat in GLOBAL_TICKERS:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(symbol)}?interval=1d&range=5d")
            try:
                r = await client.get(url)
                r.raise_for_status()
                meta = r.json()["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                chg = round((price - prev) / prev * 100, 2) if (price and prev) else None
                ts = meta.get("regularMarketTime")
                qdate = ((datetime.utcfromtimestamp(ts) + timedelta(hours=5, minutes=30)).date()
                         if ts else (datetime.utcnow() + timedelta(hours=5, minutes=30)).date())
                rows.append((symbol, name, cat, price, prev, chg, qdate))
            except Exception as e:
                errors += 1
                log.warning(f"global_indices {name}: {e}")
            await asyncio.sleep(0.4)
    if rows:
        try:
            with conn.cursor() as cur:
                for sym, nm, cat, px, pv, chg, qd in rows:
                    cur.execute(
                        """INSERT INTO global_indices
                           (symbol,name,category,price,prev_close,chg_pct,quote_date,updated_at,source)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),'yahoo')
                           ON CONFLICT (symbol,quote_date) DO UPDATE SET
                           price=EXCLUDED.price, prev_close=EXCLUDED.prev_close,
                           chg_pct=EXCLUDED.chg_pct, updated_at=NOW()""",
                        (sym, nm, cat, px, pv, chg, qd))
            conn.commit()
        except Exception as e:
            log.error(f"global_indices upsert failed: {e}")
    log.info(f"global_indices: {len(rows)}/{len(GLOBAL_TICKERS)} stored ({errors} errors)")
    return {"stored": len(rows), "errors": errors, "total": len(GLOBAL_TICKERS)}


async def backfill_global_indices(conn, years: int = 5, clean: bool = True) -> dict:
    """One-time: pull `years` of DAILY closes for every GLOBAL_TICKER from the
    Yahoo chart API (range=Ny, interval=1d) and store one dated row per trading
    day (price=close, prev_close=prior close, chg_pct). Mirrors the equity
    raw_prices 5yr EOD history, but global series only need close + chg%.

    clean=True does a CLEAN-REPLACE: erase ALL existing global_indices rows
    first (removes stale seeds + partial data), then insert the fresh series.
    Source tag = 'yahoo_backfill'. UPSERT on (symbol, quote_date) is idempotent.
    """
    ensure_table(conn)
    if clean:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM global_indices")
        conn.commit()
        log.info("global_indices: history erased (clean-replace backfill)")
    rng = f"{years}y"
    total, errors = 0, 0
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for symbol, name, cat in GLOBAL_TICKERS:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(symbol)}?interval=1d&range={rng}")
            try:
                r = await client.get(url)
                r.raise_for_status()
                res = r.json()["chart"]["result"][0]
                ts = res.get("timestamp", []) or []
                closes = (res.get("indicators", {}).get("quote", [{}])[0].get("close", []) or [])
                rows = []
                prev = None
                for i, t in enumerate(ts):
                    cl = closes[i] if i < len(closes) else None
                    if cl is None:
                        continue
                    qd = (datetime.utcfromtimestamp(t) + timedelta(hours=5, minutes=30)).date()
                    chg = round((cl - prev) / prev * 100, 2) if prev else None
                    rows.append((symbol, name, cat, cl, prev, chg, qd))
                    prev = cl
                if rows:
                    with conn.cursor() as cur:
                        for sym, nm, c, px, pv, chg, qd in rows:
                            cur.execute(
                                """INSERT INTO global_indices
                                   (symbol,name,category,price,prev_close,chg_pct,quote_date,updated_at,source)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),'yahoo_backfill')
                                   ON CONFLICT (symbol,quote_date) DO UPDATE SET
                                   price=EXCLUDED.price, prev_close=EXCLUDED.prev_close,
                                   chg_pct=EXCLUDED.chg_pct, updated_at=NOW()""",
                                (sym, nm, c, px, pv, chg, qd))
                    conn.commit()
                    total += len(rows)
                    log.info(f"backfill {name}: {len(rows)} daily rows")
            except Exception as e:
                errors += 1
                log.warning(f"backfill {name}: {e}")
            await asyncio.sleep(0.4)
    return {"backfilled": total, "errors": errors, "tickers": len(GLOBAL_TICKERS), "years": years}


def prune_global_indices(conn, years: int = 5) -> int:
    """Rolling retention: delete rows older than `years`. Keeps global history
    to ~5yr like the equity feed. Tiny table (~14 tickers x ~1250 days)."""
    ensure_table(conn)
    cutoff = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date() - timedelta(days=365 * years)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM global_indices WHERE quote_date < %s", (cutoff,))
        n = cur.rowcount
    conn.commit()
    if n:
        log.info(f"global_indices prune: {n} rows older than {cutoff} deleted")
    return n


# ── Global intraday (Gold/Silver 5-min, 7-day rolling) ──────────────────────────
# Separate table from intraday_prices (NSE futures) — global commodities must never
# leak into a futures-universe scan (v8_live_metrics / paper engine).
GLOBAL_INTRADAY_TICKERS = [
    ("GC=F", "GOLD"),
    ("SI=F", "SILVER"),
]

INTRADAY_DDL = """
CREATE TABLE IF NOT EXISTS global_intraday (
    id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL,
    ts TIMESTAMP NOT NULL,
    open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC, volume BIGINT,
    timeframe TEXT DEFAULT '5m', source TEXT DEFAULT 'yahoo',
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol, ts, timeframe)
);
CREATE INDEX IF NOT EXISTS idx_global_intraday_sym_ts ON global_intraday(symbol, ts DESC);
"""


def ensure_intraday_table(conn):
    with conn.cursor() as cur:
        cur.execute(INTRADAY_DDL)
    conn.commit()


async def fetch_global_intraday(conn, range_str: str = "7d") -> dict:
    """Fetch 5-min OHLCV for Gold (GC=F) + Silver (SI=F) via Yahoo chart API,
    range default 7d (one call per symbol returns the full rolling 7-day window,
    incl. ~24h COMEX overnight bars). UPSERT on (symbol, ts, timeframe) — idempotent
    and self-healing (re-fetching the 7d window patches any gaps). ts stored in IST.
    Returns {stored, errors, symbols}."""
    ensure_intraday_table(conn)
    total, errors = 0, 0
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for symbol, name in GLOBAL_INTRADAY_TICKERS:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(symbol)}?interval=5m&range={range_str}")
            try:
                r = await client.get(url)
                r.raise_for_status()
                res = r.json()["chart"]["result"][0]
                tstamps = res.get("timestamp", []) or []
                q = (res.get("indicators", {}).get("quote", [{}])[0]) or {}
                opens, highs, lows, closes, vols = (q.get("open", []), q.get("high", []),
                                                    q.get("low", []), q.get("close", []),
                                                    q.get("volume", []))
                rows = []
                for i, t in enumerate(tstamps):
                    cl = closes[i] if i < len(closes) else None
                    if cl is None:
                        continue
                    dt = datetime.utcfromtimestamp(t) + timedelta(hours=5, minutes=30)
                    rows.append((symbol, name, dt,
                                 opens[i] if i < len(opens) else None,
                                 highs[i] if i < len(highs) else None,
                                 lows[i] if i < len(lows) else None,
                                 cl,
                                 vols[i] if i < len(vols) else None))
                if rows:
                    with conn.cursor() as cur:
                        cur.executemany(
                            """INSERT INTO global_intraday
                               (symbol,name,ts,open,high,low,close,volume,timeframe,source,updated_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'5m','yahoo',NOW())
                               ON CONFLICT (symbol,ts,timeframe) DO UPDATE SET
                               open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                               close=EXCLUDED.close, volume=EXCLUDED.volume, updated_at=NOW()""",
                            rows)
                    conn.commit()
                    total += len(rows)
                    log.info(f"global_intraday {name}: {len(rows)} 5m bars")
            except Exception as e:
                errors += 1
                log.warning(f"global_intraday {name}: {e}")
            await asyncio.sleep(0.4)
    return {"stored": total, "errors": errors, "symbols": len(GLOBAL_INTRADAY_TICKERS)}


def prune_global_intraday(conn, days: int = 7) -> int:
    """Rolling 7-day retention for Gold/Silver 5-min bars."""
    ensure_intraday_table(conn)
    cutoff = (datetime.utcnow() + timedelta(hours=5, minutes=30)) - timedelta(days=days)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM global_intraday WHERE ts < %s", (cutoff,))
        n = cur.rowcount
    conn.commit()
    if n:
        log.info(f"global_intraday prune: {n} bars older than {days}d deleted")
    return n
