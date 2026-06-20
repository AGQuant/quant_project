"""
Scheduler — Scorr background tasks (restored 18-Jun-2026).
Deactivation: _bg_intraday_paper commented out — on-demand only via /api/intraday/tick.
"""
import asyncio, logging, os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import psycopg
from psycopg.types.json import Json

log = logging.getLogger("scorr.scheduler")
DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

_stop_event: Optional[asyncio.Event] = None
_bg_tasks: set = set()

# Dedicated, bounded pool for blocking jobs — isolated from the default loop
# executor (which FastAPI's sync request handlers also use) so a request burst
# can't starve the scheduler, and bounded so a hung job can't be masked by an
# unbounded thread spawn. Root cause of the 19-Jun stall: blocked jobs piled up
# on the small shared default pool until every to_thread dispatch silently
# queued forever — so the watchdog's "restart" never got a worker to run on.
_EXECUTOR = ThreadPoolExecutor(max_workers=12, thread_name_prefix="sched-job")

def _ist_now():
    return datetime.now(IST).replace(tzinfo=None)

# Socket-level safety applied to EVERY connection: a connect/socket that hangs in
# a worker thread is uncancellable and silently holds the worker forever (root
# cause of the 19-Jun stall). connect_timeout caps the TCP handshake (also guards
# the watchdog, which connects on the event loop); keepalives detect dead sockets.
# These never affect query logic, so they're safe for the heavy nightly jobs too.
_BASE_CONNECT_KW = dict(
    connect_timeout=10,
    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
)

def _conn(statement_timeout_ms: int = 0):
    """statement_timeout_ms>0 also caps any single query + idle-in-transaction —
    used by the live 5-min path + watchdog so a DB lock or runaway query can't pin
    a worker thread. Left OFF (0) for nightly batch jobs (EOD/GVM/yahoo/pivots),
    which legitimately compute in Python between statements within one txn."""
    kw = dict(_BASE_CONNECT_KW)
    if statement_timeout_ms > 0:
        kw["options"] = (f"-c statement_timeout={statement_timeout_ms} "
                         f"-c idle_in_transaction_session_timeout={statement_timeout_ms}")
    return psycopg.connect(DATABASE_URL, **kw)

def _spawn(fn, *args):
    """Dispatch a blocking job on the dedicated scheduler pool (never the shared
    default executor) and keep a reference so the task isn't GC'd mid-flight."""
    loop = asyncio.get_running_loop()
    t = asyncio.ensure_future(loop.run_in_executor(_EXECUTOR, fn, *args))
    _bg_tasks.add(t); t.add_done_callback(_bg_tasks.discard)
    return t

def _is_market_hours(now):
    if now.weekday() >= 5: return False
    t = (now.hour, now.minute)
    return (9,15) <= t <= (15,30)

# ── run-guards ────────────────────────────────────────────────────────────────
# signal_writer uses a started_at timestamp + token (not a bare bool) so a hung
# tick can't permanently block future ticks — see _bg_signal_writer / _check_watchdog.
_signal_writer_started_at: Optional[datetime] = None
_signal_writer_token = 0
_signal_writer_fail_streak = 0
_last_signal_writer_ok: Optional[datetime] = None
SIGNAL_WRITER_TIMEOUT = timedelta(minutes=4)   # spec #16: assume hung after ~4 min
WATCHDOG_STALE_MIN = 10                          # spec #16: stale if no tick in 10 min
WATCHDOG_RESTART_COOLDOWN = timedelta(minutes=5) # cap restart attempts (avoid storm if
                                                 # the writer itself, not the loop, is broken)
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

# ── health / watchdog state ────────────────────────────────────────────────────
_restart_requested = False
_watchdog_restarts = 0
_last_restart_ts: Optional[datetime] = None
_watchdog_alerted = False        # throttle: one alert per stall episode

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

# ── health / watchdog helpers (spec #16) ───────────────────────────────────────

def _log_alert(kind: str, message: str):
    """Write a visible alert to session_log (category=alert)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO session_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'alert', %s, %s)""",
                        (kind, Json({"message": message, "ist": _ist_now().isoformat()})))
            conn.commit()
        log.error(f"ALERT[{kind}]: {message}")
    except Exception as e:
        log.error(f"_log_alert failed ({kind}): {e}")


def _reset_guards():
    """Clear stuck run-guards so the next tick can proceed cleanly."""
    global _signal_writer_started_at, _signal_writer_token
    _signal_writer_started_at = None
    _signal_writer_token += 1   # invalidate any in-flight (hung) tick's finally


def _request_restart(reason: str):
    """Reset guards and ask the scheduler loop to restart fresh (via supervisor)."""
    global _restart_requested, _watchdog_restarts, _last_restart_ts
    _reset_guards()
    _restart_requested = True
    _watchdog_restarts += 1
    _last_restart_ts = _ist_now()
    log.error(f"scheduler RESTART requested: {reason}")


def _tick_age_minutes() -> Optional[float]:
    """Minutes since the most recent v8_metrics computed_at (DB-clock consistent)."""
    try:
        # tight cap: this runs on the event loop, so it must never block it for long.
        with _conn(statement_timeout_ms=8000) as conn, conn.cursor() as cur:
            cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - MAX(computed_at)))/60.0 FROM v8_metrics")
            r = cur.fetchone()
        return float(r[0]) if r and r[0] is not None else None
    except Exception as e:
        log.error(f"_tick_age_minutes failed: {e}")
        return None


def _check_watchdog(now):
    """During market hours, if no signal tick landed in WATCHDOG_STALE_MIN, the
    writer has silently stalled — alert, reset guards, request restart."""
    global _watchdog_alerted
    if not _is_market_hours(now):
        _watchdog_alerted = False
        return
    age = _tick_age_minutes()
    if age is None:
        return
    if age > WATCHDOG_STALE_MIN:
        if not _watchdog_alerted:
            _log_alert("scheduler_stall",
                       f"v8_metrics stale {age:.1f} min during market hours — restarting live loop")
            _watchdog_alerted = True
        # cooldown: if a restart didn't help (writer-logic bug, not a hung loop),
        # don't hammer — retry at most once per cooldown window.
        if _last_restart_ts is None or (now - _last_restart_ts) >= WATCHDOG_RESTART_COOLDOWN:
            _request_restart(f"watchdog: tick stale {age:.1f} min")
    else:
        _watchdog_alerted = False


def health_state() -> dict:
    """Snapshot for /api/health/scheduler."""
    return {
        "fail_streak": _signal_writer_fail_streak,
        "watchdog_restarts": _watchdog_restarts,
        "last_restart_ts": _last_restart_ts.isoformat() if _last_restart_ts else None,
        "last_signal_writer_ok": _last_signal_writer_ok.isoformat() if _last_signal_writer_ok else None,
        "tick_in_progress": _signal_writer_started_at is not None,
    }


# ── job wrappers ──────────────────────────────────────────────────────────────

def _bg_signal_writer():
    global _signal_writer_started_at, _signal_writer_token
    global _signal_writer_fail_streak, _last_signal_writer_ok
    now = _ist_now()
    # guard: skip only if a tick is genuinely in flight and within the timeout window
    if _signal_writer_started_at is not None:
        if (now - _signal_writer_started_at) < SIGNAL_WRITER_TIMEOUT:
            return
        # exceeded timeout → previous tick is hung; abandon it and start fresh
        _log_alert("signal_writer_timeout",
                   f"previous tick hung >{SIGNAL_WRITER_TIMEOUT} (since {_signal_writer_started_at}) — starting fresh")
        _signal_writer_token += 1   # invalidate the hung tick's finally

    my_token = _signal_writer_token + 1
    _signal_writer_token = my_token
    _signal_writer_started_at = now
    try:
        import v8_signal_writer
        # 90s query cap: writer issues many small fast statements, so a 90s ceiling
        # only ever trips on a genuine lock/hang — exactly what must not pin a thread.
        with _conn(statement_timeout_ms=90000) as conn:
            r = v8_signal_writer.run_live_signal_writer(conn)
        if isinstance(r, dict) and r.get("error"):
            raise RuntimeError(r["error"])
        _signal_writer_fail_streak = 0
        _last_signal_writer_ok = _ist_now()
        # heartbeat (sched_writer_hb) now written inside run_live_signal_writer using
        # the already-open conn — covers scheduler + MCP + API paths (task #18).
        log.info(f"signal_writer: {r.get('updated', 0) if isinstance(r, dict) else 0} updated")
    except Exception as e:
        _signal_writer_fail_streak += 1
        log.error(f"signal_writer: FAIL #{_signal_writer_fail_streak}: {e}")
        if _signal_writer_fail_streak >= 3:
            _request_restart(f"signal_writer 3 consecutive failures: {e}")
    finally:
        if _signal_writer_token == my_token:   # only the latest run clears the marker
            _signal_writer_started_at = None

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
    global _restart_requested
    log.info("Scheduler loop started")
    while not _stop_event.is_set():
        try: await asyncio.sleep(60)
        except asyncio.CancelledError: break
        now = _ist_now(); today = now.date(); h, m = now.hour, now.minute
        # watchdog: detect a silently-stalled signal writer and recover.
        # Called directly (NOT via to_thread): the check is light + thread-safe, and
        # offloading it can block when the thread pool is saturated during market hours.
        try:
            _check_watchdog(now)
        except Exception as e:
            log.error(f"watchdog check error: {e}")
        if _restart_requested:
            _restart_requested = False
            # guards already reset by _request_restart — fire an immediate recovery
            # tick (don't wait up to 5 min for the next m%5 dispatch) and keep looping.
            log.warning("scheduler: guards reset by watchdog/fail-streak — firing recovery tick")
            if _is_market_hours(now):
                _spawn(_bg_signal_writer)
        if h == 6 and m == 0:
            _spawn(_bg_fetch_global)
        if 6 <= h <= 23 and m % 5 == 0:
            _spawn(_bg_fetch_global_intraday)
        if now.weekday() == 0 and h == 8 and m == 0:
            _spawn(_bg_fu_sync)
        if _is_market_hours(now) and m % 5 == 0:
            _spawn(_bg_signal_writer)
            _spawn(_bg_v10_tick)
            _spawn(_bg_pcr_intraday)
            # _spawn(_bg_intraday_paper)  # INACTIVE 18-Jun-2026 — on-demand only via /api/intraday/tick
            if m % 15 == 0:
                _spawn(_bg_qb_intraday_mark)
        if h == 15 and m == 45: _spawn(_bg_v8_eod)
        if h == 15 and m == 50: _spawn(_bg_adr_pcr)
        # Nightly batch shifted to 01:00–01:45 IST (task #31). The old 21:00–22:05
        # window collided with CC deploy pushes — a Railway redeploy kills the
        # scheduler mid-job (caused the 18-Jun raw_prices gap). 1 AM = no-push window.
        # 15-min spacing gives each job runway; order preserves deps (prices→QB→GVM→pivots).
        if h == 1 and m == 0:   _spawn(_bg_yahoo_daily_sync)
        if h == 1 and m == 15:  _spawn(_bg_qb_eod)
        if h == 1 and m == 30:  _spawn(_bg_gvm)
        if h == 1 and m == 45:  _spawn(_bg_pivots)


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
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)
    log.info("scheduler stopped")
