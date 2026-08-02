"""
Scheduler — Scorr background tasks (restored 18-Jun-2026).
Deactivation: _bg_intraday_paper commented out — on-demand only via /api/intraday/tick.
"""
import asyncio, logging, os, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone, time as dt_time
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

_SKIPPED = "SKIPPED"   # cc#526: sentinel a flag-gated job returns from a no-op tick so the
                        # recorder can tell "did nothing" apart from "ran and succeeded" --
                        # scheduler_master previously stamped last_status='ok' on every single
                        # no-op tick of a flag-gated job (e.g. the 4 ops-metrics jobs, checked
                        # every 60s but only ever DOING something when their flag is 'pending'),
                        # which made a job that hadn't actually run in days look perfectly healthy.


def _run_recorded(fn, args):
    """cc#525 run recorder: wraps the job call so every _spawn dispatch upserts
    last_run_at/last_status/last_error/last_duration_ms into scheduler_master, with ZERO
    behavior change to fn itself (same call, same args, same return/raise). Recorder failures
    never break the job -- scheduler_master.record_run already swallows its own exceptions."""
    t0 = time.time()
    try:
        result = fn(*args)
    except Exception as e:
        try:
            import scheduler_master
            scheduler_master.record_run(fn.__name__, "error", str(e), int((time.time() - t0) * 1000))
        except Exception:
            pass
        raise
    try:
        import scheduler_master
        status = "skipped" if result == _SKIPPED else "ok"
        scheduler_master.record_run(fn.__name__, status, None, int((time.time() - t0) * 1000))
    except Exception:
        pass
    return result


def _spawn(fn, *args):
    """Dispatch a blocking job on the dedicated scheduler pool (never the shared
    default executor) and keep a reference so the task isn't GC'd mid-flight."""
    loop = asyncio.get_running_loop()
    t = asyncio.ensure_future(loop.run_in_executor(_EXECUTOR, _run_recorded, fn, args))
    _bg_tasks.add(t); t.add_done_callback(_bg_tasks.discard)
    return t

def _is_market_hours(now):
    if now.weekday() >= 5: return False
    t = (now.hour, now.minute)
    return (9,15) <= t <= (15,30)

def _is_trading_day(d):
    """cc#207: weekend + NSE-holiday aware. Weekend-only fallback if the holiday
    module is unavailable so we never crash the loop."""
    try:
        import nse_holidays
        return nse_holidays.is_trading_day(d)
    except Exception:
        return d.weekday() < 5

# ── run-guards ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# signal_writer uses a started_at timestamp + token (not a bare bool) so a hung
# tick can't permanently block future ticks — see _bg_signal_writer / _check_watchdog.
_signal_writer_started_at: Optional[datetime] = None
_signal_writer_token = 0
_signal_writer_fail_streak = 0
_last_signal_writer_ok: Optional[datetime] = None
SIGNAL_WRITER_TIMEOUT = timedelta(minutes=4)   # spec #16: assume hung after ~4 min
WATCHDOG_STALE_MIN = 10                          # spec #16: stale if no tick in 10 min
WATCHDOG_REALERT   = timedelta(minutes=15)       # cc#230: re-alert every 15 min while STILL stale
                                                 # (was one-and-done -> a persistent code-level
                                                 # crash went silent after the first alert)
WATCHDOG_RESTART_COOLDOWN = timedelta(minutes=5) # cap restart attempts (avoid storm if
                                                 # the writer itself, not the loop, is broken)
_eod_running = False
_eod_ran_today: Optional[date] = None
_adr_pcr_ran_today: Optional[date] = None
_yahoo_ran_today: Optional[date] = None
_yahoo_daily_running = False
_gvm_ran_today: Optional[date] = None
_gvm_backfill_running = False   # cc#468/470: 5yr deep-history backfill guard
_mf_backfill_running = False    # cc#477: V15 MF returns backfill single-flight guard
_bt14_fut_oi_running = False    # cc#538: ORB basis/OI research backfill single-flight guard
_yahoo_new_listings_running = False   # cc#539: new-listing Yahoo EOD backfill single-flight guard
# cc#541 (restructured 19-Jul): app-side LLM extraction runner REMOVED — extraction is done by
# Claude Code under subscription (no app anthropic calls); ops_extraction_run flag retired.
_mf_wiring_running = False      # cc#491: V15 MF wire-all (AUM/ER/holdings/scores) single-flight guard
_mf_weekly_manual_running = False   # cc#491 course-correct: manual /run_weekly single-flight guard
_intraday_scan_running = False  # cc#481: intraday scanner 15-min auto-scan single-flight guard
_pivots_ran_today: Optional[date] = None
_upivots_ran_today: Optional[date] = None   # cc#342: full-universe v8_paper_pivots rebuild
_qb_eod_ran_today: Optional[date] = None
_ut_ran_today: Optional[date] = None   # cc#154: universe_technicals nightly guard
_qb_eod_running = False
# cc#190: 15:20 gate-rebalance day-lock
_gate_rebalance_ran_today: Optional[date] = None
_qb_intraday_mark_running = False
_global_fetching = False
_global_intraday_fetching = False
_v10_running = False
_pcr_intraday_running = False
_intraday_paper_running = False
_tc_lite_running = False                          # cc_task #77: TC Lite screener pass guard
_tc_position_stars_running = False                # cc#728: hourly open-position TC-star batch guard
_smartgain_mtm_running = False                    # cc#123: SmartGain live MTM refresh guard
_fu_sync_ran_this_week: Optional[date] = None
_lot_sync_ran_today: Optional[date] = None   # cc#314: nightly Fyers lot-size audit day-lock
_v8_paper_exit_running = False                   # cc_task #72 bug_0: live exit pass guard
_v8_paper_exit_eod_ran: Optional[date] = None    # cc_task #72 bug_0: EOD fallback day-lock
_premarket_check_ran: Optional[date] = None      # cc_task #72 bug_1: 09:10 check day-lock
_v21_ks_ran_today: Optional[date] = None         # cc#158: V2.1 kill-switch day-lock

# ── health / watchdog state ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
_restart_requested = False
_watchdog_restarts = 0
_last_restart_ts: Optional[datetime] = None
_watchdog_alerted = False        # throttle: one alert per stall episode
_watchdog_last_alert: Optional[datetime] = None   # cc#230: last re-alert time within a stall

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
            # cc#591 fix_2: anchor on the LAST bar of the day WITH valid put-OI (last-good bar), NOT
            # the absolute MAX(ts). The put-leg OI feed intermittently drops to 0 (BANKNIFTY-weighted,
            # 20-Jul 13:30); with the old MAX(ts)+HAVING the close bar's zero-put skipped the whole
            # underlying and left a daily hole. Falling back to the last put-OI>0 bar fills the day
            # (a same-session proxy, better than nothing) and keeps the PCR mood-gate (id=1916) fed.
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
                  AND ts = (SELECT oc2.ts FROM option_chain oc2
                            WHERE DATE(oc2.ts) = CURRENT_DATE AND oc2.underlying = option_chain.underlying
                            GROUP BY oc2.ts
                            HAVING SUM(CASE WHEN oc2.option_type='PE' THEN oc2.oi ELSE 0 END) > 0
                            ORDER BY oc2.ts DESC LIMIT 1)
                GROUP BY DATE(ts), underlying
                HAVING SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) > 0
                ON CONFLICT (price_date, underlying) DO UPDATE SET
                    put_oi=EXCLUDED.put_oi, call_oi=EXCLUDED.call_oi,
                    pcr=EXCLUDED.pcr, computed_at=NOW()
            """)
            conn.commit()
        try:
            import pcr_backfill
            pcr_backfill.mark_pcr_quality(conn)   # cc#745: sanity-gate the just-computed PCR rows
        except Exception as _qe:
            log.warning(f"pcr quality mark failed: {_qe}")
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


def _alert_telegram(msg: str):
    """cc#580 fault_2: push a Telegram alert (best-effort) so a silent writer death is impossible.
    Reuses the proven v10_st_ema.telegram_alert sender already used by the feed watchdogs. Never
    raises — an alert-channel failure must not break the watchdog."""
    try:
        import v10_st_ema
        v10_st_ema.telegram_alert(msg)
    except Exception as e:
        log.warning(f"_alert_telegram failed: {e}")


# cc#580 fault_2: emergency job-pool rebuild. When the writer has stalled AND a restart did not
# recover it, the shared pool is likely saturated by hung/slow ticks (20-Jul: per-symbol upserts
# each blocking the 90s statement_timeout under a lock) — so the recovery _spawn just queues
# forever and "never gets a worker" (the 19-Jun root-cause comment at the top of this file).
_writer_pool_rebuilds = 0
_last_pool_rebuild_ts: Optional[datetime] = None
POOL_REBUILD_COOLDOWN = timedelta(minutes=15)


def _rebuild_executor(reason: str):
    """Swap in a FRESH job pool so the next recovery tick is guaranteed a clean worker; the old
    pool's hung threads are abandoned (they drain when their statement_timeout fires) but no longer
    block new dispatch. Throttled so a persistent code-bug stall can't leak threads unboundedly."""
    global _EXECUTOR, _writer_pool_rebuilds, _last_pool_rebuild_ts
    now = _ist_now()
    if _last_pool_rebuild_ts is not None and (now - _last_pool_rebuild_ts) < POOL_REBUILD_COOLDOWN:
        return
    old = _EXECUTOR
    _EXECUTOR = ThreadPoolExecutor(max_workers=12, thread_name_prefix="sched-job")
    _writer_pool_rebuilds += 1
    _last_pool_rebuild_ts = now
    log.error(f"scheduler: REBUILT job pool (#{_writer_pool_rebuilds}) — {reason}")
    _alert_telegram(f"🔁 Scorr job-pool REBUILT (#{_writer_pool_rebuilds}): {reason} — "
                    f"recovery tick now has a clean worker")
    try:
        old.shutdown(wait=False)   # let hung threads drain on the abandoned pool; don't block
    except Exception:
        pass


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
    # cc#580 fault_2: every writer restart pings Telegram so a silent stall can never hide (the
    # 20-Jul writer was dead ~5h with no alert reaching a human).
    _alert_telegram(f"⚠️ Scorr signal-writer RESTART #{_watchdog_restarts} "
                    f"({_ist_now():%H:%M:%S IST}): {reason}")


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
    global _watchdog_alerted, _watchdog_last_alert
    # cc#211: also skip NSE holidays. _is_market_hours is weekday+time only, so on a
    # weekday holiday (writer correctly gated → tick never advances) the watchdog would
    # otherwise restart-storm chasing a "stale" tick that will never move. Canonical guard.
    if not _is_market_hours(now) or not _is_trading_day(now.date()):
        _watchdog_alerted = False
        return
    age = _tick_age_minutes()
    if age is None:
        return
    if age > WATCHDOG_STALE_MIN:
        # cc#230: RE-ALERT every WATCHDOG_REALERT while STILL stale (was one-and-done, so a
        # persistent CODE-level crash — which a loop restart can NOT fix — went silent after
        # the first alert). Once a restart has already fired and the tick is still climbing,
        # escalate the message: a human needs to look at the writer traceback.
        if _watchdog_last_alert is None or (now - _watchdog_last_alert) >= WATCHDOG_REALERT:
            escalate = (_last_restart_ts is not None and age > WATCHDOG_STALE_MIN + 6)
            _log_alert("scheduler_stall",
                       f"v8_metrics stale {age:.1f} min during market hours" +
                       (" — restart did NOT recover it; likely a writer CODE bug needing a manual "
                        "fix (check signal_writer_crash / POST /api/v8/run_signal_writer traceback)"
                        if escalate else " — restarting live loop"))
            # cc#580 fault_2: mirror the stall to Telegram, and if a prior restart failed to clear it
            # the shared pool is probably saturated by hung ticks — swap in a fresh one so the next
            # recovery _spawn is guaranteed a worker (fixes "restart never got a worker to run on").
            _alert_telegram(f"🛑 Scorr writer STALL: v8_metrics {age:.1f} min stale in market hours "
                            f"({_ist_now():%H:%M:%S IST})" +
                            (" — restart did NOT recover; rebuilding pool" if escalate else ""))
            if escalate:
                _rebuild_executor(f"writer stale {age:.1f} min despite restart")
            _watchdog_last_alert = now
            _watchdog_alerted = True
        # cooldown: if a restart didn't help (writer-logic bug, not a hung loop),
        # don't hammer — retry at most once per cooldown window.
        if _last_restart_ts is None or (now - _last_restart_ts) >= WATCHDOG_RESTART_COOLDOWN:
            _request_restart(f"watchdog: tick stale {age:.1f} min")
    else:
        _watchdog_alerted = False
        _watchdog_last_alert = None


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
        # cc#605 fix_3: MANDATORY crash-to-ops_log. The 22-Jul writer death was invisible — the tick
        # raised and this except only log.error'd it (app logs, not the DB), so there was NO
        # signal_writer_crash row in ops_log all day while 24 restarts silently failed. Write the
        # traceback to ops_log on every failure so a writer death can NEVER again be silent.
        try:
            import json as _json, traceback as _tb
            with _conn() as _c, _c.cursor() as _cur:
                _cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                                VALUES (CURRENT_DATE, NOW(), 'alert', 'signal_writer_crash', %s::jsonb)""",
                             (_json.dumps({"fail_streak": _signal_writer_fail_streak,
                                           "error": str(e)[:300], "traceback": _tb.format_exc()[-1600:],
                                           "ist": _ist_now().isoformat()}),))
                _c.commit()
        except Exception as _le:
            log.error(f"signal_writer: crash-to-ops_log also failed: {_le}")
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
        return _SKIPPED
    _premarket_check_ran = today
    # cc#206/#211: canonical trading-day guard (single _is_trading_day helper, nse_holidays
    # based — no duplicate inline check). The 09:10 readiness restart fired on SAT 04-Jul
    # (v8_metrics "stale" 1045 min → forced restart on a non-trading day) — needless
    # cold-boot risk (id=166 class). No live writer runs off-session, so skip weekends +
    # NSE holidays entirely. (Writes are also gated at the writer choke point in cc#211.)
    if not _is_trading_day(today):
        return _SKIPPED
    try:
        age = _tick_age_minutes()
        if age is None or age > 60:
            _log_alert("scheduler_stall_9am",
                       f"09:10 pre-market: v8_metrics stale "
                       f"{f'{age:.0f}min' if age is not None else 'absent'} — forcing live-writer restart before open")
            _request_restart("premarket 09:10 readiness: v8_metrics stale/absent")
            _spawn(_bg_signal_writer)
        # cc#771 fix_1: the SmartGain opening-state rebuild was never scheduled — replay/journal drifted
        # stale EVERY morning until smartgain_backfill was run by hand (30/31-Jul mismatch banners). Run
        # it here at 09:10 so the book always opens in sync (idempotent + self-healing; safe to repeat).
        _spawn(_bg_smartgain_resync)
    except Exception as e:
        log.error(f"_premarket_writer_check: {e}")


_result_radar_running = False
def _bg_result_radar_snapshot():
    """cc#772: post-close (15:40 IST) FREEZE of the Result Radar card (next-day reporters) into
    result_radar_log with the T-1 tag/metrics captured at result-eve (returns filled later)."""
    global _result_radar_running
    if _result_radar_running:
        return
    _result_radar_running = True
    try:
        import intraday_scanner_endpoints
        log.info(f"result_radar snapshot: {intraday_scanner_endpoints.result_radar_snapshot()}")
    except Exception as e:
        log.error(f"_bg_result_radar_snapshot: {e}")
    finally:
        _result_radar_running = False


def _bg_result_radar_log():
    """cc#772: nightly T+1 EOD backfill — fills T/T+1 day-returns on frozen result_radar_log rows once
    those closes are in raw_prices (the tag is already frozen; this only fills outcomes). Idempotent."""
    try:
        import intraday_scanner_endpoints
        log.info(f"result_radar backfill: {intraday_scanner_endpoints.result_radar_backfill_returns()}")
    except Exception as e:
        log.error(f"_bg_result_radar_log: {e}")


_smartgain_resync_running = False
def _bg_smartgain_resync():
    """cc#771 fix_1: 09:10 IST pre-market SmartGain replay-resync (all accounts in smartgain_orders).
    backfill_all_batches re-runs the full-inception cascade — idempotent + self-healing (cc#309), so a
    daily pre-open run guarantees the FIFO replay / journal / weekly-opening never start desynced (the
    morning mismatch-banner recurrence). Run once, day-locked by the 09:10 hook that calls it."""
    global _smartgain_resync_running
    if _smartgain_resync_running:
        return
    _smartgain_resync_running = True
    try:
        import smartgain_reconcile
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT account FROM smartgain_orders WHERE status=%s",
                        (smartgain_reconcile.ORDER_STATUS,))
            accounts = [r[0] for r in cur.fetchall()] or [smartgain_reconcile.DEFAULT_ACCOUNT]
        for acct in accounts:
            try:
                res = smartgain_reconcile.backfill_all_batches(acct)
                log.info(f"smartgain 09:10 resync {acct}: {res.get('batches_processed')} batches")
            except Exception as e:
                log.error(f"smartgain 09:10 resync {acct}: {e}")
    except Exception as e:
        log.error(f"_bg_smartgain_resync: {e}")
    finally:
        _smartgain_resync_running = False


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
    if _v8_paper_exit_eod_ran == today: return _SKIPPED
    try:
        import v8_paper
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(price_date) FROM raw_prices")
                d = cur.fetchone()[0]
            if d is None:
                return _SKIPPED
            res = v8_paper.run_paper_exits(conn, target_date=d, mode="eod")
        _v8_paper_exit_eod_ran = today
        with _conn() as _c:   # cc#255: write a ran-ok health row regardless of close count
            _log_health(_c, "v8_paper_exit_eod", {"date": str(d), "closed": res.get("closed", 0)})
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

def _bg_tc_position_stars():
    """cc#728: HOURLY Trade Check batch for every OPEN paper position -> tc_position_stars.
    Fires on the :30 hour-mark 09:30-15:30 IST (trading days), replacing cc#720's on-demand
    15-min per-symbol cache. Direction = the position's OWN side (PRIMARY: LONG position ->
    LONG check, SHORT -> SHORT). Uses the canonical scorer via tc_resolver.get_primary_tc (cc#738),
    the SAME scorer the on-demand star used, so the star verdict vocabulary (STRONG/VALID) is
    unchanged — only the cadence moves to a predictable hourly batch. Each run inserts a fresh
    computed_at row (history kept); the dashboard reads the latest row per symbol|side, so there
    is NO live TC call from the render path anymore (call-storm eliminated)."""
    global _tc_position_stars_running
    if _tc_position_stars_running: return
    _tc_position_stars_running = True
    try:
        from tc_resolver import get_primary_tc   # cc#738: canonical scorer (never a versioned import)
        _tc = get_primary_tc()
        batch_ts = _ist_now()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS tc_position_stars (
                symbol TEXT NOT NULL, side TEXT NOT NULL, verdict TEXT,
                score NUMERIC, max_score NUMERIC, computed_at TIMESTAMP NOT NULL,
                PRIMARY KEY (symbol, side, computed_at))""")
            conn.commit()
            cur.execute("SELECT DISTINCT symbol, side FROM v8_paper_positions "
                        "WHERE status='OPEN' AND symbol IS NOT NULL")
            pairs = cur.fetchall()
        rows = []
        for sym, side in pairs:
            try:
                d = _tc(sym, side)
                if d and not d.get("error") and d.get("final_verdict") and d.get("tier1") and d.get("tier2"):
                    score = (float(d["tier1"].get("score") or 0) + float(d["tier2"].get("score") or 0))
                    mx = (float(d["tier1"].get("max") or 0) + float(d["tier2"].get("max") or 0))
                    rows.append((sym, side, d["final_verdict"], score, mx, batch_ts))
            except Exception as ie:
                log.error(f"tc_position_stars {sym}/{side}: {ie}")
        if rows:
            with _conn() as conn, conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO tc_position_stars (symbol, side, verdict, score, max_score, computed_at)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (symbol, side, computed_at) DO UPDATE SET
                           verdict=EXCLUDED.verdict, score=EXCLUDED.score, max_score=EXCLUDED.max_score""",
                    rows)
                conn.commit()
        log.info(f"tc_position_stars: {len(rows)}/{len(pairs)} scored @ {batch_ts:%H:%M IST}")
    except Exception as e:
        log.error(f"tc_position_stars: {e}")
    finally:
        _tc_position_stars_running = False


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
    write, so this job must set ltp only. Pairs with /api/smartgain/m2m.

    cc#161 (safety fix): the pure-spot-only fallback (no fresh fut tick, no
    basis) had no check for whether this symbol has EVER had real futures data.
    A structurally fut-less instrument (e.g. NIFTY -- index futures are never
    subscribed on the live feed, see cc#162) would silently get holdings.ltp
    set from spot alone forever. Now that fallback only fires for symbols that
    have at least once had a real fyers_fut tick (a normal stock future
    momentarily missing a fresh one) -- never for an instrument with zero
    futures history."""
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
                    WHERE ip.symbol = cp.symbol AND ip.source IN ('fyers_fut','fyers_fut_rest')  -- cc#770: REST fallback
                      AND ip.ts >= (NOW() AT TIME ZONE 'Asia/Kolkata') - INTERVAL '10 minutes'
                    ORDER BY ip.ts DESC LIMIT 1
                ) fut ON true
                LEFT JOIN LATERAL (
                    SELECT fb2.basis
                    FROM futures_basis fb2
                    WHERE fb2.symbol = cp.symbol
                    ORDER BY fb2.ts DESC LIMIT 1
                ) fb ON true
                LEFT JOIN LATERAL (
                    SELECT EXISTS(
                        SELECT 1 FROM intraday_prices ip2
                        WHERE ip2.symbol = cp.symbol AND ip2.source = 'fyers_fut'
                        LIMIT 1
                    ) AS ever
                ) fev ON true
                WHERE cp.symbol = h.symbol
                  AND cp.cmp IS NOT NULL
                  AND cp.updated_at >= (NOW() AT TIME ZONE 'Asia/Kolkata') - INTERVAL '10 minutes'
                  AND (fut.fut_close IS NOT NULL OR fb.basis IS NOT NULL OR fev.ever)
            """)
            n = cur.rowcount
            # cc#762 / cc#769 fix_2: INDEX-futures positions (NIFTY/BANKNIFTY/…) — the stock path above
            # drives off cmp_prices under the HOLDING symbol, but an index future has no reliably-fresh
            # tick under its own key: NIFTY's live spot ticks as 'NIFTY50' (not 'NIFTY'), and index
            # futures are never subscribed live (cc#162) so the fyers_fut bar goes stale after close.
            # cc#762 looked spot up under idx.symbol='NIFTY' (stale since 10-Jul) -> NULL -> the WHERE
            # gate failed every scheduled 5-min tick and NIFTY.ltp froze at the one recovery stamp.
            # FIX: resolve the spot leg through the SPOT_ALIAS (NIFTY->NIFTY50, others self) so the live
            # NIFTY50 tick + last-known NIFTY basis produce a fresh synthetic future price on EVERY tick.
            # Fallback chain unchanged: fresh index-fut 5m bar -> aliased-spot + basis synthetic -> spot.
            cur.execute("""
                UPDATE smartgain_holdings h
                SET ltp = COALESCE(fut.fut_close, sp.cmp + fb.basis, sp.cmp),
                    updated_at = NOW()
                FROM (SELECT DISTINCT symbol,
                             CASE UPPER(symbol) WHEN 'NIFTY' THEN 'NIFTY50' ELSE UPPER(symbol) END AS spot_sym
                      FROM smartgain_holdings
                      WHERE UPPER(symbol) IN ('NIFTY','NIFTY50','BANKNIFTY','FINNIFTY','MIDCPNIFTY','SENSEX')) idx
                LEFT JOIN LATERAL (
                    SELECT ip.close AS fut_close FROM intraday_prices ip
                    WHERE ip.symbol = idx.symbol AND ip.source IN ('fyers_fut','fyers_fut_rest')  -- cc#770: REST fallback
                      AND ip.ts >= (NOW() AT TIME ZONE 'Asia/Kolkata') - INTERVAL '15 minutes'
                    ORDER BY ip.ts DESC LIMIT 1
                ) fut ON true
                LEFT JOIN LATERAL (
                    SELECT cp2.cmp FROM cmp_prices cp2
                    WHERE cp2.symbol = idx.spot_sym
                      AND cp2.updated_at >= (NOW() AT TIME ZONE 'Asia/Kolkata') - INTERVAL '15 minutes'
                    ORDER BY cp2.updated_at DESC LIMIT 1
                ) sp ON true
                LEFT JOIN LATERAL (
                    SELECT fb2.basis FROM futures_basis fb2 WHERE fb2.symbol = idx.symbol
                    ORDER BY fb2.ts DESC LIMIT 1
                ) fb ON true
                WHERE h.symbol = idx.symbol
                  AND (fut.fut_close IS NOT NULL OR sp.cmp IS NOT NULL)
            """)
            n_idx = cur.rowcount
            conn.commit()
        log.info(f"smartgain_mtm: refreshed {n} holdings (fut-ltp-first) + {n_idx} index holdings (cc#762)")
    except Exception as e:
        log.error(f"smartgain_mtm: {e}")
    finally:
        _smartgain_mtm_running = False

_fut_rest_fallback_running = False
def _bg_fut_rest_fallback():
    """cc#770 (P0): app-side REST futures fallback. When the native WS futures leg is dark (worker acks
    but Fyers streams no futures ticks), synthesize 5-min fyers_fut_rest bars from the Fyers /data/quotes
    REST snapshot for open-position futures + indices (then the wider universe if the token isn't
    throttled). NO worker bounce — never risks the working equity leg. Self-retires when native fut
    recovers (gated on native-fut-dark inside run())."""
    global _fut_rest_fallback_running
    if _fut_rest_fallback_running:
        return
    _fut_rest_fallback_running = True
    try:
        import fut_rest_fallback
        fut_rest_fallback.run()
    except Exception as e:
        log.error(f"fut_rest_fallback bg: {e}")
    finally:
        _fut_rest_fallback_running = False


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

def _bg_v21_killswitch():
    """cc#158: nightly V2.1 candidate-filter kill-switch check (16:10 IST, after
    close + ADR/TC). Auto-disables a basket's V2.1 filters + alerts if signal
    count starves or rolling WR decays >10pp below baseline (respecting the
    20-trading-day / 15-signal sample-discipline warmup). Never auto-re-enables."""
    global _v21_ks_ran_today
    today = _ist_now().date()
    if _v21_ks_ran_today == today: return _SKIPPED
    try:
        import v8_filter_killswitch
        with _conn() as conn: res = v8_filter_killswitch.run_killswitch_check(conn)
        _v21_ks_ran_today = today
        tripped = [b for b, r in res.items() if r.get("status") == "TRIPPED_DISABLED"]
        with _conn() as _c:   # cc#255
            _log_health(_c, "v21_killswitch", {"tripped": tripped, "count": len(tripped)})
        log.info(f"v21_killswitch: {len(tripped)} tripped {tripped or ''}")
    except Exception as e:
        log.error(f"v21_killswitch: {e}")


def _bg_v8_eod():
    global _eod_running, _eod_ran_today
    today = _ist_now().date()
    if _eod_ran_today == today or _eod_running: return _SKIPPED
    _eod_running = True
    try:
        import v8_engine
        with _conn() as conn:
            result = v8_engine.run_v8_engine(conn)
            _log_health(conn, "v8_eod", {"symbols": result.get("symbols_processed")})  # cc#255
        log.info(f"v8_eod: {result.get('symbols_processed')} syms")
        _eod_ran_today = today
    except Exception as e: log.error(f"v8_eod: {e}")
    finally: _eod_running = False

_heal_ran_today = None
def _bg_heal_intraday():
    """cc#238 Branch B (addendum 1652): at session end heal any missing 5-min tick across the
    full 09:15-15:30 session, so a live-writer hiccup doesn't silently lose data. Reuses
    main._heal_morning_gaps (now full-session). Data-completion ONLY — never a v8_qualified
    re-score (EOD qual writes are disabled per V8_EOD_NO_REQUALIFICATION_V1). Deferred import
    of main to avoid the scheduler<->main circular at module load."""
    global _heal_ran_today
    today = _ist_now().date()
    if _heal_ran_today == today:
        return _SKIPPED
    try:
        import main
        res = main._heal_morning_gaps()
        with _conn() as _c:   # cc#255
            _log_health(_c, "heal_intraday",
                        {"healed": res.get("symbols_healed"), "bars": res.get("bars_inserted")})
        log.info(f"heal_intraday(EOD Branch B): healed={res.get('symbols_healed')} "
                 f"bars={res.get('bars_inserted')} window={res.get('window')}")
        _heal_ran_today = today
    except Exception as e:
        log.error(f"heal_intraday(EOD Branch B): {e}")


def _bg_gate_rebalance():
    """cc#190: 15:20 IST auto-close of over-slot paper positions (GATE_EXIT).
    Wires v8_paper.run_gate_rebalance (exit-only) into the scheduler — previously
    the gate rebalance only ran if someone manually hit /api/paper/tick.

    SAFETY: if market-mood breadth is stale (no adr_intraday bar in the last
    30 min, IST) we SKIP and alert rather than rebalance on bad data. Writes an
    ops_log(category=gate_rebalance) record on every completed run — even zero
    closes — so the 15:20 pass is always auditable."""
    global _gate_rebalance_ran_today
    today = _ist_now().date()
    if _gate_rebalance_ran_today == today:
        return _SKIPPED
    try:
        # stale-mood guard — compute age in IST (adr_intraday.ts is naive IST)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT MAX(ts),
                                  EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - MAX(ts)))/60
                           FROM adr_intraday
                           WHERE ts::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date""")
            row = cur.fetchone()
        last_ts = row[0] if row else None
        age_min = float(row[1]) if row and row[1] is not None else None
        if last_ts is None or age_min is None or age_min > 30:
            _log_alert("gate_rebalance_stale_mood",
                       f"skipped 15:20 gate rebalance — adr_intraday stale "
                       f"(last={last_ts}, age={round(age_min,1) if age_min is not None else 'n/a'} min)")
            return _SKIPPED

        import v8_endpoints, v8_paper
        mood = v8_endpoints.market_mood()
        buy_slots, sell_slots = mood.get("buy_slots"), mood.get("sell_slots")
        if buy_slots is None or sell_slots is None:
            _log_alert("gate_rebalance_no_mood",
                       f"skipped 15:20 gate rebalance — mood missing slots (mood={mood.get('mood')})")
            return _SKIPPED

        with _conn() as conn:
            res = v8_paper.run_gate_rebalance(conn, buy_slots, sell_slots)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'gate_rebalance', %s, %s)""",
                        ("gate_rebalance_15_20",
                         Json({"mood": mood.get("mood"), "buy_slots": buy_slots, "sell_slots": sell_slots,
                               "closed": res.get("closed"), "slot_math": res.get("slot_math"),
                               "gate_exits": res.get("gate_exits"), "ist": _ist_now().isoformat()})))
            conn.commit()
        _gate_rebalance_ran_today = today
        log.info(f"gate_rebalance: closed {res.get('closed')} | {res.get('slot_math')}")
    except Exception as e:
        log.error(f"gate_rebalance: {e}")

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
        # cc#591 fix_2: last-good put-OI bar (same fix as _compute_and_store_pcr), so a heal never
        # skips a day whose close bar had a zeroed put leg.
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
              AND ts = (SELECT oc2.ts FROM option_chain oc2
                        WHERE DATE(oc2.ts) = %(t)s AND oc2.underlying = option_chain.underlying
                        GROUP BY oc2.ts
                        HAVING SUM(CASE WHEN oc2.option_type='PE' THEN oc2.oi ELSE 0 END) > 0
                        ORDER BY oc2.ts DESC LIMIT 1)
            GROUP BY DATE(ts), underlying
            HAVING SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) > 0
            ON CONFLICT (price_date, underlying) DO NOTHING
        """, {"t": target})
        n = cur.rowcount
    conn.commit()
    try:
        import pcr_backfill
        pcr_backfill.mark_pcr_quality(conn)   # cc#745: sanity-gate the healed PCR rows
    except Exception as _qe:
        log.warning(f"pcr quality mark failed: {_qe}")
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
    # cc#417 fix_1: never compute/write adr_daily or pcr_daily on a non-trading day (weekend/holiday) —
    # those phantom 0-rows displaced Friday's real ADR as "latest" and broke the mood gate.
    if not _is_trading_day(today):
        log.debug(f"adr_pcr: skip — {today} is not a trading day")
        return _SKIPPED
    if _adr_pcr_ran_today == today: return _SKIPPED
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
    if not _is_trading_day(today): return _SKIPPED         # cc#417: no retry on non-trading days
    if _adr_pcr_ran_today == today: return _SKIPPED        # already verified-complete
    log.warning("adr_pcr: 15:50 run incomplete — retrying at 16:00")
    _bg_adr_pcr()

_tc_sim_running = False
def _bg_tc_sim_tick():
    """cc#748: hourly TC OUTCOME SIM tick. Scores the futures universe via tc_resolver.get_primary_styles()
    ONLY, opens sim entries for verdict=STRONG cards (+/-3% exits, 5-trading-day TIME cap) and processes
    open-position exits. Trading-day + entry logic live inside run_tc_sim_tick(); single-flight guarded so
    an hourly tick can never overlap a still-running one (universe scoring can be slow). No V8 objects (id=244)."""
    global _tc_sim_running
    if _tc_sim_running:
        return _SKIPPED
    _tc_sim_running = True
    try:
        import tc_sim
        res = tc_sim.run_tc_sim_tick()
        try:
            with _conn() as _c:
                _log_health(_c, "tc_sim_tick",
                            {"opened": len(res.get("opened", [])), "closed": len(res.get("closed", [])),
                             "open": res.get("still_open"), "errors": len(res.get("errors", []))})
        except Exception:
            pass
        log.info(f"tc_sim_tick: opened={len(res.get('opened',[]))} "
                 f"closed={len(res.get('closed',[]))} open={res.get('still_open')}")
        return res
    except Exception as e:
        log.error(f"tc_sim_tick: {e}")
    finally:
        _tc_sim_running = False

def _bg_tc_screener_precompute():
    # task #43: nightly TC screener cache @16:00 IST (after market close + ADR/PCR)
    try:
        import trade_check_v34_endpoints as tce
        res = tce.run_tc_screener_precompute()
        with _conn() as _c:   # cc#255
            _log_health(_c, "tc_screener_precompute",
                        {"rows": res.get("rows") if isinstance(res, dict) else res})
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
        # cc#657: corporate-action cliff guard — any symbol that cliffed in the last few days (fresh
        # split/bonus/demerger that the range=10d UPSERT left un-restated) gets a same-night 5y adjusted
        # re-pull, so the pollution never reopens (TRIVENI cliffed 22-Jul). ops_log = yahoo_ca_restate.
        try:
            cliffs = ydu.detect_cliff_symbols(recent_days=5)
            if cliffs:
                ca = ydu.restate_symbol_history(cliffs, lookback="5y")
                log.info(f"yahoo_ca_restate nightly: restated {len([r for r in ca if r['status']=='restated'])}/{len(cliffs)} {cliffs}")
        except Exception as e:
            log.error(f"yahoo_ca_restate nightly guard: {e}")
        # cc#658 part_3: forward heal — any CA ex_date == today gets a proactive 5y restate this evening,
        # before GVM (01:30). Supersedes reactive-only healing for KNOWN corporate actions.
        try:
            import ca_watchdog
            fh = ca_watchdog.forward_heal()
            if fh.get("symbols"):
                log.info(f"ca forward_heal: {fh['symbols']}")
        except Exception as e:
            log.error(f"ca forward_heal: {e}")
        _yahoo_ran_today = _ist_now().date()
        log.info(f"yahoo_daily: {result} | index_heal: {heal}")
    except Exception as e: log.error(f"yahoo_daily: {e}")
    finally: _yahoo_daily_running = False


def _bg_ca_daily_note():
    """cc#658 part_4: 09:00 IST — CA / data-integrity morning note (written every day)."""
    try:
        import ca_watchdog
        note = ca_watchdog.ca_daily_note()
        log.info(f"ca_daily_note: {note.get('headline')}")
    except Exception as e:
        log.error(f"ca_daily_note: {e}")


def _bg_master_watchdog_note():
    """cc#658 part_5: 09:05 IST — consolidated master watchdog note (written every day)."""
    try:
        import ca_watchdog
        note = ca_watchdog.master_watchdog_note()
        log.info(f"master_watchdog_note [{note.get('status')}]: {note.get('headline')}")
    except Exception as e:
        log.error(f"master_watchdog_note: {e}")


def _bg_ca_sweep_daily():
    """cc#659: DAILY (incl weekends) CA scrape — priority set (CA history + upcoming ex-dates) every day
    plus a rotating universe shard (full coverage <=3 days). Same one/sec NSE discipline."""
    try:
        import ca_watchdog
        s = ca_watchdog.run_ca_sweep_daily()
        log.info(f"ca_sweep_daily [{s.get('mode')}]: {s.get('rows_upserted')} rows over "
                 f"{s.get('symbols_scanned')} symbols, {s.get('nse_misses')} NSE misses")
    except Exception as e:
        log.error(f"ca_sweep_daily: {e}")


def _bg_ca_weekly_scan():
    """cc#658 part_2: Sunday full 5y distortion scan (cliff x corporate_actions; auto-restate artefacts)."""
    try:
        import ca_watchdog
        s = ca_watchdog.weekly_distortion_scan()
        log.info(f"ca_weekly_scan: {s.get('cliff_events')} cliffs, {len(s.get('artefact_symbols') or [])} artefacts, "
                 f"{len(s.get('genuine_crash_flags') or [])} genuine flags")
    except Exception as e:
        log.error(f"ca_weekly_scan: {e}")


def _bg_weekly_ops_metrics_queue():
    """cc#667: Saturday 06:10 IST — DETECT symbols needing sector-KPI extraction and QUEUE them into
    ops_metrics_t1_queue. The scheduler ONLY detects + queues; CC drains the queue in-session per the
    MAX_SUBSCRIPTION rule (id=8737) using the cc#648 doctrine (literal figures + verbatim source quotes).
    Worklist = symbols with a STORED investor doc that is either uncovered (no sector_ops_metrics row
    yet) or FRESHER than the symbol's latest extraction (a new doc landed since) — minus the
    ops_metrics_honest_absent registry (id=8859 seed) and anything already queued 'pending'. Emits
    ops_log(ops_metrics, WEEKLY_OPS_METRICS_QUEUE) with the newly-queued count + pending depth."""
    try:
        # cc#667: restrict to KPI-taxonomy sectors (those with a SPECIFIC registry, not the generic
        # OTHER_SECTORS) via the same _infer_sector map the extraction uses — so the queue never fills
        # with Diversified_Others generic-only names that have no sector KPI to extract.
        try:
            import ops_metrics_pipeline as _omp
            _specific = set(_omp.SECTOR_REGISTRY_SEED.keys())
        except Exception:
            _omp, _specific = None, set()
        from scrape_universe import UNIVERSE_CTE as _UNIVERSE_CTE   # cc#774: one canonical definition
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS ops_metrics_honest_absent (
                symbol TEXT PRIMARY KEY, sector TEXT,
                reason TEXT DEFAULT 'doc read, no printed registry KPI',
                reviewed_at TIMESTAMPTZ DEFAULT NOW())""")
            cur.execute("""
              WITH latest_doc AS (
                SELECT symbol, MAX(fetched_at) AS fa FROM doc_texts
                WHERE extract_status='stored' AND char_count>0 GROUP BY symbol),
              latest_kpi AS (
                SELECT symbol, MAX(created_at) AS ca FROM sector_ops_metrics GROUP BY symbol)
              SELECT d.symbol FROM latest_doc d
              LEFT JOIN latest_kpi k ON k.symbol=d.symbol
              WHERE (k.ca IS NULL OR d.fa > k.ca)
                AND d.symbol NOT IN (SELECT symbol FROM ops_metrics_honest_absent)
                AND d.symbol NOT IN (SELECT symbol FROM ops_metrics_t1_queue WHERE status='pending')
              ORDER BY d.symbol""")
            cand = [r[0] for r in cur.fetchall()]
            # cc#773 CATCH-UP, cc#774 UNIVERSE-BOUNDED + DOC-GAP CLOSING. Queues validated recent
            # reporters (cc#765 gate) with no ops row, LARGEST MCAP FIRST.
            # cc#774 fix a: the cc#773 version had NO universe join, so it could queue out-of-universe
            # symbols — a HARD_BOUNDARY (id=10260) violation. Now INNER JOINed to the canonical scrape
            # universe, the single definition shared with the coverage counter (scrape_universe.py).
            # cc#774 fix b: it also required a STORED DOC, which made the real gap unreachable —
            # run_t1_refresh only stages docs on a 3-DAY lookback, so any in-universe reporter that
            # missed that window had no doc, was therefore never queued, and stayed invisible forever
            # (NTPCGREEN 22-Jul, THANGAMAYL/MAHSCOOTER/VGUARD 29-Jul, JBMA 30-Jul). The doc requirement
            # is dropped: a queued no-doc symbol is exactly what run_t1_refresh needs to go stage it,
            # while symbols that already have a doc flow on to extraction as before.
            cur.execute("""
              WITH """ + _UNIVERSE_CTE + """,
              reporters AS (
                SELECT DISTINCT UPPER(e.ticker) AS symbol FROM earnings_calendar e
                JOIN scrape_universe u ON u.symbol = UPPER(e.ticker)
                WHERE e.status='reported' AND e.verified <> 'false'
                  AND e.ex_date >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date - 21
                  AND e.event_type IN ('Quarterly Result','Financial Results')),
              covered AS (SELECT DISTINCT UPPER(symbol) AS symbol FROM sector_ops_metrics
                          WHERE created_at >= NOW() - INTERVAL '21 days')
              SELECT r.symbol FROM reporters r
              LEFT JOIN covered c ON c.symbol = r.symbol
              LEFT JOIN screener_raw s ON UPPER(s.nse_code) = r.symbol
              WHERE c.symbol IS NULL
                AND r.symbol NOT IN (SELECT UPPER(symbol) FROM ops_metrics_honest_absent)
                AND r.symbol NOT IN (SELECT UPPER(symbol) FROM ops_metrics_t1_queue WHERE status='pending')
              ORDER BY s.market_cap::numeric DESC NULLS LAST""")
            catchup = [r[0] for r in cur.fetchall()]
            for s in catchup:
                if s not in cand:
                    cand.append(s)
            # taxonomy filter: keep only symbols whose segment maps to a specific-KPI sector
            if _omp and cand:
                cur.execute("SELECT DISTINCT symbol, segment FROM gvm_scores WHERE segment IS NOT NULL")
                seg = {r[0]: r[1] for r in cur.fetchall()}
                work = [s for s in cand if _omp._infer_sector(seg.get(s)) in _specific]
            else:
                work = cand
            # cc#773 BUGFIX: ops_metrics_t1_queue.ex_date is NOT NULL, but this insert only ever supplied
            # (symbol, queued_at, status) — so EVERY queue attempt raised and the whole cc#667 detector
            # silently queued nothing (queue depth sat at 0 while 214 reporters went uncovered). Supply
            # the symbol's latest validated reported ex_date (fallback today), and make it conflict-safe.
            for sym in work:
                cur.execute("""INSERT INTO ops_metrics_t1_queue (symbol, ex_date, queued_at, status)
                    VALUES (%s, COALESCE((SELECT MAX(e.ex_date) FROM earnings_calendar e
                                          WHERE UPPER(e.ticker)=%s AND e.status='reported'
                                            AND e.verified <> 'false'),
                                         (NOW() AT TIME ZONE 'Asia/Kolkata')::date), NOW(), 'pending')
                    ON CONFLICT (symbol, ex_date) DO NOTHING""", (sym, sym))
            cur.execute("SELECT COUNT(*) FROM ops_metrics_t1_queue WHERE status='pending'")
            depth = cur.fetchone()[0]
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'ops_metrics', 'WEEKLY_OPS_METRICS_QUEUE', %s)""",
                        (Json({"newly_queued": len(work), "queue_depth_pending": depth,
                               "symbols": work[:80]}),))
            conn.commit()
        log.info(f"weekly_ops_metrics_queue: +{len(work)} queued, pending depth={depth}")
    except Exception as e:
        log.error(f"weekly_ops_metrics_queue: {e}")


# cc#773 OPS-METRICS SEASON MODE: through 15-Aug-2026 the queue-filler runs at DOUBLE pace (daily
# instead of Saturday-only) because peak results season outruns the weekly burst. Auto-reverts after.
OPS_SEASON_MODE_UNTIL = date(2026, 8, 15)


def _ops_season_mode(today=None):
    return (today or _ist_now().date()) <= OPS_SEASON_MODE_UNTIL


def _bg_ops_metrics_coverage():
    """cc#773 + cc#774 DENOMINATOR FIX: nightly DUAL coverage counter, measured against the SCRAPE
    UNIVERSE ONLY (top-750 NSE non-numeric by mcap UNION sector_ops_metrics symbols;
    SCRAPE_UNIVERSE_TOP500_NSE_V1 id=9178 + HARD_BOUNDARY id=10260), 21-day window.

    cc#774: the cc#773 version measured against ALL of screener_raw (~1801), so it read ~50% coverage
    and would have fired a false <80% alert every night. Out-of-universe reporters are NOT a gap — the
    cc#700 boss rule deliberately skips them. Against the correct denominator, real coverage is ~97%.
    Per UNIVERSE_DENOMINATOR_RULE (session_log id=207): the denominator + window are now emitted IN the
    payload, so no number from this counter is ever quotable without them."""
    try:
        from scrape_universe import UNIVERSE_CTE
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
              WITH """ + UNIVERSE_CTE + """,
              reporters AS (
                SELECT DISTINCT UPPER(e.ticker) AS symbol FROM earnings_calendar e
                JOIN scrape_universe u ON u.symbol = UPPER(e.ticker)
                WHERE e.status='reported' AND e.verified <> 'false'
                  AND e.ex_date >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date - 21
                  AND e.event_type IN ('Quarterly Result','Financial Results')),
              -- cc#774: a symbol with an ops row OR an honest-absent row is RESOLVED, not a gap. The
              -- cc#773 version counted honest-absent as missing, so a doc-read-but-no-printed-KPI
              -- company (OBEROIRLTY, IRB) dragged coverage down permanently and could re-trip the alert.
              covered AS (SELECT DISTINCT UPPER(symbol) AS symbol FROM sector_ops_metrics
                          WHERE created_at >= NOW() - INTERVAL '21 days'
                          UNION SELECT UPPER(symbol) FROM ops_metrics_honest_absent),
              missing AS (SELECT r.symbol FROM reporters r LEFT JOIN covered c ON c.symbol=r.symbol
                          WHERE c.symbol IS NULL)
              SELECT (SELECT count(*) FROM reporters),
                     (SELECT count(*) FROM reporters r JOIN covered c ON c.symbol=r.symbol),
                     (SELECT count(*) FROM missing),
                     (SELECT count(*) FROM missing m WHERE EXISTS
                        (SELECT 1 FROM doc_texts d WHERE UPPER(d.symbol)=m.symbol AND d.extract_status='stored')),
                     (SELECT count(*) FROM missing m WHERE EXISTS
                        (SELECT 1 FROM ops_metrics_honest_absent h WHERE UPPER(h.symbol)=m.symbol)),
                     (SELECT count(*) FROM ops_metrics_t1_queue WHERE status='pending'),
                     (SELECT count(*) FROM scrape_universe)
            """)
            tot, cov, miss, miss_with_doc, miss_absent, qdepth, uni_n = cur.fetchone()
            scraped_pct = round(cov / tot * 100, 1) if tot else None
            # polished-pct: rows carrying a polished/narrative field (the Claude-web job's output)
            try:
                cur.execute("""SELECT count(DISTINCT UPPER(symbol)) FROM sector_ops_metrics
                               WHERE created_at >= NOW() - INTERVAL '21 days'
                                 AND notes IS NOT NULL AND length(notes) > 0""")
                pol = cur.fetchone()[0]
            except Exception:
                conn.rollback(); pol = None
            polished_pct = round(pol / tot * 100, 1) if (tot and pol is not None) else None
            # cc#774 / UNIVERSE_DENOMINATOR_RULE: denominator + window travel WITH the numbers, always.
            payload = {"denominator": "scrape_universe (top-750 NSE non-numeric by mcap UNION "
                                      "sector_ops_metrics symbols; id=9178 + id=10260)",
                       "universe_size": uni_n, "window_days": 21,
                       "reporters_21d": tot, "with_ops_rows": cov, "scraped_pct": scraped_pct,
                       "polished": pol, "polished_pct": polished_pct,
                       "missing": miss, "missing_with_stored_doc": miss_with_doc,
                       "missing_no_doc": (miss - miss_with_doc - miss_absent) if miss is not None else None,
                       "honest_absent": miss_absent, "queue_pending": qdepth,
                       "season_mode": _ops_season_mode()}
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'ops_metrics', 'OPS_METRICS_COVERAGE', %s)""",
                        (Json(payload),))
            if scraped_pct is not None and scraped_pct < 80:
                cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                               VALUES (CURRENT_DATE, NOW(), 'alert', 'OPS_METRICS_COVERAGE_LOW', %s)""",
                            (Json({**payload, "message":
                                   f"ops-metrics coverage {scraped_pct}% < 80% of {tot} in-universe reporters "
                                   f"(21d, universe {uni_n}) — {miss} missing "
                                   f"({miss_with_doc} have a stored doc and are queueable; the rest need doc staging)"}),))
            conn.commit()
        log.info(f"ops_metrics_coverage: {payload}")
    except Exception as e:
        log.error(f"_bg_ops_metrics_coverage: {e}")


_ops_polish_detect_running = False
_ops_polish_detect_ran_today = None
_POLISH_CARD_TITLE = "POLISH_QUEUE — ops-metrics forward-fill (auto-maintained, cc#736)"

def _bg_ops_polish_detector(scheduled=False):
    """cc#736 POLISH_WORKER_V1 — app-side DETECTOR (zero AI, pure SQL). Maintains ONE standing cc_task
    'POLISH_QUEUE' card listing symbols whose stored doc_texts are newer than / uncovered by
    sector_ops_metrics (the forward-fill backlog cc#733 deferred). CC drains the card IN-SESSION
    (loop-until-empty) using Opus under Max — reads each symbol's doc windows, applies the RAW_DATA
    hard_rules, writes sector_ops_metrics via run_sql — so there is ZERO API spend here; the scheduler
    only detects and refreshes the card. Triggers: the daily sweep (scheduled=True, day-locked) AND
    app_config ops_polish_run='run_now' (on-demand, single-flight like cc#734). Detection signal mirrors
    cc#667: a stored investor doc fresher than the symbol's latest extraction OR uncovered, minus the
    honest-absent registry, restricted to specific-KPI sectors. cc#732/733 already-covered
    symbol-quarters are skipped by CC at extraction time (spec instructs check-existing-rows-first)."""
    global _ops_polish_detect_running, _ops_polish_detect_ran_today
    if _ops_polish_detect_running:
        return _SKIPPED
    today = _ist_now().date()
    on_demand = False
    if not scheduled:
        # every-tick path: act ONLY on an explicit on-demand flag (no-op otherwise, cheap flag read)
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key='ops_polish_run'")
                row = cur.fetchone()
            on_demand = bool(row and row[0] == 'run_now')
        except Exception:
            on_demand = False
        if not on_demand:
            return _SKIPPED
    else:
        if _ops_polish_detect_ran_today == today:
            return _SKIPPED
    _ops_polish_detect_running = True
    try:
        import ops_metrics_pipeline as _omp
        try:
            _specific = set(_omp.SECTOR_REGISTRY_SEED.keys())
        except Exception:
            _omp, _specific = None, set()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
              WITH latest_doc AS (
                SELECT symbol, MAX(fetched_at) AS fa FROM doc_texts
                WHERE extract_status='stored' AND char_count>0 GROUP BY symbol),
              latest_kpi AS (
                SELECT symbol, MAX(created_at) AS ca FROM sector_ops_metrics GROUP BY symbol)
              SELECT d.symbol FROM latest_doc d
              LEFT JOIN latest_kpi k ON k.symbol=d.symbol
              WHERE (k.ca IS NULL OR d.fa > k.ca)
                AND d.symbol NOT IN (SELECT symbol FROM ops_metrics_honest_absent)
              ORDER BY d.symbol""")
            cand = [r[0] for r in cur.fetchall()]
            if _omp and _specific and cand:
                cur.execute("SELECT DISTINCT symbol, segment FROM gvm_scores WHERE segment IS NOT NULL")
                seg = {r[0]: r[1] for r in cur.fetchall()}
                work = [s for s in cand if _omp._infer_sector(seg.get(s)) in _specific]
            else:
                work = cand
            spec = Json({
                "worker": "POLISH_WORKER_V1", "cc": 736,
                "loop_directive": "LOOP_UNTIL_EMPTY — process a batch, write rows, ops_log, immediately "
                    "pull the next batch in the SAME session until this list is exhausted; checkpoint the "
                    "cursor to ops_log after every batch so an interrupted session resumes cleanly.",
                "description": "Ops-metrics forward-fill backlog. For each symbol: read its stored "
                    "doc_texts, extract sector-KPI registry figures per RAW_DATA_RULE (id=10079: literal "
                    "figures only, never synthesize; content vintage id=9897 decides the quarter, NOT the "
                    "doc date label; canonical metric_names cc#685; source_1 = deck name + month; HIGH "
                    "confidence only for verbatim figures). Missing KPI -> ops_metrics_honest_absent "
                    "(metric-absent only). Skip symbol-quarters already covered (check existing rows). "
                    "Write via run_sql, ops_log per batch.",
                "symbols": work, "count": len(work), "detected_at": _ist_now().isoformat(), "cursor": 0,
            })
            if work:
                cur.execute("UPDATE cc_tasks SET spec=%s, "
                            "status=CASE WHEN status='in_progress' THEN 'in_progress' ELSE 'pending' END, "
                            "priority='high', category='polish_queue' WHERE category='polish_queue'", (spec,))
                if cur.rowcount == 0:
                    cur.execute("INSERT INTO cc_tasks (title, spec, status, priority, category, created_by) "
                                "VALUES (%s, %s, 'pending', 'high', 'polish_queue', 'scheduler')",
                                (_POLISH_CARD_TITLE, spec))
            else:
                # nothing to polish -> settle the standing card to done (unless CC is mid-drain)
                cur.execute("UPDATE cc_tasks SET spec=%s, "
                            "status=CASE WHEN status='in_progress' THEN 'in_progress' ELSE 'done' END, "
                            "category='polish_queue' WHERE category='polish_queue'", (spec,))
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'ops_metrics', 'OPS_POLISH_DETECT', %s)""",
                        (Json({"trigger": "run_now" if on_demand else "scheduled",
                               "candidates": len(work), "symbols": work[:80], "ist": _ist_now().isoformat()}),))
            if on_demand:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('ops_polish_run','done',NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value='done', updated_at=NOW()")
            conn.commit()
        if scheduled:
            _ops_polish_detect_ran_today = today
        log.info(f"ops_polish_detector ({'run_now' if on_demand else 'scheduled'}): {len(work)} candidates queued")
    except Exception as e:
        log.error(f"ops_polish_detector: {e}")
        if on_demand:
            try:
                with _conn() as conn, conn.cursor() as cur:
                    cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('ops_polish_run','failed',NOW()) "
                                "ON CONFLICT (key) DO UPDATE SET value='failed', updated_at=NOW()")
                    conn.commit()
            except Exception:
                pass
    finally:
        _ops_polish_detect_running = False


def _bg_gvm():
    global _gvm_ran_today
    today = _ist_now().date()
    if _gvm_ran_today == today: return _SKIPPED
    try:
        import gvm_nightly
        with _conn() as conn:
            gvm_nightly.gvm_recompute(conn)
            _log_health(conn, "gvm_recompute", {"date": str(today)})  # cc#255
        _gvm_ran_today = today
        log.info("gvm_recompute done")
        # cc#824: predefined screens recompute HERE, immediately after the GVM scores they screen on
        # land — chained rather than given its own wall-clock slot, because a fixed time would race
        # the very job it depends on and quietly screen yesterday's scores on a night GVM ran late.
        # Guarded: a screener failure must never mark the GVM run bad.
        try:
            import screeners_eod
            with _conn() as sconn:
                res = screeners_eod.run_all(sconn)
            log.info("cc#824 screeners: %s", res.get("screens"))
        except Exception as e:
            log.error(f"cc#824 screeners_eod: {e}")
    except Exception as e: log.error(f"gvm: {e}")

def _bg_gvm_backfill():
    """cc#468/470: 5yr daily GVM deep-history reconstruction (futures-first, then
    top-500 by mcap). History-only, resumable, checkpointed. Runs OFF-MARKET only
    (heavy multi-hour job) and self-limits per invocation; next off-market tick
    resumes via the app_config checkpoint. Gated by app_config gvm_backfill_run:
    'pending'/'running' -> run; 'done' -> skip. Single-flight via a run guard."""
    global _gvm_backfill_running
    if _gvm_backfill_running:
        return
    now = _ist_now()
    if _is_market_hours(now):   # never compete with the live 5-min path
        return _SKIPPED
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='gvm_backfill_run'")
            r = cur.fetchone()
        flag = (r[0] if r else None)
        if flag == "done":
            return _SKIPPED
        _gvm_backfill_running = True
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_config (key, value, updated_at) VALUES ('gvm_backfill_run','running',NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value='running', updated_at=NOW()")
            conn.commit()
        import gvm_backfill
        res = gvm_backfill.run_backfill(time_budget_s=10800)   # 3h/invocation, resume next tick
        log.info(f"gvm_backfill run: {res}")
        # if it stopped on budget/max, leave flag 'running' -> next off-market tick resumes.
        if not res.get("complete"):
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_config (key, value, updated_at) VALUES ('gvm_backfill_run','pending',NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
    except Exception as e:
        log.error(f"gvm_backfill: {e}")
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_config (key, value, updated_at) VALUES ('gvm_backfill_run','pending',NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
        except Exception:
            pass
    finally:
        _gvm_backfill_running = False

def _bg_gvm_backfill_ext():
    """cc#471: extension backfill — ALL 5yr-deep symbols minus the already-backfilled
    top-500. Same engine (run_backfill_ext), separate checkpoint/flag
    (gvm_backfill_ext_run). Shares the _gvm_backfill_running single-flight guard so it
    never overlaps the main run; only starts once the main run flag is 'done'
    (sequence). Off-market only, self-limiting, resumes across nights."""
    global _gvm_backfill_running
    if _gvm_backfill_running:
        return
    now = _ist_now()
    if _is_market_hours(now):
        return _SKIPPED
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_config WHERE key IN ('gvm_backfill_run','gvm_backfill_ext_run')")
            flags = {k: v for k, v in cur.fetchall()}
        if flags.get('gvm_backfill_run') != 'done':   # sequence: main run first
            return _SKIPPED
        if flags.get('gvm_backfill_ext_run') == 'done':
            return _SKIPPED
        _gvm_backfill_running = True
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_config (key, value, updated_at) VALUES ('gvm_backfill_ext_run','running',NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value='running', updated_at=NOW()")
            conn.commit()
        import gvm_backfill
        res = gvm_backfill.run_backfill_ext(time_budget_s=10800)
        log.info(f"gvm_backfill_ext run: {res}")
        if not res.get("complete"):
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_config (key, value, updated_at) VALUES ('gvm_backfill_ext_run','pending',NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
    except Exception as e:
        log.error(f"gvm_backfill_ext: {e}")
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app_config (key, value, updated_at) VALUES ('gvm_backfill_ext_run','pending',NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
        except Exception:
            pass
    finally:
        _gvm_backfill_running = False

def _bg_bt14_fut_oi():
    """cc#538: probe-gated ORB basis/OI RESEARCH backfill into the DROPPABLE scratch table
    bt14_fut_oi (top-200 stock-futures OHLC+OI for the June/July ORB window, basis derived
    vs bt14_bars spot). Off-market only, single-flight. Flag app_config.bt14_fut_oi_run:
      'probe'    -> run the Step-0 hard-gate probe, log BT14_FUT_OI_PROBE, set 'probe_done'
      'backfill' -> resumable chunk (cursor bt14_fut_oi_cursor); gated on last probe verdict
                    (!=none). Stays 'backfill' until complete, then 'done'.
      else       -> skip. Touches nothing live; ORB logic unaffected."""
    global _bt14_fut_oi_running
    if _bt14_fut_oi_running:
        return
    if _is_market_hours(_ist_now()):   # never fetch/compete during market hours
        return _SKIPPED
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='bt14_fut_oi_run'")
            r = cur.fetchone()
        flag = (r[0] if r else None)
        if flag not in ("probe", "backfill"):
            return _SKIPPED
        _bt14_fut_oi_running = True
        import bt14_fut_oi
        if flag == "probe":
            # pace retries: skip if a probe ran within the last 30 min. An expired Fyers
            # token (weekends: no autologin) yields verdict=auth_error; we keep 'probe'
            # armed and retest ~half-hourly until a fresh token exists (Mon pre-market),
            # rather than spamming Fyers auth every 60s tick.
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - MAX(session_ts))) "
                            "FROM ops_log WHERE title='BT14_FUT_OI_PROBE'")
                age = cur.fetchone()[0]
            if age is not None and age < 1800:
                return _SKIPPED
            res = bt14_fut_oi.run_probe()
            verdict = res.get("verdict")
            log.info(f"bt14_fut_oi probe: verdict={verdict}")
            # AUTO MODE self-drive: auth_error => keep 'probe' (retest when a token exists);
            # OI available (both/july_only/june_only) => auto-arm 'backfill' so the pipeline
            # completes unattended; genuine 'none' => latch 'probe_done' (valid finding, no
            # backfill). The backfill is safe to auto-run: scratch-only, disk-guarded, resumable.
            if verdict == "auth_error":
                nxt = "probe"
            elif verdict in ("both", "july_only", "june_only"):
                nxt = "backfill"
            else:   # 'none'
                nxt = "probe_done"
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('bt14_fut_oi_run',%s,NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()", (nxt,))
                conn.commit()
        else:
            res = bt14_fut_oi.run_backfill(time_budget_s=1800)
            log.info(f"bt14_fut_oi backfill: {res}")
            nxt = "done" if res.get("complete") else "backfill"
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('bt14_fut_oi_run',%s,NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()", (nxt,))
                conn.commit()
    except Exception as e:
        log.error(f"bt14_fut_oi: {e}")
    finally:
        _bt14_fut_oi_running = False

def _bg_yahoo_new_listings():
    """cc#539: one-shot Yahoo EOD backfill for newly-listed symbols that have GVM but ZERO
    raw_prices (momentum pillar dark). Flag app_config.yahoo_new_listings_run='pending'
    triggers a single run over the JSON spec list in app_config.yahoo_new_listings_specs
    ([{nse,bse,isin,name}]); tries NSE.NS then BSE.BO, full history, UPSERT raw_prices,
    graceful-skips non-listed InvITs. Sets flag 'done' after. Any time (Yahoo EOD),
    single-flight. Nightly 01:30 GVM then activates the M pillar off the new bars."""
    global _yahoo_new_listings_running
    if _yahoo_new_listings_running:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='yahoo_new_listings_run'")
            r = cur.fetchone()
        if (r[0] if r else None) != "pending":
            return _SKIPPED
        _yahoo_new_listings_running = True
        import json as _json
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='yahoo_new_listings_specs'")
            r = cur.fetchone()
        specs = _json.loads(r[0]) if (r and r[0]) else []
        import yahoo_daily_update
        rep = yahoo_daily_update.backfill_new_listings(specs, lookback="5y")
        log.info(f"yahoo_new_listings: {rep}")
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) "
                        "VALUES ('yahoo_new_listings_run','done',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='done', updated_at=NOW()")
            conn.commit()
    except Exception as e:
        log.error(f"yahoo_new_listings: {e}")
    finally:
        _yahoo_new_listings_running = False

def _bg_pivots():
    global _pivots_ran_today
    today = _ist_now().date()
    if _pivots_ran_today == today: return _SKIPPED
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

def _bg_universe_pivots():
    """cc#342: nightly full-universe (~1720+) rolling-5d pivot rebuild into v8_paper_pivots.
    The 01:45 _bg_pivots only covers the 211 futures symbols, so GVM companies outside it
    showed a stale pivot_date on the CIO Pivot Range card (e.g. 2026-06-13). This refreshes
    the whole GVM universe so that card (which reads v8_paper_pivots latest) is always current.
    Same table + ON CONFLICT (symbol, pivot_date) upsert — idempotent, complements _bg_pivots."""
    global _upivots_ran_today
    today = _ist_now().date()
    if _upivots_ran_today == today: return _SKIPPED
    try:
        import gvm_universe_pivots
        res = gvm_universe_pivots.compute_universe_pivots(today)
        built = res.get("built", 0) if isinstance(res, dict) else 0
        with _conn() as conn:
            _log_health(conn, "universe_pivots_build",
                        {"date": str(today), "status": "ok" if built else "empty", "built": built})
        if built:
            _upivots_ran_today = today
        else:
            _log_alert("universe_pivots_missing", f"universe pivots built 0 rows for {today} at 01:47")
        log.info(f"universe pivots built: {res}")
    except Exception as e:
        log.error(f"universe_pivots: {e}")
        _log_alert("universe_pivots_error", f"universe pivot build failed for {today}: {e}")

def _bg_universe_technicals():
    """cc#154: nightly RSI/DMA/returns/pivots for the full ~1766 GVM universe
    (not just the 209 futures_universe symbols v8_metrics covers). Scheduled
    after GVM (01:30) since it reads gvm_scores for its symbol list.
    NOTE: this tasks spec said "22:10 IST after GVM 22:00" but GVM was moved to
    01:30 IST on 18-Jun-2026 (task #31, comment above _bg_gvm) - scheduled here
    in the real 01:00-02:00 nightly chain instead, after the last existing job."""
    global _ut_ran_today
    today = _ist_now().date()
    if _ut_ran_today == today: return _SKIPPED
    try:
        import universe_technicals
        with _conn() as conn:
            res = universe_technicals.run_universe_technicals(conn, today)
            _log_health(conn, "universe_technicals",  # cc#255: success-path health write
                        {"date": str(today), "rows": res.get("rows") if isinstance(res, dict) else None})
        _ut_ran_today = today
        log.info(f"universe_technicals: {res}")
    except Exception as e:
        log.error(f"universe_technicals: {e}")
        _log_alert("universe_technicals_error", f"nightly run failed for {today}: {e}")


_rvol_ran_today: Optional[date] = None   # cc#674: RVOL profiles nightly guard


def _bg_rvol_profiles():
    """cc#674: ~02:10 IST (after universe_technicals) — rebuild the per-slot average cumulative-volume
    profiles used by time-of-day-adjusted RVOL. Day-locked; ~212 symbols × ~75 slots."""
    global _rvol_ran_today
    today = _ist_now().date()
    if _rvol_ran_today == today:
        return _SKIPPED
    try:
        import rvol_engine
        with _conn() as conn:
            res = rvol_engine.build_profiles(conn)
        _rvol_ran_today = today
        log.info(f"rvol_profiles: {res}")
    except Exception as e:
        log.error(f"rvol_profiles: {e}")


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

def _bg_oi_snapshot():
    """cc#445 fix_4: persist today's ATM call/put OI per F&O underlying (post-open + EOD) so the
    Derivative Cockpit ATM OI d/d has a real prior-day snapshot to diff against."""
    try:
        import deriv_metrics
        with _conn() as conn:
            res = deriv_metrics.snapshot_all_atm_oi(conn)
        log.info(f"oi_snapshot: {res}")
    except Exception as e:
        log.error(f"oi_snapshot: {e}")


def _bg_nse_eod_ingest():
    """cc#517: NSE EOD ingest suite -- delivery, FII/DII cash, participant-wise OI, F&O ban list.
    Scheduled 18:30 IST trading days with retries at 19:30/20:30 (NSE's own publication windows
    drift; idempotent upserts make re-running safe -- a retry just re-fills whatever the first
    pass found not-yet-published)."""
    try:
        if not _is_trading_day(_ist_now().date()):
            return _SKIPPED
        import nse_eod_ingest
        res = nse_eod_ingest.run_nightly()
        log.info(f"nse_eod_ingest: {res}")
    except Exception as e:
        log.error(f"nse_eod_ingest: {e}")


def _bg_fo_eod():
    """cc#682: ~23:00 IST weekdays — ingest the day's NSE F&O bhavcopy for per-contract EOD open
    interest; COALESCE-fills the day-end futures_basis OI where the live DEPTH feed missed it (permanent
    OI fallback) + cross-checks vs the live value. Trading days only."""
    now = _ist_now()
    if not _is_trading_day(now.date()):
        return _SKIPPED
    try:
        import nse_fo_eod
        res = nse_fo_eod.run_nightly()
        log.info(f"fo_eod: {res}")
    except Exception as e:
        log.error(f"fo_eod: {e}")


def _bg_fo_ban_fetch():
    """cc#677: daily F&O ban-list (MWPL) fetch ~08:45 IST so the TC ban ALERT is current. The ban is
    alert-only now (zero-veto), but must reflect today's NSE list — a stale list makes the alert blind.
    Trading days only; idempotent per-date replace via upsert_fo_ban."""
    now = _ist_now()
    if not _is_trading_day(now.date()):
        return _SKIPPED
    try:
        import nse_eod_ingest as nse
        sess = nse._nse_session()
        rows = nse.fetch_fo_ban(sess, now.date())
        with _conn() as conn, conn.cursor() as cur:
            nse._ensure_fo_ban_table(cur)
            nse.upsert_fo_ban(cur, now.date(), rows)
            conn.commit()
        log.info(f"fo_ban_fetch: {len(rows)} banned for {now.date()}")
    except Exception as e:
        log.error(f"fo_ban_fetch: {e}")


# cc#660 FEED_GUARDIAN_V1: the five legacy feed watchdogs (feed_staleness_watch cc#475,
# feed_incident_relay cc#501, open_bars_alarm cc#229, oi_feed_health cc#515, writer-no-bars
# reaction) are consolidated into feed_guardian.py — ONE detect->repair owner. These thin
# wrappers dispatch its ticks on the scheduler pool. engine_watchdog / scheduler master /
# CA watchdog stay separate (different domains).
def _bg_guardian_tick():
    """cc#660: 5-min market-hours per-leg detect->repair (resubscribe->restart, Telegram only on
    repair FAIL). Replaces open_bars_alarm + feed_staleness_watch + intraday incident relay."""
    try:
        import feed_guardian
        res = feed_guardian.guardian_tick()
        if res.get("actions"):
            log.info(f"feed_guardian tick: legs={res.get('legs')} actions={res.get('actions')}")
    except Exception as e:
        log.error(f"guardian_tick: {e}")


def _bg_guardian_offhours():
    """cc#660: 15-min ALL-DAYS worker-liveness tick (heartbeat >45min -> Telegram) + off-hours
    incident relay. Weekend worker death becomes a <=45-min alert, not a 13h discovery."""
    try:
        import feed_guardian
        res = feed_guardian.offhours_liveness_tick()
        if res.get("alerted"):
            log.error(f"feed_guardian offhours: worker silent {res.get('heartbeat_age_min')}min")
    except Exception as e:
        log.error(f"guardian_offhours: {e}")


def _bg_guardian_eod_oi():
    """cc#660: post-close (16:15) universe-wide OI-degradation tripwire (cc#515 parity)."""
    try:
        import feed_guardian
        res = feed_guardian.eod_oi_check()
        log.info(f"feed_guardian eod_oi: {res}")
    except Exception as e:
        log.error(f"guardian_eod_oi: {e}")


_v14_running = False

def _bg_v14_cycle():
    """cc#442: one V14 intraday paper cycle (manage exits + evaluate 3 setups + paper-open triggers).
    Non-reentrant; read-only on V8/V10 engine state."""
    global _v14_running
    if _v14_running:
        return
    _v14_running = True
    try:
        import v14_engine
        with _conn() as conn:
            res = v14_engine.run_v14_cycle(conn)
        if res.get("status") == "ok":
            log.info(f"v14_cycle: slots={res.get('open_slots_used')} opened={len(res.get('opened', []))} "
                     f"exits={res.get('exits', {}).get('closed')}")
    except Exception as e:
        log.error(f"v14_cycle: {e}")
    finally:
        _v14_running = False


def _bg_qb_eod():
    # cc#439 fix_2: this job used to call qb_eod_checker.run_eod_check(conn) — a function that does
    # NOT exist (the real one is run_eod_checker(conn, basket_name=...)) — so it threw AttributeError
    # every day (swallowed), which is why QB exit alerts (HS1/HS2) were NEVER processed. Now it runs
    # the correct checker for EVERY active basket, and fix_1: when a basket's next_rebalance is due it
    # runs the scheduled rebalance (exits + residual + advance next_rebalance + log). Trading-day guarded.
    global _qb_eod_ran_today, _qb_eod_running
    today = _ist_now().date()
    if _qb_eod_ran_today == today or _qb_eod_running: return _SKIPPED
    if not _is_trading_day(today): return _SKIPPED
    _qb_eod_running = True
    try:
        import qb_eod_checker, qb_rebalance
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT basket_name, next_rebalance FROM quant_basket_registry "
                            "WHERE is_active=TRUE ORDER BY basket_name")
                baskets = [(r[0], r[1]) for r in cur.fetchall()]
            # cc#838 step_3: SEED BEFORE REBALANCE. An active registry basket that has never held a
            # position is not in a rebalance situation — it has never started. run_scheduled_rebalance
            # deliberately never buys (its docstring is explicit), so a never-seeded basket would keep
            # logging zero-entry rows and advancing its cadence forever. qb_seed enumerates from the
            # registry and discovers each basket's selector by convention, so a new registry row
            # participates with no code change here.
            try:
                import qb_seed
                seeded = qb_seed.seed_pending(conn)
                if seeded.get("seeded"):
                    log.info("qb_eod: seeded %s", seeded)
            except Exception as e:
                log.error(f"qb_eod seed pass: {e}")
            checked = 0; rebalanced = []
            for name, next_reb in baskets:
                try:
                    if next_reb and next_reb <= today:
                        qb_rebalance.run_scheduled_rebalance(conn, name)
                        rebalanced.append(name); checked += 1
                    else:
                        qb_eod_checker.run_eod_checker(conn, basket_name=name); checked += 1
                except Exception as e:
                    log.error(f"qb_eod {name}: {e}")
            # cc#839: append today's NAV point for every basket in the SAME pass (spec: one loop,
            # no second job). Runs after the marks so it reads the freshest position values.
            nav_out = None
            try:
                import qb_nav
                nav_out = qb_nav.persist_all(conn)
            except Exception as e:
                log.error(f"qb_eod nav pass: {e}")
            _log_health(conn, "qb_eod", {"checked": checked, "rebalanced": rebalanced,
                                         "nav": (nav_out or {}).get("baskets")})  # cc#255
        log.info(f"qb_eod: {checked} baskets checked, rebalanced={rebalanced}")
        _qb_eod_ran_today = today
    except Exception as e: log.error(f"qb_eod: {e}")
    finally: _qb_eod_running = False

def _bg_fu_sync():
    global _fu_sync_ran_this_week
    today = _ist_now().date()
    if _fu_sync_ran_this_week == today: return _SKIPPED
    try:
        import fyers_sync
        with _conn() as conn:
            fyers_sync.sync_futures_universe(conn)
            _log_health(conn, "fu_sync", {"date": str(today)})  # cc#255
        # cc#314: the lot_size audit moved to its own NIGHTLY job (_bg_lot_sync, ~01:05 IST)
        # so an NSE lot revision at expiry is picked up within ~1 day, not up to 6 (Monday-only).
        # Membership add/remove stays weekly here.
        _fu_sync_ran_this_week = today
        log.info("fu_sync done")
    except Exception as e: log.error(f"fu_sync: {e}")


def _bg_lot_sync():
    """cc#314: NIGHTLY Fyers lot-size audit/correction (~01:05 IST). Day-locked. Keeps
    futures_universe.lot_size <=~1 day stale (was Monday-only inside _bg_fu_sync, up to 6 days
    stale after an expiry-day lot revision -> mis-sized V8 paper + client qty). Idempotent."""
    global _lot_sync_ran_today
    today = _ist_now().date()
    if _lot_sync_ran_today == today: return _SKIPPED
    try:
        import lot_sync
        with _conn() as conn:
            rep = lot_sync.audit_and_fix_lots(conn, apply=True)
            _log_health(conn, "lot_sync", {"date": str(today), "applied": rep.get("applied"),
                                           "changed": rep.get("changed_count")})
        _lot_sync_ran_today = today
        log.info(f"lot_sync: {rep.get('applied')} lots corrected, {rep.get('changed_count')} stale")
    except Exception as e: log.error(f"lot_sync: {e}")

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

def _bg_earnings_refresh():
    """cc#225: daily 06:15 IST earnings_calendar refresh (runs BEFORE the 09:10 pre-market
    readiness check), so the blackout logic sees a fresh calendar at open. Reuses the same
    code path as the load_earnings_from_screener MCP tool via admin_data.refresh_earnings_calendar,
    which NEVER wipes the table on a scrape error / 0 rows (prior data kept). Fires an alert on
    failure or empty load (same pattern as the ADR gate).

    cc#490: success now also counts nse_rows_inserted — the NSE board-meetings fetch (forward
    dates) can legitimately land 0 NEW same-day Screener rows on a quiet day while still
    successfully loading fresh forward dates, which must not fire a false failure alert."""
    try:
        import admin_data
        res = asyncio.run(admin_data.refresh_earnings_calendar())
        total_inserted = (res.get("rows_inserted") or 0) + (res.get("nse_rows_inserted") or 0) if isinstance(res, dict) else 0
        if not isinstance(res, dict) or res.get("status") != "ok" or not total_inserted:
            _log_alert("earnings_refresh_failed",
                       f"06:15 earnings_calendar refresh loaded no rows — prior data kept: {res}")
        else:
            log.info(f"earnings_refresh: {res.get('rows_inserted')} screener + "
                     f"{res.get('nse_rows_inserted')} nse rows loaded (loaded_at=today)")
    except Exception as e:
        _log_alert("earnings_refresh_failed",
                   f"06:15 earnings_calendar refresh raised (prior data kept): {e}")
        log.error(f"earnings_refresh: {e}")

def _bg_feed_daily_log():
    """cc#495 change_4: daily 16:15 IST post-close feed summary — ONE ops_log entry
    (category=feed_log, title=daily_summary) rolling up the day's feed health so the
    founder can review next-day or weekly instead of grepping Railway logs. Pulls from
    ops_log entries the worker writes live (feed_ws_connect/close, feed_watchdog_*,
    feed_invalid_symbol_dropped, feed_health_floor_breach, oi_poll_summary — all added
    or already present as of cc#489/cc#495) plus a direct intraday_prices summary for
    last-bar-ts and a rough gap count (75 = expected 5-min buckets in 09:15-15:30)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT title, COUNT(*) FROM ops_log
                           WHERE session_date=CURRENT_DATE
                             AND title IN ('feed_ws_connect','feed_ws_close',
                                           'feed_watchdog_reconnect','feed_watchdog_exit')
                           GROUP BY title""")
            counts = {row[0]: row[1] for row in cur.fetchall()}

            cur.execute("""SELECT session_ts, details FROM ops_log
                           WHERE session_date=CURRENT_DATE AND title='feed_invalid_symbol_dropped'
                           ORDER BY session_ts""")
            dropped = [{"ts": str(r[0]), "detail": r[1]} for r in cur.fetchall()]

            cur.execute("""SELECT session_ts, details FROM ops_log
                           WHERE session_date=CURRENT_DATE AND title='feed_health_floor_breach'
                           ORDER BY session_ts""")
            floor_breaches = [{"ts": str(r[0]), "detail": r[1]} for r in cur.fetchall()]

            cur.execute("""SELECT details->>'label' AS label,
                                  ROUND(AVG((details->>'rate')::numeric), 3) AS avg_rate,
                                  COUNT(*) AS polls
                           FROM ops_log
                           WHERE session_date=CURRENT_DATE AND title='oi_poll_summary'
                             AND details->>'rate' IS NOT NULL
                           GROUP BY details->>'label'""")
            oi_rates = {row[0]: {"avg_rate": float(row[1]), "polls": row[2]} for row in cur.fetchall()}

            cur.execute("""SELECT source, MAX(ts) AS last_bar, COUNT(DISTINCT ts) AS buckets
                           FROM intraday_prices
                           WHERE ts::date=CURRENT_DATE AND source IN ('fyers_eq','fyers_fut')
                           GROUP BY source""")
            EXPECTED_BUCKETS = 75   # 09:15-15:30 IST in 5-min steps
            bars = {}
            for source, last_bar, buckets in cur.fetchall():
                bars[source] = {"last_bar_ts": str(last_bar), "buckets": buckets,
                                 "gaps": max(0, EXPECTED_BUCKETS - buckets)}

        # cc#660: fold in the feed_guardian liveness snapshot (per-leg age, worker heartbeat,
        # open repairs, OI tripwire) so the daily log is one place, sourced from the guardian.
        guardian = {}
        try:
            import feed_guardian
            guardian = feed_guardian.guardian_summary()
        except Exception as e:
            guardian = {"error": str(e)[:120]}

        summary = {
            "date": str(_ist_now().date()),
            "ws_connects": counts.get("feed_ws_connect", 0),
            "ws_closes": counts.get("feed_ws_close", 0),
            "watchdog_rung1_reconnects": counts.get("feed_watchdog_reconnect", 0),
            "watchdog_rung2_exits": counts.get("feed_watchdog_exit", 0),
            "symbols_dropped_blacklisted": dropped,
            "health_floor_breaches": floor_breaches,
            "oi_poll_rates": oi_rates,
            "bars_by_source": bars,
            "guardian": guardian,   # cc#660
        }
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'feed_log', 'daily_summary', %s)""",
                        (Json(summary),))
            conn.commit()
        log.info(f"feed_daily_log: {summary['ws_connects']} connects, "
                 f"{summary['watchdog_rung1_reconnects']} reconnects, "
                 f"{len(dropped)} dropped, {len(floor_breaches)} floor breaches")
    except Exception as e:
        log.error(f"feed_daily_log failed: {e}")

# cc#660: _bg_open_bars_alarm (cc#229 feed-silent-at-open) folded into feed_guardian.guardian_tick()
# — the 09:20-09:30 open-silence check now lives there with the same <400-bars floor.

# cc#217: _bg_fetch_company_news / _bg_company_news_wave2 / _bg_company_news_retry
# removed — the 500-company Google waves were retired (cc#207) and fully deleted here.
# Position News (open V8 + SmartGain symbols) is the successor: _bg_fetch_position_news.

def _bg_cleanup_news():
    """cc#192: daily news retention — unpolished raw_news dies at 48h, polished (and its raw
    parent) lives 90 days (cc#208). news_retention() logs both counts to
    ops_log(category=news_retention) and alerts on an implausible unpolished backlog.
    cc#244: retired the position_news quarantine purge — that table is superseded by the
    single raw_news/polished_news funnel (id=1660); position_news.py is left unwired in-repo."""
    try:
        import news_fetcher
        with _conn() as conn:
            res = news_fetcher.news_retention(conn)
            _log_health(conn, "cleanup_news",  # cc#255
                        {"result": res if isinstance(res, dict) else str(res)})
        log.info(f"news_retention: {res}")
    except Exception as e: log.error(f"news_retention: {e}")

def _bg_log_retention():
    """cc#469 audit_1(e): keep tick-class telemetry 30 days, delete older. Only pure
    heartbeat/telemetry categories are purged — NEVER the memory-taxonomy rows
    (task/spec_locked/canonical_spec/decision/day_log/... are institutional memory).
    Idempotent; runs nightly. ops_log(category=log_retention)."""
    TICK_OPS = ('v10_tick_hb', 'scheduler_health')   # ops_log heartbeats
    TICK_SESSION = ('v10_tick_hb',)                   # session_log heartbeats only
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ops_log WHERE category = ANY(%s) "
                        "AND session_ts < NOW() - INTERVAL '30 days'", (list(TICK_OPS),))
            n_ops = cur.rowcount
            cur.execute("DELETE FROM session_log WHERE category = ANY(%s) "
                        "AND session_ts < NOW() - INTERVAL '30 days'", (list(TICK_SESSION),))
            n_sess = cur.rowcount
            cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                        "VALUES (CURRENT_DATE, NOW(), 'log_retention', 'tick_purge', %s)",
                        (Json({"ops_deleted": n_ops, "session_deleted": n_sess,
                               "categories_ops": list(TICK_OPS), "keep_days": 30}),))
            conn.commit()
        log.info(f"log_retention: ops={n_ops} session={n_sess} tick rows purged (>30d)")
    except Exception as e:
        log.error(f"log_retention: {e}")
    # cc#548: drive the consolidated data-retention matrix off the same nightly retention
    # chain (own connection + try/except, so it always runs regardless of the log purge above).
    _bg_data_retention()

def _bg_data_retention():
    """cc#548 RETENTION MATRIX (founder-locked 19-Jul-2026): single consolidated nightly purge
    for the 7 time-series tables. One DELETE per table/source, idempotent (INTERVAL-based, safe
    to re-run), fault-isolated (each DELETE commits independently — one failure never aborts the
    rest). Piggybacks the 01:52 retention chain via _bg_log_retention — NO new scheduler slot.
    Per-table deleted counts land on a single ops_log(category=data_retention) line.
      bt14_fut_oi      7d    research killed (id=6078)
      option_chain     7d    5-min strike detail — index-wide + stocks ATM+/-3
      pcr_intraday     60d   (min is ~45d today, so effectively a no-op cap for now)
      options_oi_daily 730d  EOD OI distillation — the long memory
      pcr_daily        730d
      adr_daily        730d
      intraday_prices  730d  for source IN (fyers_eq, fyers_fut, fyers_hist) — cc#381 doctrine;
                             fyers_eq=equity spot, fyers_fut=futures, fyers_hist=backtest 5m,
                             verified separate sources, no duplication. ALSO drops stray legacy
                             source='yahoo' timeframe='1m' rows outright (~158 rows)."""
    PURGES = [
        ("bt14_fut_oi_7d",          "DELETE FROM bt14_fut_oi      WHERE ts < NOW() - INTERVAL '7 days'"),
        ("option_chain_7d",         "DELETE FROM option_chain     WHERE ts < NOW() - INTERVAL '7 days'"),
        ("pcr_intraday_60d",        "DELETE FROM pcr_intraday     WHERE ts < NOW() - INTERVAL '60 days'"),
        ("options_oi_daily_730d",   "DELETE FROM options_oi_daily WHERE d < CURRENT_DATE - INTERVAL '730 days'"),
        ("pcr_daily_730d",          "DELETE FROM pcr_daily        WHERE price_date < CURRENT_DATE - INTERVAL '730 days'"),
        ("adr_daily_730d",          "DELETE FROM adr_daily        WHERE price_date < CURRENT_DATE - INTERVAL '730 days'"),
        ("intraday_fyers_730d",     "DELETE FROM intraday_prices  WHERE source IN ('fyers_eq','fyers_fut','fyers_hist') AND ts < NOW() - INTERVAL '730 days'"),
        ("intraday_yahoo_1m_stray", "DELETE FROM intraday_prices  WHERE source='yahoo' AND timeframe='1m'"),
    ]
    counts = {}
    try:
        with _conn() as conn:
            for label, sql in PURGES:
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql)
                        counts[label] = cur.rowcount
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    counts[label] = f"err:{type(e).__name__}"
            with conn.cursor() as cur:
                cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                            "VALUES (CURRENT_DATE, NOW(), 'data_retention', 'retention_matrix', %s)",
                            (Json({"deleted": counts, "task": "cc#548"}),))
            conn.commit()
        log.info(f"data_retention: {counts}")
    except Exception as e:
        log.error(f"data_retention: {e}")

def _bg_fetch_stock_news():
    """cc#242 (POSITION_NEWS_PIPELINE_V1): per-stock Google News -> raw_news (source_type='company'
    + symbol), alias-filtered at ingest. Single funnel with market news; supersedes the
    position_news quarantine fetch (cc#207/id=402). Runs once daily ~00:30 IST (cc#321).

    cc#787 DOCSTRING FIX: this said "full active futures universe", which has been wrong since
    cc#243/cc#244 deliberately narrowed news_fetcher._stock_universe to OPEN POSITIONS ONLY.
    Universe-wide coverage is now a separate funnel-2 job (_bg_fetch_universe_reco_news)."""
    try:
        import news_fetcher
        with _conn() as conn:
            res = news_fetcher.fetch_stock_news(conn)
        log.info(f"fetch_stock_news: {res}")
    except Exception as e:
        log.error(f"fetch_stock_news: {e}")

def _bg_fetch_position_news():
    """cc#611 (revive): dedicated per-open-position Google News (V8 OPEN ∪ SmartGain net) -> the
    position_news table, 3 slots/day. The wiring was dropped ~06-Jul (cc#244 retired the old slot as
    "superseded") and the tab silently starved to ~1 item. Restored here. Calls fetch_and_alert so a
    silent stall (open positions but nothing fetched in >24h) raises an ops_log alert; also runs the
    7-day purge. OFF the fyers feed -> safe any hour, every day (news breaks on weekends too)."""
    try:
        import position_news
        with _conn() as conn:
            res = position_news.fetch_and_alert(conn)
            try:
                position_news.purge_position_news(conn)
            except Exception:
                pass
        log.info(f"fetch_position_news: {res}")
    except Exception as e:
        log.error(f"fetch_position_news: {e}")

def _bg_fetch_universe_reco_news(slot: int = 0):
    """cc#787 FUNNEL 2 coverage: per-stock Google News across the ACTIVE FUTURES UNIVERSE
    (~209 symbols), query biased to recommendation content, landing in raw_news as
    source_type='company' with is_reco set by the shared classifier.

    Distinct from _bg_fetch_stock_news, which is OPEN POSITIONS ONLY (cc#243/cc#244) — this is
    the universe-wide leg the Stock Views funnel needs.

    2 slots/day, OFF market hours (03:10 / 21:10 IST), each taking half the universe (slot 0 /
    slot 1 alternate) so one run never walks all ~209 symbols and the Google 429 budget stays
    intact. Politeness stack is position_news's, reused verbatim: sequential, browser UA, 2-4s
    jitter, Retry-After honoured, abort on 10 consecutive 429s. On abort the remainder is left
    for the next slot; the halves alternate so nothing is starved permanently."""
    try:
        import stock_views_funnel
        with _conn() as conn:
            res = stock_views_funnel.fetch_universe_reco_news(slot=slot, conn=conn)
        log.info(f"fetch_universe_reco_news(slot={slot}): {res}")
    except Exception as e:
        log.error(f"fetch_universe_reco_news(slot={slot}): {e}")


def _bg_stock_news_watchdog():
    """cc#245: staleness / all-blocked watchdog for the per-stock Google News fetch. Piggybacks
    the 16:00 IST retry slot (no new slot). Reads the latest ops_log title='fetch_stock_news';
    fires ops_log(category='alert', title='stock_news_stale') if (a) no run in the last 24h, OR
    (b) inserted=0 AND every parsed item was gate-killed/blocked
    (alias_filtered+quality_rejected==parsed) AND symbols_count>0 — the breakage/IP-block
    signature. NEVER alerts on inserted=0 alone (a genuinely quiet news day, or all-dups, is
    fine — dup_skipped keeps alias_filtered+quality_rejected < parsed)."""
    try:
        from psycopg.types.json import Json
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT details, session_ts FROM ops_log
                           WHERE title='fetch_stock_news' ORDER BY session_ts DESC LIMIT 1""")
            row = cur.fetchone()
            alert = None
            if not row:
                alert = "no fetch_stock_news run on record"
            else:
                details, ts = row
                cur.execute("SELECT (NOW() - %s) > INTERVAL '24 hours'", (ts,))
                stale = cur.fetchone()[0]
                d = details or {}
                parsed  = int(d.get('parsed') or 0)
                inserted = int(d.get('inserted') or 0)
                alias_f = int(d.get('alias_filtered') or 0)
                qrej    = int(d.get('quality_rejected') or 0)
                symc    = int(d.get('symbols_count') or 0)
                if stale:
                    alert = f"no fetch_stock_news run in >24h (last {ts})"
                elif inserted == 0 and symc > 0 and (alias_f + qrej) == parsed:
                    alert = (f"fetch_stock_news inserted=0 with all {parsed} parsed items "
                             f"gate-killed/blocked (alias_filtered={alias_f}, quality_rejected={qrej}, "
                             f"symbols={symc}) — breakage / IP-block signature, not a quiet day")
            if alert:
                cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                               VALUES (CURRENT_DATE, NOW(), 'alert', 'stock_news_stale', %s)""",
                            (Json({"message": alert}),))
                conn.commit()
                log.error(f"stock_news_stale: {alert}")
            else:
                log.info("stock_news watchdog: OK")
    except Exception as e:
        log.error(f"_bg_stock_news_watchdog: {e}")

def _bg_tag_news():
    """cc#207 Part C: tag untagged polished_news with universe symbols so company pages
    populate without per-company Google waves. First run backfills 30 days."""
    try:
        import news_tagger
        with _conn() as conn:
            res = news_tagger.tag_untagged(conn)
        log.info(f"news_tagger: {res}")
    except Exception as e:
        log.error(f"news_tagger: {e}")

def _bg_mf_nav():
    """cc#466: V15 MF data layer — daily AMFI NAV pull + master upsert + append NAV history, then a
    seed<->AMFI reconcile pass (idempotent; only matches still-unmatched curated funds). ops_logged."""
    try:
        import mf_pipeline
        r1 = mf_pipeline.run_amfi_nav()
        r2 = mf_pipeline.reconcile_seed()
        log.info(f"_bg_mf_nav: nav={r1} reconcile_matched={sum(1 for x in r2.get('results',[]) if x.get('amfi_code'))}")
    except Exception as e:
        log.error(f"_bg_mf_nav: {e}")


_mf_derived_nightly_armed_day = None
def _bg_mf_derived_nightly():
    """cc#777 Part 2 + cc#778: NIGHTLY holdings-derived NAV rebuild + metric recompute.

    cc#778 amendment: derivation is NIGHTLY, not weekly — it sits inside the 01:00-02:05 IST chain
    AFTER the 01:00 Yahoo EOD load so the same-day closes are consumed, which keeps the screener and
    fund pages T-1 fresh instead of up to 6 days stale. Compute is trivial (~512 funds x ~60 holdings)."""
    global _mf_derived_nightly_armed_day
    now = _ist_now()
    if not (now.hour == 1 and 35 <= now.minute < 50):   # after the 01:00 yahoo EOD load
        return _SKIPPED
    day_key = now.date().isoformat()
    if _mf_derived_nightly_armed_day == day_key:
        return _SKIPPED
    _mf_derived_nightly_armed_day = day_key
    try:
        import mf_derived
        log.info(f"_bg_mf_derived_nightly: {mf_derived.run_nightly()}")
    except Exception as e:
        log.error(f"_bg_mf_derived_nightly: {e}")


_mf_derived_audit_armed_day = None
def _bg_mf_derived_audit():
    """cc#778: Saturday SELF-AUDIT ONLY — re-grade tracking error / confidence from the accumulated
    daily points (~22 obs/fund/month vs 4 under the old weekly design). Does NOT rebuild the series;
    the nightly job owns that."""
    global _mf_derived_audit_armed_day
    now = _ist_now()
    if not (now.weekday() == 5 and now.hour == 7 and now.minute < 15):
        return _SKIPPED
    day_key = now.date().isoformat()
    if _mf_derived_audit_armed_day == day_key:
        return _SKIPPED
    _mf_derived_audit_armed_day = day_key
    try:
        import mf_derived
        log.info(f"_bg_mf_derived_audit: {mf_derived.run_weekly_audit()}")
    except Exception as e:
        log.error(f"_bg_mf_derived_audit: {e}")


_mf_monthly_mc_armed_day = None
def _bg_mf_monthly_mc():
    """cc#777 Part 1: MONTHLY Moneycontrol refresh on the 11th (AMC portfolio disclosures are
    SEBI-mandated within 10 days of month-end, so the 11th is the first safe day).

    (a) resolve_mc_map — maps any NEW schemes to their MC imid/slug (idempotent, ~3.5 min);
    (b) run_mc_oneshot — holdings+weights (getInvestmentByStock pageSize=300) + AUM/TER/manager/
        inception (__NEXT_DATA__ overview) over the ~519 equity universe. Reuses the cc#500
        orchestrator as-is: checkpointed via app_config mf_mc_oneshot_cursor, 1.7s pacing, step_5
        stop rules (3 consecutive 403/no-__NEXT_DATA__, or >20% unparseable in first 50) and the
        AMC-aggregate-AUM sanity reject (>Rs 5,00,000 Cr). ~90 min. The cursor self-deletes on a
        clean finish, so each monthly run starts fresh; a stop_event leaves it so the next run
        RESUMES rather than restarting — both correct.
    (c) NAV ANCHOR — stamps the month-end actual NAV per fund into mf_nav_history as nav_kind='m',
        the truth point tracking-error is measured against.

    Registered in schedule_registry (id=9116) and writes a job_runs row via the ops_control_plane
    spine; any failure ALSO writes ops_log category=scrape_review (universal failure rule)."""
    global _mf_monthly_mc_armed_day
    now = _ist_now()
    if now.day != 11 or now.hour != 3:      # 03:30 IST — clear of the 01:00-02:00 nightly cascade
        return _SKIPPED
    if now.minute >= 40:
        return _SKIPPED
    day_key = now.date().isoformat()
    if _mf_monthly_mc_armed_day == day_key:
        return _SKIPPED
    _mf_monthly_mc_armed_day = day_key

    started = _ist_now()
    landed, err, status = {}, None, "ok"
    try:
        import mf_pipeline
        try:
            m = mf_pipeline.resolve_mc_map()
            landed["mc_map"] = True
        except Exception as e:
            landed["mc_map"] = False; err = f"resolve_mc_map: {e}"; m = None
        res = mf_pipeline.run_mc_oneshot()
        landed["holdings"] = bool(res and res.get("holdings_filled"))
        landed["overview"] = bool(res and res.get("overview_filled"))
        if res and res.get("stop_event"):
            status = "partial"
            err = f"stop_event: {res.get('stop_event')}"
        elif not landed["holdings"]:
            status = "partial"
            err = err or "no holdings rows written"
        # (c) NAV anchor — month-end actual NAV per fund, the tracking-error truth point.
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO mf_nav_history (scheme_code, nav_date, nav, nav_kind)
                    SELECT DISTINCT ON (h.scheme_code) h.scheme_code, h.nav_date, h.nav, 'm'
                    FROM mf_nav_history h
                    WHERE h.nav_date >= (date_trunc('month', (NOW() AT TIME ZONE 'Asia/Kolkata')::date)
                                         - INTERVAL '1 month')::date
                      AND h.nav_date <  date_trunc('month', (NOW() AT TIME ZONE 'Asia/Kolkata')::date)::date
                      AND h.nav IS NOT NULL
                    ORDER BY h.scheme_code, h.nav_date DESC
                    ON CONFLICT DO NOTHING""")
                landed["nav_anchor"] = cur.rowcount
                conn.commit()
        except Exception as e:
            landed["nav_anchor"] = 0
            err = (err + " | " if err else "") + f"nav_anchor: {e}"
            status = "partial" if status == "ok" else status
        log.info(f"_bg_mf_monthly_mc: status={status} landed={landed} res={res}")
    except Exception as e:
        status, err = "failed", str(e)[:400]
        log.error(f"_bg_mf_monthly_mc: {e}")
    # job_runs spine + universal failure rule (ops_log category=scrape_review on non-ok)
    try:
        import ops_control_plane
        ops_control_plane.record_run("mf_monthly_mc", status=status, sections_landed=landed,
                                     error=err, scope="v15_mf")
    except Exception as e:
        log.warning(f"_bg_mf_monthly_mc record_run failed: {e}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO job_runs (job_key, started_at, finished_at, status, sections_landed, error)
                           VALUES ('mf_monthly_mc', %s, NOW(), %s, %s, %s)""",
                        (started, status, Json(landed), err))
            if status != "ok":
                cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                               VALUES (CURRENT_DATE, NOW(), 'scrape_review', 'MF_MONTHLY_MC_FAILED', %s)""",
                            (Json({"status": status, "error": err, "landed": landed}),))
            conn.commit()
    except Exception as e:
        log.warning(f"_bg_mf_monthly_mc job_runs write failed: {e}")


def _bg_mf_returns_backfill():
    """cc#477: flag-gated one-shot V15 MF returns backfill (AUM>5000cr, monthly+weekly NAV history,
    1W/1M/3M/6M/1Y/2Y). Runs on the APP server (has AMFI/mfapi outbound), independent of the feed
    worker. Single-flight; ~8-10 min. Flag app_config mf_returns_backfill_run: 'pending'->run,
    'done'->skip. Set by POST /api/v15/mf/returns_backfill or manually."""
    global _mf_backfill_running
    if _mf_backfill_running:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='mf_returns_backfill_run'")
            r = cur.fetchone()
        if not r or r[0] != 'pending':
            return _SKIPPED
        _mf_backfill_running = True
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_returns_backfill_run','running',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='running', updated_at=NOW()")
            conn.commit()
        import mf_pipeline
        res = mf_pipeline.run_mf_returns_backfill()
        log.info(f"_bg_mf_returns_backfill: {res}")
    except Exception as e:
        log.error(f"_bg_mf_returns_backfill: {e}")
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_returns_backfill_run','pending',NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
        except Exception:
            pass
    finally:
        _mf_backfill_running = False


def _bg_mf_v15_wiring():
    """cc#491: flag-gated one-shot V15 wiring run (AUM sweep, expense ratio, holdings
    orchestration, MQS scoring, category averages) over the canonical equity universe.
    Same single-flight/flag-gated pattern as cc#477's returns backfill. Flag app_config
    mf_v15_wiring_run: 'pending'->run, 'done'->skip. Set by POST /api/v15/mf/wire_all."""
    global _mf_wiring_running
    if _mf_wiring_running:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='mf_v15_wiring_run'")
            r = cur.fetchone()
        if not r or r[0] != 'pending':
            return _SKIPPED
        _mf_wiring_running = True
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_v15_wiring_run','running',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='running', updated_at=NOW()")
            conn.commit()
        import mf_pipeline
        res = mf_pipeline.run_v15_wiring()
        log.info(f"_bg_mf_v15_wiring: {res}")
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_v15_wiring_run','done',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='done', updated_at=NOW()")
            conn.commit()
    except Exception as e:
        log.error(f"_bg_mf_v15_wiring: {e}")
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_v15_wiring_run','pending',NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
        except Exception:
            pass
    finally:
        _mf_wiring_running = False


def _bg_mf_weekly():
    """cc#477 phase_4: Saturday 06:30 IST — weekly-rolling-12mo NAV sync + returns recompute
    (from the full mfapi series, cc#491 CADENCE_RETENTION_FINAL) for the AUM>5000cr set."""
    try:
        import mf_pipeline
        res = mf_pipeline.mf_weekly_refresh()
        log.info(f"_bg_mf_weekly: {res}")
    except Exception as e:
        log.error(f"_bg_mf_weekly: {e}")


def _bg_mf_weekly_manual():
    """cc#491 course-correct: manual POST /api/v15/mf/run_weekly trigger — same weekly NAV+returns
    sync as the Saturday cron, on demand. Flag-gated single-flight, app_config mf_weekly_run:
    'pending'->run, 'done'->skip. Exists because CC's own session has restricted egress and
    cannot run this fetch itself — this lets the founder/Claude.ai fire it and have it execute
    server-side on Railway, which has full internet."""
    global _mf_weekly_manual_running
    if _mf_weekly_manual_running:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='mf_weekly_run'")
            r = cur.fetchone()
        if not r or r[0] != 'pending':
            return _SKIPPED
        _mf_weekly_manual_running = True
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_weekly_run','running',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='running', updated_at=NOW()")
            conn.commit()
        import mf_pipeline
        res = mf_pipeline.mf_weekly_refresh()
        log.info(f"_bg_mf_weekly_manual: {res}")
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_weekly_run','done',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='done', updated_at=NOW()")
            conn.commit()
    except Exception as e:
        log.error(f"_bg_mf_weekly_manual: {e}")
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_weekly_run','pending',NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
        except Exception:
            pass
    finally:
        _mf_weekly_manual_running = False


def _bg_mf_score_nightly():
    """cc#520 step_4: nightly MQS recompute (category averages + scores) -- pure reads of already-
    stored mf_master data, zero external HTTP calls. Distinct from cc#499's disabled scraping jobs:
    the founder explicitly wants the score COMPUTE to keep running nightly even with scraping off
    ("cc#499 deactivates SCRAPERS only, the score compute from stored data stays")."""
    try:
        import mf_pipeline
        res = mf_pipeline.run_mf_score_nightly()
        log.info(f"_bg_mf_score_nightly: {res}")
    except Exception as e:
        log.error(f"_bg_mf_score_nightly: {e}")


def _bg_mf_aum_monthly():
    """cc#477 phase_4, cadence moved to the 12th by cc#491 course-correct (session_log id=4734):
    monthly comprehensive job — AUM, TER, holdings (crawled AMC registry), monthly AUM/TER
    snapshot, rolling-12mo prune — via mf_pipeline.run_v15_wiring() (mf_aum_monthly_refresh is
    now a thin repoint to it, see mf_pipeline.py)."""
    try:
        import mf_pipeline
        res = mf_pipeline.mf_aum_monthly_refresh()
        log.info(f"_bg_mf_aum_monthly: {res}")
    except Exception as e:
        log.error(f"_bg_mf_aum_monthly: {e}")


_mc_discover_running = False   # cc#500: Moneycontrol discovery-probe single-flight guard


def _bg_mf_mc_discover():
    """cc#500 step_1: flag-gated one-shot Moneycontrol discovery probe (seconds, not minutes) —
    checked every tick (no m%3 offset) so the dev session gets fast turnaround while iterating
    on URL/field discovery. Flag app_config mf_mc_discover_run: 'pending'->run, 'done'->skip.
    Set by POST /api/v15/mf/mc_discover_run."""
    global _mc_discover_running
    if _mc_discover_running:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='mf_mc_discover_run'")
            r = cur.fetchone()
        if not r or r[0] != 'pending':
            return _SKIPPED
        _mc_discover_running = True
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_mc_discover_run','running',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='running', updated_at=NOW()")
            conn.commit()
        import mf_pipeline
        with mf_pipeline._conn() as conn, conn.cursor() as cur:
            mf_pipeline.ensure_tables(cur)
            res = mf_pipeline._discover_mc_search_api(cur)
            conn.commit()
        log.info(f"_bg_mf_mc_discover: {res}")
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_mc_discover_run','done',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='done', updated_at=NOW()")
            conn.commit()
    except Exception as e:
        log.error(f"_bg_mf_mc_discover: {e}")
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_mc_discover_run','pending',NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
        except Exception:
            pass
    finally:
        _mc_discover_running = False


_mc_oneshot_running = False   # cc#500: Moneycontrol full-set fill single-flight guard


def _bg_mf_mc_oneshot():
    """cc#500: flag-gated ONE-TIME Moneycontrol full-set fill (AUM/TER/ret_3y/5y/manager/
    inception/holdings + ret_1y via the existing mfapi pipeline) over the 519-scheme canonical
    equity universe. Resumable via app_config mf_mc_oneshot_cursor if interrupted mid-run
    (~30-45 min full pass, same single-flight/checkpoint pattern as cc#477's returns backfill).
    Flag app_config mf_mc_oneshot_run: 'pending'->run, 'done'->skip. Set by
    POST /api/v15/mf/run_mc_oneshot. A stop_event in the result (step_5_stop_rules) still marks
    the run 'done' — reported, not silently auto-retried."""
    global _mc_oneshot_running
    if _mc_oneshot_running:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='mf_mc_oneshot_run'")
            r = cur.fetchone()
        if not r or r[0] != 'pending':
            return _SKIPPED
        _mc_oneshot_running = True
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_mc_oneshot_run','running',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='running', updated_at=NOW()")
            conn.commit()
        import mf_pipeline
        res = mf_pipeline.run_mc_oneshot()
        log.info(f"_bg_mf_mc_oneshot: {res}")
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_mc_oneshot_run','done',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='done', updated_at=NOW()")
            conn.commit()
    except Exception as e:
        log.error(f"_bg_mf_mc_oneshot: {e}")
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_mc_oneshot_run','pending',NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
        except Exception:
            pass
    finally:
        _mc_oneshot_running = False


_ops_metrics_running = False   # cc#523: ops-metrics backfill/monthly single-flight guard


def _bg_ops_metrics_backfill():
    """cc#523: flag-gated ops-metrics backfill runner -- same mechanism as cc#500's
    _bg_mf_mc_oneshot (checked every tick, no-ops unless app_config['ops_metrics_backfill_run']
    is 'pending'). Serves BOTH the one-time 500-company first leg (armed via
    POST /api/admin/ops_metrics/run_backfill) AND the monthly incremental re-run (armed by
    _bg_ops_metrics_monthly below on the 5th of each month) -- run_company()'s idempotency
    check (doc URL unchanged + already extracted -> 'already_current', no LLM re-spend) is what
    makes reusing the identical runner safe for both call sites.

    cc#526 item 2: window-gated to 01:00-06:00 IST, same overnight slot cc#361's fundamentals
    backfill used successfully on 11-Jul -- this pipeline hits the same screener.in pages, so
    it should share the same politeness window rather than scraping at arbitrary times of day.
    A stale-running resume (see below) also waits for this window rather than firing immediately."""
    global _ops_metrics_running
    if _ops_metrics_running:
        return _SKIPPED   # cc#734 guardrail: single-flight — a concurrent trigger no-ops (already_running)
    now_ist = datetime.now(IST)
    # cc#734: read the flag BEFORE the window gate so an on-demand 'run_now' can bypass it. The
    # cc#526 overnight window (01:00-06:00 IST) still governs the DEFAULT scheduled 'pending' path
    # unchanged; 'run_now' executes immediately at any clock time. Source = screener.in result pages
    # (NOT the fyers live feed), so a market-hours run_now is source-safe (no live-feed contention).
    try:
        with _conn() as conn, conn.cursor() as cur:
            # cc#526 BUG FIX: a Railway redeploy mid-run kills this process with a SIGTERM, not
            # a catchable Python exception -- the 'except Exception' below that resets the flag
            # back to 'pending' on failure never runs, so the flag was found stuck at 'running'
            # forever (confirmed live: value='running' since 06:12, cursor frozen at company
            # 275/500, 18-Jul). Any 'running' value older than STALE_RUNNING_MIN is now treated
            # as an orphaned/crashed run and retried rather than blocking indefinitely.
            cur.execute("""SELECT value, EXTRACT(EPOCH FROM (NOW()-updated_at))/60.0
                           FROM app_config WHERE key='ops_metrics_backfill_run'""")
            r = cur.fetchone()
    except Exception as e:
        log.error(f"_bg_ops_metrics_backfill flag read: {e}")
        return
    STALE_RUNNING_MIN = 20
    val = r[0] if r else None
    is_run_now = (val == 'run_now')            # cc#734: on-demand, any clock time
    is_pending = (val == 'pending')
    is_stale_running = (val == 'running' and r[1] is not None and r[1] > STALE_RUNNING_MIN)
    if not (is_run_now or is_pending or is_stale_running):
        return _SKIPPED
    # window gate applies to the SCHEDULED path only (pending / stale-resume); run_now is clock-free.
    if not is_run_now and not (1 <= now_ist.hour < 6):
        return _SKIPPED
    if is_stale_running:
        log.warning(f"_bg_ops_metrics_backfill: flag stuck at 'running' for {r[1]:.0f} min -- "
                    f"treating as an orphaned run (likely killed by a mid-run redeploy) and resuming")
    _ops_metrics_running = True
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('ops_metrics_backfill_run','running',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='running', updated_at=NOW()")
            conn.commit()
        import ops_metrics_pipeline
        res = ops_metrics_pipeline.run_ops_metrics_backfill()
        log.info(f"_bg_ops_metrics_backfill ({'run_now' if is_run_now else 'scheduled'}): {res}")
        with _conn() as conn, conn.cursor() as cur:
            # cc#734: audit row so Claude web can verify the on-demand run + its counts.
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'ops_metrics', 'OPS_METRICS_BACKFILL_RUN', %s)""",
                        (Json({"trigger": "run_now" if is_run_now else "scheduled",
                               "result": res if isinstance(res, dict) else {"raw": str(res)[:400]},
                               "ist": _ist_now().isoformat()}),))
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('ops_metrics_backfill_run','done',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='done', updated_at=NOW()")
            conn.commit()
    except Exception as e:
        log.error(f"_bg_ops_metrics_backfill: {e}")
        try:
            # cc#734: a failed run_now settles to 'failed' (never left stuck on run_now); a failed
            # scheduled run settles to 'pending' so the next overnight window retries it (unchanged).
            end_val = 'failed' if is_run_now else 'pending'
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('ops_metrics_backfill_run',%s,NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()", (end_val,))
                cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                               VALUES (CURRENT_DATE, NOW(), 'ops_metrics', 'OPS_METRICS_BACKFILL_RUN', %s)""",
                            (Json({"trigger": "run_now" if is_run_now else "scheduled",
                                   "status": "failed", "error": str(e)[:300], "ist": _ist_now().isoformat()}),))
                conn.commit()
        except Exception:
            pass
    finally:
        _ops_metrics_running = False


_ops_text_fetch_running = False


def _bg_ops_text_fetch():
    """cc#527 PHASE-SPLIT: flag-gated fetch-only backfill runner -- same pending/running/done +
    stale-running(>20min)-recovery mechanism as _bg_ops_metrics_backfill above, but its OWN flag
    key (ops_text_fetch_run, NOT ops_metrics_backfill_run -- that job stays disarmed/untouched
    for this one-time text fetch) and a WIDER window: 23:00-06:00 IST (founder explicitly wants
    a 23:00 start tonight; the 01:00 start on the extraction job was CC-inferred politeness, the
    founder overrides that for this one-time fetch pass)."""
    global _ops_text_fetch_running
    if _ops_text_fetch_running:
        return _SKIPPED
    now_ist = datetime.now(IST)
    in_window = now_ist.hour >= 23 or now_ist.hour < 6
    if not in_window:
        return _SKIPPED
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT value, EXTRACT(EPOCH FROM (NOW()-updated_at))/60.0
                           FROM app_config WHERE key='ops_text_fetch_run'""")
            r = cur.fetchone()
        STALE_RUNNING_MIN = 20
        is_pending = r and r[0] == 'pending'
        is_stale_running = r and r[0] == 'running' and r[1] is not None and r[1] > STALE_RUNNING_MIN
        if not (is_pending or is_stale_running):
            return _SKIPPED
        if is_stale_running:
            log.warning(f"_bg_ops_text_fetch: flag stuck at 'running' for {r[1]:.0f} min -- "
                        f"treating as an orphaned run and resuming")
        _ops_text_fetch_running = True
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('ops_text_fetch_run','running',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='running', updated_at=NOW()")
            conn.commit()
        import ops_metrics_pipeline
        res = ops_metrics_pipeline.run_text_fetch_backfill()
        log.info(f"_bg_ops_text_fetch: {res}")
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('ops_text_fetch_run','done',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='done', updated_at=NOW()")
            conn.commit()
    except Exception as e:
        log.error(f"_bg_ops_text_fetch: {e}")
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('ops_text_fetch_run','pending',NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
                conn.commit()
        except Exception:
            pass
    finally:
        _ops_text_fetch_running = False


_ops_peer_benchmark_ran_on = None   # cc#593 day-lock: nightly materialization once/day


def _bg_ops_peer_benchmark():
    """cc#593: nightly rebuild of the ops-metrics peer-benchmark materialization (ops_peer_benchmark)
    from sector_ops_metrics. Day-locked, ~02:10 IST (after universe_technicals 02:05). Pure compute,
    no scraping. The cc#596 post-T+1 chain + the app-startup seed also refresh it; this is the daily
    floor so the peer stats never drift stale even on days with no T+1 extraction."""
    global _ops_peer_benchmark_ran_on
    today = _ist_now().date()
    if _ops_peer_benchmark_ran_on == today:
        return _SKIPPED
    try:
        import ops_peer_benchmark
        res = ops_peer_benchmark.run_rebuild()
        _ops_peer_benchmark_ran_on = today
        log.info(f"_bg_ops_peer_benchmark: {res}")
    except Exception as e:
        log.error(f"_bg_ops_peer_benchmark: {e}")
    return None


_engine_watchdog_ran_on = None   # cc#599 day-lock


def _bg_engine_watchdog():
    """cc#599 (ENGINE_WATCHDOG_V1): daily ~08:45 IST outcome/data-freshness audit of ALL scheduled
    jobs -> watchdog_gaps. After the nightly chain (01:00-02:05) + T+1 (08:00). DETECT+WRITE+SUGGEST
    only (no auto-fix); Claude-web reads open gaps + triggers fixes. Day-locked."""
    global _engine_watchdog_ran_on
    now = datetime.now(IST)
    if not (now.hour == 5 and 58 <= now.minute < 60):   # cc#622 C: watchdog 08:45 -> 05:58 (LAST, after result-corner 05:55)
        return _SKIPPED
    today = now.date()
    if _engine_watchdog_ran_on == today:
        return _SKIPPED
    _engine_watchdog_ran_on = today
    try:
        import engine_watchdog
        res = engine_watchdog.run_watchdog_conn()
        log.info(f"_bg_engine_watchdog: {res}")
    except Exception as e:
        log.error(f"_bg_engine_watchdog: {e}")
    return None

def _bg_watchdog_market():
    """cc#618 Section D: 15-min MARKET-HOURS writer-tick freshness check (engine-watchdog subset) —
    catches the intra-session 09:35-death class the daily 08:45 pass can't see. Gap + ops_log alert
    on breach; auto-resolves when the writer catches up."""
    try:
        import engine_watchdog
        res = engine_watchdog.run_market_checks_conn()
        if res.get("breached"):
            log.warning(f"_bg_watchdog_market: {res}")
    except Exception as e:
        log.error(f"_bg_watchdog_market: {e}")


_result_corner_ran_on = None   # cc#602 day-lock: news-vs-calendar verify + reconcile once/day


def _bg_result_corner_verify():
    """cc#602 (RESULT_CORNER_SPEC_V1): daily ~08:15 IST — verify the polished_news result-announcement
    set against earnings_calendar (alert if news leads = rule 7130 watchdog) + reconcile the missing
    reported companies (news catches small/midcaps the NSE feed misses), enqueuing each into the
    T+1 chain. Day-locked. Runs after the 08:00 T+1 so freshly-reconciled rows flow next cycle."""
    global _result_corner_ran_on
    now = datetime.now(IST)
    if not (now.hour == 5 and 55 <= now.minute < 60):   # cc#622 C: result-corner 08:15 -> 05:55 (AFTER T+1 05:40)
        return _SKIPPED
    today = now.date()
    if _result_corner_ran_on == today:
        return _SKIPPED
    _result_corner_ran_on = today
    try:
        import result_corner
        res = result_corner.run_daily_verify()
        log.info(f"_bg_result_corner_verify: {res}")
    except Exception as e:
        log.error(f"_bg_result_corner_verify: {e}")
    return None

_result_analysis_sweep_ran_on = None   # cc#618 Section B: Sunday full-season completion sweep day-lock

def _bg_result_analysis_weekly_sweep():
    """cc#618 Section B weekly catch-all: Sunday ~09:00 IST full-season re-check of announced-vs-analysis
    across the whole universe — regenerate any leftovers the daily T+1 auto-wire missed (canonical
    build_card, latest-quarter, content-diff so unchanged cards keep their vintage). Result coverage =
    daily auto-regen + this weekly sweep = zero manual batches. Day-locked."""
    global _result_analysis_sweep_ran_on
    now = datetime.now(IST)
    if not (now.weekday() == 6 and now.hour == 5 and now.minute < 15):   # cc#622 C: Sun sweep 09:00 -> 05:00 (clear 06:00-09:10)
        return _SKIPPED
    if _result_analysis_sweep_ran_on == now.date():
        return _SKIPPED
    _result_analysis_sweep_ran_on = now.date()
    try:
        import result_analysis_gen
        from datetime import timedelta as _td
        with _conn() as conn:
            res = result_analysis_gen.regenerate(conn, since=now.date() - _td(days=120))
        log.info(f"_bg_result_analysis_weekly_sweep: {res}")
    except Exception as e:
        log.error(f"_bg_result_analysis_weekly_sweep: {e}")
    return None


_futures_gap_backfill_ran_on = None   # cc#621 Issue 4: nightly futures gap-detect+backfill day-lock

def _bg_futures_gap_backfill():
    """cc#621 Issue 4 (founder-approved 23-Jul): nightly ~00:15 IST futures gap-detect + backfill,
    BEFORE the 01:00 nightly chain. Scan the last 7 trading days of fyers_fut (source='fyers_fut')
    5-min bars in intraday_prices; a session is a GAP if it has zero bars or fewer than 30% of the
    busiest day's count (thin/partial). If any gaps found, run the app-side futures backfill
    (fyers_range_backfill._run_backfill_sync) for the gap date RANGE only (min→max gap date, DO
    NOTHING upsert so present bars are never clobbered), then write outcome to ops_log. Day-locked,
    idempotent, off-market (00:1x IST is well outside 09:15-15:30, and fyers_backfill's own
    _assert_not_market_hours() is a second guard). NEVER touches the feed worker."""
    global _futures_gap_backfill_ran_on
    now = datetime.now(IST)
    if not (now.hour == 0 and 15 <= now.minute < 45):
        return _SKIPPED
    today = now.date()
    if _futures_gap_backfill_ran_on == today:
        return _SKIPPED
    _futures_gap_backfill_ran_on = today
    try:
        with _conn() as conn, conn.cursor() as cur:
            # per-day fyers_fut bar counts across the last 7 DISTINCT present trading dates
            cur.execute("""
                SELECT d, cnt FROM (
                    SELECT ts::date AS d, COUNT(*) AS cnt
                    FROM intraday_prices
                    WHERE source='fyers_fut' AND ts::date >= CURRENT_DATE - INTERVAL '10 days'
                    GROUP BY ts::date
                ) q ORDER BY d DESC LIMIT 7
            """)
            rows = cur.fetchall()   # [(date, count), ...] newest-first
        if not rows:
            log.info("_bg_futures_gap_backfill: no fyers_fut bars in window — nothing to scan")
            return None
        max_cnt = max(c for _, c in rows)
        thin = 0.30 * max_cnt
        gap_dates = sorted(d for d, c in rows if c == 0 or c < thin)
        if not gap_dates:
            log.info(f"_bg_futures_gap_backfill: no gaps (max/day={max_cnt}, days={len(rows)})")
            return None
        d_from, d_to = gap_dates[0], gap_dates[-1]
        import fyers_range_backfill_endpoints as frb
        res = frb._run_backfill_sync(str(d_from), str(d_to), None, None)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'futures_gap_backfill', %s, %s)""",
                        ("cc621_nightly_futures_backfill",
                         Json({"gap_dates": [str(x) for x in gap_dates],
                               "range": [str(d_from), str(d_to)], "max_per_day": max_cnt,
                               "result": res, "ist": now.isoformat()})))
            conn.commit()
        log.info(f"_bg_futures_gap_backfill: backfilled {gap_dates} -> {res}")
    except Exception as e:
        log.error(f"_bg_futures_gap_backfill: {e}")
    return None


_v9_paper_ran_month = None   # cc#629: (year,month) of the last V9 Brahmastra monthly rebalance

def _bg_v9_paper_monthly():
    """cc#629: V9 SECTOR PAIRS "Brahmastra" monthly rebalance — FIRST trading day of the month,
    ~01:50 IST (after the nightly GVM at 01:30). Month-locked here + idempotent per calendar month in
    the engine. PAPER-ONLY, strictly separate book (v9_paper_*); never touches any V8 table."""
    global _v9_paper_ran_month
    now = datetime.now(IST)
    if not (now.hour == 1 and now.minute == 50):
        return _SKIPPED
    if now.day > 7 or not _is_trading_day(now.date()):   # first trading day window
        return _SKIPPED
    mkey = (now.year, now.month)
    if _v9_paper_ran_month == mkey:
        return _SKIPPED
    _v9_paper_ran_month = mkey
    try:
        import v9_paper_engine
        res = v9_paper_engine.run_monthly()
        log.info(f"_bg_v9_paper_monthly: {res}")
    except Exception as e:
        log.error(f"_bg_v9_paper_monthly: {e}")
    return None


_shareholding_q_running = False
_shareholding_q_ran_on = None   # date of the last same-day dispatch (day-lock: 1 attempt/day in-window)


def _bg_shareholding_quarterly():
    """cc#598 (SHAREHOLDING_SCRAPE_CADENCE_V1, session_log id=7121): rolling-forward shareholding
    refresh. Self-gated to the LAST WEEK (day>=25) of Jul/Oct/Jan/Apr — SEBI LODR Reg 31 gives
    listed cos 21 days after quarter-end to file the shareholding pattern, so ~25th catches all
    filers. Dispatched once per day at ~09:30 IST (h==9/m==30 at the call site); day-locked here so
    it runs at most once per calendar day, retrying daily inside the window until the just-closed
    quarter is captured for the universe, then idling (the runner skips symbols that already carry
    it). Reuses the cc#391 pipeline (fundamentals_scraper.run_shareholding_quarterly); scheduler.py
    owns cadence only. Append-only (rolling 12Q); never computes GVM."""
    global _shareholding_q_running, _shareholding_q_ran_on
    now_ist = datetime.now(IST)
    if now_ist.month not in (1, 4, 7, 10) or now_ist.day < 25:
        return _SKIPPED
    today = now_ist.date()
    if _shareholding_q_ran_on == today or _shareholding_q_running:
        return _SKIPPED
    _shareholding_q_running = True
    try:
        import fundamentals_scraper
        res = fundamentals_scraper.run_shareholding_quarterly()
        _shareholding_q_ran_on = today
        log.info(f"_bg_shareholding_quarterly: {res}")
    except Exception as e:
        log.error(f"_bg_shareholding_quarterly: {e}")
    finally:
        _shareholding_q_running = False


# cc#524 QUARTERLY UPDATE FRAMEWORK (OPS_METRICS_FRAMEWORK_V1) supersedes cc#523's simple
# "monthly 5th" re-arm (spec: "the earlier off-season monthly 5th sweep is REPLACED by the four
# season-close bulk sweeps"). Three day-locked triggers below: daily T+1, Saturday scoped retry,
# and the Sep/Dec/Mar/Jun season-close sweep. Each just calls into ops_metrics_pipeline's
# already-built run_t1_refresh/run_saturday_retry/run_season_sweep -- scheduler.py only owns
# the cadence, not the logic (same split as every other _bg_* wrapper in this file).

_ops_metrics_t1_armed_day = None
_ops_metrics_saturday_armed_day = None
_ops_metrics_season_armed_month = None


def _bg_ops_metrics_t1():
    """cc#524 item 1: T+1 -- once daily ~08:00 IST, refresh every company whose
    earnings_calendar row flipped to status='reported' yesterday (fundamentals re-scrape +
    ops-metrics doc discovery/extraction, one screener visit per company)."""
    global _ops_metrics_t1_armed_day
    now = datetime.now(IST)
    if not (now.hour == 5 and 40 <= now.minute < 50):   # cc#622 C: T+1 08:00 -> 05:40 (clear 06:00-09:10)
        return _SKIPPED
    day_key = now.date().isoformat()
    if _ops_metrics_t1_armed_day == day_key:
        return _SKIPPED
    _ops_metrics_t1_armed_day = day_key
    try:
        import ops_metrics_pipeline
        res = ops_metrics_pipeline.run_t1_refresh()
        log.info(f"_bg_ops_metrics_t1: {res}")
    except Exception as e:
        log.error(f"_bg_ops_metrics_t1: {e}")


def _bg_ops_metrics_saturday():
    """cc#524 item 2: Saturday 10:00 IST SCOPED retry -- only earnings-calendar-dated
    companies whose T+1 run failed or found docs unpublished."""
    global _ops_metrics_saturday_armed_day
    now = datetime.now(IST)
    if not (now.weekday() == 5 and now.hour == 10 and now.minute < 10):
        return _SKIPPED
    day_key = now.date().isoformat()
    if _ops_metrics_saturday_armed_day == day_key:
        return _SKIPPED
    _ops_metrics_saturday_armed_day = day_key
    try:
        import ops_metrics_pipeline
        res = ops_metrics_pipeline.run_saturday_retry()
        log.info(f"_bg_ops_metrics_saturday: {res}")
    except Exception as e:
        log.error(f"_bg_ops_metrics_saturday: {e}")


def _bg_ops_metrics_season_sweep():
    """cc#524 item 3: one paced sweep each in Sep/Dec/Mar/Jun (1st of month, 10:00 IST) for
    companies with no tracked earnings_calendar result date -- staleness predicate on the
    latest stored fundamentals_history quarter (~120 days), not a blind scrape."""
    global _ops_metrics_season_armed_month
    now = datetime.now(IST)
    if now.month not in (3, 6, 9, 12):
        return _SKIPPED
    if not (now.day == 1 and now.hour == 10 and now.minute < 10):
        return _SKIPPED
    month_key = f"{now.year}-{now.month:02d}"
    if _ops_metrics_season_armed_month == month_key:
        return _SKIPPED
    _ops_metrics_season_armed_month = month_key
    try:
        import ops_metrics_pipeline
        res = ops_metrics_pipeline.run_season_sweep(dry_run=False)
        log.info(f"_bg_ops_metrics_season_sweep: {res}")
    except Exception as e:
        log.error(f"_bg_ops_metrics_season_sweep: {e}")


# cc#660 FEED_GUARDIAN_V1: _bg_feed_staleness_watch (cc#475), _log_feed_alert_ops, and
# _bg_feed_incident_relay (cc#501 item_4) are RETIRED — feed_guardian.py now owns per-source
# staleness detection+repair (guardian_tick) and the worker-incident relay (Telegram only for
# escalations, not routine reconnects — the 24-Jul alert-fatigue fix). Their in-process/app_config
# state is superseded by feed_guardian's single app_config JSON state (survives app redeploy).


_tc_scanner_running = False   # cc#464: single-flight guard for the 15-min scan+exit-check


def _bg_tc_scanner():
    """cc#464: TC Scanner — 13-check binary engine (id=399/400), full futures universe, BOTH
    sides, every 15 min during market hours (shares the qb_intraday_mark slot). Scans for new
    qualifiers (LATCH — first per symbol/side/day, never re-evaluated) then checks every OPEN
    hold against the current futures LTP for target/SL touch. Single-flight."""
    global _tc_scanner_running
    if _tc_scanner_running:
        return
    try:
        _tc_scanner_running = True
        import tc_scanner_endpoints as tcs
        scan_res = tcs.run_scan()
        exit_res = tcs.check_exits()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                        "VALUES (CURRENT_DATE, NOW(), 'tc_scanner', 'scan_tick', %s)",
                        (Json({**scan_res, **exit_res}),))
            conn.commit()
        log.info(f"tc_scanner tick: {scan_res} | {exit_res}")
    except Exception as e:
        log.error(f"_bg_tc_scanner: {e}")
    finally:
        _tc_scanner_running = False


def _bg_tc_scanner_eod():
    """cc#464: EOD sweep — one final check against the last available price so a target/SL
    touch between 15-min polls is not missed. Still-open positions stay OPEN (screener-only)."""
    try:
        import tc_scanner_endpoints as tcs
        res = tcs.eod_sweep()
        log.info(f"tc_scanner EOD sweep: {res}")
    except Exception as e:
        log.error(f"_bg_tc_scanner_eod: {e}")


def _bg_intraday_scan():
    """cc#481: 15-min intraday scanner WRITER. Runs BOTH the BUY and SHORT scans over the full
    active futures universe and records passes to intraday_watchlist (ON CONFLICT keeps one
    signal per symbol/side/day). The scans self-gate on the 09:30-15:15 IST window; the scheduler
    just fires m%15 on trading days. Single-flight; feed-independent (app-server, read-only reuse
    of FILTER_CONFIG)."""
    global _intraday_scan_running
    if _intraday_scan_running:
        return
    try:
        _intraday_scan_running = True
        import intraday_scanner_endpoints as ise
        buy = ise.scanner_intraday(limit=1)
        short = ise.scanner_intraday_short(limit=1)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                        "VALUES (CURRENT_DATE, NOW(), 'intraday_scan', 'auto_scan', %s)",
                        (Json({"buy_status": buy.get("status"), "buy_signals": buy.get("count"),
                               "buy_recorded": buy.get("recorded_to_watchlist"),
                               "short_status": short.get("status"), "short_signals": short.get("count"),
                               "short_recorded": short.get("recorded_to_watchlist"),
                               "universe": buy.get("universe")}),))
            conn.commit()
        log.info(f"intraday_scan: BUY {buy.get('status')} sig={buy.get('count')} rec={buy.get('recorded_to_watchlist')} | "
                 f"SHORT {short.get('status')} sig={short.get('count')} rec={short.get('recorded_to_watchlist')}")
    except Exception as e:
        log.error(f"_bg_intraday_scan: {e}")
    finally:
        _intraday_scan_running = False


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
        # cc#622 C: morning prep pulled BEFORE 06:00 so the 06:00-09:10 pre-market window is empty
        # (deploys/market-open never land on a scheduled batch). Ordered stagger 05:05-05:58.
        if h == 5 and m == 15:
            _spawn(_bg_fetch_global)        # cc#622 C: global indices 06:00 -> 05:15
        if h == 5 and m == 20:
            _spawn(_bg_fetch_market_news)   # cc#622 C: domestic + global RSS 06:00 -> 05:20 (task #38)
        # cc#242 (POSITION_NEWS_PIPELINE_V1): per-stock Google News for the full active futures
        # universe -> raw_news (source_type='company', alias-filtered). Supersedes the cc#207
        # position_news quarantine fetch.
        # cc#321: moved from 3x/day (08:30/12:30/16:30, market/dev hours) to ONCE at ~00:30 IST —
        # the no-push window just before the 01:00 nightly chain — so a CC deploy landing on the
        # fetch tick can no longer silently skip the run (the 16:30 miss found 08-Jul). Company
        # freshness is 1x/day now (intentional tradeoff for schedule stability); the 120h ingest
        # gate (news_fetcher.COMPANY_STALE_HOURS) widens per-run coverage to compensate.
        if h == 0 and m == 30:
            _spawn(_bg_fetch_stock_news)               # cc#321: once daily ~00:30 IST
        # cc#787 FUNNEL 2: universe-wide reco-biased fetch, 2 slots/day OFF market hours, each
        # taking half the ~209-symbol active futures universe. EVERY day (broker notes land on
        # weekends too). Deliberately not near 00:30 (_bg_fetch_stock_news) or the 01:00-02:40
        # nightly chain, so the Google-News 429 budget is never spent by two fetchers at once.
        # _spawn(fn, *args) forwards args and records fn.__name__ in scheduler_master — passing
        # the slot as an arg (not via a lambda) keeps the telemetry name real, not '<lambda>'.
        if h == 3 and m == 10:
            _spawn(_bg_fetch_universe_reco_news, 0)
        if h == 21 and m == 10:
            _spawn(_bg_fetch_universe_reco_news, 1)
        # cc#611: revived dedicated per-open-position Google News, 3 slots/day (07:35/13:35/19:35 IST),
        # EVERY day (positions held over weekends still get fresh news). Off the fyers feed -> any hour
        # is safe; alerting + the cc#599 watchdog freshness check catch a silent stall like the 06-Jul one.
        if m == 35 and h in (5, 13, 19):               # cc#622 C: slot 1 07:35 -> 05:35 (slots 2/3 unchanged)
            _spawn(_bg_fetch_position_news)
        if _is_trading_day(today) and ((h == 5 and m == 30) or (m == 20 and h in (16, 22))):
            _spawn(_bg_tag_news)                       # cc#207 Part C: symbol tagger. cc#622 C: morning slot 07:20 -> 05:30
        # cc#291: global intraday now runs 24x7 (was 06:00-23:30) — its symbols are commodities/
        # crypto/forex that trade near-continuously, so refresh them outside NSE hours too.
        if m % 5 == 0:
            _spawn(_bg_fetch_global_intraday)
        if now.weekday() == 0 and h == 5 and m == 40:
            _spawn(_bg_fu_sync)               # cc#622 C: Mon universe sync 08:00 -> 05:40 (same slot as T+1)
        if h == 5 and m == 10:
            # cc#420: EVERY day incl weekends/holidays — boards announce results over weekends and
            # Monday reporters confirm Sat/Sun dates; a trading-day guard here starved the calendar.
            _spawn(_bg_earnings_refresh)      # cc#225: refresh earnings_calendar. cc#622 C: 06:15 -> 05:10 (before T+1 + result-corner)
        if now.weekday() < 5 and h == 8 and m == 45:
            _spawn(_bg_fo_ban_fetch)          # cc#677: daily F&O ban-list fetch (keeps the TC ban alert current)
        if now.weekday() < 5 and h == 23 and m == 0:
            _spawn(_bg_fo_eod)                # cc#682: 23:00 NSE F&O bhavcopy EOD OI ingest (permanent OI fallback)
        if h == 9 and m == 0:
            _spawn(_bg_ca_daily_note)         # cc#658 part_4: 09:00 CA/data-integrity morning note
        if h == 9 and m == 5:
            _spawn(_bg_master_watchdog_note)  # cc#658 part_5: 09:05 consolidated master watchdog note
        if h == 5 and m == 15:
            _spawn(_bg_ca_sweep_daily)        # cc#659: DAILY (incl weekends) sharded full-universe CA scrape
        if now.weekday() == 6 and h == 5 and m == 30:
            _spawn(_bg_ca_weekly_scan)        # cc#658 part_2: Sunday 05:30 full 5y distortion scan
        # cc#667: Sat 06:10 detect new docs -> ops-metrics queue.
        # cc#773 SEASON MODE: through 15-Aug run it DAILY (2x pace) — peak results season outruns the
        # weekly burst (48% coverage gap on 01-Aug). Auto-reverts to Saturday-only after that date.
        # cc#795 RETIRED (founder 02-Aug): ops metrics are retired completely, so neither the
        # detect-and-queue sweep nor the coverage counter has anything to act on — the queue would
        # fill with work nobody drains, and the counter would alert on coverage of a dead metric.
        # The DOC scrape is untouched: _bg_ops_text_fetch below still runs, and the T+1 / Saturday /
        # season jobs keep staging doc_texts and re-scraping fundamentals for Level-2 Result Analysis.
        # if h == 6 and m == 10 and (now.weekday() == 5 or _ops_season_mode(now.date())):
        #     _spawn(_bg_weekly_ops_metrics_queue)
        # if h == 22 and m == 40:
        #     _spawn(_bg_ops_metrics_coverage)     # cc#773: nightly dual coverage counter + <80% alert
        if h == 6 and m == 25:
            _spawn(_bg_ops_polish_detector, True)  # cc#736: DAILY 06:25 sweep -> refresh POLISH_QUEUE card (day-locked)
        if h == 9 and m == 10:
            _spawn(_premarket_writer_check)   # cc_task #72 bug_1: 09:10 pre-market writer readiness
        # cc#660: 09:25 feed-silent-at-open now handled inside feed_guardian.guardian_tick()
        # (runs every 5 min market-hours; its 09:20-09:30 window covers the open-silence case).
        # cc#445 fix_4: ATM-OI daily snapshot — post-open (09:20) + EOD (15:35), trading days.
        if now.weekday() < 5 and _is_trading_day(now.date()) and ((h == 9 and m == 20) or (h == 15 and m == 35)):
            _spawn(_bg_oi_snapshot)
        if _is_trading_day(now.date()) and h == 15 and m == 40:
            _spawn(_bg_result_radar_snapshot)   # cc#772: post-close freeze of the Result Radar card (T-1 tag)
        if _is_trading_day(now.date()) and h == 16 and m == 15:
            _spawn(_bg_result_radar_log)        # cc#772: nightly T+1 outcome backfill for the study
        if _is_market_hours(now) and m % 5 == 0:
            _spawn(_bg_signal_writer)
            _spawn(_bg_guardian_tick)          # cc#660: 5-min market-hours per-leg detect->repair (feed_guardian)
        if m % 15 == 0:
            # cc#660: 15-min ALL-DAYS worker-liveness (heartbeat >45min) + off-hours incident relay.
            # Replaces the m%5 feed_incident_relay — worker incidents any hour still surface here.
            _spawn(_bg_guardian_offhours)
        # cc#442: V14 intraday engine 5-min cycle (paper) — app-side, trading days only, market hours.
        # Read-only on V8/V10; does NOT touch worker/** (Phase A safe).
        if _is_market_hours(now) and _is_trading_day(now.date()) and m % 5 == 0:
            _spawn(_bg_v14_cycle)
            _spawn(_bg_v8_paper_exit)         # cc_task #72 bug_0: live exit pass (primary)
            _spawn(_bg_v10_tick)
            _spawn(_bg_pcr_intraday)
            _spawn(_bg_tc_lite)               # cc_task #77: TC Lite screener (09:30-15:15 gate inside)
            _spawn(_bg_smartgain_mtm)         # cc#123: refresh SmartGain LTP/MTM from live cmp_prices
            _spawn(_bg_fut_rest_fallback)     # cc#770: REST futures fallback when native WS fut leg is dark
            # _spawn(_bg_intraday_paper)  # INACTIVE 18-Jun-2026 — on-demand only via /api/intraday/tick
            if m % 15 == 0:
                _spawn(_bg_qb_intraday_mark)
                _spawn(_bg_fetch_market_news)   # task #40: live RSS refresh during market hours
                _spawn(_bg_intraday_scan)       # cc#481: 15-min BUY+SHORT scan -> intraday_watchlist (09:30-15:15 gate inside)
                _spawn(_bg_tc_scanner)          # cc#464: TC Scanner 13-check binary engine -> tc_scanner_holds
        # cc_task #89: yahoo EOD raw_prices refresh at 15:35 IST (5 min after close) so
        # v8_engine EOD (15:45) and the evening journal review see today's official closes
        # ~5h sooner. The 01:00 IST run (below) stays as the nightly safety re-run.
        # Weekday-only. NO GVM/QB cascade here — that stays in the 01:00-02:00 nightly chain.
        # cc#728: HOURLY open-position TC-star batch on the :30 mark, 09:30-15:30 IST (trading days).
        # Predictable star cadence (7 marks/day) replacing cc#720's per-render 15-min cache.
        if now.weekday() < 5 and _is_trading_day(today) and m == 30 and 9 <= h <= 15:
            _spawn(_bg_tc_position_stars)
        # cc#748: TC outcome sim — hourly STRONG-entry tracker on the session's entry marks (09:30-15:30
        # today, sourced from nse_session so it follows the 03-Aug NSE change). Trading-day gate + all
        # entry/exit logic live inside run_tc_sim_tick(); dispatch stays crash-proof if the module is absent.
        try:
            import nse_session as _nss
            if now.weekday() < 5 and _nss.is_entry_tick(h, m):
                _spawn(_bg_tc_sim_tick)
        except Exception as _e:
            log.error(f"tc_sim dispatch: {_e}")
        if now.weekday() < 5 and h == 15 and m == 20: _spawn(_bg_gate_rebalance)  # cc#190: auto-close over-slot paper positions
        if now.weekday() < 5 and h == 15 and m == 35: _spawn(_bg_yahoo_daily_sync)
        if now.weekday() < 5 and _is_trading_day(now.date()) and h == 15 and m == 32:
            _spawn(_bg_tc_scanner_eod)   # cc#464: EOD sweep, just before close-of-session jobs
        if now.weekday() < 5 and h == 15 and m == 40: _spawn(_bg_heal_intraday)  # cc#238 Branch B: heal session gaps before EOD
        if h == 15 and m == 45: _spawn(_bg_v8_eod)
        if h == 15 and m == 50: _spawn(_bg_adr_pcr)
        if h == 16 and m == 0:
            _spawn(_bg_adr_pcr_retry)            # task #59: 10-min ADR/PCR watchdog retry
            _spawn(_bg_tc_screener_precompute)   # task #43: TC screener cache
            _spawn(_bg_stock_news_watchdog)      # cc#245: stock-news staleness/all-blocked alert
        if now.weekday() < 5 and h == 16 and m == 10:
            _spawn(_bg_v21_killswitch)           # cc#158: V2.1 filter kill-switch check
        if h == 16 and m == 15:
            _spawn(_bg_feed_daily_log)           # cc#495 change_4: daily feed health summary (reads feed_guardian state, cc#660)
            _spawn(_bg_guardian_eod_oi)          # cc#660: universe-wide futures OI-staleness tripwire (cc#515 parity)
        # cc#517: NSE EOD ingest suite, 18:30 IST + retries 19:30/20:30 (idempotent upserts --
        # a retry just fills whatever NSE hadn't published yet on the prior pass).
        if now.weekday() < 5 and h == 18 and m == 30: _spawn(_bg_nse_eod_ingest)
        if now.weekday() < 5 and h == 19 and m == 30: _spawn(_bg_nse_eod_ingest)
        if now.weekday() < 5 and h == 20 and m == 30: _spawn(_bg_nse_eod_ingest)
        # Nightly batch shifted to 01:00–01:45 IST (task #31). The old 21:00–22:05
        # window collided with CC deploy pushes — a Railway redeploy kills the
        # scheduler mid-job (caused the 18-Jun raw_prices gap). 1 AM = no-push window.
        # 15-min spacing gives each job runway; order preserves deps (prices→QB→GVM→pivots).
        if h == 1 and m == 0:   _spawn(_bg_yahoo_daily_sync)
        if h == 1 and m == 5:   _spawn(_bg_lot_sync)   # cc#314: nightly Fyers lot-size audit (day-locked)
        if h == 1 and m == 15:  _spawn(_bg_qb_eod)
        if h == 1 and m == 30:  _spawn(_bg_gvm)
        if h == 1 and m == 45:  _spawn(_bg_pivots)
        if h == 1 and m == 47:  _spawn(_bg_universe_pivots)    # cc#342: full-universe v8_paper_pivots refresh
        if h == 1 and m == 55:  _spawn(_check_pivots_health)   # cc_task #68 Bug 1: pivot watchdog
        if h == 1 and m == 50:  _spawn(_bg_cleanup_news)   # task #38: 30-day news purge
        if h == 1 and m == 50:  _spawn(_bg_v9_paper_monthly)   # cc#629: V9 Brahmastra monthly rebalance (first trading day, month-locked)
        if h == 1 and m == 52:  _spawn(_bg_log_retention)  # cc#469: 30d tick-class telemetry purge
        if h == 2 and m == 10:  _spawn(_bg_rvol_profiles)  # cc#674: nightly RVOL cum-vol profiles (after universe_technicals)
        # cc#499 (session_log id=5415, 18-Jul-2026): ALL scheduled MF scraping OFF after 17-Jul --
        # the scrape build was a one-time model exercise, go-forward = data vendor. The three
        # UNCONDITIONAL jobs below (no flag gate -- they ran on their timer regardless of any human
        # action) are disabled here. The FLAG-GATED jobs (_bg_mf_returns_backfill/_bg_mf_v15_wiring/
        # _bg_mf_weekly_manual/_bg_mf_mc_discover/_bg_mf_mc_oneshot) are NOT scraping-on-a-timer --
        # they are the execution mechanism the /api/v15/mf/* manual arm endpoints rely on (arm sets
        # an app_config flag to 'pending', these poll and pick it up within ~3 min) and per spec
        # "manual trigger endpoints STAY -- they become on-demand-only", so they stay registered.
        # if h == 1 and m == 10:  _spawn(_bg_mf_nav)             # cc#466: AMFI daily NAV + seed reconcile -- DISABLED cc#499
        if h == 1 and m == 20:  _spawn(_bg_mf_score_nightly)   # cc#520: MQS recompute stays on -- pure compute, no scraping
        if m % 3 == 0:          _spawn(_bg_mf_returns_backfill)  # flag-gated -- manual /returns_backfill arm only
        if m % 3 == 1:          _spawn(_bg_mf_v15_wiring)  # flag-gated -- manual /wire_all or /run_monthly arm only
        if m % 3 == 2:          _spawn(_bg_mf_weekly_manual)  # flag-gated -- manual /run_weekly arm only
        # if now.weekday() == 5 and h == 6 and m == 30:  _spawn(_bg_mf_weekly)        # unconditional Sat cron -- DISABLED cc#499
        # if now.day == 12 and h == 6 and m == 20:       _spawn(_bg_mf_aum_monthly)   # unconditional monthly cron -- DISABLED cc#499
        # cc#777: V15 MF REVIVAL — monthly MC refresh on the 11th. Founder 02-Aug explicitly
        # SUPERSEDES the cc#499/id=5415 cadence-void and the id=5428 no-further-scrape framing for
        # THIS design (vendor stays the eventual production source; this is the interim). Day+hour
        # gate lives inside the job, which is day-locked and writes the job_runs spine.
        if now.day == 11:       _spawn(_bg_mf_monthly_mc)
        # cc#777 Part 2 + cc#778: NIGHTLY derived-NAV rebuild (post-01:00 Yahoo EOD) + Saturday
        # self-audit only. Hour/minute gates + day-locks live inside each job.
        if h == 1:              _spawn(_bg_mf_derived_nightly)
        if now.weekday() == 5 and h == 7:  _spawn(_bg_mf_derived_audit)
        _spawn(_bg_mf_mc_discover)  # cc#500: flag-gated, checked every tick for fast dev-iteration turnaround
        _spawn(_bg_mf_mc_oneshot)   # cc#500: flag-gated one-time full-set fill, checked every tick
        _spawn(_bg_bt14_fut_oi)     # cc#538: flag-gated ORB basis/OI research backfill (probe|backfill), off-market
        _spawn(_bg_yahoo_new_listings)  # cc#539: flag-gated one-shot Yahoo EOD backfill for new listings
        # cc#795 RETIRED: this runner calls run_company -> run_extraction/write_extraction, i.e. the
        # LLM spend and the sector_ops_metrics write. Disarmed here AND hard-stopped inside
        # ops_metrics_pipeline, so an armed app_config flag cannot resurrect it either.
        # _spawn(_bg_ops_metrics_backfill)  # cc#523/524: flag-gated, checked every tick
        _spawn(_bg_ops_polish_detector)    # cc#736: flag-gated (ops_polish_run=run_now), checked every tick — refreshes the POLISH_QUEUE card
        _spawn(_bg_ops_text_fetch)         # cc#527: flag-gated fetch-only phase, 23:00-06:00 IST window
        _spawn(_bg_ops_metrics_t1)             # cc#524: daily ~08:00 IST T+1 refresh (day-locked inside)
        _spawn(_bg_ops_metrics_saturday)       # cc#524: Saturday 10:00 IST scoped retry (day-locked inside)
        _spawn(_bg_ops_metrics_season_sweep)   # cc#524: Sep/Dec/Mar/Jun 1st 10:00 IST bulk sweep (month-locked inside)
        _spawn(_bg_result_corner_verify)       # cc#602: daily ~08:15 IST news-vs-calendar result verify + reconcile (self-gated)
        _spawn(_bg_result_analysis_weekly_sweep)  # cc#618 Section B: Sunday ~09:00 IST full-season result_analysis catch-all (self-gated)
        _spawn(_bg_engine_watchdog)            # cc#599: daily ~08:45 IST engine watchdog outcome audit (self-gated)
        _spawn(_bg_futures_gap_backfill)       # cc#621 Issue 4: nightly ~00:15 IST futures gap-detect+backfill (self-gated, off-market)
        if _is_market_hours(now) and _is_trading_day(today) and m % 15 == 0:
            _spawn(_bg_watchdog_market)         # cc#618 Section D: 15-min market-hours writer-tick freshness
        if now.month in (1, 4, 7, 10) and now.day >= 25 and h == 0 and m == 45:
            _spawn(_bg_shareholding_quarterly)   # cc#598: last-week Jul/Oct/Jan/Apr shareholding refresh (day-locked inside). cc#622 B: 09:30 market-hours -> 00:45 night window
        if h == 5 and m == 5:  _spawn(_bg_scheduler_master_daily_audit)   # cc#525: registry drift audit. cc#622 C: 08:45 -> 05:05 (clear 06:00-09:10)
        if h == 2 and m == 0:   _spawn(_bg_v8_paper_exit_eod)  # cc_task #72 bug_0: EOD-close exit fallback (after EOD load + heal)
        if h == 2 and m == 5:   _spawn(_bg_universe_technicals)  # cc#154: full-universe technicals, after GVM (01:30) + pivots (01:45)
        # cc#795 RETIRED: ops_peer_benchmark is derived FROM sector_ops_metrics, which is now a frozen
        # archive — rebuilding it nightly from frozen input would only rewrite the same rows forever.
        # The table is left in place (read paths unaffected), just no longer regenerated.
        # if h == 2 and m == 10:  _spawn(_bg_ops_peer_benchmark)   # cc#593: nightly rebuild
        # cc#468/470: GVM 5yr deep backfill — primary nightly kick + hourly off-market
        # resume (both no-op once flag='done' or a run is in-flight; off-market gate inside).
        if h == 2 and m == 20:  _spawn(_bg_gvm_backfill)
        if m == 25 and not _is_market_hours(now):  _spawn(_bg_gvm_backfill)
        # cc#471: extension backfill (all 5yr-deep syms). Sequenced after the main run
        # (gate inside checks gvm_backfill_run='done') + shares the single-flight guard.
        if h == 2 and m == 40:  _spawn(_bg_gvm_backfill_ext)
        if m == 45 and not _is_market_hours(now):  _spawn(_bg_gvm_backfill_ext)


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
    # cc#525: seed + drift-audit the scheduler_master registry on every startup (Railway
    # auto-deploys ~90s per push, so this doubles as the "on app startup" half of the daily
    # 08:45 IST cadence -- most deploys land well inside a day of any registration drift).
    _spawn(_bg_scheduler_master_startup_audit)
    log.info("scheduler.start_background: launched")
    return t


def _bg_scheduler_master_startup_audit():
    try:
        import scheduler_master
        scheduler_master.seed_registry()
        res = scheduler_master.run_drift_audit()
        log.info(f"scheduler_master startup audit: {res}")
    except Exception as e:
        log.error(f"scheduler_master startup audit failed: {e}")


_scheduler_master_audit_armed_day = None


def _bg_scheduler_master_daily_audit():
    """cc#525 item 4: daily 08:45 IST drift audit (the startup audit above covers the
    "on app startup" half of the requirement; this covers days with no deploy)."""
    global _scheduler_master_audit_armed_day
    now = datetime.now(IST)
    if not (now.hour == 5 and now.minute == 5):   # cc#622 C: drift audit 08:45 -> 05:05 (clear 06:00-09:10)
        return _SKIPPED
    day_key = now.date().isoformat()
    if _scheduler_master_audit_armed_day == day_key:
        return _SKIPPED
    _scheduler_master_audit_armed_day = day_key
    try:
        import scheduler_master
        res = scheduler_master.run_drift_audit()
        log.info(f"scheduler_master daily audit: {res}")
    except Exception as e:
        log.error(f"scheduler_master daily audit failed: {e}")


async def stop_background():
    if _stop_event: _stop_event.set()
    for t in list(_bg_tasks): t.cancel()
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)
    log.info("scheduler stopped")
