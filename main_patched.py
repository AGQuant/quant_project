# main_patched.py - v1.6.11
# v1.6.11: Disable Yahoo 5-min intraday feed. Fyers local scripts take over.
#          EOD raw_prices + CMP refresh remain active on Railway.
# v1.6.11b: re-assert get_intraday DB-first + Yahoo on-demand (force redeploy
#           after a concurrent scheduler commit left the live process stale).
# v1.6.10: GC fix (strong task refs) + futures cadence 1min -> 5min
# v1.6.9:  Reduce Yahoo load. Futures sem=5. Equity DISABLED.
#
# Activated by Procfile: `web: uvicorn main_patched:app --host 0.0.0.0 --port $PORT`
import asyncio
import urllib.parse
from datetime import datetime, timedelta, date
from typing import Optional, List
import httpx
import main

VERSION_HOTFIX = "1.6.11"

_real_main_scheduler = main._scheduler

async def _main_scheduler_disabled():
    main.log.info(f"v{VERSION_HOTFIX}: main.py original startup() task replaced.")
    return

main._scheduler = _main_scheduler_disabled

# === FEATURE FLAGS ===
INTRADAY_ENABLED = False  # Disabled — Fyers local scripts handle intraday now
EQUITY_ENABLED   = False  # Disabled
FUTURES_SEM      = 5
FUTURES_CADENCE_SEC = 300

_BG_TASKS: set = set()

# ========== FUTURES INTRADAY: DISABLED ==========
async def _scheduler_futures_5min():
    if not INTRADAY_ENABLED:
        main.log.info(f"v{VERSION_HOTFIX}: Futures intraday DISABLED (Fyers local script active).")
        while True:
            await asyncio.sleep(86400)

# ========== EQUITY: DISABLED ==========
async def _scheduler_equity_10min():
    main.log.info(f"v{VERSION_HOTFIX}: Equity scheduler DISABLED.")
    while True:
        await asyncio.sleep(86400)

# ========== EOD: DAILY raw_prices via CHART API — ACTIVE ==========
_raw_prices_done_today: Optional[date] = None

async def _fetch_daily_yahoo(symbol: str, range_str: str = "10d") -> List[dict]:
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
    main.log.info(f"v{VERSION_HOTFIX} EOD raw_prices: {total_inserted} rows, {failed}/{len(all_symbols)} failed")


async def _scheduler_eod():
    main.log.info(f"v{VERSION_HOTFIX}: EOD scheduler started")
    while True:
        try:
            if main._is_eod_window():
                await _task_update_raw_prices_v169()
        except Exception as e:
            main.log.error(f"v{VERSION_HOTFIX} EOD scheduler error: {e}")
        await asyncio.sleep(300)


# ============================================================
# ON-DEMAND INTRADAY for NON-FUTURES (Yahoo, no DB writes)
# ------------------------------------------------------------
# Futures intraday lives in `intraday_prices` (Fyers, 1-min, 7d).
# For a 5-min / intraday read on a NON-FUTURES stock we pull just
# that one symbol LIVE from Yahoo (5-min available up to ~60d) and
# DO NOT store it. The existing `get_intraday` MCP tool is upgraded
# to be DB-first (futures) then live-Yahoo (non-futures, ephemeral).
# Implemented as a monkeypatch so the large main.py stays untouched.
# ============================================================
import yahoo_ondemand

_orig_call_tool = main._call_tool

async def _call_tool_with_ondemand(name, args):
    if name == "get_intraday":
        sym = (args.get("symbol") or "").upper()
        try:
            days = int(args.get("days") or 15)
        except (TypeError, ValueError):
            days = 15
        interval = (args.get("interval") or "5m").lower()
        # Off-load the blocking Yahoo/DB call so the event loop stays free.
        return await asyncio.to_thread(
            yahoo_ondemand.get_intraday_smart, sym, days, interval
        )
    return await _orig_call_tool(name, args)

main._call_tool = _call_tool_with_ondemand

# Upgrade the get_intraday tool schema/description so the smart behaviour
# (DB-first + live Yahoo) and the new `interval` / larger `days` are discoverable.
for _t in main.MCP_TOOLS:
    if _t.get("name") == "get_intraday":
        _t["description"] = (
            "Intraday OHLC for ANY stock. Futures -> stored Fyers 1-min (DB, last 7d). "
            "Non-futures -> fetched LIVE from Yahoo and NOT stored (5-min default, up to ~60d). "
            "Params: symbol, days (default 15), interval (1m/5m/15m/30m/60m/1d)."
        )
        _t["inputSchema"]["properties"]["days"] = {"type": "integer"}
        _t["inputSchema"]["properties"]["interval"] = {"type": "string"}
        break


# === STARTUP ===
@main.app.on_event("startup")
async def startup_v169():
    schedulers = [
        ("main_scheduler",  _real_main_scheduler),   # CMP refresh + earnings (main.py)
        ("futures_5min",    _scheduler_futures_5min), # DISABLED
        ("equity_10min",    _scheduler_equity_10min), # DISABLED
        ("eod_raw_prices",  _scheduler_eod),          # ACTIVE — EOD OHLCV
    ]
    for name, coro_fn in schedulers:
        t = asyncio.create_task(coro_fn(), name=name)
        _BG_TASKS.add(t)
        t.add_done_callback(_BG_TASKS.discard)
    main.log.info(
        f"v{VERSION_HOTFIX}: intraday=DISABLED | equity=DISABLED | EOD=ACTIVE | CMP=ACTIVE | "
        f"get_intraday=DB-first+Yahoo-ondemand | bg_tasks={len(_BG_TASKS)}"
    )


# === HEALTH ===
@main.app.get("/api/v169/health")
def v169_health():
    return {
        "version": VERSION_HOTFIX,
        "intraday_enabled": INTRADAY_ENABLED,
        "equity_enabled": EQUITY_ENABLED,
        "raw_prices_done_today": str(_raw_prices_done_today) if _raw_prices_done_today else None,
        "is_market_hours": main._is_market_hours(),
        "is_eod_window": main._is_eod_window(),
        "bg_tasks_held": len(_BG_TASKS),
        "bg_tasks": [{"name": t.get_name(), "done": t.done()} for t in _BG_TASKS],
    }


@main.app.post("/api/v169/trigger_eod")
async def v169_trigger_eod():
    global _raw_prices_done_today
    _raw_prices_done_today = None
    t = asyncio.create_task(_task_update_raw_prices_v169(), name="manual_eod")
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return {"status": "ok", "message": "EOD raw_prices task queued"}


# On-demand intraday for a single non-future, no DB writes (HTTP convenience).
@main.app.get("/api/intraday_ondemand/{symbol}")
async def intraday_ondemand(symbol: str, days: int = 15, interval: str = "5m"):
    return await asyncio.to_thread(
        yahoo_ondemand.get_intraday_smart, symbol.upper(), days, interval
    )


app = main.app
