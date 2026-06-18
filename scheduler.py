"""
Scheduler + background tasks — extracted from main.py (refactor file 4/5, 04-Jun-2026).

Self-contained: own _conn, _ist_now, own copies of the data-feed helpers the
scheduled jobs call in-process (Yahoo CMP/intraday, heal gaps, ADR/PCR compute).
Imports engine modules directly (same as main.py)

Schedule (IST, validated vs live prod as of 08-Jun-2026):

  06:00         Refresh-due check (stale data alert). Global indices fetch (every day incl weekends).
  06:00-23:30   Global intraday fetch every 5-min (time-gated).
                  Intraday tick

  Mkt open      Fyers feed starts live 1-min bars (fyers_feed.py standalone worker).

  Mkt 1-min     paper_tick + Yahoo CMP fallback (if enabled)
  Mkt 5-min     v8_signal_writer (19 live metrics) + V10 ST+EMA tick
                              + pcr_intraday (5-min PCR rollup, self-healing)
                              + intraday_paper INACTIVE 18-Jun-2026 — on-demand only
  Mkt 15-min    qb_intraday_mark (QB positions price mark)

  15:45         V8 EOD engine — sector_week/month peer-avg frozen (5 EOD metrics)
  15:50         ADR + PCR compute-and-store
  21:00         Yahoo daily OHLC update (raw_prices)
  21:05         QB EOD checker — P&L mark + hard-stop check (4 baskets)
  22:00         GVM recompute (momentum M + sector ratings)
  22:05         Paper pivot levels rebuild (rolling-5-day)

Changes log:
  06-Jun-2026: Global indices fetch moved to 06:00 IST, runs every day incl weekends.
  07-Jun-2026: pcr_intraday wired — every 5-min during market hours, rolls option_chain
    into pcr_intraday (ATM+/-5 + total PCR per bar). Self-healing. Isolated/advisory.
  08-Jun-2026 RELIABILITY FIX: CancelledError wrapped in both _live_loop + _scheduler
    sleeps (commit 84ea7d3). Supervisor interval 300->60s.
  10-Jun-2026: fyers_feed v5 wired as standalone worker (not in scheduler).
    Logs changes to session_log. MCP tool: sync_futures_universe.
  18-Jun-2026: intraday paper engine wired — every 5-min during market hours,
    refresh_tc_cache + scan both sides + auto-enter (no cap, +/-1.5%) + exit/square-off.
    Context-isolated (tc_intraday_* tables). Entry cutoff 15:00, hard square-off 15:15.
  18-Jun-2026: intraday paper DEACTIVATED from auto-run. refresh_tc_cache (420 computes)
    every 5-min too heavy. On-demand only via POST /api/intraday/tick.
"""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Set

import psycopg

log = logging.getLogger("scorr.scheduler")

DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

_BG_TASKS: Set[asyncio.Task] = set()


def _ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _conn():
    return psycopg.connect(DATABASE_URL)


def _is_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return (9, 15) <= t <= (15, 30)


def _set_heartbeat(label: str):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO system_health (component, status, checked_at, details)
                VALUES (%s, 'ok', NOW(), '{}')
                ON CONFLICT (component) DO UPDATE
                  SET status='ok', checked_at=NOW(), details='{}'
            """, (label,))
            conn.commit()
    except Exception:
        pass


# ── per-job run-guards ────────────────────────────────────────────────────────
_signal_writer_running:     bool           = False
_eod_running:               bool           = False
_eod_ran_today:             Optional[date] = None
_adr_pcr_ran_today:         Optional[date] = None
_yahoo_ran_today:           Optional[date] = None
_gvm_ran_today:             Optional[date] = None
_pivots_ran_today:          Optional[date] = None
_qb_eod_ran_today:          Optional[date] = None
_qb_eod_running:            bool           = False
_qb_intraday_mark_running:  bool           = False
_global_fetching:           bool           = False
_global_intraday_fetching:  bool           = False
_v10_running:               bool           = False
_pcr_intraday_running:      bool           = False
_intraday_paper_running:    bool           = False
_fu_sync_ran_this_week:     Optional[date] = None
_daily_metrics_ran_today:   Optional[date] = None
_earnings_ran_today:        Optional[date] = None
_refresh_due_ran_today:     Optional[date] = None


# ── CMP helpers (Yahoo fallback) ──────────────────────────────────────────────

async def _fetch_cmp_yahoo(conn, symbol: str) -> Optional[float]:
    try:
        import yahoo_cmp
        return await yahoo_cmp.fetch(symbol)
    except Exception:
        return None


async def _task_refresh_cmp(conn):
    try:
        import yahoo_cmp
        await yahoo_cmp.refresh_all(conn)
    except Exception as e:
        log.warning(f"cmp_refresh failed: {e}")


# ── background job wrappers ───────────────────────────────────────────────────

async def _bg_yahoo_daily(conn):
    global _yahoo_ran_today
    today = _ist_now().date()
    if _yahoo_ran_today == today:
        return
    try:
        import yahoo_daily
        result = await yahoo_daily.run(conn)
        log.info(f"yahoo_daily: {result}")
        _yahoo_ran_today = today
    except Exception as e:
        log.error(f"yahoo_daily failed: {e}")


async def _task_update_raw_prices():
    global _yahoo_ran_today
    today = _ist_now().date()
    if _yahoo_ran_today == today:
        return
    try:
        import yahoo_daily
        with _conn() as conn:
            result = await yahoo_daily.run(conn)
        log.info(f"yahoo_daily: {result}")
        _yahoo_ran_today = today
    except Exception as e:
        log.error(f"yahoo_daily failed: {e}")


async def _task_run_v8_engine():
    global _eod_running, _eod_ran_today
    today = _ist_now().date()
    if _eod_ran_today == today or _eod_running:
        return
    _eod_running = True
    try:
        import v8_engine
        with _conn() as conn:
            result = v8_engine.run_v8_engine(conn)
        log.info(f"v8_eod: {result.get('symbols_processed')} syms "
                 f"{len(result.get('errors', []))} errors")
        _eod_ran_today = today
    except Exception as e:
        log.error(f"v8_eod failed: {e}")
    finally:
        _eod_running = False


async def _task_recompute_gvm_daily():
    global _gvm_ran_today
    today = _ist_now().date()
    if _gvm_ran_today == today:
        return
    try:
        import gvm_nightly
        with _conn() as conn:
            result = gvm_nightly.gvm_recompute(conn)
        log.info(f"gvm_recompute: {result}")
        _gvm_ran_today = today
    except Exception as e:
        log.error(f"gvm_recompute failed: {e}")


async def _task_build_paper_pivots():
    global _pivots_ran_today
    today = _ist_now().date()
    if _pivots_ran_today == today:
        return
    try:
        import v8_paper
        with _conn() as conn:
            result = v8_paper.rebuild_pivots(conn)
        log.info(f"paper_pivots: {result}")
        _pivots_ran_today = today
    except Exception as e:
        log.error(f"paper_pivots failed: {e}")


async def _task_fetch_global():
    try:
        import global_indices
        with global_indices.get_conn_from_env() as conn:
            result = await global_indices.fetch_global_indices(conn)
        log.info(f"global_indices: {result}")
    except Exception as e:
        log.error(f"global_fetch failed: {e}")


async def _task_fetch_global_intraday():
    global _global_intraday_fetching
    if _global_intraday_fetching:
        return
    _global_intraday_fetching = True
    try:
        import global_indices
        with global_indices.get_conn_from_env() as conn:
            res = await global_indices.fetch_global_intraday(conn)
            try:
                global_indices.prune_global_intraday(conn, days=7)
            except Exception:
                pass
        log.debug(f"global_intraday: {res.get('stored')} bars")
    except Exception as e:
        log.debug(f"global_intraday failed: {e}")
    finally:
        _global_intraday_fetching = False


async def _task_qb_eod_checker():
    global _qb_eod_ran_today, _qb_eod_running
    today = _ist_now().date()
    if _qb_eod_ran_today == today or _qb_eod_running:
        return
    _qb_eod_running = True
    try:
        import qb_eod_checker
        with _conn() as conn:
            result = qb_eod_checker.run_eod_check(conn)
        log.info(f"qb_eod: {result.get('checked')} checked {result.get('exited')} exited")
        _qb_eod_ran_today = today
    except Exception as e:
        log.error(f"qb_eod_checker failed: {e}")
    finally:
        _qb_eod_running = False


async def _task_check_refresh_due():
    global _refresh_due_ran_today
    today = _ist_now().date()
    if _refresh_due_ran_today == today:
        return
    try:
        import refresh_takeaways as rt
        rt.check_refresh_due()
        _refresh_due_ran_today = today
    except Exception as e:
        log.warning(f"refresh_due failed: {e}")


async def _task_compute_daily_metrics():
    global _adr_pcr_ran_today
    today = _ist_now().date()
    if _adr_pcr_ran_today == today:
        return
    try:
        import adr_pcr
        with _conn() as conn:
            result = adr_pcr.compute_and_store(conn)
        log.info(f"adr_pcr: {result}")
        _adr_pcr_ran_today = today
    except Exception as e:
        log.error(f"adr_pcr failed: {e}")


async def _task_load_earnings_daily():
    global _earnings_ran_today
    today = _ist_now().date()
    if _earnings_ran_today == today:
        return
    try:
        import earnings_loader
        with _conn() as conn:
            result = earnings_loader.load_from_screener(conn)
        log.info(f"earnings_load: {result}")
        _earnings_ran_today = today
    except Exception as e:
        log.warning(f"earnings_load failed: {e}")


async def _task_sync_futures_universe():
    global _fu_sync_ran_this_week
    today = _ist_now().date()
    if _fu_sync_ran_this_week == today:
        return
    try:
        import fyers_sync
        with _conn() as conn:
            result = fyers_sync.sync_futures_universe(conn)
        log.info(f"fu_sync: {result}")
        _fu_sync_ran_this_week = today
    except Exception as e:
        log.error(f"fu_sync failed: {e}")


# ── 5-min market jobs (thread-based for blocking DB work) ────────────────────

def _bg_signal_writer():
    global _signal_writer_running
    if _signal_writer_running:
        return
    _signal_writer_running = True
    try:
        import v8_signal_writer
        with _conn() as conn:
            result = v8_signal_writer.run_live_signal_writer(conn)
        log.info(f"signal_writer: {result.get('updated',0)} updated "
                 f"no_bar={result.get('no_bar',0)}")
    except Exception as e:
        log.error(f"signal_writer failed: {e}")
    finally:
        _signal_writer_running = False


def _bg_v10_tick():
    global _v10_running
    if _v10_running:
        return
    _v10_running = True
    try:
        import v10_st_ema
        with _conn() as conn:
            v10_st_ema.tick(conn)
    except Exception as e:
        log.error(f"v10_tick failed: {e}")
    finally:
        _v10_running = False


def _bg_pcr_intraday():
    global _pcr_intraday_running
    if _pcr_intraday_running:
        return
    _pcr_intraday_running = True
    try:
        import pcr_intraday
        with _conn() as conn:
            res = pcr_intraday.compute_pcr_intraday(conn=conn)
        log.info(f"pcr_intraday: {res.get('computed', res.get('bars'))}")
    except Exception as e:
        log.error(f"pcr_intraday failed: {e}")
    finally:
        _pcr_intraday_running = False


def _bg_intraday_paper():
    """DEACTIVATED 18-Jun-2026. On-demand only via POST /api/intraday/tick.
    refresh_tc_cache (420 computes/5-min) was too heavy for auto-scheduler."""
    global _intraday_paper_running
    if _intraday_paper_running:
        return
    _intraday_paper_running = True
    try:
        import tc_intraday
        rc = tc_intraday.refresh_tc_cache()
        en = tc_intraday.run_intraday_paper_entry()
        ex = tc_intraday.run_intraday_paper_exit()
        log.info(f"intraday_paper: cache={rc.get('written')} "
                 f"entered={en.get('entered')} exited={ex.get('exited')} "
                 f"squareoff={ex.get('square_off')}")
    except Exception as e:
        log.error(f"intraday_paper failed: {e}")
    finally:
        _intraday_paper_running = False


def _bg_qb_intraday_mark():
    global _qb_intraday_mark_running
    if _qb_intraday_mark_running:
        return
    _qb_intraday_mark_running = True
    try:
        import qb_eod_checker
        with _conn() as conn:
            res = qb_eod_checker.qb_intraday_mark(conn)
        log.info(f"qb_intraday_mark: {res.get('marked')}/{res.get('symbols')} marked")
    except Exception as e:
        log.error(f"qb_intraday_mark failed: {e}")
    finally:
        _qb_intraday_mark_running = False


# ── main scheduler ────────────────────────────────────────────────────────────

async def _scheduler():
    log.info("Scheduler started")
    _set_heartbeat("sched_started")

    while True:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            log.info("Scheduler cancelled")
            return

        now   = _ist_now()
        today = now.date()
        h, m  = now.hour, now.minute

        # ── 06:00: daily startup jobs ─────────────────────────────────────────
        if h == 6 and m == 0:
            asyncio.create_task(_task_check_refresh_due())
            asyncio.create_task(_task_fetch_global())

        # ── global intraday 5-min, 06:00–23:30 ───────────────────────────────
        if 6 <= h <= 23 and m % 5 == 0:
            asyncio.create_task(_task_fetch_global_intraday())

        # ── 09:00: earnings calendar load ────────────────────────────────────
        if h == 9 and m == 0:
            asyncio.create_task(_task_load_earnings_daily())

        # ── Monday 08:00: futures universe sync ──────────────────────────────
        if now.weekday() == 0 and h == 8 and m == 0:
            asyncio.create_task(_task_sync_futures_universe())

        # ── market hours: every 5-min ─────────────────────────────────────────
        if _is_market_hours(now) and m % 5 == 0:
            asyncio.create_task(asyncio.to_thread(_bg_signal_writer))
            asyncio.create_task(asyncio.to_thread(_bg_v10_tick))
            asyncio.create_task(asyncio.to_thread(_bg_pcr_intraday))
            # asyncio.create_task(asyncio.to_thread(_bg_intraday_paper))  # INACTIVE 18-Jun-2026 — on-demand only via /api/intraday/tick

            if m % 15 == 0:
                asyncio.create_task(asyncio.to_thread(_bg_qb_intraday_mark))

        # ── 15:45: V8 EOD ────────────────────────────────────────────────────
        if h == 15 and m == 45:
            asyncio.create_task(_task_run_v8_engine())

        # ── 15:50: ADR + PCR ─────────────────────────────────────────────────
        if h == 15 and m == 50:
            asyncio.create_task(_task_compute_daily_metrics())

        # ── 21:00: Yahoo daily OHLC ───────────────────────────────────────────
        if h == 21 and m == 0:
            asyncio.create_task(_task_update_raw_prices())

        # ── 21:05: QB EOD checker ─────────────────────────────────────────────
        if h == 21 and m == 5:
            asyncio.create_task(_task_qb_eod_checker())

        # ── 22:00: GVM recompute ──────────────────────────────────────────────
        if h == 22 and m == 0:
            asyncio.create_task(_task_recompute_gvm_daily())

        # ── 22:05: paper pivots ───────────────────────────────────────────────
        if h == 22 and m == 5:
            asyncio.create_task(_task_build_paper_pivots())

        # ── heartbeat ─────────────────────────────────────────────────────────
        if m % 30 == 0:
            _set_heartbeat("sched_loop")


async def _live_loop():
    """Supervisor: restarts _scheduler if it crashes."""
    while True:
        try:
            await _scheduler()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"scheduler crashed, restarting in 60s: {e}")
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return


def start_background():
    t = asyncio.create_task(_live_loop())
    _BG_TASKS.add(t); t.add_done_callback(_BG_TASKS.discard)
    _set_heartbeat("sched_started")
    log.info("scheduler.start_background: scheduler task launched")
    return t
