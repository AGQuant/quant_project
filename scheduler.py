"""
Scheduler + background tasks — extracted from main.py (refactor file 4/5, 04-Jun-2026).

Self-contained: own _conn, _ist_now, own copies of the data-feed helpers the
scheduled jobs call in-process (Yahoo CMP/intraday, heal gaps, ADR/PCR compute).
Imports engine modules directly (same as main.py): v8_engine, v8_signal_writer,
v8_paper, global_indices, qb_eod_checker, gvm_nightly, refresh_takeaways, nse_holidays.

main.py calls start_background(app, base_url, admin_token) inside its startup event.
The earnings job calls main's HTTP endpoint /api/admin/load_earnings_from_screener
(served by admin_data.py) — kept as HTTP call, unchanged.

ADR/PCR compute (_compute_and_store_adr/_pcr) live HERE now and are ALSO imported
back by main.py so the manual endpoint /api/daily/compute_metrics keeps working.

Single live engine: v8_signal_writer.run_live_signal_writer() every 5-min.
v8_live.py archived — v8_history_cache no longer built or used.

04-Jun-2026 fix: PCR subquery referenced invalid column `ts2` — corrected.
06-Jun-2026 fix: Global indices fetch moved to 06:00 IST, runs every day incl weekends.
  Global intraday: 5-min fetch, restricted to 06:00-23:30 IST (time guard added).
  Intraday tickers expanded: Gold, Silver, WTI, Brent, Natural Gas, Bitcoin.
06-Jun-2026: v8_live removed. Single engine = v8_signal_writer (5-min, 19 metrics live).
07-Jun-2026: V10 ST+EMA wired — every 5-min during market hours, appends 5m bar from
  live 1m feed into nifty_5m_test_data, computes signal, Telegram alert on BUY/SELL.
  Isolated/advisory; does not touch V8 or the 1m feed writes.
"""

import os
import asyncio
import time
import logging
import urllib.parse
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List

import httpx
import psycopg

from nse_holidays import is_trading_day
from v8_engine import run_v8_engine
from gvm_nightly import recompute_gvm
import v8_paper
import global_indices
import v8_signal_writer
import qb_eod_checker
import refresh_takeaways as rt
import v10_st_ema

log = logging.getLogger("scorr.scheduler")

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_TOKEN  = os.getenv("ADMIN_TOKEN", "")

# Set by start_background()
_BASE_URL = "https://quantproject-production.up.railway.app"


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _is_market_hours() -> bool:
    now = _ist_now()
    if not is_trading_day(now.date()):
        return False
    return now.replace(hour=9, minute=15, second=0, microsecond=0) <= now <= now.replace(hour=15, minute=30, second=0, microsecond=0)


def _get_futures_symbols() -> List[str]:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE ORDER BY symbol")
            rows = cur.fetchall()
            if rows:
                return [r[0] for r in rows]
            cur.execute("SELECT DISTINCT symbol FROM v8_universe ORDER BY symbol")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"_get_futures_symbols failed: {e}"); return []


def _get_all_gvm_symbols() -> List[str]:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM gvm_scores ORDER BY symbol")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"_get_all_gvm_symbols failed: {e}"); return []


def _get_full_cmp_universe() -> List[str]:
    return sorted(set(_get_all_gvm_symbols()) | set(_get_futures_symbols()))


def _get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
            r = cur.fetchone(); return r[0] if r else default
    except Exception as e:
        log.error(f"_get_config {key} failed: {e}"); return default


def _yahoo_cmp_fallback_on() -> bool:
    return str(_get_config("yahoo_cmp_fallback", "off")).lower() in ("on", "true", "1", "yes")


def _yahoo_ticker(symbol: str) -> str:
    return {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK"}.get(symbol, f"{symbol}.NS")


async def _fetch_cmp_yahoo(symbols: List[str]) -> Dict[str, float]:
    results = {}
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for symbol in symbols:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(_yahoo_ticker(symbol))}?interval=1d&range=2d"
            try:
                r = await client.get(url); r.raise_for_status(); data = r.json()
                chart = data.get("chart", {}).get("result", [])
                if chart:
                    closes = chart[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    closes = [x for x in closes if x is not None]
                    if closes:
                        results[symbol] = float(closes[-1])
            except Exception as e:
                log.warning(f"CMP chart API {symbol}: {e}")
            await asyncio.sleep(0.1)
    log.info(f"CMP fetched: {len(results)}/{len(symbols)} symbols")
    return results


def _upsert_cmp(cmp_map):
    if not cmp_map:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            for sym, price in cmp_map.items():
                cur.execute("INSERT INTO cmp_prices (symbol, cmp, updated_at, source) VALUES (%s, %s, NOW(), 'yahoo') ON CONFLICT (symbol) DO UPDATE SET cmp = EXCLUDED.cmp, updated_at = NOW(), source = 'yahoo'", (sym, price))
            conn.commit()
        log.info(f"CMP upserted: {len(cmp_map)} symbols")
    except Exception as e:
        log.error(f"_upsert_cmp failed: {e}")


# ── ADR / PCR compute (also imported back by main.py for manual endpoint) ────

def _compute_and_store_adr(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            WITH latest_date AS (SELECT MAX(price_date) AS pd FROM raw_prices),
            latest AS (
                SELECT r.symbol, r.close FROM raw_prices r
                JOIN futures_universe fu ON r.symbol = fu.symbol AND fu.is_active = TRUE
                WHERE r.price_date = (SELECT pd FROM latest_date)
            ),
            prev AS (
                SELECT DISTINCT ON (r.symbol) r.symbol, r.close AS prev_close
                FROM raw_prices r
                JOIN futures_universe fu ON r.symbol = fu.symbol AND fu.is_active = TRUE
                WHERE r.price_date < (SELECT pd FROM latest_date)
                ORDER BY r.symbol, r.price_date DESC
            ),
            agg AS (
                SELECT (SELECT pd FROM latest_date) AS price_date,
                    COUNT(*) FILTER (WHERE l.close > p.prev_close) AS advances,
                    COUNT(*) FILTER (WHERE l.close < p.prev_close) AS declines,
                    COUNT(*) FILTER (WHERE l.close = p.prev_close) AS unchanged
                FROM latest l JOIN prev p ON l.symbol = p.symbol
            )
            INSERT INTO adr_daily (price_date, advances, declines, unchanged, adr)
            SELECT price_date, advances, declines, unchanged,
                   ROUND(advances::numeric / NULLIF(declines, 0), 3)
            FROM agg
            ON CONFLICT (price_date) DO UPDATE SET
                advances = EXCLUDED.advances, declines = EXCLUDED.declines,
                unchanged = EXCLUDED.unchanged, adr = EXCLUDED.adr, computed_at = NOW()
            RETURNING price_date, advances, declines, unchanged, adr
        """)
        row = cur.fetchone(); conn.commit()
        if row:
            return {"price_date": str(row[0]), "advances": row[1], "declines": row[2], "unchanged": row[3], "adr": float(row[4] or 0)}
        return {"status": "no_data"}


def _compute_and_store_pcr(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pcr_daily (price_date, underlying, put_oi, call_oi, pcr)
            SELECT DATE(ts), underlying,
                SUM(CASE WHEN option_type = 'PE' THEN oi ELSE 0 END),
                SUM(CASE WHEN option_type = 'CE' THEN oi ELSE 0 END),
                ROUND(SUM(CASE WHEN option_type = 'PE' THEN oi ELSE 0 END)::numeric /
                    NULLIF(SUM(CASE WHEN option_type = 'CE' THEN oi ELSE 0 END), 0), 3)
            FROM option_chain
            WHERE ts IN (SELECT MAX(oc2.ts) FROM option_chain oc2 WHERE DATE(oc2.ts) = DATE(option_chain.ts) GROUP BY DATE(oc2.ts))
            AND DATE(ts) = (SELECT MAX(DATE(ts)) FROM option_chain)
            GROUP BY DATE(ts), underlying
            ON CONFLICT (price_date, underlying) DO UPDATE SET
                put_oi = EXCLUDED.put_oi, call_oi = EXCLUDED.call_oi, pcr = EXCLUDED.pcr, computed_at = NOW()
        """)
        rowcount = cur.rowcount; conn.commit()
        return {"status": "ok", "rows": rowcount}


# ── State flags ───────────────────────────────────────────────────────────────
_raw_prices_updated_today:  Optional[date] = None
_earnings_loaded_today:     Optional[date] = None
_yahoo_daily_running:       bool           = False
_v8_engine_ran_today:       Optional[date] = None
_v8_engine_running:         bool           = False
_signal_writer_running:     bool           = False
_gvm_recompute_ran_today:   Optional[date] = None
_gvm_recompute_running:     bool           = False
_paper_tick_running:        bool           = False
_paper_pivots_built:        Optional[date] = None
_global_fetched_today:      Optional[date] = None
_global_fetching:           bool           = False
_global_intraday_fetching:  bool           = False
_qb_eod_ran_today:          Optional[date] = None
_qb_eod_running:            bool           = False
_qb_intraday_mark_running:  bool           = False
_daily_metrics_ran_today:   Optional[date] = None
_daily_metrics_running:     bool           = False
_refresh_check_ran_today:   Optional[date] = None
_v10_running:               bool           = False


# ── Background tasks ───────────────────────────────────────────────────────────

async def _task_refresh_cmp():
    if not _yahoo_cmp_fallback_on():
        return
    symbols = _get_full_cmp_universe()
    if not symbols:
        return
    cmp_map = await _fetch_cmp_yahoo(symbols); _upsert_cmp(cmp_map)


async def _bg_yahoo_daily(symbols=None, lookback=None):
    global _raw_prices_updated_today, _yahoo_daily_running
    if _yahoo_daily_running:
        return
    _yahoo_daily_running = True
    try:
        import yahoo_daily_update as ydu
        result = await ydu.run_async(symbols=symbols, lookback=lookback)
        _raw_prices_updated_today = _ist_now().date()
        log.info(f"yahoo_daily done: {result}")
    except Exception as e:
        log.error(f"yahoo_daily failed: {e}")
    finally:
        _yahoo_daily_running = False


async def _task_update_raw_prices():
    global _raw_prices_updated_today
    today = _ist_now().date()
    if _raw_prices_updated_today == today:
        return
    log.info("21:00 IST: Launching raw_prices update")
    asyncio.create_task(_bg_yahoo_daily())


def _bg_run_v8_engine():
    global _v8_engine_ran_today, _v8_engine_running
    if _v8_engine_running:
        return
    _v8_engine_running = True
    try:
        with _conn() as conn:
            results = run_v8_engine(conn)
        _v8_engine_ran_today = _ist_now().date()
        log.info(f"V8 engine done: {results.get('symbols_processed')} symbols")
    except Exception as e:
        log.error(f"V8 engine failed: {e}")
    finally:
        _v8_engine_running = False


async def _task_run_v8_engine():
    global _v8_engine_ran_today
    if _v8_engine_ran_today == _ist_now().date():
        return
    log.info("15:45 IST: V8 engine auto-run")
    asyncio.create_task(asyncio.to_thread(_bg_run_v8_engine))


def _bg_recompute_gvm():
    global _gvm_recompute_ran_today, _gvm_recompute_running
    if _gvm_recompute_running:
        return
    _gvm_recompute_running = True
    try:
        res = recompute_gvm(refresh_momentum=True)
        _gvm_recompute_ran_today = _ist_now().date()
        log.info(f"GVM recompute done: scored={res.get('scored')}")
    except Exception as e:
        log.error(f"GVM recompute failed: {e}")
    finally:
        _gvm_recompute_running = False


async def _task_recompute_gvm_daily():
    if _gvm_recompute_ran_today == _ist_now().date():
        return
    log.info("22:00 IST: GVM daily recompute")
    asyncio.create_task(asyncio.to_thread(_bg_recompute_gvm))


def _bg_paper_tick():
    global _paper_tick_running
    if _paper_tick_running:
        return
    _paper_tick_running = True
    try:
        buy_slots = sell_slots = None
        try:
            with httpx.Client(timeout=30) as c:
                mood = c.get(f"{_BASE_URL}/api/v8/market_mood").json()
                buy_slots, sell_slots = mood.get("buy_slots"), mood.get("sell_slots")
        except Exception as e:
            log.warning(f"paper mood fetch failed: {e}")
        with _conn() as conn:
            res = v8_paper.paper_tick(conn, buy_slots=buy_slots, sell_slots=sell_slots)
        if res.get("entries") or res.get("exits") or res.get("gate_exits"):
            log.info(f"paper_tick: {len(res.get('entries',[]))}E {len(res.get('exits',[]))}X")
    except Exception as e:
        log.error(f"paper_tick failed: {e}")
    finally:
        _paper_tick_running = False


def _bg_build_paper_pivots():
    global _paper_pivots_built
    try:
        with _conn() as conn:
            res = v8_paper.compute_pivots(conn)
        _paper_pivots_built = _ist_now().date()
        log.info(f"paper pivots built: {res.get('built')}/{res.get('total')}")
    except Exception as e:
        log.error(f"paper pivots build failed: {e}")


async def _task_build_paper_pivots():
    if _paper_pivots_built == _ist_now().date():
        return
    log.info("22:05 IST: Building rolling-5 paper pivots")
    asyncio.create_task(asyncio.to_thread(_bg_build_paper_pivots))


def _bg_fetch_global():
    global _global_fetched_today, _global_fetching
    if _global_fetching:
        return
    _global_fetching = True
    try:
        with global_indices.get_conn_from_env() as conn:
            res = asyncio.run(global_indices.fetch_global_indices(conn))
            try:
                global_indices.prune_global_indices(conn, years=5)
            except Exception:
                pass
        _global_fetched_today = _ist_now().date()
        log.info(f"global_indices done: {res.get('stored')}/{res.get('total')}")
    except Exception as e:
        log.error(f"global_indices failed: {e}")
    finally:
        _global_fetching = False


async def _task_fetch_global():
    if _global_fetched_today == _ist_now().date():
        return
    log.info("06:00 IST: Fetching global indices (every day incl weekends — Gold/Silver/BTC 24x5/7)")
    asyncio.create_task(asyncio.to_thread(_bg_fetch_global))


def _bg_fetch_global_intraday():
    global _global_intraday_fetching
    if _global_intraday_fetching:
        return
    _global_intraday_fetching = True
    try:
        with global_indices.get_conn_from_env() as conn:
            res = asyncio.run(global_indices.fetch_global_intraday(conn))
            try:
                global_indices.prune_global_intraday(conn, days=7)
            except Exception as e:
                log.warning(f"global_intraday prune failed: {e}")
        log.info(f"global_intraday done: {res.get('stored')} bars ({res.get('symbols')} symbols)")
    except Exception as e:
        log.error(f"global_intraday failed: {e}")
    finally:
        _global_intraday_fetching = False


async def _task_fetch_global_intraday():
    _n = _ist_now()
    if not (6 <= _n.hour <= 23):
        return
    asyncio.create_task(asyncio.to_thread(_bg_fetch_global_intraday))


def _bg_signal_writer():
    global _signal_writer_running
    if _signal_writer_running:
        return
    _signal_writer_running = True
    try:
        with _conn() as conn:
            res = v8_signal_writer.run_live_signal_writer(conn)
        log.info(f"signal_writer: updated={res.get('updated',0)} no_bar={res.get('no_bar',0)} source={res.get('source','?')}")
    except Exception as e:
        log.error(f"signal_writer failed: {e}")
    finally:
        _signal_writer_running = False


def _bg_v10_tick():
    """V10 ST+EMA: append closed 5m bar from live 1m feed, compute signal, alert on BUY/SELL.
    Isolated/advisory; reads intraday_prices (1m) + nifty_5m_test_data only."""
    global _v10_running
    if _v10_running:
        return
    _v10_running = True
    try:
        res = v10_st_ema.tick()
        if res.get("signal") in ("BUY", "SELL"):
            log.info(f"V10 SIGNAL: {res['signal']} NIFTY @ {res.get('price')} | alert={res.get('alert')}")
    except Exception as e:
        log.error(f"v10_tick failed: {e}")
    finally:
        _v10_running = False


def _bg_qb_intraday_mark():
    global _qb_intraday_mark_running
    if _qb_intraday_mark_running:
        return
    _qb_intraday_mark_running = True
    try:
        with _conn() as conn:
            res = qb_eod_checker.qb_intraday_mark(conn)
        log.info(f"qb_intraday_mark: {res.get('marked')}/{res.get('symbols')} marked")
    except Exception as e:
        log.error(f"qb_intraday_mark failed: {e}")
    finally:
        _qb_intraday_mark_running = False


def _bg_qb_eod_checker():
    global _qb_eod_ran_today, _qb_eod_running
    if _qb_eod_running:
        return
    _qb_eod_running = True
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT basket_name FROM quant_paper_positions WHERE status='open'")
                baskets = [r[0] for r in cur.fetchall()]
            for basket in baskets:
                res = qb_eod_checker.run_eod_checker(conn, basket_name=basket)
                log.info(f"qb_eod {basket}: marked={res.get('positions_marked')} HS1={res.get('hard_stop_1_exits')} HS2={res.get('hard_stop_2_exits')}")
        _qb_eod_ran_today = _ist_now().date()
    except Exception as e:
        log.error(f"qb_eod_checker failed: {e}")
    finally:
        _qb_eod_running = False


async def _task_qb_eod_checker():
    if _qb_eod_ran_today == _ist_now().date():
        return
    log.info("21:05 IST: QB EOD stop-loss check + P&L mark (all baskets)")
    asyncio.create_task(asyncio.to_thread(_bg_qb_eod_checker))


def _bg_check_refresh_due():
    global _refresh_check_ran_today
    try:
        result = rt.check_and_flag_due_refreshes()
        _refresh_check_ran_today = _ist_now().date()
        if result.get("flagged"):
            log.info(f"Refresh due: {result['flagged']}")
        else:
            log.info("Refresh check: nothing due today")
    except Exception as e:
        log.error(f"refresh check failed: {e}")


async def _task_check_refresh_due():
    global _refresh_check_ran_today
    now = _ist_now()
    if now.hour == 6 and now.minute < 5 and _refresh_check_ran_today != now.date():
        asyncio.create_task(asyncio.to_thread(_bg_check_refresh_due))


def _bg_compute_daily_metrics():
    global _daily_metrics_ran_today, _daily_metrics_running
    if _daily_metrics_running:
        return
    _daily_metrics_running = True
    try:
        with _conn() as conn:
            adr = _compute_and_store_adr(conn); pcr = _compute_and_store_pcr(conn)
        _daily_metrics_ran_today = _ist_now().date()
        log.info(f"daily_metrics: ADR={adr.get('adr')} PCR_rows={pcr.get('rows')}")
    except Exception as e:
        log.error(f"daily_metrics failed: {e}")
    finally:
        _daily_metrics_running = False


async def _task_compute_daily_metrics():
    if _daily_metrics_ran_today == _ist_now().date():
        return
    log.info("15:50 IST: Computing daily ADR + PCR")
    asyncio.create_task(asyncio.to_thread(_bg_compute_daily_metrics))


async def _task_load_earnings_daily():
    global _earnings_loaded_today
    today = _ist_now().date()
    if _earnings_loaded_today == today:
        return
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            headers = {"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}
            r = await client.post(f"{_BASE_URL}/api/admin/load_earnings_from_screener", headers=headers)
            log.info(f"Earnings daily load: {r.json()}")
            _earnings_loaded_today = today
    except Exception as e:
        log.error(f"_task_load_earnings_daily failed: {e}")


# ── Loops ────────────────────────────────────────────────────────────────────

async def _scheduler():
    log.info("Scheduler started (scheduler.py)")
    asyncio.create_task(_live_loop())
    while True:
        try:
            now = _ist_now(); trading_day = is_trading_day(now.date())
            # Runs every day including weekends
            if now.hour == 6 and now.minute < 5:
                await _task_check_refresh_due()
                await _task_fetch_global()
            # Global intraday every 5 min, 06:00-23:30 IST (incl weekends)
            await _task_fetch_global_intraday()
            if trading_day and now.hour == 9 and now.minute < 5:   await _task_load_earnings_daily()
            if _is_market_hours():                                   await _task_refresh_cmp()
            if trading_day and now.hour == 15 and 45 <= now.minute < 55: await _task_run_v8_engine()
            if trading_day and now.hour == 15 and 50 <= now.minute < 60: await _task_compute_daily_metrics()
            if trading_day and now.hour == 21 and now.minute < 5:   await _task_update_raw_prices()
            if trading_day and now.hour == 21 and 5 <= now.minute < 15: await _task_qb_eod_checker()
            if now.hour == 22 and now.minute < 10:                  await _task_recompute_gvm_daily()
            if now.hour == 22 and 5 <= now.minute < 15:             await _task_build_paper_pivots()
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        await asyncio.sleep(300)


async def _live_loop():
    """
    1-min heartbeat during market hours.
    - Every tick: paper_tick
    - Every 5 ticks (5-min): signal_writer (single live engine — 19 metrics + qualified)
                              + V10 ST+EMA tick (append 5m bar, signal, alert)
    - Every 15 ticks (15-min): qb_intraday_mark
    """
    log.info("Live loop started — engines: v8_signal_writer + V10 ST+EMA every 5-min")
    tick_count = 0
    while True:
        try:
            if _is_market_hours():
                asyncio.create_task(asyncio.to_thread(_bg_paper_tick))
                tick_count += 1
                if tick_count % 5 == 0:
                    asyncio.create_task(asyncio.to_thread(_bg_signal_writer))
                    asyncio.create_task(asyncio.to_thread(_bg_v10_tick))
                if tick_count % 15 == 0:
                    asyncio.create_task(asyncio.to_thread(_bg_qb_intraday_mark))
            else:
                tick_count = 0
        except Exception as e:
            log.error(f"live loop error: {e}")
        await asyncio.sleep(60)


_BG_TASKS: set = set()


def start_background(app, base_url: str, admin_token: str = ""):
    """Called from main.py startup. Launches the scheduler coroutine and keeps a strong ref."""
    global _BASE_URL, ADMIN_TOKEN
    _BASE_URL = base_url
    if admin_token:
        ADMIN_TOKEN = admin_token
    t = asyncio.create_task(_scheduler())
    _BG_TASKS.add(t); t.add_done_callback(_BG_TASKS.discard)
    log.info("scheduler.start_background: scheduler task launched")
    return t
