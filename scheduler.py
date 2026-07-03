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

# ── run-guards ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
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
_ut_ran_today: Optional[date] = None   # cc#154: universe_technicals nightly guard
_qb_eod_running = False
_qb_intraday_mark_running = False
_global_fetching = False
_global_intraday_fetching = False
_v10_running = False
_pcr_intraday_running = False
_intraday_paper_running = False
_tc_lite_running = False                          # cc_task #77: TC Lite screener pass guard
_smartgain_mtm_running = False                    # cc#123: SmartGain live MTM refresh guard
_fu_sync_ran_this_week: Optional[date] = None
_v8_paper_exit_running = False                   # cc_task #72 bug_0: live exit pass guard
_v8_paper_exit_eod_ran: Optional[date] = None    # cc_task #72 bug_0: EOD fallback day-lock
_premarket_check_ran: Optional[date] = None      # cc_task #72 bug_1: 09:10 check day-lock

# ── health / watchdog state ─────────────────────────────────────────────────────────────────────────────────────────────────────────
_restart_requested = False
_watchdog_restarts = 0
_last_restart_ts: Optional[datetime] = None
_watchdog_alerted = False        # throttle: one alert per stall episode

# ── exported to main.py ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

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
                INSERT INTO adr_daily (price_date, advances, declines, unchanged, adr)
                SELECT CURRENT_DATE, advances, declines, unchanged,
                    CASE WHEN declines>0 THEN ROUND(advances::numeric/declines,3) ELSE advances END
                FROM counts
                ON CONFLICT (price_date) DO UPDATE SET
                    advances=EXCLUDED.advances, declines=EXCLUDED.declines,
                    unchanged=EXCLUDED.unchanged, adr=EXCLUDED.adr
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
            # cc#121: rewritten to match the real pcr_daily schema
            # (price_date, underlying, put_oi, call_oi, pcr). The old INSERT wrote
            # non-existent columns (symbol/pcr_total/pcr_atm5) AND referenced a
            # non-existent option_chain column (atm_distance) -> every run raised and
            # was swallowed by the except below, so pcr_daily silently froze at Jun 19
            # while ADR (a separate function) kept writing. Mirrors the proven
            # pcr_backfill._recompute_pcr_daily_for_range query: last snapshot of the
            # day per underlying, grouped by underlying so NIFTY and BANKNIFTY are
            # independent rows. HAVING put-OI>0 skips a broken-feed underlying
            # (BANKNIFTY currently reports put_oi=0) so it never writes a bogus pcr=0
            # and never blocks NIFTY.
            cur.execute("""
                INSERT INTO pcr_daily (price_date, underlying, put_oi, call_oi, pcr)
                SELECT DATE(ts), underlying,
                    SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END),
                    SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END),
                    ROUND(SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END)::numeric /
                          NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END),0), 3)
                FROM option_chain
                WHERE DATE(ts) = CURRENT_DATE
                  AND underlying IN ('NIFTY','BANKNIFTY')
                  AND ts = (SELECT MAX(oc2.ts) FROM option_chain oc2
                            WHERE DATE(oc2.ts) = CURRENT_DATE AND oc2.underlying = option_chain.underlying)
                GROUP BY DATE(ts), underlying
                HAVING SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) > 0
                ON CONFLICT (price_date, underlying) DO UPDATE SET
                    put_oi=EXCLUDED.put_oi, call_oi=EXCLUDED.call_oi,
                    pcr=EXCLUDED.pcr, computed_at=NOW()
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
        result = await ydu.run_async()                 # cc_task #72 bug_2: was ydu.run_update(conn) — undefined -> silent AttributeError, nightly EOD never ran
        with _conn() as conn:
            heal = ydu.heal_indices(conn)              # bug_2: verify + self-heal index symbols
        _yahoo_ran_today = _ist_now().date()
        log.info(f"yahoo_daily: {result} | index_heal: {heal}")
        return result
    except Exception as e:
        log.error(f"_bg_yahoo_daily: {e}"); return {"error": str(e)}
    finally:
        _yahoo_daily_running = False

# ── job wrappers ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

# ── health / watchdog helpers (spec #16) ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _log_alert(kind: str, message: str):
    """Write a visible alert to ops_log (category=alert). cc#156: telemetry
    categories moved off session_log to ops_log per MEMORY_TAXONOMY_V1."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
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


# ── job wrappers ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

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


def _premarket_writer_check():
    """cc_task #72 bug_1: 09:10 IST pre-market readiness. The existing _check_watchdog
    only acts DURING market hours, so a live writer that died overnight isn't caught
    until after the 09:15 open (stale metrics -> wrong mood/slots at open). At 09:10,
    if v8_metrics is >60 min stale (or absent), force a fresh restart + fire a recovery
    tick + alert (category=alert, title=scheduler_stall_9am) so the writer is primed."""
    global _premarket_check_ran
    today = _ist_now().date()
    if _premarket_check_ran == today:
        return
    _premarket_check_ran = today
    try:
        age = _tick_age_minutes()
        if age is None or age > 60:
            _log_alert("scheduler_stall_9am",
                       f"09:10 pre-market: v8_metrics stale "
                       f"{f'{age:.0f}min' if age is not None else 'absent'} — forcing live-writer restart before open")
            _request_restart("premarket 09:10 readiness: v8_metrics stale/absent")
            _spawn(_bg_signal_writer)
    except Exception as e:
        log.error(f"_premarket_writer_check: {e}")


def _bg_v8_paper_exit():
    """cc_task #72 bug_0: EXIT-ONLY live pass every 5 min. paper_tick (which holds the
    exit logic) was never scheduled — only entries (signal_writer) ran, so nothing
    closed since 19-Jun. run_paper_exits applies the SAME target/stop rules, no entries."""
    global _v8_paper_exit_running
    if _v8_paper_exit_running: return
    _v8_paper_exit_running = True
    try:
        import v8_paper
        with _conn() as conn:
            res = v8_paper.run_paper_exits(conn, mode="live")
        if res.get("closed"):
            log.info(f"v8_paper_exit live: closed {res['closed']}")
    except Exception as e:
        log.error(f"v8_paper_exit: {e}")
    finally:
        _v8_paper_exit_running = False


def _bg_v8_paper_exit_eod():
    """cc_task #72 bug_0: EOD fall-back. After the nightly EOD load, close any open
    position whose OFFICIAL daily close (raw_prices) breached target/stop but the live
    loop missed (e.g. the writer was down). Uses the latest completed trading day."""
    global _v8_paper_exit_eod_ran
    today = _ist_now().date()
    if _v8_paper_exit_eod_ran == today: return
    try:
        import v8_paper
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(price_date) FROM raw_prices")
                d = cur.fetchone()[0]
            if d is None:
                return
            res = v8_paper.run_paper_exits(conn, target_date=d, mode="eod")
        _v8_paper_exit_eod_ran = today
        if res.get("closed"):
            _log_alert("v8_paper_eod_exit",
                       f"EOD fallback closed {res['closed']} position(s) on {d} daily close")
        log.info(f"v8_paper_exit_eod: {res}")
    except Exception as e:
        log.error(f"v8_paper_exit_eod: {e}")

def _bg_v10_tick():
    global _v10_running
    if _v10_running: return
    _v10_running = True
    try:
        import v10_st_ema
        with _conn() as conn:
            res = v10_st_ema.tick(conn)
            summary = [{"feed": p.get("feed"), "signal": p.get("signal"),
                        "status": p.get("status"), "price": p.get("price"),
                        "events": len(p.get("events", []))}
                       for p in (res.get("paper") or [])]
            with conn.cursor() as cur:
                # cc#156: telemetry categories moved off session_log to ops_log.
                cur.execute(
                    "INSERT INTO ops_log (category, title, details) "
                    "VALUES ('v10_tick_hb', %s, %s)",
                    ("v10 tick", Json({"feeds": summary})))
            conn.commit()
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

def _bg_tc_lite():
    """cc_task #77: TC Lite intraday SCREENER — flag active futures passing the
    5-check LONG/SHORT gate, save one signal per symbol/side/day. Screener only —
    no P&L, no exit, no paper trade. Lightweight SQL on the existing 5-min tick
    (09:30-15:15 IST gate lives inside scan_tc_lite)."""
    global _tc_lite_running
    if _tc_lite_running: return
    _tc_lite_running = True
    try:
        import tc_lite_scanner
        res = tc_lite_scanner.scan_tc_lite()
        if isinstance(res, dict) and (res.get("new_long") or res.get("new_short")):
            log.info(f"tc_lite: +{res.get('new_long')}L +{res.get('new_short')}S")
    except Exception as e:
        log.error(f"tc_lite: {e}")
    finally:
        _tc_lite_running = False

def _bg_smartgain_mtm():
    """cc#123 (P0): refresh smartgain_holdings.ltp/updated_at from the live feed
    every 5 min during market hours. The stored ltp was a manual stamp that froze
    ~24h while the dashboard re-fetched the same stale row and stamped it with the
    wall clock — stale data looking live on a real-money book.

    cc#147 (BUG-1): MHK40 trades FUTURES but this job priced ltp from cmp_prices
    SPOT, drifting from broker MTM by basis*qty. Now FUT-LTP-FIRST: latest
    fyers_fut 5m bar close when fresh (<=10 min), else spot + latest
    futures_basis.basis. Only touches a holding whose chosen source tick is fresh
    (<=10 min IST) so a dead per-symbol feed can never overwrite good data with a
    stale price.

    mtm is a GENERATED ALWAYS column (discovered 02-Jul) — it derives from ltp on
    write, so this job must set ltp only. Pairs with /api/smartgain/m2m."""
    global _smartgain_mtm_running
    if _smartgain_mtm_running: return
    _smartgain_mtm_running = True
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE smartgain_holdings h
                SET ltp = COALESCE(fut.fut_close, cp.cmp + fb.basis, cp.cmp),
                    updated_at = NOW()
                FROM cmp_prices cp
                LEFT JOIN LATERAL (
                    SELECT ip.close AS fut_close
                    FROM intraday_prices ip
                    WHERE ip.symbol = cp.symbol AND ip.source = 'fyers_fut'
                      AND ip.ts >= (NOW() AT TIME ZONE 'Asia/Kolkata') - INTERVAL '10 minutes'
                    ORDER BY ip.ts DESC LIMIT 1
                ) fut ON true
                LEFT JOIN LATERAL (
                    SELECT fb2.basis
                    FROM futures_basis fb2
                    WHERE fb2.symbol = cp.symbol
                    ORDER BY fb2.ts DESC LIMIT 1
                ) fb ON true
                WHERE cp.symbol = h.symbol
                  AND cp.cmp IS NOT NULL
                  AND cp.updated_at >= (NOW() AT TIME ZONE 'Asia/Kolkata') - INTERVAL '10 minutes'
            """)
            n = cur.rowcount
            conn.commit()
        log.info(f"smartgain_mtm: refreshed {n} holdings (fut-ltp-first)")
    except Exception as e:
        log.error(f"smartgain_mtm: {e}")
    finally:
        _smartgain_mtm_running = False

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

# ── ADR/PCR watchdog + health (task #59) ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _log_health(conn, title: str, details: dict):
    """Health ping → ops_log (category=scheduler_health) so a stalled compute
    job is visible after the fact (root cause of the silent 18-19 Jun ADR gap).
    cc#156: telemetry categories moved off session_log to ops_log."""
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'scheduler_health', %s, %s)""",
                        (title, Json(details)))
        conn.commit()
    except Exception as e:
        log.error(f"_log_health {title}: {e}")


def _backfill_adr_for_date(conn, target) -> bool:
    """EOD-based ADR for a past day from raw_prices (day-over-day close), used to
    heal a missed live (intraday-based) compute. Never overwrites a real row."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH cur AS (SELECT symbol, close FROM raw_prices WHERE price_date=%(t)s),
            prev AS (SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices
                     WHERE price_date < %(t)s ORDER BY symbol, price_date DESC),
            counts AS (
                SELECT COUNT(*) FILTER (WHERE cur.close > prev.close) AS adv,
                       COUNT(*) FILTER (WHERE cur.close < prev.close) AS dcl,
                       COUNT(*) FILTER (WHERE cur.close = prev.close) AS unch,
                       COUNT(*) AS total
                FROM cur JOIN prev USING (symbol))
            INSERT INTO adr_daily (price_date, advances, declines, unchanged, adr)
            SELECT %(t)s, adv, dcl, unch,
                   CASE WHEN dcl>0 THEN ROUND(adv::numeric/dcl,3) ELSE adv END
            FROM counts WHERE total > 0
            ON CONFLICT (price_date) DO NOTHING
        """, {"t": target})
        n = cur.rowcount
    conn.commit()
    return n > 0


def _backfill_pcr_for_date(conn, target) -> bool:
    """Heal a missed PCR day from option_chain — only if that day's chain is still
    retained (intraday option_chain is short-lived, so this is usually a no-op)."""
    with conn.cursor() as cur:
        # cc#121: same fix as _compute_and_store_pcr — correct pcr_daily columns
        # (underlying/put_oi/call_oi/pcr), last snapshot of the day per underlying,
        # HAVING put-OI>0 to skip a broken-feed underlying. DO NOTHING preserves any
        # existing good row (heal-only).
        cur.execute("""
            INSERT INTO pcr_daily (price_date, underlying, put_oi, call_oi, pcr)
            SELECT DATE(ts), underlying,
                SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END),
                SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END),
                ROUND(SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END)::numeric /
                      NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END),0), 3)
            FROM option_chain
            WHERE DATE(ts) = %(t)s
              AND underlying IN ('NIFTY','BANKNIFTY')
              AND ts = (SELECT MAX(oc2.ts) FROM option_chain oc2
                        WHERE DATE(oc2.ts) = %(t)s AND oc2.underlying = option_chain.underlying)
            GROUP BY DATE(ts), underlying
            HAVING SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) > 0
            ON CONFLICT (price_date, underlying) DO NOTHING
        """, {"t": target})
        n = cur.rowcount
    conn.commit()
    return n > 0


def _backfill_missing_adr_pcr(conn, today):
    """Watchdog auto-backfill: any recent weekday with raw_prices EOD data but no
    adr_daily row gets recomputed (the 18-19 Jun stall went unnoticed for days)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT price_date FROM raw_prices
                           WHERE price_date >= %s - INTERVAL '7 days' AND price_date < %s
                             AND price_date NOT IN (SELECT price_date FROM adr_daily)
                           ORDER BY price_date""", (today, today))
            missing = [r[0] for r in cur.fetchall()]
        for d in missing:
            if _backfill_adr_for_date(conn, d):
                _backfill_pcr_for_date(conn, d)
                _log_health(conn, "adr_compute", {"date": str(d), "status": "backfilled", "source": "raw_prices"})
                _log_alert("adr_backfill", f"ADR was missing for {d} — auto-backfilled from raw_prices EOD")
    except Exception as e:
        log.error(f"_backfill_missing_adr_pcr: {e}")


def _read_adr_pcr_today(conn, today):
    with conn.cursor() as cur:
        cur.execute("SELECT adr FROM adr_daily WHERE price_date=%s", (today,))
        r = cur.fetchone(); adr_val = float(r[0]) if r and r[0] is not None else None
        cur.execute("SELECT COUNT(*) FROM pcr_daily WHERE price_date=%s", (today,))
        pcr_rows = cur.fetchone()[0]
    return adr_val, pcr_rows


def _bg_adr_pcr():
    global _adr_pcr_ran_today
    today = _ist_now().date()
    if _adr_pcr_ran_today == today: return
    try:
        with _conn() as conn:
            _compute_and_store_adr(conn)
            _compute_and_store_pcr(conn)
            _backfill_missing_adr_pcr(conn, today)        # heal prior-day gaps
            adr_val, pcr_rows = _read_adr_pcr_today(conn, today)
            _log_health(conn, "adr_compute",
                        {"date": str(today), "status": "ok" if adr_val is not None else "missing",
                         "adr_value": adr_val})
            _log_health(conn, "pcr_compute",
                        {"date": str(today), "status": "ok" if pcr_rows else "missing",
                         "pcr_rows": pcr_rows})
        if adr_val is not None:
            _adr_pcr_ran_today = today                    # lock the day only once ADR is present
        else:
            _log_alert("adr_missing", f"ADR not computed for {today} after 15:50 — retry scheduled 16:00")
        log.info("adr_pcr done")
    except Exception as e: log.error(f"adr_pcr: {e}")


def _bg_adr_pcr_retry():
    """task #59: 10-min retry — if the 15:50 run didn't produce today's ADR, redo
    it once at 16:00 (covers a transient feed/scheduler hiccup)."""
    today = _ist_now().date()
    if _adr_pcr_ran_today == today: return                # already verified-complete
    log.warning("adr_pcr: 15:50 run incomplete — retrying at 16:00")
    _bg_adr_pcr()

def _bg_tc_screener_precompute():
    # task #43: nightly TC screener cache @16:00 IST (after market close + ADR/PCR)
    try:
        import trade_check_v34_endpoints as tce
        res = tce.run_tc_screener_precompute()
        log.info(f"tc_screener_precompute: {res.get('rows') if isinstance(res, dict) else res} rows")
    except Exception as e: log.error(f"tc_screener_precompute: {e}")

def _check_universe_shrink(conn):
    """Task #35: after the nightly EOD load, alert if raw_prices symbol coverage
    dropped sharply vs the prior trading day — catches silent partial loads (the
    17-Jun 1717→1676 drop went unnoticed). Threshold: >10 symbols."""
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT price_date, COUNT(DISTINCT symbol)
                           FROM raw_prices GROUP BY price_date
                           ORDER BY price_date DESC LIMIT 2""")
            rows = cur.fetchall()
        if len(rows) < 2:
            return
        (today_d, today_n), (prev_d, prev_n) = rows[0], rows[1]
        drop = prev_n - today_n
        if drop <= 10:
            return
        with conn.cursor() as cur:
            cur.execute("""SELECT symbol FROM raw_prices WHERE price_date=%s
                           EXCEPT SELECT symbol FROM raw_prices WHERE price_date=%s
                           ORDER BY symbol""", (prev_d, today_d))
            dropped = [r[0] for r in cur.fetchall()]
        sample = ", ".join(dropped[:30]) + (" …" if len(dropped) > 30 else "")
        msg = (f"raw_prices universe shrank {prev_n}→{today_n} ({drop} symbols) "
               f"{prev_d}→{today_d}. Dropped: {sample}")
        _log_alert("universe_shrink", msg)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO cc_task_logs (task_id, actor, level, message)
                           VALUES (35, 'scheduler', 'warn', %s)""", (msg,))
            conn.commit()
        log.warning(f"universe_shrink alert: {prev_n}->{today_n} ({drop})")
    except Exception as e:
        log.error(f"_check_universe_shrink: {e}")

def _bg_yahoo_daily_sync():
    global _yahoo_daily_running, _yahoo_ran_today
    if _yahoo_daily_running: return
    _yahoo_daily_running = True
    try:
        import yahoo_daily_update as ydu
        result = asyncio.run(ydu.run_async())          # cc_task #72 bug_2: was ydu.run_update(conn) — undefined -> silent AttributeError, nightly EOD never ran
        with _conn() as conn:
            _check_universe_shrink(conn)   # task #35: alert on silent coverage drop
            heal = ydu.heal_indices(conn)  # cc_task #72 bug_2: verify + self-heal index symbols post-EOD
        _yahoo_ran_today = _ist_now().date()
        log.info(f"yahoo_daily: {result} | index_heal: {heal}")
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
        with _conn() as conn:
            res = v8_paper.compute_pivots(conn, today)   # was rebuild_pivots (did not exist -> silent AttributeError, cc_task #68 Bug 1)
            built = res.get("built", 0) if isinstance(res, dict) else 0
            _log_health(conn, "pivots_build",
                        {"date": str(today), "status": "ok" if built else "empty",
                         "built": built, "total": res.get("total") if isinstance(res, dict) else None})
        if built:
            _pivots_ran_today = today          # lock the day only once pivots are present
        else:
            _log_alert("pivots_missing", f"paper pivots built 0 rows for {today} at 01:45")
        log.info(f"pivots built: {res}")
    except Exception as e:
        log.error(f"pivots: {e}")
        _log_alert("pivots_error", f"paper pivot build failed for {today}: {e}")

def _bg_universe_technicals():
    """cc#154: nightly RSI/DMA/returns/pivots for the full ~1766 GVM universe
    (not just the 209 futures_universe symbols v8_metrics covers). Scheduled
    after GVM (01:30) since it reads gvm_scores for its symbol list.
    NOTE: this tasks spec said "22:10 IST after GVM 22:00" but GVM was moved to
    01:30 IST on 18-Jun-2026 (task #31, comment above _bg_gvm) - scheduled here
    in the real 01:00-02:00 nightly chain instead, after the last existing job."""
    global _ut_ran_today
    today = _ist_now().date()
    if _ut_ran_today == today: return
    try:
        import universe_technicals
        with _conn() as conn:
            res = universe_technicals.run_universe_technicals(conn, today)
        _ut_ran_today = today
        log.info(f"universe_technicals: {res}")
    except Exception as e:
        log.error(f"universe_technicals: {e}")
        _log_alert("universe_technicals_error", f"nightly run failed for {today}: {e}")


def _check_pivots_health():
    """cc_task #68 Bug 1: 10-min pivot watchdog (mirrors _bg_adr_pcr_retry). If the
    01:45 build produced no pivots for today, rebuild once at 01:55 + alert."""
    global _pivots_ran_today
    today = _ist_now().date()
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM v8_paper_pivots WHERE pivot_date=%s", (today,))
                n = int(cur.fetchone()[0])
            if n == 0:
                import v8_paper
                res = v8_paper.compute_pivots(conn, today)
                built = res.get("built", 0) if isinstance(res, dict) else 0
                _log_alert("pivots_watchdog", f"paper pivots missing for {today} at 01:55 — rebuilt {built}")
                _log_health(conn, "pivots_build", {"date": str(today), "status": "watchdog_rebuilt", "built": built})
                if built:
                    _pivots_ran_today = today
            else:
                _log_health(conn, "pivots_build", {"date": str(today), "status": "ok", "rows": n})
    except Exception as e:
        log.error(f"pivots_watchdog: {e}")
        _log_alert("pivots_watchdog_error", f"pivot watchdog failed for {today}: {e}")

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

# ── news fetch (task #38) ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _bg_fetch_market_news():
    try:
        import news_fetcher
        with _conn() as conn: res = news_fetcher.fetch_market_news(conn)
        log.info(f"news_market: {res.get('inserted') if isinstance(res, dict) else res} new")
    except Exception as e: log.error(f"news_market: {e}")

def _bg_fetch_company_news():
    try:
        import news_fetcher
        with _conn() as conn: res = news_fetcher.fetch_company_news(conn)
        log.info(f"news_company: {res.get('inserted') if isinstance(res, dict) else res} new")
    except Exception as e: log.error(f"news_company: {e}")

def _bg_cleanup_news():
    try:
        import news_fetcher
        with _conn() as conn: res = news_fetcher.cleanup_old_news(conn)
        log.info(f"news_cleanup: {res.get('deleted') if isinstance(res, dict) else res} deleted")
    except Exception as e: log.error(f"news_cleanup: {e}")

# ── main loop ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

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
            _spawn(_bg_fetch_market_news)   # task #38: domestic + global RSS
        if h == 6 and m == 30:
            _spawn(_bg_fetch_company_news)  # task #38: top-500 Google News RSS
        if 6 <= h <= 23 and m % 5 == 0:
            _spawn(_bg_fetch_global_intraday)
        if now.weekday() == 0 and h == 8 and m == 0:
            _spawn(_bg_fu_sync)
        if h == 9 and m == 10:
            _spawn(_premarket_writer_check)   # cc_task #72 bug_1: 09:10 pre-market writer readiness
        if _is_market_hours(now) and m % 5 == 0:
            _spawn(_bg_signal_writer)
            _spawn(_bg_v8_paper_exit)         # cc_task #72 bug_0: live exit pass (primary)
            _spawn(_bg_v10_tick)
            _spawn(_bg_pcr_intraday)
            _spawn(_bg_tc_lite)               # cc_task #77: TC Lite screener (09:30-15:15 gate inside)
            _spawn(_bg_smartgain_mtm)         # cc#123: refresh SmartGain LTP/MTM from live cmp_prices
            # _spawn(_bg_intraday_paper)  # INACTIVE 18-Jun-2026 — on-demand only via /api/intraday/tick
            if m % 15 == 0:
                _spawn(_bg_qb_intraday_mark)
                _spawn(_bg_fetch_market_news)   # task #40: live RSS refresh during market hours
        # cc_task #89: yahoo EOD raw_prices refresh at 15:35 IST (5 min after close) so
        # v8_engine EOD (15:45) and the evening journal review see today's official closes
        # ~5h sooner. The 01:00 IST run (below) stays as the nightly safety re-run.
        # Weekday-only. NO GVM/QB cascade here — that stays in the 01:00-02:00 nightly chain.
        if now.weekday() < 5 and h == 15 and m == 35: _spawn(_bg_yahoo_daily_sync)
        if h == 15 and m == 45: _spawn(_bg_v8_eod)
        if h == 15 and m == 50: _spawn(_bg_adr_pcr)
        if h == 16 and m == 0:
            _spawn(_bg_adr_pcr_retry)            # task #59: 10-min ADR/PCR watchdog retry
            _spawn(_bg_tc_screener_precompute)   # task #43: TC screener cache
        # Nightly batch shifted to 01:00–01:45 IST (task #31). The old 21:00–22:05
        # window collided with CC deploy pushes — a Railway redeploy kills the
        # scheduler mid-job (caused the 18-Jun raw_prices gap). 1 AM = no-push window.
        # 15-min spacing gives each job runway; order preserves deps (prices→QB→GVM→pivots).
        if h == 1 and m == 0:   _spawn(_bg_yahoo_daily_sync)
        if h == 1 and m == 15:  _spawn(_bg_qb_eod)
        if h == 1 and m == 30:  _spawn(_bg_gvm)
        if h == 1 and m == 45:  _spawn(_bg_pivots)
        if h == 1 and m == 55:  _spawn(_check_pivots_health)   # cc_task #68 Bug 1: pivot watchdog
        if h == 1 and m == 50:  _spawn(_bg_cleanup_news)   # task #38: 30-day news purge
        if h == 2 and m == 0:   _spawn(_bg_v8_paper_exit_eod)  # cc_task #72 bug_0: EOD-close exit fallback (after EOD load + heal)
        if h == 2 and m == 5:   _spawn(_bg_universe_technicals)  # cc#154: full-universe technicals, after GVM (01:30) + pivots (01:45)


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
