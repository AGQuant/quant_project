"""
Scorr Scheduler — async background task loop (uvicorn lifespan).

All scheduled work runs here. main.py calls `start_scheduler()` on startup
and `stop_scheduler()` on shutdown.

Schedule (IST):
  06:00         Refresh-due check + global indices fetch (every day incl weekends)
  06:00-23:30   Global intraday fetch every 5-min (time-gated)
  Monday 08:00  futures_universe sync from Fyers feed
  Market hours  Every 5-min tick:
                  - v8_signal_writer (19 live metrics + qualified)
                  - V10 ST+EMA tick (append 5m bar, signal, alert)
                  - pcr_intraday (5-min PCR rollup, self-healing)
                  # intraday_paper INACTIVE 18-Jun-2026 — on-demand only
  Every 15-min  qb_intraday_mark
  15:45         V8 EOD engine — sector_week/month frozen (5 EOD metrics)
  15:50         ADR + PCR compute-and-store
  21:00         Yahoo daily OHLC update (raw_prices)
  21:05         QB EOD checker — P&L mark + hard-stop check (4 baskets)
  22:00         GVM recompute (momentum M + sector ratings)
  22:05         Paper pivot levels rebuild (rolling-5-day)

08-Jun-2026 SELF-HEALING: each tick's work is wrapped so any failure in one
  job doesn't break other jobs. CancelledError handled in both _live_loop +
  _scheduler sleeps (commit 84ea7d3).
07-Jun-2026: V10 ST+EMA wired — every 5-min during market hours.
  Logs changes to session_log. MCP tool: sync_futures_universe.
18-Jun-2026: intraday paper engine DEACTIVATED from auto-run.
  Was refresh_tc_cache (420 computes) + scan + entry/exit every 5-min — too heavy.
  Now on-demand only via POST /api/intraday/tick.
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta, date
from typing import Optional

import v8_engine
import v8_signal_writer
import gvm_nightly
import qb_eod_checker
import refresh_takeaways as rt
import v10_st_ema
import pcr_intraday
import tc_intraday

log = logging.getLogger("scorr.scheduler")

DATABASE_URL = os.getenv("DATABASE_URL", "")

_scheduler_task: Optional[asyncio.Task] = None
_stop_event = asyncio.Event()

# ── per-job run-guards (prevent overlap) ─────────────────────────────────────
_signal_writer_running:     bool           = False
_eod_ran_today:             Optional[date] = None
_adr_pcr_ran_today:         Optional[date] = None
_yahoo_ran_today:           Optional[date] = None
_gvm_ran_today:             Optional[date] = None
_pivots_ran_today:          Optional[date] = None
_qb_eod_ran_today:          Optional[date] = None
_qb_eod_running:            bool           = False
_qb_intraday_mark_running:  bool           = False
_daily_metrics_ran_today:   Optional[date] = None
_global_fetching:           bool           = False
_global_intraday_fetching:  bool           = False
_fu_sync_ran_this_week:     Optional[date] = None
_v10_running:               bool           = False
_pcr_intraday_running:      bool           = False
_intraday_paper_running:    bool           = False  # kept for on-demand use


def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _conn():
    import psycopg
    return psycopg.connect(DATABASE_URL)


def _is_market_hours(now: datetime) -> bool:
    return (
        now.weekday() < 5
        and (now.hour, now.minute) >= (9, 15)
        and (now.hour, now.minute) <= (15, 30)
    )


# ── background job wrappers ───────────────────────────────────────────────────

def _bg_signal_writer():
    global _signal_writer_running
    if _signal_writer_running:
        return
    _signal_writer_running = True
    try:
        with _conn() as conn:
            v8_signal_writer.run_live_signal_writer(conn)
    except Exception as e:
        log.error(f"signal_writer failed: {e}")
    finally:
        _signal_writer_running = False


def _bg_fetch_global():
    global _global_fetching
    if _global_fetching:
        return
    _global_fetching = True
    try:
        import global_indices
        with global_indices.get_conn_from_env() as conn:
            asyncio.run(global_indices.fetch_global_indices(conn))
    except Exception as e:
        log.error(f"global_fetch failed: {e}")
    finally:
        _global_fetching = False


async def _task_fetch_global():
    _n = _ist_now()
    if _n.hour >= 6:
        asyncio.create_task(asyncio.to_thread(_bg_fetch_global))


def _bg_fetch_global_intraday():
    global _global_intraday_fetching
    if _global_intraday_fetching:
        return
    _global_intraday_fetching = True
    try:
        import global_indices
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


def _bg_v10_tick():
    """V10 ST+EMA tick: append 5m bar, compute signal, alert on BUY/SELL.
    Isolated/advisory; reads intraday_prices (1m) + nifty_5m_test_data only."""
    global _v10_running
    if _v10_running:
        return
    _v10_running = True
    try:
        with _conn() as conn:
            v10_st_ema.tick(conn)
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


# INACTIVE 18-Jun-2026 — kept for reference + on-demand /api/intraday/tick
def _bg_intraday_paper():
    """Intraday paper engine (18-Jun-2026): refresh tc_cache, scan both sides,
    auto-enter every fresh match (no cap, +/-1.5%), then check exits + 15:15 square-off.
    Context-isolated (tc_intraday_* tables) — never mixes with v8_paper.
    INACTIVE from auto-scheduler. On-demand only via POST /api/intraday/tick."""
    global _intraday_paper_running
    if _intraday_paper_running:
        return
    _intraday_paper_running = True
    try:
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
        with _conn() as conn:
            res = qb_eod_checker.qb_intraday_mark(conn)
        log.info(f"qb_intraday_mark: {res.get('marked')}/{res.get('symbols')} marked")
    except Exception as e:
        log.error(f"qb_intraday_mark failed: {e}")
    finally:
        _qb_intraday_mark_running = False


def _bg_qb_eod_checker():
    global _qb_eod_ran_today, _qb_eod_running
    today = _ist_now().date()
    if _qb_eod_ran_today == today or _qb_eod_running:
        return
    _qb_eod_running = True
    try:
        with _conn() as conn:
            res = qb_eod_checker.run_eod_check(conn)
        log.info(f"qb_eod: {res.get('checked')} checked, {res.get('exited')} exited")
        _qb_eod_ran_today = today
    except Exception as e:
        log.error(f"qb_eod_checker failed: {e}")
    finally:
        _qb_eod_running = False


# ── main scheduler loop ───────────────────────────────────────────────────────

async def _live_loop():
    global _eod_ran_today, _adr_pcr_ran_today, _yahoo_ran_today
    global _gvm_ran_today, _pivots_ran_today, _fu_sync_ran_this_week

    log.info("Scorr scheduler started")

    while not _stop_event.is_set():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break

        now = _ist_now()
        today = now.date()
        hour, minute = now.hour, now.minute

        # ── 06:00: refresh-due check + global fetch ──────────────────────────
        if hour == 6 and minute == 0:
            await _task_fetch_global()

        # ── global intraday every 5-min, 06:00-23:30 ─────────────────────────
        if minute % 5 == 0:
            await _task_fetch_global_intraday()

        # ── Monday 08:00: futures universe sync ──────────────────────────────
        if now.weekday() == 0 and hour == 8 and minute == 0:
            if _fu_sync_ran_this_week != today:
                try:
                    import fyers_sync
                    with _conn() as conn:
                        fyers_sync.sync_futures_universe(conn)
                    _fu_sync_ran_this_week = today
                except Exception as e:
                    log.error(f"fu_sync failed: {e}")

        # ── market hours: every 5-min block ──────────────────────────────────
        if _is_market_hours(now) and minute % 5 == 0:
            asyncio.create_task(asyncio.to_thread(_bg_signal_writer))
            asyncio.create_task(asyncio.to_thread(_bg_v10_tick))
            asyncio.create_task(asyncio.to_thread(_bg_pcr_intraday))
            # asyncio.create_task(asyncio.to_thread(_bg_intraday_paper))  # INACTIVE 18-Jun-2026 — on-demand only via /api/intraday/tick

            # 15-min boundary by wall clock
            if minute % 15 == 0:
                asyncio.create_task(asyncio.to_thread(_bg_qb_intraday_mark))

        # Always heartbeat the loop itself so a stall is detectable
        if minute % 30 == 0:
            log.debug(f"scheduler heartbeat {now.strftime('%H:%M')} IST")

        # ── 15:45: V8 EOD engine ──────────────────────────────────────────────
        if hour == 15 and minute == 45 and _eod_ran_today != today:
            try:
                with _conn() as conn:
                    v8_engine.run_v8_engine(conn)
                _eod_ran_today = today
                log.info("V8 EOD engine done")
            except Exception as e:
                log.error(f"V8 EOD failed: {e}")

        # ── 15:50: ADR + PCR daily ────────────────────────────────────────────
        if hour == 15 and minute == 50 and _adr_pcr_ran_today != today:
            try:
                import adr_pcr
                with _conn() as conn:
                    adr_pcr.compute_and_store(conn)
                _adr_pcr_ran_today = today
            except Exception as e:
                log.error(f"adr_pcr failed: {e}")

        # ── 21:00: Yahoo daily OHLC ───────────────────────────────────────────
        if hour == 21 and minute == 0 and _yahoo_ran_today != today:
            try:
                import yahoo_daily
                with _conn() as conn:
                    yahoo_daily.run_daily_update(conn)
                _yahoo_ran_today = today
                log.info("Yahoo daily OHLC done")
            except Exception as e:
                log.error(f"yahoo_daily failed: {e}")

        # ── 21:05: QB EOD checker ─────────────────────────────────────────────
        if hour == 21 and minute == 5:
            asyncio.create_task(asyncio.to_thread(_bg_qb_eod_checker))

        # ── 22:00: GVM recompute ──────────────────────────────────────────────
        if hour == 22 and minute == 0 and _gvm_ran_today != today:
            try:
                with _conn() as conn:
                    gvm_nightly.gvm_recompute(conn)
                _gvm_ran_today = today
                log.info("GVM recompute done")
            except Exception as e:
                log.error(f"gvm_recompute failed: {e}")

        # ── 22:05: paper pivot rebuild ────────────────────────────────────────
        if hour == 22 and minute == 5 and _pivots_ran_today != today:
            try:
                import v8_paper
                with _conn() as conn:
                    v8_paper.rebuild_pivots(conn)
                _pivots_ran_today = today
                log.info("Paper pivots rebuilt")
            except Exception as e:
                log.error(f"pivot rebuild failed: {e}")


async def start_scheduler():
    global _scheduler_task
    _stop_event.clear()
    _scheduler_task = asyncio.create_task(_live_loop())
    log.info("Scheduler task created")


async def stop_scheduler():
    global _scheduler_task
    _stop_event.set()
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    log.info("Scheduler stopped")
