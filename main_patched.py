# main_patched.py - v1.6.10 hotfix
# v1.6.10: GC fix (strong task refs) + futures cadence 1min -> 5min (reduce Yahoo load)
#          + restored main._scheduler (CMP refresh + earnings + EOD) -- previous noop patch removed
# v1.6.9:  Reduce Yahoo load to escape IP block. Futures sem=5 (was 8). Equity DISABLED (INFIN takes over).
#
# Activated by Procfile: `web: uvicorn main_patched:app --host 0.0.0.0 --port $PORT`
import asyncio
import urllib.parse
from datetime import datetime, timedelta, date
from typing import Optional, List
import httpx
import main

VERSION_HOTFIX = "1.6.10"

# === FEATURE FLAGS ===
EQUITY_ENABLED = False    # Disabled - INFIN replaces Yahoo equity feed this evening
FUTURES_SEM = 5           # Yahoo burst pressure control
FUTURES_CADENCE_SEC = 300 # 5-min cadence (was 60s in v1.6.9). Reduces Yahoo load.

# === Strong refs to background tasks (prevents asyncio GC death) ===
_BG_TASKS: set = set()


# ========== FUTURES: 5-MIN CADENCE ==========
_futures_failures = 0
_futures_backoff_until = None
_futures_last_tick: Optional[str] = None
_futures_last_total = 0

async def _fetch_futures_intraday_parallel():
    """Parallel fetch via chart API with concurrency=FUTURES_SEM."""
    global _futures_failures, _futures_backoff_until, _futures_last_tick, _futures_last_total

    if _futures_backoff_until and main._ist_now() < _futures_backoff_until:
        return

    futures = main._get_futures_symbols()
    if not futures:
        return

    sem = asyncio.Semaphore(FUTURES_SEM)

    async def fetch_one(sym):
        async with sem:
            try:
                candles = await main._fetch_intraday_yahoo(sym, range_str="1d")
                if candles:
                    main._insert_intraday(candles)
                    return ('ok', len(candles))
            except Exception as e:
                main.log.warning(f"v{VERSION_HOTFIX} futures {sym}: {e}")
            return ('fail', 0)

    results = await asyncio.gather(*[fetch_one(s) for s in futures])
    fail_count = sum(1 for r in results if r[0] == 'fail')
    total_candles = sum(r[1] for r in results)
    _futures_last_total = total_candles
    _futures_last_tick = str(main._ist_now())

    if fail_count > len(futures) // 2:
        _futures_failures += 1
        backoff = min(60 * (2 ** _futures_failures), 600)
        _futures_backoff_until = main._ist_now() + timedelta(seconds=backoff)
        main.log.error(f"v{VERSION_HOTFIX} futures: {fail_count}/{len(futures)} failed, backoff {backoff}s")
    else:
        _futures_failures = 0
        _futures_backoff_until = None
        main.log.info(f"v{VERSION_HOTFIX} futures tick: {total_candles} candles, {fail_count}/{len(futures)} failed")


async def _scheduler_futures_5min():
    main.log.info(f"v{VERSION_HOTFIX}: Futures 5-min scheduler started (sem={FUTURES_SEM}, cadence={FUTURES_CADENCE_SEC}s)")
    while True:
        try:
            if main._is_market_hours():
                await _fetch_futures_intraday_parallel()
        except Exception as e:
            main.log.error(f"v{VERSION_HOTFIX} futures scheduler error: {e}")
        await asyncio.sleep(FUTURES_CADENCE_SEC)


# ========== EQUITY (NON-FUTURES): DISABLED via flag ==========
async def _fetch_equity_cmp_chart_api():
    """Sequential chart API for non-futures, writes to cmp_prices."""
    futures_set = set(main._get_futures_symbols())
    all_symbols = main._get_all_gvm_symbols()
    non_futures = [s for s in all_symbols if s not in futures_set]
    if not non_futures:
        return

    cmp_map = {}
    failed = 0
    for sym in non_futures:
        try:
            candles = await main._fetch_intraday_yahoo(sym, range_str="1d")
            if candles:
                cmp_map[sym] = float(candles[-1]["close"])
            else:
                failed += 1
        except Exception as e:
            failed += 1
            main.log.warning(f"v{VERSION_HOTFIX} equity {sym}: {e}")
        await asyncio.sleep(0.2)

    main._upsert_cmp(cmp_map)
    main.log.info(f"v{VERSION_HOTFIX} equity CMP: {len(cmp_map)}/{len(non_futures)} updated, {failed} failed")


async def _scheduler_equity_10min():
    if not EQUITY_ENABLED:
        main.log.info(f"v{VERSION_HOTFIX}: Equity scheduler DISABLED (EQUITY_ENABLED=False). main._scheduler handles CMP.")
        while True:
            await asyncio.sleep(86400)
    main.log.info(f"v{VERSION_HOTFIX}: Equity 10-min scheduler started")
    while True:
        try:
            if main._is_market_hours():
                await _fetch_equity_cmp_chart_api()
        except Exception as e:
            main.log.error(f"v{VERSION_HOTFIX} equity scheduler error: {e}")
        await asyncio.sleep(600)


# ========== EOD: DAILY raw_prices via CHART API ==========
_raw_prices_done_today: Optional[date] = None

async def _fetch_daily_yahoo(symbol: str, range_str: str = "10d") -> List[dict]:
    """Fetch daily OHLC via chart API interval=1d."""
    ticker = main._yahoo_ticker(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?interval=1d&range={range_str}"
    try:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
            r.raise_for_status()
            data = r.json()
        chart = data.get("chart", {}).get("result", [])
        if not chart:
            return []
        result = chart[0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        adjclose_arr = []
        adj_section = result.get("indicators", {}).get("adjclose", [])
        if adj_section and isinstance(adj_section, list) and len(adj_section) > 0:
            adjclose_arr = adj_section[0].get("adjclose", [])
        opens, highs, lows, closes, volumes = (
            quote.get(k, []) for k in ("open", "high", "low", "close", "volume")
        )
        candles = []
        for j, ts in enumerate(timestamps):
            c_val = closes[j] if j < len(closes) else None
            if c_val is None:
                continue
            d = (datetime.utcfromtimestamp(ts) + timedelta(hours=5, minutes=30)).date()
            candles.append({
                "symbol": symbol,
                "price_date": d,
                "open": opens[j] if j < len(opens) else None,
                "high": highs[j] if j < len(highs) else None,
                "low": lows[j] if j < len(lows) else None,
                "close": c_val,
                "volume": int(volumes[j]) if j < len(volumes) and volumes[j] is not None else None,
                "adjusted_close": adjclose_arr[j] if j < len(adjclose_arr) else None,
            })
        return candles
    except Exception as e:
        main.log.warning(f"v{VERSION_HOTFIX} daily {symbol}: {e}")
        return []


def _upsert_raw_prices(candles):
    if not candles:
        return 0
    try:
        with main.get_conn() as conn, conn.cursor() as cur:
            for c in candles:
                cur.execute("""
                    INSERT INTO raw_prices (symbol, price_date, open, high, low, close, volume, adjusted_close)
                    VALUES (%(symbol)s, %(price_date)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s, %(adjusted_close)s)
                    ON CONFLICT (symbol, price_date) DO UPDATE SET
                        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                        close = EXCLUDED.close, volume = EXCLUDED.volume, adjusted_close = EXCLUDED.adjusted_close
                """, c)
            conn.commit()
        return len(candles)
    except Exception as e:
        main.log.error(f"v{VERSION_HOTFIX} raw_prices upsert: {e}")
        return 0


async def _task_update_raw_prices_v169():
    """EOD raw_prices via chart API."""
    global _raw_prices_done_today
    today = main._ist_now().date()
    if _raw_prices_done_today == today:
        return

    main.log.info(f"v{VERSION_HOTFIX} EOD raw_prices: starting")
    all_symbols = main._get_all_gvm_symbols()

    total_inserted = 0
    failed = 0
    for sym in all_symbols:
        candles = await _fetch_daily_yahoo(sym, range_str="10d")
        if candles:
            total_inserted += _upsert_raw_prices(candles)
        else:
            failed += 1
        await asyncio.sleep(0.25)

    _raw_prices_done_today = today
    main.log.info(f"v{VERSION_HOTFIX} EOD raw_prices: {total_inserted} rows, {failed}/{len(all_symbols)} symbols failed")


async def _scheduler_eod():
    main.log.info(f"v{VERSION_HOTFIX}: EOD scheduler started")
    while True:
        try:
            if main._is_eod_window():
                await _task_update_raw_prices_v169()
        except Exception as e:
            main.log.error(f"v{VERSION_HOTFIX} EOD scheduler error: {e}")
        await asyncio.sleep(300)


# === STARTUP: launch schedulers with STRONG REFERENCES ===
@main.app.on_event("startup")
async def startup_v169():
    # Hold strong refs to prevent asyncio GC from killing background tasks.
    # Python docs: "The event loop only keeps weak references to tasks.
    # Save a strong reference to the result, or the task can disappear mid-execution."
    for coro_fn in (_scheduler_futures_5min, _scheduler_equity_10min, _scheduler_eod):
        t = asyncio.create_task(coro_fn(), name=coro_fn.__name__)
        _BG_TASKS.add(t)
        t.add_done_callback(_BG_TASKS.discard)
    main.log.info(
        f"v{VERSION_HOTFIX} hotfix active: futures {FUTURES_CADENCE_SEC}s sem={FUTURES_SEM} "
        f"| equity DISABLED | EOD on | bg_tasks_held={len(_BG_TASKS)}"
    )


# === HEALTH ENDPOINTS ===
@main.app.get("/api/v169/health")
def v169_health():
    return {
        "version": VERSION_HOTFIX,
        "equity_enabled": EQUITY_ENABLED,
        "futures_semaphore": FUTURES_SEM,
        "futures_cadence_sec": FUTURES_CADENCE_SEC,
        "futures_failures": _futures_failures,
        "futures_backoff_until": str(_futures_backoff_until) if _futures_backoff_until else None,
        "futures_last_tick": _futures_last_tick,
        "futures_last_total_candles": _futures_last_total,
        "raw_prices_done_today": str(_raw_prices_done_today) if _raw_prices_done_today else None,
        "is_market_hours": main._is_market_hours(),
        "is_eod_window": main._is_eod_window(),
        "bg_tasks_held": len(_BG_TASKS),
        "bg_tasks": [{"name": t.get_name(), "done": t.done(), "cancelled": t.cancelled()} for t in _BG_TASKS],
    }


@main.app.post("/api/v169/trigger_eod")
async def v169_trigger_eod():
    """Manual EOD raw_prices trigger."""
    global _raw_prices_done_today
    _raw_prices_done_today = None
    t = asyncio.create_task(_task_update_raw_prices_v169(), name="manual_eod")
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return {"status": "ok", "message": "EOD raw_prices task queued"}


# Re-export app for uvicorn
app = main.app
