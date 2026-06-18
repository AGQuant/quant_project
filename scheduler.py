"""
Scheduler — Scorr background tasks (restored 18-Jun-2026).
Deactivation: _bg_intraday_paper commented out — on-demand only via /api/intraday/tick.
"""
import asyncio, logging, os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import psycopg

log = logging.getLogger("scorr.scheduler")
DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

_stop_event: Optional[asyncio.Event] = None
_bg_tasks: set = set()

def _ist_now():
    return datetime.now(IST).replace(tzinfo=None)

def _conn():
    return psycopg.connect(DATABASE_URL)

def _is_market_hours(now):
    if now.weekday() >= 5: return False
    t = (now.hour, now.minute)
    return (9,15) <= t <= (15,30)

# ── run-guards ────────────────────────────────────────────────────────────────
_signal_writer_running = False
_eod_running = False
_eod_ran_today: Optional[date] = None
_adr_pcr_ran_today: Optional[date] = None
_yahoo_ran_today: Optional[date] = None
_yahoo_daily_running = False
_gvm_ran_today: Optional[date] = None
_pivots_ran_today: Optional[date] = None
_qb_eod_ran_today: Optional[date] = None
_qb_eod_running = False
_qb_intraday_mark_running = False
_global_fetching = False
_global_intraday_fetching = False
_v10_running = False
_pcr_intraday_running = False
_intraday_paper_running = False
_fu_sync_ran_this_week: Optional[date] = None

# ── exported to main.py ───────────────────────────────────────────────────────

def _compute_and_store_adr(conn=None):
    close_conn = conn is None
    if conn is None: conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH li AS (
                    SELECT DISTINCT ON (symbol) symbol, close AS cmp
                    FROM intraday_prices WHERE ts::date = CURRENT_DATE
                    ORDER BY symbol, ts DESC
                ),
                pc AS (
                    SELECT DISTINCT ON (symbol) symbol, close AS pclose
                    FROM raw_prices WHERE price_date < CURRENT_DATE
                    ORDER BY symbol, price_date DESC
                ),
                counts AS (
                    SELECT
                        COUNT(*) FILTER (WHERE li.cmp > pc.pclose) AS advances,
                        COUNT(*) FILTER (WHERE li.cmp < pc.pclose) AS declines,
                        COUNT(*) FILTER (WHERE li.cmp = pc.pclose) AS unchanged,
                        COUNT(*) AS total
                    FROM li JOIN pc ON pc.symbol = li.symbol
                )
                INSERT INTO adr_daily (price_date, advances, declines, unchanged, adr, universe_count)
                SELECT CURRENT_DATE, advances, declines, unchanged,
                    CASE WHEN declines>0 THEN ROUND(advances::numeric/declines,3) ELSE advances END,
                    total
                FROM counts
                ON CONFLICT (price_date) DO UPDATE SET
                    advances=EXCLUDED.advances, declines=EXCLUDED.declines,
                    unchanged=EXCLUDED.unchanged, adr=EXCLUDED.adr,
                    universe_count=EXCLUDED.universe_count
            """)
            conn.commit()
        log.info("ADR computed and stored")
        return {"ok": True}
    except Exception as e:
        log.error(f"_compute_and_store_adr: {e}"); return {"ok": False, "error": str(e)}
    finally:
        if close_conn: conn.close()


def _compute_and_store_pcr(conn=None):
    close_conn = conn is None
    if conn is None: conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pcr_daily (price_date, symbol, pcr_total, pcr_atm5, computed_at)
                SELECT CURRENT_DATE, symbol,
                    ROUND(SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END)::numeric /
                          NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END),0),3),
                    ROUND(SUM(CASE WHEN option_type='PE' AND atm_distance<=5 THEN oi ELSE 0 END)::numeric /
                          NULLIF(SUM(CASE WHEN option_type='CE' AND atm_distance<=5 THEN oi ELSE 0 END),0),3),
                    NOW()
                FROM option_chain WHERE ts::date = CURRENT_DATE
                GROUP BY symbol
                ON CONFLICT (price_date, symbol) DO UPDATE SET
                    pcr_total=EXCLUDED.pcr_total, pcr_atm5=EXCLUDED.pcr_atm5, computed_at=NOW()
            """)
            conn.commit()
        log.info("PCR computed and stored")
        return {"ok": True}
    except Exception as e:
        log.error(f"_compute_and_store_pcr: {e}"); return {"ok": False, "error": str(e)}
    finally:
        if close_conn: conn.close()


async def _bg_yahoo_daily(app=None):
    global _yahoo_daily_running, _yahoo_ran_today
    if _yahoo_daily_running: return {"status": "already_running"}
    _yahoo_daily_running = True
    try:
        import yahoo_daily_update as ydu
        with _conn() as conn:
            result = ydu.run_update(conn)
        _yahoo_ran_today = _ist_now().date()
        log.info(f"yahoo_daily: {result}")
        return result
    except Exception as e:
        log.error(f"_bg_yahoo_daily: {e}"); return {"error": str(e)}
    finally:
        _yahoo_daily_running = False

# ── job wrappers ──────────────────────────────────────────────────────────────

def _bg_signal_writer():
    global _signal_writer_running
    if _signal_writer_running: return
    _signal_writer_running = True
    try:
        import v8_signal_writer
        with _conn() as conn:
            r = v8_signal_writer.run_live_signal_writer(conn)
        log.info(f"signal_writer: {r.get('updated',0)} updated")
    except Exception as e: log.error(f"signal_writer: {e}")
    finally: _signal_writer_running = False

def _bg_v10_tick():
    global _v10_running
    if _v10_running: return
    _v10_running = True
    try:
        import v10_st_ema
        with _conn() as conn: v10_st_ema.tick(conn)
    except Exception as e: log.error(f"v10_tick: {e}")
    finally: _v10_running = False

def _bg_pcr_intraday():
    global _pcr_intraday_running
    if _pcr_intraday_running: return
    _pcr_intraday_running = True
    try:
        import pcr_intraday
        with _conn() as conn: res = pcr_intraday.compute_pcr_intraday(conn=conn)
        log.info(f"pcr_intraday: {res.get('computed', res.get('bars'))}")
    except Exception as e: log.error(f"pcr_intraday: {e}")
    finally: _pcr_intraday_running = False

def _bg_intraday_paper():
    """DEACTIVATED 18-Jun-2026. On-demand via /api/intraday/tick only."""
    global _intraday_paper_running
    if _intraday_paper_running: return
    _intraday_paper_running = True
    try:
        import tc_intraday
        rc = tc_intraday.refresh_tc_cache()
        en = tc_intraday.run_intraday_paper_entry()
        ex = tc_intraday.run_intraday_paper_exit()
        log.info(f"intraday_paper: cache={rc.get('written')} entered={en.get('entered')}")
    except Exception as e: log.error(f"intraday_paper: {e}")
    finally: _intraday_paper_running = False

def _bg_qb_intraday_mark():
    global _qb_intraday_mark_running
    if _qb_intraday_mark_running: return
    _qb_intraday_mark_running = True
    try:
        import qb_eod_checker
        with _conn() as conn: res = qb_eod_checker.qb_intraday_mark(conn)
        log.info(f"qb_intraday_mark: {res.get('marked')}/{res.get('symbols')}")
    except Exception as e: log.error(f"qb_intraday_mark: {e}")
    finally: _qb_intraday_mark_running = False

def _bg_v8_eod():
    global _eod_running, _eod_ran_today
    today = _ist_now().date()
    if _eod_ran_today == today or _eod_running: return
    _eod_running = True
    try:
        import v8_engine
        with _conn() as conn: result = v8_engine.run_v8_engine(conn)
        log.info(f"v8_eod: {result.get('symbols_processed')} syms")
        _eod_ran_today = today
    except Exception as e: log.error(f"v8_eod: {e}")
    finally: _eod_running = False

def _bg_adr_pcr():
    global _adr_pcr_ran_today
    today = _ist_now().date()
    if _adr_pcr_ran_today == today: return
    try:
        with _conn() as conn:
            _compute_and_store_adr(conn)
            _compute_and_store_pcr(conn)
        _adr_pcr_ran_today = today
        log.info("adr_pcr done")
    except Exception as e: log.error(f"adr_pcr: {e}")

def _bg_yahoo_daily_sync():
    global _yahoo_daily_running, _yahoo_ran_today
    if _yahoo_daily_running: return
    _yahoo_daily_running = True
    try:
        import yahoo_daily_update as ydu
        with _conn() as conn: result = ydu.run_update(conn)
        _yahoo_ran_today = _ist_now().date()
        log.info(f"yahoo_daily: {result}")
    except Exception as e: log.error(f"yahoo_daily: {e}")
    finally: _yahoo_daily_running = False

def _bg_gvm():
    global _gvm_ran_today
    today = _ist_now().date()
    if _gvm_ran_today == today: return
    try:
        import gvm_nightly
        with _conn() as conn: gvm_nightly.gvm_recompute(conn)
        _gvm_ran_today = today
        log.info("gvm_recompute done")
    except Exception as e: log.error(f"gvm: {e}")

def _bg_pivots():
    global _pivots_ran_today
    today = _ist_now().date()
    if _pivots_ran_today == today: return
    try:
        import v8_paper
        with _conn() as conn: v8_paper.rebuild_pivots(conn)
        _pivots_ran_today = today
        log.info("pivots rebuilt")
    except Exception as e: log.error(f"pivots: {e}")

def _bg_qb_eod():
    global _qb_eod_ran_today, _qb_eod_running
    today = _ist_now().date()
    if _qb_eod_ran_today == today or _qb_eod_running: return
    _qb_eod_running = True
    try:
        import qb_eod_checker
        with _conn() as conn: result = qb_eod_checker.run_eod_check(conn)
        log.info(f"qb_eod: {result.get('checked')} checked")
        _qb_eod_ran_today = today
    except Exception as e: log.error(f"qb_eod: {e}")
    finally: _qb_eod_running = False

def _bg_fu_sync():
    global _fu_sync_ran_this_week
    today = _ist_now().date()
    if _fu_sync_ran_this_week == today: return
    try:
        import fyers_sync
        with _conn() as conn: fyers_sync.sync_futures_universe(conn)
        _fu_sync_ran_this_week = today
        log.info("fu_sync done")
    except Exception as e: log.error(f"fu_sync: {e}")

def _bg_fetch_global():
    global _global_fetching
    if _global_fetching: return
    _global_fetching = True
    try:
        import global_indices
        with global_indices.get_conn_from_env() as conn:
            asyncio.run(global_indices.fetch_global_indices(conn))
    except Exception as e: log.error(f"global_fetch: {e}")
    finally: _global_fetching = False

def _bg_fetch_global_intraday():
    global _global_intraday_fetching
    if _global_intraday_fetching: return
    _global_intraday_fetching = True
    try:
        import global_indices
        with global_indices.get_conn_from_env() as conn:
            res = asyncio.run(global_indices.fetch_global_intraday(conn))
            try: global_indices.prune_global_intraday(conn, days=7)
            except Exception: pass
        log.debug(f"global_intraday: {res.get('stored')} bars")
    except Exception as e: log.debug(f"global_intraday: {e}")
    finally: _global_intraday_fetching = False

# ── main loop ─────────────────────────────────────────────────────────────────

async def _scheduler_loop():
    log.info("Scheduler loop started")
    while not _stop_event.is_set():
        try: await asyncio.sleep(60)
        except asyncio.CancelledError: break
        now = _ist_now(); today = now.date(); h, m = now.hour, now.minute
        if h == 6 and m == 0:
            asyncio.create_task(asyncio.to_thread(_bg_fetch_global))
        if 6 <= h <= 23 and m % 5 == 0:
            asyncio.create_task(asyncio.to_thread(_bg_fetch_global_intraday))
        if now.weekday() == 0 and h == 8 and m == 0:
            asyncio.create_task(asyncio.to_thread(_bg_fu_sync))
        if _is_market_hours(now) and m % 5 == 0:
            asyncio.create_task(asyncio.to_thread(_bg_signal_writer))
            asyncio.create_task(asyncio.to_thread(_bg_v10_tick))
            asyncio.create_task(asyncio.to_thread(_bg_pcr_intraday))
            # asyncio.create_task(asyncio.to_thread(_bg_intraday_paper))  # INACTIVE 18-Jun-2026 — on-demand only via /api/intraday/tick
            if m % 15 == 0:
                asyncio.create_task(asyncio.to_thread(_bg_qb_intraday_mark))
        if h == 15 and m == 45: asyncio.create_task(asyncio.to_thread(_bg_v8_eod))
        if h == 15 and m == 50: asyncio.create_task(asyncio.to_thread(_bg_adr_pcr))
        if h == 21 and m == 0:  asyncio.create_task(asyncio.to_thread(_bg_yahoo_daily_sync))
        if h == 21 and m == 5:  asyncio.create_task(asyncio.to_thread(_bg_qb_eod))
        if h == 22 and m == 0:  asyncio.create_task(asyncio.to_thread(_bg_gvm))
        if h == 22 and m == 5:  asyncio.create_task(asyncio.to_thread(_bg_pivots))


async def _supervisor():
    while not _stop_event.is_set():
        try: await _scheduler_loop()
        except asyncio.CancelledError: return
        except Exception as e:
            log.error(f"scheduler crashed, restarting: {e}")
            try: await asyncio.sleep(60)
            except asyncio.CancelledError: return


def start_background(app=None, base_url: str = "", admin_token: str = ""):
    """Called from main.py startup."""
    global _stop_event
    _stop_event = asyncio.Event()
    t = asyncio.create_task(_supervisor())
    _bg_tasks.add(t); t.add_done_callback(_bg_tasks.discard)
    log.info("scheduler.start_background: launched")
    return t


async def stop_background():
    if _stop_event: _stop_event.set()
    for t in list(_bg_tasks): t.cancel()
    log.info("scheduler stopped")
