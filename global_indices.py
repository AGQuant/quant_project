"""
Global Indices — Scorr V8 / Daily Digest
=========================================
Fetches daily close + prev close for global indices, commodities, currency, crypto
via Yahoo CHART API one-at-a-time (proven reliable; batch yfinance is BROKEN).

Feeds Daily Digest section: OVERNIGHT SCORECARD.
Table: global_indices (UPSERT on symbol, quote_date) — 5-year EOD rolling.

EOD tickers (17): Dow, S&P, Nasdaq, US VIX, India VIX, Nikkei, FTSE, DAX,
                  Shanghai, Brent, WTI, Gold, Silver, Natural Gas, Bitcoin, USDINR, DXY

Intraday tickers (13, 5-min, 7-day rolling):
  Gold, Silver, WTI, Brent, Natural Gas, Bitcoin (commodities/crypto, 24x5/7)
  + cc#282: Dow, S&P 500, Nasdaq, Nikkei, DXY, USDINR, US VIX (index/currency/volatility)
  (all trade outside NSE hours — need live data across Asian/European/US sessions)

Wired into scheduler.py:
  - EOD fetch: 06:00 IST daily (incl weekends — commodities/crypto trade 24x5/7)
  - Intraday fetch: every 5 min, 06:00-23:30 IST

Standalone:
  py -3.11 -c "import asyncio,global_indices as g; print(asyncio.run(g.fetch_global_indices(g.get_conn_from_env())))"
"""
import os
import asyncio
import logging
import urllib.parse
from datetime import datetime, timedelta

import httpx
import psycopg

log = logging.getLogger("scorr.global_indices")


def _ops_log(conn, category: str, title: str, details: dict) -> None:
    """cc#282: lightweight ops_log writer (mirrors v8_signal_writer._ops_log) for the
    intraday-fetch skip warnings and the total-failure alert."""
    try:
        from psycopg.types.json import Json
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), %s, %s, %s)""",
                        (category, title, Json(details)))
        conn.commit()
    except Exception as e:
        log.error(f"_ops_log failed ({category}/{title}): {e}")

# ── EOD tickers — 5yr history + daily close ───────────────────────────────────
GLOBAL_TICKERS = [
    # Indices
    ("^DJI",      "Dow",         "index"),
    ("^GSPC",     "S&P 500",     "index"),
    ("^IXIC",     "Nasdaq",      "index"),
    ("^VIX",      "US VIX",      "volatility"),
    ("^INDIAVIX", "India VIX",   "volatility", "INDIAVIX"),  # store as INDIAVIX (task #59)
    ("^N225",     "Nikkei",      "index"),
    ("^FTSE",     "FTSE",        "index"),
    ("^GDAXI",    "DAX",         "index"),
    ("000001.SS", "Shanghai",    "index"),
    # Commodities
    ("BZ=F",      "Brent",       "commodity"),
    ("CL=F",      "WTI",         "commodity"),
    ("GC=F",      "Gold",        "commodity"),   # COMEX futures — Yahoo has NO XAUUSD=X spot chart data (verified)
    ("SI=F",      "Silver",      "commodity"),   # COMEX futures — Yahoo has NO XAGUSD=X spot chart data (verified)
    ("NG=F",      "Natural Gas", "commodity"),
    # Crypto
    ("BTC-USD",   "Bitcoin",     "crypto"),
    # Currency
    ("INR=X",     "USDINR",      "currency"),
    ("DX-Y.NYB",  "DXY",         "currency"),
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
    await ensure_india_vix_history(conn)   # task #59: seed 30d INDIAVIX EOD on first run
    rows, errors = [], 0
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for entry in GLOBAL_TICKERS:
            symbol, name, cat = entry[0], entry[1], entry[2]
            store_sym = entry[3] if len(entry) > 3 else symbol   # decouple Yahoo ticker from stored symbol
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(symbol)}?interval=1d&range=5d")
            try:
                r = await client.get(url)
                r.raise_for_status()
                meta = r.json()["chart"]["result"][0]["meta"]
                price = meta.get("regularMarketPrice")
                prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
                chg   = round((price - prev) / prev * 100, 2) if (price and prev) else None
                ts    = meta.get("regularMarketTime")
                qdate = ((datetime.utcfromtimestamp(ts) + timedelta(hours=5, minutes=30)).date()
                         if ts else (datetime.utcnow() + timedelta(hours=5, minutes=30)).date())
                rows.append((store_sym, name, cat, price, prev, chg, qdate))
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
    raw_prices 5yr EOD history.

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
        for entry in GLOBAL_TICKERS:
            symbol, name, cat = entry[0], entry[1], entry[2]
            store_sym = entry[3] if len(entry) > 3 else symbol
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(symbol)}?interval=1d&range={rng}")
            try:
                r = await client.get(url)
                r.raise_for_status()
                res    = r.json()["chart"]["result"][0]
                ts     = res.get("timestamp", []) or []
                closes = (res.get("indicators", {}).get("quote", [{}])[0].get("close", []) or [])
                rows   = []
                prev   = None
                for i, t in enumerate(ts):
                    cl = closes[i] if i < len(closes) else None
                    if cl is None:
                        continue
                    qd  = (datetime.utcfromtimestamp(t) + timedelta(hours=5, minutes=30)).date()
                    chg = round((cl - prev) / prev * 100, 2) if prev else None
                    rows.append((store_sym, name, cat, cl, prev, chg, qd))
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


async def backfill_india_vix(conn, days: int = 30) -> dict:
    """Targeted India VIX (Yahoo ^INDIAVIX) daily-close backfill, stored as
    symbol=INDIAVIX. Non-destructive — touches only INDIAVIX rows (unlike the full
    clean-replace backfill). Gives the V10 VIX chart cross-day EOD history (task #59)."""
    ensure_table(conn)
    rng = "3mo" if days > 30 else "1mo"
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote('^INDIAVIX')}?interval=1d&range={rng}")
    stored = 0
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = await client.get(url)
            r.raise_for_status()
            res    = r.json()["chart"]["result"][0]
            ts     = res.get("timestamp", []) or []
            closes = (res.get("indicators", {}).get("quote", [{}])[0].get("close", []) or [])
            rows, prev = [], None
            for i, t in enumerate(ts):
                cl = closes[i] if i < len(closes) else None
                if cl is None:
                    continue
                qd  = (datetime.utcfromtimestamp(t) + timedelta(hours=5, minutes=30)).date()
                chg = round((cl - prev) / prev * 100, 2) if prev else None
                rows.append(("INDIAVIX", "India VIX", "volatility", cl, prev, chg, qd))
                prev = cl
            rows = rows[-days:]
            if rows:
                with conn.cursor() as cur:
                    for sym, nm, cat, px, pv, chg, qd in rows:
                        cur.execute(
                            """INSERT INTO global_indices
                               (symbol,name,category,price,prev_close,chg_pct,quote_date,updated_at,source)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),'yahoo_backfill')
                               ON CONFLICT (symbol,quote_date) DO UPDATE SET
                               price=EXCLUDED.price, prev_close=EXCLUDED.prev_close,
                               chg_pct=EXCLUDED.chg_pct, updated_at=NOW()""",
                            (sym, nm, cat, px, pv, chg, qd))
                conn.commit()
                stored = len(rows)
        log.info(f"backfill_india_vix: {stored} daily rows")
    except Exception as e:
        log.warning(f"backfill_india_vix: {e}")
    return {"stored": stored}


async def ensure_india_vix_history(conn):
    """First-run seed: run the 30-day India VIX backfill once, only when no INDIAVIX
    EOD history exists yet. Idempotent — a no-op on every subsequent daily fetch."""
    ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM global_indices WHERE symbol='INDIAVIX'")
        n = cur.fetchone()[0]
    if n == 0:
        log.info("global_indices: seeding India VIX 30-day history (first run)")
        await backfill_india_vix(conn, days=30)


def prune_global_indices(conn, years: int = 5) -> int:
    """Rolling retention: delete rows older than `years`."""
    ensure_table(conn)
    cutoff = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date() - timedelta(days=365 * years)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM global_indices WHERE quote_date < %s", (cutoff,))
        n = cur.rowcount
    conn.commit()
    if n:
        log.info(f"global_indices prune: {n} rows older than {cutoff} deleted")
    return n


# ── Global intraday (5-min, 7-day rolling) ────────────────────────────────────
# Commodities + Crypto that trade outside NSE hours (24x5 or 24x7).
# Separate table — never leaks into NSE futures universe scans.
GLOBAL_INTRADAY_TICKERS = [
    ("GC=F",    "GOLD"),      # COMEX futures — Yahoo has NO XAUUSD=X spot 5m chart data (verified)
    ("SI=F",    "SILVER"),    # COMEX futures — Yahoo has NO XAGUSD=X spot 5m chart data (verified)
    ("CL=F",    "WTI"),
    ("BZ=F",    "BRENT"),
    ("NG=F",    "NATURAL_GAS"),
    ("BTC-USD", "BITCOIN"),
    # cc#282: index / currency / volatility — same Yahoo 5m fetch, same table (schema fits), so
    # /api/v8/global_indices overlays them live automatically (cc#281's generic overlay). These
    # are non-Indian instruments; the 06:00-23:30 IST window covers Asian/European/US hours.
    ("^DJI",     "DOW"),
    ("^GSPC",    "SP500"),
    ("^IXIC",    "NASDAQ"),
    ("^N225",    "NIKKEI"),
    ("DX-Y.NYB", "DXY"),
    ("INR=X",    "USDINR"),
    ("^VIX",     "US_VIX"),
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
    """Fetch 5-min OHLCV for Gold, Silver, WTI, Brent, Natural Gas, Bitcoin
    via Yahoo chart API. UPSERT on (symbol, ts, timeframe) — idempotent + self-healing.
    ts stored in IST. 7-day rolling window. Returns {stored, errors, symbols}."""
    ensure_intraday_table(conn)
    total, errors = 0, 0
    failed = []
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for symbol, name in GLOBAL_INTRADAY_TICKERS:
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
                   f"{urllib.parse.quote(symbol)}?interval=5m&range={range_str}")
            try:
                r = await client.get(url)
                r.raise_for_status()
                res    = r.json()["chart"]["result"][0]
                tstamps = res.get("timestamp", []) or []
                q = (res.get("indicators", {}).get("quote", [{}])[0]) or {}
                opens  = q.get("open",   [])
                highs  = q.get("high",   [])
                lows   = q.get("low",    [])
                closes = q.get("close",  [])
                vols   = q.get("volume", [])
                rows = []
                for i, t in enumerate(tstamps):
                    cl = closes[i] if i < len(closes) else None
                    if cl is None:
                        continue
                    dt = datetime.utcfromtimestamp(t) + timedelta(hours=5, minutes=30)
                    rows.append((
                        symbol, name, dt,
                        opens[i]  if i < len(opens)  else None,
                        highs[i]  if i < len(highs)  else None,
                        lows[i]   if i < len(lows)   else None,
                        cl,
                        int(vols[i]) if (i < len(vols) and vols[i]) else None,
                    ))
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
                # cc#282: graceful skip — a per-symbol failure never crashes the cycle and never
                # overwrites the last-known-good rows (nothing is written for this symbol); it just
                # retries on the next 5-min run. Log a warning-level ops_log note for visibility.
                errors += 1
                failed.append(name)
                log.warning(f"global_intraday {name}: {e}")
                _ops_log(conn, "warning", "global_intraday_symbol_skip",
                         {"symbol": symbol, "name": name, "error": str(e)[:200]})
            await asyncio.sleep(0.4)
    # cc#282: if EVERY symbol failed this cycle, Yahoo itself is likely down — raise ONE louder
    # alert (not one per symbol), mirroring the total-failure escalation used elsewhere (cc#246).
    if failed and len(failed) == len(GLOBAL_INTRADAY_TICKERS):
        _ops_log(conn, "alert", "global_intraday_total_failure",
                 {"failed": len(failed),
                  "message": "all global_intraday symbols failed this cycle — Yahoo likely "
                             "unreachable; last-known-good values retained, retrying next run"})
    return {"stored": total, "errors": errors, "failed": failed,
            "symbols": len(GLOBAL_INTRADAY_TICKERS)}


def prune_global_intraday(conn, days: int = 7) -> int:
    """Rolling 7-day retention for commodity/crypto 5-min bars."""
    ensure_intraday_table(conn)
    cutoff = (datetime.utcnow() + timedelta(hours=5, minutes=30)) - timedelta(days=days)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM global_intraday WHERE ts < %s", (cutoff,))
        n = cur.rowcount
    conn.commit()
    if n:
        log.info(f"global_intraday prune: {n} bars older than {days}d deleted")
    return n
