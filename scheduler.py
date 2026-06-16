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
07-Jun-2026: pcr_intraday wired — every 5-min during market hours, rolls option_chain
  into pcr_intraday (ATM±5 + total PCR per bar). Self-healing. Isolated/advisory.
08-Jun-2026 RELIABILITY: _live_loop is now self-healing (per-tick try/except — a single
  failed tick never kills the loop). Heartbeats written to app_config:
    sched_live_loop_hb  — every live-loop iteration (proves loop alive)
    sched_writer_hb     — every successful signal_writer run (proves writer firing)
    sched_loop_hb       — every 5-min scheduler iteration
  _scheduler SUPERVISES _live_loop: if the task ever dies/completes, it is relaunched.
  Root cause fixed: previously a redeploy or unhandled error could silently kill the
  live loop, stopping all 5-min writes for the rest of the session with no recovery.
10-Jun-2026 CANCELLATION FIX: Railway redeploy sends CancelledError to all tasks.
  CancelledError is NOT caught by `except Exception` — it propagated through
  `await asyncio.sleep()` and silently killed both _live_loop and _scheduler.
  Fix 1: _live_loop sleep wrapped in try/except CancelledError → clean return so
          supervisor immediately sees task.done() and relaunches.
  Fix 2: _scheduler sleep wrapped in try/except CancelledError → checks and relaunches
          _live_loop immediately on cancel rather than waiting up to 300s.
  Fix 3: _scheduler supervisor interval reduced 300s → 60s for faster death detection.
16-Jun-2026: futures_universe auto-sync from Fyers feed, Monday 08:00 IST.
  Strips expiry suffix from Fyers symbols (e.g. RADICO26JUNFUT → RADICO).
  Adds missing symbols as active, deactivates symbols absent 2+ consecutive Mondays.
  Logs changes to session_log. MCP tool: sync_futures_universe.
"""

import os
import asyncio
import time
import logging
import re
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
import pcr_intraday

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


def _set_heartbeat(key: str, value: Optional[str] = None):
    """
    Write a liveness heartbeat to app_config (upsert). value defaults to the
    current IST timestamp. Used to prove the live loop + writer are alive so the
    dashboard / supervisor can detect a stall and alarm on staleness.
    """
    if value is None:
        value = _ist_now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_config (key, value, updated_at) VALUES (%s, %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
                (key, value),
            )
            conn.commit()
    except Exception as e:
        log.warning(f"_set_heartbeat {key} failed: {e}")


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


# ── futures_universe auto-sync from Fyers feed ────────────────────────────────

# Regex to strip expiry suffix from Fyers futures symbols
# e.g. RADICO26JUNFUT → RADICO, BAJAJ-AUTO26JUNFUT → BAJAJ-AUTO
_EXPIRY_SUFFIX_RE = re.compile(r'\d{2}[A-Z]{3}FUT$', re.IGNORECASE)

# Indices to always exclude from equity futures universe
_EXCLUDE_SYMBOLS = {"NIFTY50", "BANKNIFTY", "NIFTYMID50"}


def _strip_expiry(fyers_symbol: str) -> str:
    """Strip Fyers expiry suffix to get base NSE symbol."""
    return _EXPIRY_SUFFIX_RE.sub("", fyers_symbol).strip()


def sync_futures_universe(conn) -> dict:
    """
    Sync futures_universe with Fyers feed (futures_basis last 7 days).
    - Strips expiry suffix from Fyers symbols to get base symbol
    - ADDs missing active symbols (lot_size=1 default, updated manually later)
    - DEACTIVATEs symbols absent from Fyers for 2+ consecutive Mondays
      (tracked via app_config key 'fu_absent_last_monday')
    Logs result to session_log.
    """
    today = _ist_now().date()

    # 1. Get distinct base symbols from Fyers feed (last 7 days)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT symbol FROM futures_basis
            WHERE DATE(ts) >= CURRENT_DATE - 7
        """)
        fyers_raw = [r[0] for r in cur.fetchall()]

    fyers_base = set()
    for sym in fyers_raw:
        base = _strip_expiry(sym)
        if base and base not in _EXCLUDE_SYMBOLS:
            fyers_base.add(base)

    # 2. Get current futures_universe
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, is_active FROM futures_universe")
        fu_rows = {r[0]: r[1] for r in cur.fetchall()}

    fu_active   = {s for s, active in fu_rows.items() if active}
    fu_all      = set(fu_rows.keys())

    # 3. Symbols to ADD (in Fyers but not in FU at all)
    to_add = fyers_base - fu_all - _EXCLUDE_SYMBOLS

    # 4. Symbols to REACTIVATE (in Fyers, in FU but inactive)
    to_reactivate = (fyers_base & fu_all) - fu_active - _EXCLUDE_SYMBOLS

    # 5. Absent from Fyers this Monday — track for deactivation
    absent_this_week = fu_active - fyers_base - _EXCLUDE_SYMBOLS

    # Load last week's absent list from app_config
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='fu_absent_last_monday'")
            row = cur.fetchone()
            absent_last_week = set(row[0].split(",")) if row and row[0] else set()
    except Exception:
        absent_last_week = set()

    # Deactivate only if absent BOTH this week AND last week (2 consecutive Mondays)
    to_deactivate = absent_this_week & absent_last_week

    # Save this week's absent list for next Monday
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_config (key, value, updated_at) VALUES ('fu_absent_last_monday', %s, NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                (",".join(sorted(absent_this_week)),)
            )
        conn.commit()
    except Exception as e:
        log.warning(f"fu_absent save failed: {e}")

    # 6. Execute changes
    added = []; reactivated = []; deactivated = []

    with conn.cursor() as cur:
        for sym in sorted(to_add):
            try:
                cur.execute("""
                    INSERT INTO futures_universe (symbol, lot_size, is_active, added_at, updated_at)
                    VALUES (%s, 1, TRUE, NOW(), NOW())
                    ON CONFLICT (symbol) DO NOTHING
                """, (sym,))
                if cur.rowcount:
                    added.append(sym)
            except Exception as e:
                log.warning(f"fu add {sym}: {e}")

        for sym in sorted(to_reactivate):
            try:
                cur.execute("UPDATE futures_universe SET is_active=TRUE, updated_at=NOW() WHERE symbol=%s", (sym,))
                reactivated.append(sym)
            except Exception as e:
                log.warning(f"fu reactivate {sym}: {e}")

        for sym in sorted(to_deactivate):
            try:
                cur.execute("UPDATE futures_universe SET is_active=FALSE, updated_at=NOW() WHERE symbol=%s", (sym,))
                deactivated.append(sym)
            except Exception as e:
                log.warning(f"fu deactivate {sym}: {e}")

    conn.commit()

    result = {
        "sync_date": str(today),
        "fyers_base_symbols": len(fyers_base),
        "fu_active_before": len(fu_active),
        "added": added,
        "reactivated": reactivated,
        "deactivated": deactivated,
        "absent_this_week": sorted(absent_this_week),
    }

    # Log to session_log
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO session_log (session_date, session_ts, category, title, details)
                VALUES (CURRENT_DATE, NOW(), 'task', 'futures_universe_sync', %s)
            """, (psycopg.types.json.Jsonb(result),))
        conn.commit()
    except Exception as e:
        log.warning(f"fu sync session_log: {e}")

    log.info(f"fu_sync: +{len(added)} added, +{len(reactivated)} reactivated, -{len(deactivated)} deactivated")
    return result


# ── State flags ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
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
_pcr_intraday_running:      bool           = False
_fu_sync_ran_this_week:     Optional[date] = None   # tracks Monday sync
_fu_sync_running:           bool           = False

# Supervisor handle for the live loop task (relaunched if it ever dies)
_live_loop_task: Optional[asyncio.Task] = None


# ── Background tasks ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

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
        # Heartbeat: prove the writer actually fired (dashboard staleness alarm reads this)
        _set_heartbeat("sched_writer_hb")
        _set_heartbeat("sched_writer_last_result",
                       f"updated={res.get('updated',0)} source={res.get('source','?')}")
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


def _bg_pcr_intraday():
    """5-min intraday PCR rollup (ATM±5 + total) from option_chain into pcr_intraday.
    Self-heals any missed bars. Isolated/advisory; reads option_chain + intraday_prices."""
    global _pcr_intraday_running
    if _pcr_intraday_running:
        return
    _pcr_intraday_running = True
    try:
        with _conn() as conn:
            res = pcr_intraday.compute_pcr_intraday(conn=conn)
        log.info(f"pcr_intraday: {res.get('computed', res.get('bars'))}")
    except Exception as e:
        log.error(f"pcr_intraday failed: {e}")
    finally:
        _pcr_intraday_running = False


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


def _bg_sync_futures_universe():
    """Monday 08:00 IST: sync futures_universe with Fyers feed."""
    global _fu_sync_ran_this_week, _fu_sync_running
    if _fu_sync_running:
        return
    _fu_sync_running = True
    try:
        with _conn() as conn:
            result = sync_futures_universe(conn)
        _fu_sync_ran_this_week = _ist_now().date()
        log.info(f"fu_sync done: +{len(result.get('added',[]))} added "
                 f"+{len(result.get('reactivated',[]))} reactivated "
                 f"-{len(result.get('deactivated',[]))} deactivated")
    except Exception as e:
        log.error(f"fu_sync failed: {e}")
    finally:
        _fu_sync_running = False


async def _task_sync_futures_universe():
    """Runs Monday 08:00 IST only — idempotent via _fu_sync_ran_this_week."""
    global _fu_sync_ran_this_week
    now = _ist_now()
    # weekday() == 0 = Monday
    if now.weekday() != 0:
        return
    if now.hour != 8 or now.minute >= 5:
        return
    if _fu_sync_ran_this_week == now.date():
        return
    log.info("Monday 08:00 IST: futures_universe auto-sync from Fyers feed")
    asyncio.create_task(asyncio.to_thread(_bg_sync_futures_universe))


# ── Loops ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _scheduler():
    global _live_loop_task
    log.info("Scheduler started (scheduler.py)")
    _live_loop_task = asyncio.create_task(_live_loop())
    while True:
        try:
            now = _ist_now(); trading_day = is_trading_day(now.date())

            # SUPERVISOR: if the live loop task ever died/completed, relaunch it.
            # 10-Jun fix: CancelledError causes _live_loop to return cleanly →
            # task.done() is True immediately → supervisor relaunches within 60s.
            if _live_loop_task is None or _live_loop_task.done():
                if _live_loop_task is not None:
                    exc = None
                    try:
                        exc = _live_loop_task.exception()
                    except Exception:
                        pass
                    log.error(f"live_loop task was dead (exc={exc!r}) — relaunching")
                _live_loop_task = asyncio.create_task(_live_loop())
                _set_heartbeat("sched_live_loop_relaunched")

            # Runs every day including weekends
            if now.hour == 6 and now.minute < 5:
                await _task_check_refresh_due()
                await _task_fetch_global()
            # Global intraday every 5 min, 06:00-23:30 IST (incl weekends)
            await _task_fetch_global_intraday()
            # Monday 08:00 IST: futures_universe sync from Fyers
            await _task_sync_futures_universe()
            if trading_day and now.hour == 9 and now.minute < 5:   await _task_load_earnings_daily()
            if _is_market_hours():                                   await _task_refresh_cmp()
            if trading_day and now.hour == 15 and 45 <= now.minute < 55: await _task_run_v8_engine()
            if trading_day and now.hour == 15 and 50 <= now.minute < 60: await _task_compute_daily_metrics()
            if trading_day and now.hour == 21 and now.minute < 5:   await _task_update_raw_prices()
            if trading_day and now.hour == 21 and 5 <= now.minute < 15: await _task_qb_eod_checker()
            if now.hour == 22 and now.minute < 10:                  await _task_recompute_gvm_daily()
            if now.hour == 22 and 5 <= now.minute < 15:             await _task_build_paper_pivots()

            _set_heartbeat("sched_loop_hb")
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        # 10-Jun fix: reduced 300→60s so supervisor detects _live_loop death within 60s.
        # CancelledError wrapped so Railway redeploy doesn't kill the scheduler itself.
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            log.warning("scheduler sleep cancelled — checking live_loop before exit")
            if _live_loop_task is None or _live_loop_task.done():
                _live_loop_task = asyncio.create_task(_live_loop())
                _set_heartbeat("sched_live_loop_relaunched")
            return


async def _live_loop():
    """
    1-min heartbeat during market hours.
    - Every tick: paper_tick
    - Every 5 ticks (5-min): signal_writer (single live engine — 19 metrics + qualified)
                              + V10 ST+EMA tick (append 5m bar, signal, alert)
                              + pcr_intraday (5-min PCR rollup, self-healing)
    - Every 15 ticks (15-min): qb_intraday_mark

    08-Jun-2026 SELF-HEALING: each tick's work is wrapped so any failure is logged
    and the loop CONTINUES. tick_count is derived from wall-clock IST minutes (not a
    fragile in-memory counter) so a 5-min boundary is hit deterministically even if a
    prior tick was skipped. A liveness heartbeat is written every iteration.

    10-Jun-2026 CANCELLATION FIX: asyncio.CancelledError (raised by Railway on
    redeploy) is NOT caught by `except Exception`. Previously it propagated through
    `await asyncio.sleep(60)` and killed the while-True loop silently, stopping all
    5-min writes for the rest of the session. Fix: wrap the sleep in
    try/except CancelledError → clean return → supervisor sees task.done() and
    relaunches immediately (within 60s supervisor interval).
    """
    log.info("Live loop started — engines: v8_signal_writer + V10 ST+EMA every 5-min (self-healing)")
    while True:
        try:
            now = _ist_now()
            if _is_market_hours():
                minute = now.minute
                # paper_tick every minute
                asyncio.create_task(asyncio.to_thread(_bg_paper_tick))
                # 5-min boundary by wall clock (deterministic, survives skipped ticks)
                if minute % 5 == 0:
                    asyncio.create_task(asyncio.to_thread(_bg_signal_writer))
                    asyncio.create_task(asyncio.to_thread(_bg_v10_tick))
                    asyncio.create_task(asyncio.to_thread(_bg_pcr_intraday))
                # 15-min boundary by wall clock
                if minute % 15 == 0:
                    asyncio.create_task(asyncio.to_thread(_bg_qb_intraday_mark))
            # Always heartbeat the loop itself so a stall is detectable
            _set_heartbeat("sched_live_loop_hb")
        except Exception as e:
            # NEVER let an exception kill the loop — log and continue next tick.
            log.error(f"live loop error (continuing): {e}")
        # 10-Jun fix: CancelledError wrapped — clean return so supervisor relaunches.
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            log.warning("live_loop sleep cancelled — exiting cleanly for supervisor relaunch")
            return


_BG_TASKS: set = set()


def start_background(app, base_url: str, admin_token: str = ""):
    """Called from main.py startup. Launches the scheduler coroutine and keeps a strong ref."""
    global _BASE_URL, ADMIN_TOKEN
    _BASE_URL = base_url
    if admin_token:
        ADMIN_TOKEN = admin_token
    t = asyncio.create_task(_scheduler())
    _BG_TASKS.add(t); t.add_done_callback(_BG_TASKS.discard)
    _set_heartbeat("sched_started")
    log.info("scheduler.start_background: scheduler task launched")
    return t
