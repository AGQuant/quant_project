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
    # cc#206/#211: canonical trading-day guard (single _is_trading_day helper, nse_holidays
    # based — no duplicate inline check). The 09:10 readiness restart fired on SAT 04-Jul
    # (v8_metrics "stale" 1045 min → forced restart on a non-trading day) — needless
    # cold-boot risk (id=166 class). No live writer runs off-session, so skip weekends +
    # NSE holidays entirely. (Writes are also gated at the writer choke point in cc#211.)
    if not _is_trading_day(today):
        return
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

def _bg_v21_killswitch():
    """cc#158: nightly V2.1 candidate-filter kill-switch check (16:10 IST, after
    close + ADR/TC). Auto-disables a basket's V2.1 filters + alerts if signal
    count starves or rolling WR decays >10pp below baseline (respecting the
    20-trading-day / 15-signal sample-discipline warmup). Never auto-re-enables."""
    global _v21_ks_ran_today
    today = _ist_now().date()
    if _v21_ks_ran_today == today: return
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
    if _eod_ran_today == today or _eod_running: return
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
        return
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
        return
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
            return

        import v8_endpoints, v8_paper
        mood = v8_endpoints.market_mood()
        buy_slots, sell_slots = mood.get("buy_slots"), mood.get("sell_slots")
        if buy_slots is None or sell_slots is None:
            _log_alert("gate_rebalance_no_mood",
                       f"skipped 15:20 gate rebalance — mood missing slots (mood={mood.get('mood')})")
            return

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
    # cc#417 fix_1: never compute/write adr_daily or pcr_daily on a non-trading day (weekend/holiday) —
    # those phantom 0-rows displaced Friday's real ADR as "latest" and broke the mood gate.
    if not _is_trading_day(today):
        log.debug(f"adr_pcr: skip — {today} is not a trading day")
        return
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
    if not _is_trading_day(today): return                 # cc#417: no retry on non-trading days
    if _adr_pcr_ran_today == today: return                # already verified-complete
    log.warning("adr_pcr: 15:50 run incomplete — retrying at 16:00")
    _bg_adr_pcr()

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
        with _conn() as conn:
            gvm_nightly.gvm_recompute(conn)
            _log_health(conn, "gvm_recompute", {"date": str(today)})  # cc#255
        _gvm_ran_today = today
        log.info("gvm_recompute done")
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
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='gvm_backfill_run'")
            r = cur.fetchone()
        flag = (r[0] if r else None)
        if flag == "done":
            return
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
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_config WHERE key IN ('gvm_backfill_run','gvm_backfill_ext_run')")
            flags = {k: v for k, v in cur.fetchall()}
        if flags.get('gvm_backfill_run') != 'done':   # sequence: main run first
            return
        if flags.get('gvm_backfill_ext_run') == 'done':
            return
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

def _bg_universe_pivots():
    """cc#342: nightly full-universe (~1720+) rolling-5d pivot rebuild into v8_paper_pivots.
    The 01:45 _bg_pivots only covers the 211 futures symbols, so GVM companies outside it
    showed a stale pivot_date on the CIO Pivot Range card (e.g. 2026-06-13). This refreshes
    the whole GVM universe so that card (which reads v8_paper_pivots latest) is always current.
    Same table + ON CONFLICT (symbol, pivot_date) upsert — idempotent, complements _bg_pivots."""
    global _upivots_ran_today
    today = _ist_now().date()
    if _upivots_ran_today == today: return
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
    if _ut_ran_today == today: return
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
    if _qb_eod_ran_today == today or _qb_eod_running: return
    if not _is_trading_day(today): return
    _qb_eod_running = True
    try:
        import qb_eod_checker, qb_rebalance
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT basket_name, next_rebalance FROM quant_basket_registry "
                            "WHERE is_active=TRUE ORDER BY basket_name")
                baskets = [(r[0], r[1]) for r in cur.fetchall()]
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
            _log_health(conn, "qb_eod", {"checked": checked, "rebalanced": rebalanced})  # cc#255
        log.info(f"qb_eod: {checked} baskets checked, rebalanced={rebalanced}")
        _qb_eod_ran_today = today
    except Exception as e: log.error(f"qb_eod: {e}")
    finally: _qb_eod_running = False

def _bg_fu_sync():
    global _fu_sync_ran_this_week
    today = _ist_now().date()
    if _fu_sync_ran_this_week == today: return
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
    if _lot_sync_ran_today == today: return
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

def _bg_open_bars_alarm():
    """cc#229: 09:25 IST trading-day feed-silence alarm. If almost no intraday bars landed
    since 09:15, the live feed is silent at open (cold-boot zombie / dead worker) — fire a
    feed_silent_at_open alert. Independent of subscribe_verify (which can pass while bars are
    still zero, so it must NOT suppress this).
    Threshold: spec said <1000, but at 09:25 a healthy feed has only ~2 five-min buckets
    (~420 eq+fut symbols x 2 ≈ 840 bars), so 1000 would false-alarm daily. Using <400 catches
    genuine silence (~0) with margin below the healthy floor — founder can retune."""
    try:
        now = _ist_now()
        if not _is_trading_day(now.date()):
            return
        open_ts = now.replace(hour=9, minute=15, second=0, microsecond=0).replace(tzinfo=None)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM intraday_prices WHERE ts >= %s AND timeframe='5m'",
                        (open_ts,))
            bars = cur.fetchone()[0]
        if bars < 400:
            _log_alert("feed_silent_at_open",
                       f"09:25 IST: only {bars} intraday 5m bars since 09:15 — live feed appears "
                       f"SILENT at open (cold-boot zombie / dead worker). Manual restart may be needed.")
            log.error(f"FEED SILENT AT OPEN: {bars} 5m bars since 09:15 (<400)")
        else:
            log.info(f"open-bars alarm OK: {bars} 5m bars since 09:15")
    except Exception as e:
        log.error(f"open_bars_alarm: {e}")

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

def _bg_fetch_stock_news():
    """cc#242 (POSITION_NEWS_PIPELINE_V1): per-stock Google News for the full active futures
    universe -> raw_news (source_type='company' + symbol), alias-filtered at ingest. Single
    funnel with market news; supersedes the position_news quarantine fetch (cc#207/id=402).
    3x/day on trading days: 08:30/12:30/16:30."""
    try:
        import news_fetcher
        with _conn() as conn:
            res = news_fetcher.fetch_stock_news(conn)
        log.info(f"fetch_stock_news: {res}")
    except Exception as e:
        log.error(f"fetch_stock_news: {e}")

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
            return
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
            return
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
            return
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
            return
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



# cc#475: feed staleness Telegram alert — INDEPENDENT of the signal-writer watchdogs (which
# only restart the writer). This checks the FEED WORKER's own output (intraday_prices) from
# the main app, so it survives the worker dying outright. Per-source alert/recovery state kept
# in-process (module globals) — cheap, no DB table needed for a 5-min-cadence check.
_FEED_ALERT_STATE = {}   # source -> {'alerted_at': datetime|None, 'was_stale': bool}
FEED_STALE_MIN = 15
FEED_ALERT_THROTTLE_MIN = 30
FEED_SOURCES = ("fyers_eq", "fyers_fut")


def _bg_feed_staleness_watch():
    """cc#475: every 5 min, 09:20-15:30 IST on trading days — check MAX(ts) per source in
    intraday_prices. Stale >15min -> Telegram alert (throttled 1/30min/source) + ops_log
    (category=feed_alert). Recovery message + ops_log when bars resume. 09:35 first-bar-of-day
    check: zero bars since open -> alert immediately (skip the 15-min wait)."""
    now = _ist_now()
    if not (_is_trading_day(now.date()) and dt_time(9, 20) <= now.time() <= dt_time(15, 30)):
        return
    try:
        import v10_st_ema
        with _conn() as conn, conn.cursor() as cur:
            for src in FEED_SOURCES:
                cur.execute("SELECT MAX(ts) FROM intraday_prices WHERE source=%s AND ts::date=%s",
                            (src, now.date()))
                last = cur.fetchone()[0]
                st = _FEED_ALERT_STATE.setdefault(src, {"alerted_at": None, "was_stale": False})

                # first-bar-of-day: 09:35 and NOTHING since open -> alert now, don't wait for 15min math
                if last is None:
                    if now.time() >= dt_time(9, 35) and now.time() < dt_time(9, 40) and not st["alerted_at"]:
                        msg = f"FEED STALE — no {src} bars since 09:15 open ({now.strftime('%H:%M')} IST). Worker likely down — check truthful-friendship."
                        v10_st_ema.telegram_alert(msg)
                        st["alerted_at"] = now; st["was_stale"] = True
                        _log_feed_alert_ops(cur, src, "alert", msg, None)
                        conn.commit()
                    continue

                age_min = (now - last).total_seconds() / 60.0
                if age_min > FEED_STALE_MIN:
                    can_alert = (st["alerted_at"] is None or
                                 (now - st["alerted_at"]).total_seconds() >= FEED_ALERT_THROTTLE_MIN * 60)
                    if can_alert:
                        msg = (f"FEED STALE — last {src} bar {last.strftime('%H:%M:%S')} IST "
                               f"({age_min:.0f} min ago). Worker likely down — check truthful-friendship.")
                        v10_st_ema.telegram_alert(msg)
                        st["alerted_at"] = now
                        _log_feed_alert_ops(cur, src, "alert", msg, age_min)
                        conn.commit()
                    st["was_stale"] = True
                elif st["was_stale"]:
                    msg = f"FEED RECOVERED — {src} flowing, last bar {last.strftime('%H:%M:%S')} IST."
                    v10_st_ema.telegram_alert(msg)
                    st["was_stale"] = False
                    st["alerted_at"] = None
                    _log_feed_alert_ops(cur, src, "recovery", msg, age_min)
                    conn.commit()
    except Exception as e:
        log.error(f"_bg_feed_staleness_watch: {e}")


def _log_feed_alert_ops(cur, source, kind, message, age_min):
    try:
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), 'feed_alert', %s, %s)",
                    (kind, Json({"source": source, "message": message,
                                 "age_min": round(age_min, 1) if age_min is not None else None})))
    except Exception as e:
        log.warning(f"_log_feed_alert_ops: {e}")


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
        if h == 6 and m == 0:
            _spawn(_bg_fetch_global)
            _spawn(_bg_fetch_market_news)   # task #38: domestic + global RSS
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
        if _is_trading_day(today) and m == 20 and h in (7, 16, 22):
            _spawn(_bg_tag_news)                       # cc#207 Part C: symbol tagger — off-session (backfills on first run)
        # cc#291: global intraday now runs 24x7 (was 06:00-23:30) — its symbols are commodities/
        # crypto/forex that trade near-continuously, so refresh them outside NSE hours too.
        if m % 5 == 0:
            _spawn(_bg_fetch_global_intraday)
        if now.weekday() == 0 and h == 8 and m == 0:
            _spawn(_bg_fu_sync)
        if h == 6 and m == 15:
            # cc#420: EVERY day incl weekends/holidays — boards announce results over weekends and
            # Monday reporters confirm Sat/Sun dates; a trading-day guard here starved the calendar.
            _spawn(_bg_earnings_refresh)      # cc#225: refresh earnings_calendar BEFORE the 09:10 pre-market check
        if h == 9 and m == 10:
            _spawn(_premarket_writer_check)   # cc_task #72 bug_1: 09:10 pre-market writer readiness
        if h == 9 and m == 25:
            _spawn(_bg_open_bars_alarm)       # cc#229: 09:25 feed-silent-at-open alarm
        # cc#445 fix_4: ATM-OI daily snapshot — post-open (09:20) + EOD (15:35), trading days.
        if now.weekday() < 5 and _is_trading_day(now.date()) and ((h == 9 and m == 20) or (h == 15 and m == 35)):
            _spawn(_bg_oi_snapshot)
        if _is_market_hours(now) and m % 5 == 0:
            _spawn(_bg_signal_writer)
            _spawn(_bg_feed_staleness_watch)   # cc#475: independent feed-worker watchdog + Telegram alert
        # cc#442: V14 intraday engine 5-min cycle (paper) — app-side, trading days only, market hours.
        # Read-only on V8/V10; does NOT touch worker/** (Phase A safe).
        if _is_market_hours(now) and _is_trading_day(now.date()) and m % 5 == 0:
            _spawn(_bg_v14_cycle)
            _spawn(_bg_v8_paper_exit)         # cc_task #72 bug_0: live exit pass (primary)
            _spawn(_bg_v10_tick)
            _spawn(_bg_pcr_intraday)
            _spawn(_bg_tc_lite)               # cc_task #77: TC Lite screener (09:30-15:15 gate inside)
            _spawn(_bg_smartgain_mtm)         # cc#123: refresh SmartGain LTP/MTM from live cmp_prices
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
            _spawn(_bg_feed_daily_log)           # cc#495 change_4: daily feed health summary (every day, worker runs weekends too)
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
        if h == 1 and m == 52:  _spawn(_bg_log_retention)  # cc#469: 30d tick-class telemetry purge
        if h == 1 and m == 10:  _spawn(_bg_mf_nav)             # cc#466: AMFI daily NAV + seed reconcile (V15 MF)
        # cc#477: V15 MF returns. Flag-gated one-shot fires within ~3 min of arming (feed-independent,
        # app-server only, real internet). Weekly cron Sat 06:30 IST; monthly comprehensive job on
        # the 12th (cc#491 course-correct, moved off the 3rd — session_log id=4734).
        if m % 3 == 0:          _spawn(_bg_mf_returns_backfill)
        if m % 3 == 1:          _spawn(_bg_mf_v15_wiring)  # cc#491: AUM/ER/holdings wire-all, offset from returns backfill
        if m % 3 == 2:          _spawn(_bg_mf_weekly_manual)  # cc#491 course-correct: manual /run_weekly trigger poll
        if now.weekday() == 5 and h == 6 and m == 30:  _spawn(_bg_mf_weekly)
        if now.day == 12 and h == 6 and m == 20:       _spawn(_bg_mf_aum_monthly)
        _spawn(_bg_mf_mc_discover)  # cc#500: flag-gated, checked every tick for fast dev-iteration turnaround
        if h == 2 and m == 0:   _spawn(_bg_v8_paper_exit_eod)  # cc_task #72 bug_0: EOD-close exit fallback (after EOD load + heal)
        if h == 2 and m == 5:   _spawn(_bg_universe_technicals)  # cc#154: full-universe technicals, after GVM (01:30) + pivots (01:45)
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
    log.info("scheduler.start_background: launched")
    return t


async def stop_background():
    if _stop_event: _stop_event.set()
    for t in list(_bg_tasks): t.cancel()
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)
    log.info("scheduler stopped")
