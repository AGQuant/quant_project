# main_patched.py - v1.6.7 hotfix
# Wraps main.py without modifying it. Disables the buggy original scheduler
# and registers a new one that runs live intraday during market hours.
#
# Activated by Procfile: `web: uvicorn main_patched:app --host 0.0.0.0 --port $PORT`
import asyncio
from datetime import timedelta
import main

VERSION_HOTFIX = "1.6.7"

# --- Disable original scheduler by replacing the function ---
async def _scheduler_noop():
    """No-op stub for original buggy scheduler."""
    main.log.info(f"v{VERSION_HOTFIX}: Original scheduler disabled")
    while True:
        await asyncio.sleep(86400)

main._scheduler = _scheduler_noop

# --- New live intraday fetcher (no daily guard, runs every tick during market hours) ---
_intraday_live_failures = 0
_intraday_live_backoff_until = None

async def _task_fetch_intraday_live():
    """Live intraday updater - runs every scheduler tick during market hours.
    Implements exponential backoff on rate-limit failures."""
    global _intraday_live_failures, _intraday_live_backoff_until
    
    if _intraday_live_backoff_until and main._ist_now() < _intraday_live_backoff_until:
        return
    
    futures = main._get_futures_symbols()
    if not futures: return
    
    total, failed = 0, 0
    for sym in futures:
        try:
            candles = await main._fetch_intraday_yahoo(sym, range_str="1d")
            if candles:
                main._insert_intraday(candles)
                total += len(candles)
            else:
                failed += 1
        except Exception as e:
            failed += 1
            main.log.warning(f"v{VERSION_HOTFIX} intraday {sym}: {e}")
        await asyncio.sleep(0.25)  # 0.25s x 290 = ~72s per cycle
    
    if failed > len(futures) // 2:
        _intraday_live_failures += 1
        backoff_min = min(5 * (2 ** _intraday_live_failures), 30)
        _intraday_live_backoff_until = main._ist_now() + timedelta(minutes=backoff_min)
        main.log.error(f"v{VERSION_HOTFIX} intraday: {failed}/{len(futures)} failed - backoff {backoff_min}m")
    else:
        _intraday_live_failures = 0
        _intraday_live_backoff_until = None
        main.log.info(f"v{VERSION_HOTFIX} intraday tick: {total} candles, {failed} symbols failed")

# --- New scheduler: live intraday during market hours + EOD raw_prices ---
async def _scheduler_v167():
    main.log.info(f"Scheduler v{VERSION_HOTFIX} started (live intraday during market hours)")
    while True:
        try:
            if main._is_market_hours():
                await _task_fetch_intraday_live()
                await main._task_refresh_cmp()
            if main._is_eod_window():
                await main._task_fetch_intraday()
                await main._task_update_raw_prices()
        except Exception as e:
            main.log.error(f"v{VERSION_HOTFIX} scheduler error: {e}")
        await asyncio.sleep(300)

# --- Register additional startup handler that creates the v1.6.7 task ---
@main.app.on_event("startup")
async def startup_v167():
    asyncio.create_task(_scheduler_v167())
    main.log.info(f"v{VERSION_HOTFIX} hotfix active: live intraday + EOD final + retry/backoff")

# --- New health endpoint for the hotfix ---
@main.app.get("/api/v167/health")
def v167_health():
    return {
        "version": VERSION_HOTFIX,
        "live_intraday_failures": _intraday_live_failures,
        "backoff_until": str(_intraday_live_backoff_until) if _intraday_live_backoff_until else None,
        "is_market_hours": main._is_market_hours(),
        "is_eod_window": main._is_eod_window(),
    }

# --- Re-export the FastAPI app for uvicorn ---
app = main.app
