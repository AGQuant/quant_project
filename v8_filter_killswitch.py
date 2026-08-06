"""
cc#158 — V2.1 candidate-filter kill-switches (live, no backtest).

Base baskets are already proven (262-day locked backtests). The V2.1 filters
(hourly / week_index_52 / fall_from_day_high) ship enabled:true and are policed
LIVE by two kill-switches instead of a pre-deploy backtest gate:

  1. STARVATION  — auto-disable + alert if a basket's signal count over the
     trailing 5 trading days falls below 20% of its normal rate
     (< 0.2 * normal_rate/day * 5 signals in the window).
  2. WR DECAY    — auto-disable + alert if rolling win-rate (closed paper
     trades since enable) falls > 10pp below the locked baseline, after >= 10
     closed signals.

Sample discipline (same rigor as backtesting, collected live): no lock/reject
judgment until >= 20 trading days OR >= 15 signals have accrued since the group
was enabled. Kill-switches only ever DISABLE — never auto-re-enable. Every trip
alerts Arpit (ops_log category=alert + cc_task_logs #158).

Disabling a group makes the signal writer skip that basket's V2.1 hard gate on
the next tick -> the basket reverts to its exact locked behavior.

State table: v8_filter_state (basket PK, enabled, baseline_wr, normal_rate,
enabled_at, disabled_at, disabled_reason, updated_at).
"""

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("scorr.killswitch")
IST = timezone(timedelta(hours=5, minutes=30))

SAMPLE_MIN_TRADING_DAYS = 20
SAMPLE_MIN_SIGNALS      = 15
STARVATION_WINDOW_TD    = 5
STARVATION_FRACTION     = 0.20   # < 20% of normal rate over the window
WR_MIN_CLOSED           = 10
WR_DROP_PP              = 10.0   # > 10pp below baseline


def _ist_now():
    return datetime.now(IST).replace(tzinfo=None)


def _log_alert(conn, basket: str, message: str):
    """Visible alert -> ops_log (category=alert) + cc_task_logs(#158)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ops_log (session_date, session_ts, category, title, details)
                   VALUES (CURRENT_DATE, NOW(), 'alert', %s, %s)""",
                (f"v21_killswitch:{basket}",
                 '{"basket":"%s","message":"%s","ist":"%s"}'
                 % (basket, message.replace('"', "'"), _ist_now().isoformat())))
            cur.execute(
                """INSERT INTO cc_task_logs (task_id, actor, level, message)
                   VALUES (158, 'killswitch', 'warn', %s)""",
                (f"{basket}: {message}",))
        conn.commit()
        log.error(f"ALERT v21_killswitch[{basket}]: {message}")
    except Exception as e:
        log.error(f"_log_alert failed ({basket}): {e}")


def _last_n_trading_days(cur, n: int):
    cur.execute(
        """SELECT DISTINCT price_date FROM raw_prices
           WHERE price_date <= CURRENT_DATE ORDER BY price_date DESC LIMIT %s""", (n,))
    return [r[0] for r in cur.fetchall()]


def _trading_days_since(cur, since_date) -> int:
    cur.execute(
        """SELECT COUNT(DISTINCT price_date) FROM raw_prices
           WHERE price_date >= %s AND price_date <= CURRENT_DATE""", (since_date,))
    return int(cur.fetchone()[0] or 0)


def _signals_since(cur, basket: str, since_date) -> int:
    cur.execute(
        """SELECT COUNT(*) FROM v8_qualified
           WHERE basket = %s AND signal_date >= %s""", (basket, since_date))
    return int(cur.fetchone()[0] or 0)


def _signals_in_window(cur, basket: str, days: list) -> int:
    if not days:
        return 0
    cur.execute(
        """SELECT COUNT(*) FROM v8_qualified
           WHERE basket = %s AND signal_date = ANY(%s)""", (basket, days))
    return int(cur.fetchone()[0] or 0)


def _wr_since(cur, basket: str, since_ts) -> tuple:
    """(closed_count, win_rate_pct). Win = return_pct > 0."""
    # cc#325: closed_at is now naive IST; enabled_at (since_ts) is timestamptz -> convert
    # it to naive IST for an apples-to-apples compare (was skewed 5:30h before the fix).
    cur.execute(
        """SELECT COUNT(*), COUNT(*) FILTER (WHERE return_pct > 0)
           FROM v8_paper_trades
           WHERE basket = %s AND closed_at >= (%s AT TIME ZONE 'Asia/Kolkata')""", (basket, since_ts))
    total, wins = cur.fetchone()
    total = int(total or 0); wins = int(wins or 0)
    wr = (wins / total * 100.0) if total else None
    return total, wr


def _disable(conn, basket: str, reason: str):
    """RETIRED cc#875 (founder decision, session_log 16710). DEAD-PRESERVED, not deleted.

    No code path reaches this any more — run_killswitch_check no longer evaluates the two
    switches, so nothing calls _disable(). Kept intact, in the repo convention used for OH-OL and
    Raw Data, because it is the only written record of HOW the five historical disables were
    written (state + log in ONE transaction), and cc#873's archaeology depends on that being
    readable. Deleting it would delete the explanation of the five rows it produced.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE v8_filter_state
               SET enabled = FALSE, disabled_at = NOW(), disabled_reason = %s,
                   updated_at = NOW()
               WHERE basket = %s AND enabled = TRUE""", (reason, basket))
        if cur.rowcount:   # cc#324: append the flip to the point-in-time history (sim/BT7 reads it)
            cur.execute("INSERT INTO v8_filter_state_log (basket, enabled) VALUES (%s, FALSE)", (basket,))
    conn.commit()


def check_state_log_divergence(conn) -> list:
    """cc#873 — STATE vs POINT-IN-TIME LOG must agree. Returns the baskets where they do not.

    v8_filter_state is what LIVE reads; v8_filter_state_log is what a sim/BT7 replay reads to
    reconstruct the gate as of a past day (cc#324). _disable() writes BOTH in one transaction, so
    under normal operation they can never drift. If they have drifted, something wrote the state
    table out of band — and the two readers are now disagreeing about history with nothing on
    screen to say so. That is exactly what happened to buy_reversal: enabled=FALSE in state,
    disabled_at NULL, and no FALSE row in the log, undetected from 09-Jul to 06-Aug.

    LOUD, NOT SILENT. This raises through the existing _log_alert path (ops_log category=alert +
    cc_task_logs #158), the same channel a kill-switch trip uses. It fires on every nightly run
    while the divergence stands — that repetition IS the alarm, and it stops the moment state and
    log agree again. It never writes to either table: detecting drift and deciding the true
    history are different jobs, and the second one needs a human.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.basket, s.enabled, x.enabled AS log_enabled, x.changed_at, s.disabled_at
            FROM v8_filter_state s
            LEFT JOIN LATERAL (
                SELECT enabled, changed_at FROM v8_filter_state_log l
                WHERE l.basket = s.basket ORDER BY id DESC LIMIT 1
            ) x ON TRUE
            WHERE s.enabled IS DISTINCT FROM x.enabled
            ORDER BY s.basket
        """)
        rows = cur.fetchall()

    diverged = []
    for basket, state_enabled, log_enabled, changed_at, disabled_at in rows:
        d = {"basket": basket, "state_enabled": state_enabled, "log_enabled": log_enabled,
             "log_changed_at": str(changed_at) if changed_at else None,
             "state_disabled_at": str(disabled_at) if disabled_at else None}
        diverged.append(d)
        _log_alert(conn, basket,
                   f"STATE/LOG DIVERGENCE — v8_filter_state.enabled={state_enabled} but the latest "
                   f"v8_filter_state_log row says enabled={log_enabled} "
                   f"(changed_at={d['log_changed_at']}, state.disabled_at={d['state_disabled_at']}). "
                   f"LIVE and every point-in-time replay disagree about this basket. The kill-switch "
                   f"writes both together, so this was written out of band. Reconcile the log; do not "
                   f"guess a timestamp.")
    return diverged


def run_killswitch_check(conn) -> dict:
    """NIGHTLY JOB. As of cc#875 this runs the DIVERGENCE GUARD ONLY.

    RETIRED cc#875 — founder decision, session_log 16710
    (V21_KILLSWITCH_RETIRED_AND_CC873_RECONCILE_A_06AUG2026). The two kill-switches (starvation
    and WR decay) no longer evaluate and _disable() is no longer reachable from any code path.

    Why: cc#502 turned all four surviving baskets into dedicated strict-AND handlers, and none of
    them calls v21_hard_gate_pass. v8_signal_writer._load_filter_state is dead code (cc#869/cc#873
    confirmed it is defined and never called). V21_BASELINE_WR is marked STALE by its own comment.
    So the switch was policing a subsystem nothing calls, judging it against baselines the code
    itself calls out of date. Auto-disable protection is not wanted for the rebuilt baskets at
    this stage.

    What STILL runs, deliberately: check_state_log_divergence (cc#873 item 6). It is a
    data-integrity guard on two tables, independent of the kill-switch logic, and the tables are
    kept as frozen historical record. The scheduler row stays registered and this function keeps
    returning a summary so the guard's liveness remains observable (ENGINE_LIVENESS_RULE 13829) —
    a job that stops reporting is indistinguishable from a job that stopped running.

    Nothing here writes to v8_filter_state or v8_filter_state_log. Read-only by design now.
    """
    out = {}

    # cc#873: the divergence guard, and it runs independently of `enabled`. The old per-basket
    # loop returned early on any disabled basket, which is precisely why buy_reversal sitting
    # wrongly at enabled=FALSE was never looked at again for four weeks.
    try:
        _div = check_state_log_divergence(conn)
    except Exception as e:
        log.error(f"cc#873 divergence check failed: {e}")
        _div = []

    # Every value in `out` is a dict, so the scheduler's `r.get("status")` over res.items() is
    # safe on every entry including these two.
    out["_state_log_divergence"] = {"count": len(_div), "baskets": [d["basket"] for d in _div],
                                    "detail": _div}
    out["_evaluation"] = {"status": "retired",
                          "since": "cc#875 / session_log 16710 (06-Aug-2026)",
                          "note": ("V2.1 starvation + WR-decay evaluation retired; _disable() is "
                                   "unreachable. Divergence guard only.")}

    # Per-basket state is still REPORTED — read-only, no judgment, no writes. Keeping it means the
    # nightly summary still shows what the frozen tables say, so the retirement is visible as data
    # rather than as an absence.
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT basket, enabled, disabled_at, disabled_reason
                           FROM v8_filter_state ORDER BY basket""")
            rows = cur.fetchall()
        _div_baskets = set(out["_state_log_divergence"]["baskets"])
        for basket, enabled, disabled_at, reason in rows:
            out[basket] = {"status": "enabled" if enabled else "disabled",
                           "evaluation": "retired",
                           "disabled_at": str(disabled_at) if disabled_at else None,
                           "disabled_reason": reason,
                           "state_log_divergence": basket in _div_baskets}
    except Exception as e:
        log.error(f"cc#875 state read failed: {e}")

    log.info(f"killswitch check (divergence only, evaluation retired cc#875): {out}")
    return out
