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
    cur.execute(
        """SELECT COUNT(*), COUNT(*) FILTER (WHERE return_pct > 0)
           FROM v8_paper_trades
           WHERE basket = %s AND closed_at >= %s""", (basket, since_ts))
    total, wins = cur.fetchone()
    total = int(total or 0); wins = int(wins or 0)
    wr = (wins / total * 100.0) if total else None
    return total, wr


def _disable(conn, basket: str, reason: str):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE v8_filter_state
               SET enabled = FALSE, disabled_at = NOW(), disabled_reason = %s,
                   updated_at = NOW()
               WHERE basket = %s AND enabled = TRUE""", (reason, basket))
        if cur.rowcount:   # cc#324: append the flip to the point-in-time history (sim/BT7 reads it)
            cur.execute("INSERT INTO v8_filter_state_log (basket, enabled) VALUES (%s, FALSE)", (basket,))
    conn.commit()


def run_killswitch_check(conn) -> dict:
    """Evaluate both kill-switches for every currently-enabled basket. Returns a
    per-basket summary. Idempotent — safe to run nightly."""
    out = {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT basket, enabled, baseline_wr, normal_rate, enabled_at
               FROM v8_filter_state ORDER BY basket""")
        rows = cur.fetchall()
        last5 = _last_n_trading_days(cur, STARVATION_WINDOW_TD)

    for basket, enabled, baseline_wr, normal_rate, enabled_at in rows:
        if not enabled:
            out[basket] = {"status": "disabled"}
            continue
        since_date = (enabled_at.date() if hasattr(enabled_at, "date") else enabled_at)
        with conn.cursor() as cur:
            td      = _trading_days_since(cur, since_date)
            signals = _signals_since(cur, basket, since_date)

        # Sample discipline — no judgment until the sample is big enough.
        if td < SAMPLE_MIN_TRADING_DAYS and signals < SAMPLE_MIN_SIGNALS:
            out[basket] = {"status": "warming_up", "trading_days": td,
                           "signals": signals}
            continue

        tripped = None

        # Rule 2 — WR decay.
        with conn.cursor() as cur:
            closed, wr = _wr_since(cur, basket, enabled_at)
        if wr is not None and closed >= WR_MIN_CLOSED and baseline_wr is not None:
            if wr < float(baseline_wr) - WR_DROP_PP:
                tripped = (f"WR decay: rolling {wr:.1f}% over {closed} closed vs "
                           f"baseline {float(baseline_wr):.1f}% "
                           f"(> {WR_DROP_PP:.0f}pp below)")

        # Rule 1 — starvation (only meaningful once >= window trading days exist).
        if tripped is None and normal_rate and len(last5) >= STARVATION_WINDOW_TD:
            with conn.cursor() as cur:
                win_signals = _signals_in_window(cur, basket, last5)
            expected = float(normal_rate) * STARVATION_WINDOW_TD
            if win_signals < STARVATION_FRACTION * expected:
                tripped = (f"starvation: {win_signals} signals in last "
                           f"{STARVATION_WINDOW_TD} trading days vs expected "
                           f"~{expected:.1f} (< {int(STARVATION_FRACTION*100)}%)")

        if tripped:
            _disable(conn, basket, tripped)
            _log_alert(conn, basket,
                       f"V2.1 filters AUTO-DISABLED — {tripped}. Basket reverted to "
                       f"locked behavior. Will NOT auto-re-enable; review + re-enable "
                       f"manually.")
            out[basket] = {"status": "TRIPPED_DISABLED", "reason": tripped,
                           "wr": wr, "closed": closed, "signals": signals,
                           "trading_days": td}
        else:
            out[basket] = {"status": "ok", "wr": wr, "closed": closed,
                           "signals": signals, "trading_days": td}

    log.info(f"killswitch check: {out}")
    return out
