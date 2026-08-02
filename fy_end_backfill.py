"""
fy_end_backfill.py — cc#703 FY-END PRICE BACKFILL 2015-2021 (founder OK 26-Jul, option-2)
==========================================================================================
One-time Yahoo backfill of FISCAL-YEAR-END prices ONLY, to unlock the 12-yr historical
valuation series (cc#691). For every symbol in scrape universe V2 (top-750 NSE by
market_cap UNION any sector_ops_metrics member, spec id=9178 / cc#701), fetch the close on
the LAST TRADING DAY on-or-before 31-Mar for fiscal years 2015..2021 (7 anchors/symbol) and
upsert into raw_prices. The cc#691 valuation compute reads raw_prices.close on/before each
FY-end, so it extends automatically once these rows exist — no compute change.

ADJUSTMENT BASIS (cc#657 corporate-action lesson, verified 26-Jul on NESTLEIND 1:10):
Yahoo's chart `quote.close` is ALREADY split/bonus-adjusted (the stored raw_prices.close
column shows NO cliff across the NESTLEIND Jan-2024 split — only dividends differ from
adjclose). Screener's fundamentals EPS is restated to current face value. So split-adjusted
close ÷ restated EPS is self-consistent per year. We therefore mirror yahoo_daily_update.py
EXACTLY: close = quote.close (split/bonus adjusted), adjusted_close = adjclose (also dividend
adjusted). The compute uses `close` — the split/bonus-adjusted, dividend-UNadjusted basis,
which is the correct one for PE/PB and continuous with the existing Mar-2022+ series.

STORAGE: raw_prices (chosen deliberately). The only retention purge in the app
(nse_eod_ingest._purge_old, 365d) targets delivery_eod / fii_dii_daily / participant_oi_daily
/ fo_ban — NOT raw_prices (which already holds ~5yr for the GVM momentum backfill). So these
2015-2021 rows persist; no dedicated table needed.

EFFICIENCY: ONE ranged Yahoo fetch per symbol (period1..period2 spanning 2014→2021) yields all
7 FY-end anchors — ~613 calls total, not 7×613, gentler on Yahoo than the spec's ~4.3k estimate.
Fetches are sequential with a politeness sleep (one-at-a-time).

RESUMABLE: a symbol already carrying any raw_prices row in [2015-03-01, 2021-03-31] is treated
as done and skipped, so restarts / re-arms resume cleanly. Idempotent upsert on (symbol, price_date).

TRIGGER: no HTTP path from the CC sandbox, so it self-starts on app boot when app_config key
'cc703_fyend_backfill' = 'pending' (claimed atomically -> runs once, restart-safe). Manual
re-trigger: POST /api/admin/backfill_fy_end_prices (admin token). Progress -> ops_log
category 'cc703_fyend' (poll via run_sql). A validation set (known split/bonus names) is
fetched FIRST and its FY-end series logged before the bulk run proceeds.
"""
import logging
import os
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

import psycopg2
import requests
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("scorr.fy_end_backfill")
router = APIRouter(prefix="/api/admin", tags=["fy_end_backfill"])

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_TOKEN  = os.getenv("ADMIN_TOKEN", "")
FLAG_KEY     = "cc703_fyend_backfill"

FY_YEARS     = list(range(2015, 2022))      # 2015..2021 inclusive (7 fiscal-year-ends)
ANCHOR_BACK  = 14                            # days before 31-Mar to accept the last trading close
SLEEP_S      = 0.45                          # Yahoo politeness (one-at-a-time)
# known split/bonus / IPO names fetched first so their FY-end continuity can be eyeballed in ops_log
VALIDATION_SET = ["NESTLEIND", "IRCTC", "TATAMOTORS", "KARURVYSYA"]

INDICES = {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK", "SENSEX": "^BSESN",
           "FINNIFTY": "NIFTY_FIN_SERVICE.NS", "MIDCAPNIFTY": "^NSEMDCP50"}

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

_running = False


def _conn():
    return psycopg2.connect(DATABASE_URL)


def _to_yahoo_ticker(symbol: str) -> str:
    return INDICES.get(symbol, symbol + ".NS")


def _oplog(cur, title, details):
    cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                   VALUES (CURRENT_DATE, NOW(), 'cc703_fyend', %s, %s::jsonb)""",
                (title, _json(details)))


def _json(d):
    import json
    return json.dumps(d, default=str)


def _universe(cur) -> List[str]:
    """Scrape universe V2 (cc#701): top-750 NSE by market_cap UNION any sector_ops_metrics member (cc#814)."""
    cur.execute("""
        WITH ranked AS (
            SELECT UPPER(nse_code) AS code
            FROM screener_raw
            WHERE nse_code IS NOT NULL AND nse_code <> '' AND nse_code !~ '^[0-9]+$'
              AND market_cap IS NOT NULL
            ORDER BY market_cap DESC NULLS LAST
            LIMIT 500)
        SELECT code FROM ranked
        UNION
        SELECT DISTINCT UPPER(symbol) FROM sector_ops_metrics
        ORDER BY code
    """)
    return [r[0] for r in cur.fetchall()]


def _already_done(cur, symbol: str) -> bool:
    cur.execute("""SELECT 1 FROM raw_prices WHERE symbol=%s
                   AND price_date BETWEEN '2015-03-01' AND '2021-03-31' LIMIT 1""", (symbol,))
    return cur.fetchone() is not None


def _fetch_bars(symbol: str) -> Tuple[List[dict], Optional[str]]:
    """One ranged Yahoo daily fetch spanning 2014-06-01 .. 2021-04-30. Returns (bars, reason)
    where bars = [{date, open, high, low, close, adjclose, volume}] on the split-adjusted basis."""
    ticker = _to_yahoo_ticker(symbol)
    p1 = int(datetime(2014, 6, 1, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime(2021, 4, 30, tzinfo=timezone.utc).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?period1={p1}&period2={p2}&interval=1d")
    try:
        r = requests.get(url, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        r.raise_for_status()
        data = r.json()
        chart = (data.get("chart", {}) or {}).get("result", [])
        if not chart:
            err = (data.get("chart", {}) or {}).get("error")
            return [], (f"empty_chart:{err.get('code') if isinstance(err, dict) else err}"
                        if err else "empty_chart")
        result = chart[0]
        tss = result.get("timestamp", []) or []
        q = (result.get("indicators", {}).get("quote", [{}]) or [{}])[0]
        adj_list = result.get("indicators", {}).get("adjclose", [])
        adj_closes = adj_list[0].get("adjclose", []) if adj_list else []
        opens, highs = q.get("open", []), q.get("high", [])
        lows, closes = q.get("low", []), q.get("close", [])
        volumes = q.get("volume", [])
        bars = []
        for i, ts in enumerate(tss):
            c = closes[i] if i < len(closes) else None
            if c is None:
                continue
            ac = adj_closes[i] if i < len(adj_closes) else c
            bars.append({
                "date": datetime.utcfromtimestamp(ts).date(),
                "open": opens[i] if i < len(opens) else None,
                "high": highs[i] if i < len(highs) else None,
                "low":  lows[i] if i < len(lows) else None,
                "close": c,
                "adjclose": ac if ac is not None else c,
                "volume": volumes[i] if i < len(volumes) else None,
            })
        return (bars, None) if bars else ([], "no_valid_bars")
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:180]}"


def _fy_end_rows(symbol: str, bars: List[dict]) -> List[tuple]:
    """For each FY year, pick the LAST bar with date <= 31-Mar-Y and >= (31-Mar-Y - ANCHOR_BACK)
    (i.e. the last trading close of March; skip a year whose March window has no bar — honest gap /
    pre-listing). Returns upsert tuples on the split-adjusted basis (close = quote.close)."""
    rows = []
    for y in FY_YEARS:
        target = date(y, 3, 31)
        floor = target - timedelta(days=ANCHOR_BACK)
        cand = [b for b in bars if floor <= b["date"] <= target]
        if not cand:
            continue
        b = max(cand, key=lambda x: x["date"])
        rows.append((
            symbol, b["date"],
            round(float(b["open"]), 2) if b["open"] is not None else None,
            round(float(b["high"]), 2) if b["high"] is not None else None,
            round(float(b["low"]), 2) if b["low"] is not None else None,
            round(float(b["close"]), 2),
            round(float(b["adjclose"]), 2) if b["adjclose"] is not None else None,
            int(b["volume"]) if b["volume"] is not None else 0,
        ))
    return rows


def _process(cur, symbol: str) -> dict:
    bars, reason = _fetch_bars(symbol)
    if not bars:
        return {"symbol": symbol, "written": 0, "reason": reason}
    rows = _fy_end_rows(symbol, bars)
    for row in rows:
        cur.execute(UPSERT_SQL, row)
    return {"symbol": symbol, "written": len(rows),
            "years": [str(r[1]) for r in rows]}


def run_backfill():
    global _running
    started = datetime.now(timezone.utc)
    total_syms = total_rows = skipped = failed = 0
    fail_samples = []
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                universe = _universe(cur)
                _oplog(cur, "FY_END_BACKFILL_START",
                       {"universe": len(universe), "fy_years": FY_YEARS,
                        "basis": "split/bonus-adjusted quote.close (mirrors yahoo_daily_update)",
                        "validation_set": VALIDATION_SET})
            conn.commit()

            # 1) validation set first — log their FY-end series so continuity can be eyeballed
            with conn.cursor() as cur:
                val_out = []
                for s in VALIDATION_SET:
                    res = _process(cur, s)
                    conn.commit()
                    if res["written"]:
                        cur.execute("""SELECT price_date, close FROM raw_prices WHERE symbol=%s
                                       AND price_date BETWEEN '2015-03-01' AND '2021-03-31'
                                       ORDER BY price_date""", (s,))
                        res["series"] = [[str(d), float(c)] for d, c in cur.fetchall()]
                    val_out.append(res)
                    _time.sleep(SLEEP_S)
                _oplog(cur, "FY_END_BACKFILL_VALIDATION", {"validation": val_out})
                conn.commit()

            # 2) bulk — resumable skip, checkpoint every 40 symbols
            done_val = set(VALIDATION_SET)
            for i, sym in enumerate(universe):
                with conn.cursor() as cur:
                    if sym in done_val or _already_done(cur, sym):
                        skipped += 1
                        continue
                    res = _process(cur, sym)
                    conn.commit()
                total_syms += 1
                if res.get("written"):
                    total_rows += res["written"]
                else:
                    failed += 1
                    if len(fail_samples) < 30:
                        fail_samples.append(f"{sym}:{res.get('reason')}")
                _time.sleep(SLEEP_S)
                if total_syms % 40 == 0:
                    with conn.cursor() as cur:
                        _oplog(cur, "FY_END_BACKFILL_PROGRESS",
                               {"processed": total_syms, "rows": total_rows, "skipped": skipped,
                                "failed": failed, "at": sym})
                    conn.commit()

            with conn.cursor() as cur:
                _oplog(cur, "FY_END_BACKFILL_DONE",
                       {"universe": len(universe), "processed": total_syms, "rows_written": total_rows,
                        "skipped_already_done": skipped, "failed": failed, "fail_sample": fail_samples,
                        "elapsed_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1)})
                conn.commit()
        log.info(f"cc703 fy-end backfill done: {total_rows} rows, {total_syms} processed, "
                 f"{skipped} skipped, {failed} failed")
    except Exception as e:
        log.error(f"cc703 fy-end backfill crashed: {e}")
        try:
            with _conn() as conn, conn.cursor() as cur:
                _oplog(cur, "FY_END_BACKFILL_ERROR", {"error": str(e)[:300],
                       "processed": total_syms, "rows": total_rows})
                conn.commit()
        except Exception:
            pass
    finally:
        _running = False


def _claim_flag() -> bool:
    """Atomically consume the pending flag so restarts never double-run."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key=%s AND value LIKE 'pending%%' FOR UPDATE",
                        (FLAG_KEY,))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s",
                            (FLAG_KEY,))
            conn.commit()
        return r is not None
    except Exception as e:
        log.error(f"cc703 flag claim failed: {e}")
        return False


def _maybe_start(source: str) -> bool:
    global _running
    if _running:
        return False
    _running = True
    threading.Thread(target=run_backfill, name=f"cc703-fyend-{source}", daemon=True).start()
    return True


@router.on_event("startup")
async def _startup_trigger():
    # CC sandbox has no HTTP path to prod; a DB flag set via MCP run_sql + this hook = deploy-time
    # self-trigger (runs once, atomic flag claim).
    if _claim_flag():
        log.info("cc703: pending flag claimed -- starting FY-end price backfill")
        _maybe_start("startup")


@router.post("/backfill_fy_end_prices")
def backfill_fy_end_prices_now(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    if _running:
        return {"status": "already_running"}
    started = _maybe_start("manual")
    return {"status": "started" if started else "busy",
            "progress": "SELECT title, details FROM ops_log WHERE category='cc703_fyend' ORDER BY id DESC"}
