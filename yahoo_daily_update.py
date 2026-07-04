"""
yahoo_daily_update.py — v2.1
Chart API async update — replaces slow yf.Ticker().history()
Fetches last 10 days OHLC for all symbols in raw_prices (~1720 stocks).

v2.1: GENTLER on Yahoo + RETRY PASS.
  - SEMAPHORE 8 -> 3, per-request sleep 0.05 -> 0.4 to stay under Yahoo's
    rate-limit threshold (8-wide caused deterministic partial days, e.g.
    28-May-2026 landed 693/1717).
  - After the first pass, any symbol that returned empty (timeout / 429 /
    empty chart) is RE-FETCHED in a second pass at an even slower rate
    (sem=2, sleep 0.8). Two-pass run ~15-30 min for full universe.
  - UPSERT on (symbol, price_date) — safe to re-run, self-heals trailing-10d holes.

Tunables via run_async(sem=, sleep=, retry=): override defaults per call.
"""

import asyncio
import os
import json
import datetime
import psycopg
import httpx
import urllib.parse
import logging

log = logging.getLogger("yahoo_daily")

DB_URL = os.environ.get("DATABASE_URL")
LOOKBACK = "10d"

# Gentler defaults (v2.1). Override per call via run_async kwargs.
SEMAPHORE_DEFAULT = 3
SLEEP_DEFAULT = 0.4
RETRY_SEMAPHORE = 2
RETRY_SLEEP = 0.8
MAX_RETRY_PASSES = 2

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
    "NIFTY50":     "^NSEI",
    "BANKNIFTY":   "^NSEBANK",
    # Backfilled via the index backfill flow; kept here so the nightly run
    # (SELECT DISTINCT symbol FROM raw_prices) fetches them with the right ticker.
    "SENSEX":      "^BSESN",
    "FINNIFTY":    "NIFTY_FIN_SERVICE.NS",  # ^CNXFINANCE is unreliable on Yahoo
    "MIDCAPNIFTY": "^NSEMDCP50",            # Nifty Midcap 50 (proxy for Midcap Nifty)
}

def _to_yahoo_ticker(symbol: str) -> str:
    return INDICES.get(symbol, symbol + ".NS")


async def _fetch_symbol(client: httpx.AsyncClient, sem: asyncio.Semaphore, symbol: str, sleep_s: float):
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
                # cc#188: surface Yahoo's own error code (e.g. "Not Found" for
                # delisted REITs/InvITs/SME) instead of a bare empty result.
                err = (data.get("chart", {}) or {}).get("error")
                reason = f"empty_chart:{err.get('code') if isinstance(err, dict) else err}" if err else "empty_chart"
                return symbol, [], reason
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
            if not rows:
                return symbol, [], "no_valid_bars"      # cc#188: chart present but all closes null
            return symbol, rows, None
        except Exception as e:
            log.warning(f"yahoo_daily {symbol}: {e}")
            return symbol, [], f"{type(e).__name__}: {str(e)[:200]}"   # cc#188: capture class+msg
        finally:
            await asyncio.sleep(sleep_s)


async def _run_pass(symbols, sem_size: int, sleep_s: float):
    """One fetch pass over `symbols`. Returns (results_dict, failed_list, rows_count)."""
    sem = asyncio.Semaphore(sem_size)
    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        limits=httpx.Limits(max_connections=sem_size * 2, max_keepalive_connections=sem_size),
    ) as client:
        tasks   = [_fetch_symbol(client, sem, s, sleep_s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    got = {}
    failed = []
    reasons = {}                                    # cc#188: {symbol: drop reason}
    for symbol, rows, reason in results:
        if rows:
            got[symbol] = rows
        else:
            failed.append(symbol)
            reasons[symbol] = reason or "unknown"
    return got, failed, reasons


def _log_drops_and_heal(drops, attempted):
    """cc#188: write the full EOD drop list to ops_log(category=yahoo_eod_drops),
    ONE row per run (symbol + reason each), and compute how many symbols dropped by
    the PREVIOUS run were healed by this one — i.e. the 15:35 -> 21:00/01:00 safety
    re-run relationship. Returns (heal_count, healed_symbols)."""
    dropped_syms = {d["symbol"] for d in drops}
    healed = []
    try:
        with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
            cur.execute("""SELECT details FROM ops_log WHERE category='yahoo_eod_drops'
                           ORDER BY session_ts DESC LIMIT 1""")
            r = cur.fetchone()
            prev = set()
            if r and r[0]:
                d = r[0] if isinstance(r[0], dict) else json.loads(r[0])
                prev = {x.get("symbol") for x in (d.get("drops") or [])}
            healed = sorted(prev - dropped_syms)     # in prev drop list, not in this one -> healed
            details = {
                "attempted": attempted, "drop_count": len(drops), "drops": drops,
                "healed_from_prev": len(healed), "healed_symbols": healed[:200],
            }
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'yahoo_eod_drops', %s, %s::jsonb)""",
                        (f"{len(drops)} dropped, {len(healed)} healed vs prev run", json.dumps(details)))
            conn.commit()
    except Exception as e:
        log.error(f"_log_drops_and_heal: {e}")
    return len(healed), healed


def _commit_rows(results_map):
    """UPSERT all rows from a {symbol: rows} map. Returns total rows written."""
    total = 0
    if not results_map:
        return 0
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        for symbol, rows in results_map.items():
            cur.executemany(UPSERT_SQL, rows)
            total += len(rows)
        conn.commit()
    return total


async def run_async(symbols=None, lookback=None, sem=None, sleep=None, retry=None):
    """
    Main async entry point. If symbols is None, fetches all from raw_prices.
    Two-pass (or more) with retry on failures. Returns dict with stats.
    """
    global LOOKBACK
    if lookback:
        LOOKBACK = lookback

    sem_size  = sem   if sem   is not None else SEMAPHORE_DEFAULT
    sleep_s   = sleep if sleep is not None else SLEEP_DEFAULT
    retries   = retry if retry is not None else MAX_RETRY_PASSES

    if symbols is None:
        with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM raw_prices ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]

    if not symbols:
        return {"updated": 0, "failed": 0, "rows": 0}

    total_attempted = len(symbols)
    total_rows = 0
    pass_log = []

    # Pass 1 — gentle main pass
    got, failed, last_reasons = await _run_pass(symbols, sem_size, sleep_s)
    total_rows += _commit_rows(got)
    pass_log.append({"pass": 1, "ok": len(got), "failed": len(failed), "sem": sem_size, "sleep": sleep_s})

    # Retry passes — only the failures, even slower each time
    pass_num = 1
    while failed and pass_num <= retries:
        pass_num += 1
        # back off harder on later passes
        r_sem = max(1, RETRY_SEMAPHORE - (pass_num - 2))
        r_sleep = RETRY_SLEEP + 0.4 * (pass_num - 2)
        log.info(f"yahoo_daily retry pass {pass_num}: {len(failed)} symbols (sem={r_sem}, sleep={r_sleep})")
        got_r, failed, last_reasons = await _run_pass(failed, r_sem, r_sleep)
        total_rows += _commit_rows(got_r)
        pass_log.append({"pass": pass_num, "ok": len(got_r), "failed": len(failed), "sem": r_sem, "sleep": r_sleep})

    # cc#188: per-symbol drop capture + heal count vs the previous run
    drops = [{"symbol": s, "reason": last_reasons.get(s, "unknown")} for s in sorted(failed)]
    heal_count, _healed = _log_drops_and_heal(drops, total_attempted)

    summary = {
        "symbols_attempted": total_attempted,
        "updated":           total_attempted - len(failed),
        "failed":            len(failed),
        "rows_upserted":     total_rows,
        "passes":            pass_log,
        "failed_symbols":    failed[:20],
        "drop_count":        len(failed),
        "healed_from_prev":  heal_count,
    }
    log.info(f"yahoo_daily done: {summary}")
    return summary


def heal_indices(conn=None, indices=("NIFTY50", "BANKNIFTY")):
    """cc_task #72 bug_2: post-EOD verification + self-heal. After the nightly run,
    if an index's raw_prices lags the freshest universe trading day, re-fetch JUST
    that index (one symbol at a time, gentle) and UPSERT. Logs the outcome to
    session_log (category=alert if still stale after heal, else scheduler_health).
    Catches the silent index-symbol skips the bulk pass leaves behind."""
    own = conn is None
    if own:
        conn = psycopg.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(price_date) FROM raw_prices")
            target = cur.fetchone()[0]
        if target is None:
            return {"status": "no_data"}
        stale = []
        with conn.cursor() as cur:
            for sym in indices:
                cur.execute("SELECT MAX(price_date) FROM raw_prices WHERE symbol=%s", (sym,))
                mx = cur.fetchone()[0]
                if mx is None or mx < target:
                    stale.append(sym)
        healed = {}
        for sym in stale:
            got, _, _ = asyncio.run(_run_pass([sym], 1, 0.5))   # cc#188: _run_pass now returns (got, failed, reasons)
            healed[sym] = _commit_rows(got)
        index_max = {}
        with conn.cursor() as cur:
            for sym in indices:
                cur.execute("SELECT MAX(price_date) FROM raw_prices WHERE symbol=%s", (sym,))
                r = cur.fetchone()[0]
                index_max[sym] = str(r) if r else None
        still_stale = [s for s in indices if index_max[s] is None or index_max[s] < str(target)]
        details = {"target_day": str(target), "stale_before": stale,
                   "rows_healed": healed, "index_max": index_max, "still_stale": still_stale}
        category = "alert" if still_stale else "scheduler_health"
        title = "index_backfill_failed" if still_stale else "index_backfill_ok"
        # cc#156: telemetry categories moved off session_log to ops_log.
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                        (category, title, json.dumps(details)))
            conn.commit()
        log.info(f"heal_indices: {details}")
        return details
    finally:
        if own:
            conn.close()


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
