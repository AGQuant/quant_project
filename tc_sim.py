"""
tc_sim.py — cc#748: TC OUTCOME SIM (first-ever measurement of TC verdict quality).

WHAT: every hourly entry tick on trading days (09:30-15:30 IST, sourced from nse_session so it follows
the 03-Aug NSE change), score the TC-scanner universe (active futures) with the canonical STYLE-level
scorer via tc_resolver.get_primary_styles() ONLY (cc#738 — never a direct versioned import). Every
symbol scoring verdict=STRONG on any of the 4 styles (buy_reversal / buy_momentum / sell_reversal /
sell_momentum) opens a zero-cost sim entry at that hour's CMP — unless the same (symbol, direction) is
already open (no pyramiding). Exits: LONG target +3% / stop -3%; SHORT target -3% / stop +3%; first
touch wins; both breach intra-hour -> resolve on the hourly close direction, ambiguous=true. Max hold
5 TRADING days -> force-close at the 5th-day close, exit_reason=TIME. Every entry terminates TARGET /
STOP / TIME.

Context isolation id=244 (ABSOLUTE): this module touches NO V8 object (no v8_metrics / qualified /
baskets / paper). It reads only cmp_prices, intraday_prices, futures_universe, and the TC scorer.
"""

import os
import json
import psycopg
from datetime import datetime, timedelta, timezone

from nse_session import force_close_time
from nse_holidays import is_trading_day

_DB = os.getenv("DATABASE_URL", "")

# fixed +/-3% exits (LONG target above / SHORT target below), zero-cost v1 (no brokerage/slippage).
_TGT = 0.03
_STOP = 0.03
_MAX_TRADING_DAYS = 5              # force-close at the 5th trading-day close, exit_reason=TIME
_FEED_SOURCES = ("fyers", "fyers_eq")   # intraday_prices feed rows for the hi/lo window

_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now():
    # cc#1559 P0 fix: was `datetime.utcnow() + timedelta(hours=5,minutes=30)` — a NAIVE datetime
    # whose fields LOOK like IST wall-clock but carry no tzinfo. Two real bugs followed from that:
    #   1. entry_ts/exit_ts (TIMESTAMPTZ columns) store the naive value as if it were ALREADY UTC,
    #      double-shifting every stored instant 5.5h into the future (confirmed live: KALYANKJIL's
    #      13:30 IST entry read back as 19:00 IST via AT TIME ZONE 'Asia/Kolkata').
    #   2. psycopg returns a TIMESTAMPTZ column's value as a TIMEZONE-AWARE datetime on read —
    #      confirmed by this file's own SELECT at the top of run_tc_sim_tick() — so `now - entry_ts`
    #      (line ~188, naive minus aware) raised TypeError on EVERY exit check that reached it,
    #      caught by the per-position try/except and silently appended to out['errors'] — never
    #      surfacing as a scheduler failure. Confirmed live via ops_log (scheduler_health,
    #      title='tc_sim_tick'): every tick since at least 28-Aug shows closed:0 with 116-138
    #      non-zero errors, growing tick over tick, while open climbs 145->168 — a total, silent
    #      exit failure, exactly matching 168/168 rows never closing.
    # Fix: a genuinely timezone-AWARE IST datetime. Its .hour/.minute/.date() still read as true
    # IST wall-clock (unchanged for at_close / today / out['ts'] elsewhere in this file), but it is
    # now safely subtractable against another aware datetime, and stores the CORRECT instant.
    return datetime.now(timezone.utc).astimezone(_IST)


# ── schema (app-side ensure; CREATE TABLE is MCP-safe, but the ensure runs on every deploy so the
# table is guaranteed without a manual step — MAINTENANCE_LOCK_RULE: no ALTER/REINDEX here) ────────
def ensure_tc_sim_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tc_sim_trades (
                id           SERIAL PRIMARY KEY,
                symbol       VARCHAR(20)  NOT NULL,
                direction    VARCHAR(8)   NOT NULL,     -- LONG | SHORT
                style        VARCHAR(20)  NOT NULL,     -- buy_reversal | buy_momentum | sell_reversal | sell_momentum
                verdict      VARCHAR(12)  NOT NULL,     -- STRONG at entry
                score        NUMERIC,
                max_score    NUMERIC,
                entry_ts     TIMESTAMPTZ  NOT NULL,
                entry_price  NUMERIC      NOT NULL,
                exit_ts      TIMESTAMPTZ,
                exit_price   NUMERIC,
                exit_reason  VARCHAR(10),               -- TARGET | STOP | TIME
                pnl_pct      NUMERIC,
                hours_held   NUMERIC,
                ambiguous    BOOLEAN      DEFAULT FALSE,
                tc_snapshot  JSONB,                      -- full rule-level dual result at entry
                created_at   TIMESTAMPTZ  DEFAULT NOW()
            )""")
        # open-position lookup (no-pyramiding guard) + summary scans
        cur.execute("""CREATE INDEX IF NOT EXISTS ix_tc_sim_open
                       ON tc_sim_trades (symbol, direction) WHERE exit_ts IS NULL""")
        cur.execute("""CREATE INDEX IF NOT EXISTS ix_tc_sim_closed
                       ON tc_sim_trades (exit_reason) WHERE exit_ts IS NOT NULL""")
    conn.commit()


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────
def _universe(cur):
    """TC-scanner universe = active futures (identical to the TC screener pre-compute source)."""
    cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE ORDER BY symbol")
    return [r[0] for r in cur.fetchall()]


def _cmp(cur, symbol):
    cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (symbol,))
    r = cur.fetchone()
    try:
        return float(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


def _recent_hilo(cur, symbol, bars=12):
    """MAX(high)/MIN(low) over the most recent ~hour of 5-min feed bars (last `bars` rows). Order-free
    'did it touch this window' signal for first-touch + both-breach detection; None if no feed bars."""
    cur.execute("""
        SELECT MAX(high), MIN(low) FROM (
            SELECT high, low FROM intraday_prices
            WHERE symbol=%s AND source = ANY(%s)
            ORDER BY ts DESC LIMIT %s
        ) t
    """, (symbol, list(_FEED_SOURCES), bars))
    r = cur.fetchone()
    hi = float(r[0]) if r and r[0] is not None else None
    lo = float(r[1]) if r and r[1] is not None else None
    return hi, lo


def _trading_days_between(d0, d1):
    """Inclusive count of trading days from d0 to d1 (entry day = day 1)."""
    n, d = 0, d0
    while d <= d1:
        if is_trading_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def _style_name(side, style):
    """card {side: BUY|SELL, style: MOMENTUM|REVERSAL} -> buy_momentum / buy_reversal / sell_* ."""
    return f"{side.lower()}_{style.lower()}"


def _evaluate_exit(entry_price, direction, cmp_now, rec_hi, rec_lo):
    """Fixed +/-3% exit. Returns dict{reason, exit_price, ambiguous} or None (stays open).
    First touch wins; both-breach-intra-hour -> ambiguous, resolve on the hourly close (cmp_now)."""
    long = direction == "LONG"
    tgt = entry_price * (1 + _TGT) if long else entry_price * (1 - _TGT)
    stop = entry_price * (1 - _STOP) if long else entry_price * (1 + _STOP)
    if long:
        t_touch = (cmp_now is not None and cmp_now >= tgt) or (rec_hi is not None and rec_hi >= tgt)
        s_touch = (cmp_now is not None and cmp_now <= stop) or (rec_lo is not None and rec_lo <= stop)
    else:
        t_touch = (cmp_now is not None and cmp_now <= tgt) or (rec_lo is not None and rec_lo <= tgt)
        s_touch = (cmp_now is not None and cmp_now >= stop) or (rec_hi is not None and rec_hi >= stop)
    if t_touch and s_touch:
        if cmp_now is None:
            return None                       # cannot resolve the close direction yet -> stay open
        if long:
            reason = "TARGET" if cmp_now >= entry_price else "STOP"
        else:
            reason = "TARGET" if cmp_now <= entry_price else "STOP"
        return {"reason": reason, "exit_price": round(cmp_now, 2), "ambiguous": True}
    if t_touch:
        return {"reason": "TARGET", "exit_price": round(tgt, 2), "ambiguous": False}
    if s_touch:
        return {"reason": "STOP", "exit_price": round(stop, 2), "ambiguous": False}
    return None


def _pnl_pct(entry_price, exit_price, direction):
    if entry_price in (None, 0) or exit_price is None:
        return None
    raw = (exit_price / entry_price - 1.0) if direction == "LONG" else (entry_price / exit_price - 1.0)
    return round(raw * 100.0, 2)


# ── the hourly tick: exits first (incl. TIME), then STRONG entries ─────────────────────────────────
def run_tc_sim_tick():
    now = _ist_now()
    today = now.date()
    out = {"ts": now.strftime("%Y-%m-%d %H:%M IST"), "trading_day": is_trading_day(today),
           "closed": [], "opened": [], "still_open": 0, "errors": []}
    if not is_trading_day(today):
        out["skipped"] = "non-trading day"
        return out

    at_close = (now.hour, now.minute) >= force_close_time()
    conn = psycopg.connect(_DB)
    try:
        ensure_tc_sim_table(conn)

        # 1) EXITS — walk every open position; target/stop first, then the 5-trading-day TIME cap.
        with conn.cursor() as cur:
            cur.execute("""SELECT id, symbol, direction, entry_ts, entry_price
                           FROM tc_sim_trades WHERE exit_ts IS NULL ORDER BY entry_ts""")
            opens = cur.fetchall()
        for pid, sym, direction, entry_ts, entry_price in opens:
            try:
                entry_price = float(entry_price)
                with conn.cursor() as rcur:
                    cmp_now = _cmp(rcur, sym)
                    rec_hi, rec_lo = _recent_hilo(rcur, sym)
                ev = _evaluate_exit(entry_price, direction, cmp_now, rec_hi, rec_lo)
                if ev is None:
                    # no price exit — check the 5-trading-day TIME cap (force-close at day-5 close).
                    # entry_ts comes back timezone-aware from the TIMESTAMPTZ column but not
                    # necessarily IST-labelled (psycopg's own read-back tzinfo, typically UTC) — the
                    # trading-day calendar is IST's, so normalise explicitly before .date() rather
                    # than rely on every entry happening to fall inside NSE hours (it always does
                    # today, but that is a coincidence this line should not depend on).
                    tdays = _trading_days_between(entry_ts.astimezone(_IST).date(), today)
                    time_due = tdays > _MAX_TRADING_DAYS or (tdays >= _MAX_TRADING_DAYS and at_close)
                    if time_due and cmp_now is not None:
                        ev = {"reason": "TIME", "exit_price": round(cmp_now, 2), "ambiguous": False}
                    else:
                        continue
                pnl = _pnl_pct(entry_price, ev["exit_price"], direction)
                hours = round((now - entry_ts).total_seconds() / 3600.0, 2)
                with conn.cursor() as cur:
                    cur.execute("""UPDATE tc_sim_trades
                                   SET exit_ts=%s, exit_price=%s, exit_reason=%s, pnl_pct=%s,
                                       hours_held=%s, ambiguous=%s
                                   WHERE id=%s AND exit_ts IS NULL""",
                                (now, ev["exit_price"], ev["reason"], pnl, hours,
                                 ev["ambiguous"], pid))
                conn.commit()
                out["closed"].append({"symbol": sym, "direction": direction,
                                      "reason": ev["reason"], "pnl_pct": pnl,
                                      "ambiguous": ev["ambiguous"]})
            except Exception as e:
                conn.rollback()
                out["errors"].append(f"exit {sym}: {type(e).__name__}: {str(e)[:120]}")

        # 2) ENTRIES — score the universe via the resolver's STYLE-level primary (cc#738/#748).
        from tc_resolver import get_primary_styles
        scorer = get_primary_styles()
        with conn.cursor() as cur:
            universe = _universe(cur)
        for sym in universe:
            try:
                res = scorer(sym, "ALL")
                if not isinstance(res, dict) or res.get("error"):
                    continue
                cmp_v = res.get("cmp")
                if cmp_v is None:
                    continue
                strong = [c for c in (res.get("cards") or []) if c.get("verdict") == "STRONG"]
                if not strong:
                    continue
                # best STRONG card per DIRECTION (no-pyramiding key is symbol+direction, not style)
                by_dir = {}
                for c in strong:
                    d = "LONG" if c.get("side") == "BUY" else "SHORT"
                    if d not in by_dir or (c.get("score") or 0) > (by_dir[d].get("score") or 0):
                        by_dir[d] = c
                for direction, c in by_dir.items():
                    with conn.cursor() as cur:
                        cur.execute("""SELECT 1 FROM tc_sim_trades
                                       WHERE symbol=%s AND direction=%s AND exit_ts IS NULL LIMIT 1""",
                                    (sym, direction))
                        if cur.fetchone():
                            continue                       # already open -> no pyramiding
                        style = _style_name(c.get("side"), c.get("style"))
                        cur.execute("""INSERT INTO tc_sim_trades
                            (symbol, direction, style, verdict, score, max_score, entry_ts,
                             entry_price, tc_snapshot)
                            VALUES (%s,%s,%s,'STRONG',%s,%s,%s,%s,%s::jsonb)""",
                            (sym, direction, style, c.get("score"), c.get("max"), now,
                             round(float(cmp_v), 2), json.dumps(res)))
                    conn.commit()
                    out["opened"].append({"symbol": sym, "direction": direction,
                                          "style": style, "score": c.get("score")})
            except Exception as e:
                conn.rollback()
                out["errors"].append(f"entry {sym}: {type(e).__name__}: {str(e)[:120]}")

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tc_sim_trades WHERE exit_ts IS NULL")
            out["still_open"] = int(cur.fetchone()[0] or 0)
    finally:
        conn.close()
    return out


# ── summary (MCP Scorr:tc_sim_summary + /api/tc-sim/summary) ───────────────────────────────────────
def tc_sim_summary():
    """Open positions + closed stats (WR, avg pnl, avg hours, exit_reason split) grouped by style AND
    direction. Read-only; ensures the table so a first call before any tick still returns cleanly."""
    conn = psycopg.connect(_DB)
    try:
        ensure_tc_sim_table(conn)
        with conn.cursor() as cur:
            # open positions
            cur.execute("""SELECT symbol, direction, style, score, max_score, entry_ts, entry_price
                           FROM tc_sim_trades WHERE exit_ts IS NULL ORDER BY entry_ts DESC""")
            open_rows = [{"symbol": r[0], "direction": r[1], "style": r[2],
                          "score": float(r[3]) if r[3] is not None else None,
                          "max_score": float(r[4]) if r[4] is not None else None,
                          "entry_ts": r[5].strftime("%Y-%m-%d %H:%M") if r[5] else None,
                          "entry_price": float(r[6]) if r[6] is not None else None}
                         for r in cur.fetchall()]

            # closed stats grouped by style AND direction
            cur.execute("""
                SELECT style, direction,
                       COUNT(*)                                              AS n,
                       SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END)          AS wins,
                       ROUND(AVG(pnl_pct)::numeric, 2)                       AS avg_pnl,
                       ROUND(AVG(hours_held)::numeric, 2)                    AS avg_hours,
                       SUM(CASE WHEN exit_reason='TARGET' THEN 1 ELSE 0 END) AS n_target,
                       SUM(CASE WHEN exit_reason='STOP'   THEN 1 ELSE 0 END) AS n_stop,
                       SUM(CASE WHEN exit_reason='TIME'   THEN 1 ELSE 0 END) AS n_time,
                       SUM(CASE WHEN ambiguous THEN 1 ELSE 0 END)           AS n_ambiguous
                FROM tc_sim_trades
                WHERE exit_ts IS NOT NULL
                GROUP BY style, direction
                ORDER BY style, direction
            """)
            groups = []
            for r in cur.fetchall():
                n = int(r[2] or 0)
                wins = int(r[3] or 0)
                groups.append({
                    "style": r[0], "direction": r[1], "closed": n,
                    "win_rate": round(wins / n * 100, 1) if n else None,
                    "avg_pnl_pct": float(r[4]) if r[4] is not None else None,
                    "avg_hours_held": float(r[5]) if r[5] is not None else None,
                    "exit_split": {"TARGET": int(r[6] or 0), "STOP": int(r[7] or 0),
                                   "TIME": int(r[8] or 0)},
                    "ambiguous": int(r[9] or 0),
                })

            # overall closed line
            cur.execute("""SELECT COUNT(*), SUM(CASE WHEN pnl_pct>0 THEN 1 ELSE 0 END),
                                  ROUND(AVG(pnl_pct)::numeric,2), ROUND(AVG(hours_held)::numeric,2)
                           FROM tc_sim_trades WHERE exit_ts IS NOT NULL""")
            o = cur.fetchone()
            oc = int(o[0] or 0)
            overall = {"closed": oc,
                       "win_rate": round(int(o[1] or 0) / oc * 100, 1) if oc else None,
                       "avg_pnl_pct": float(o[2]) if o[2] is not None else None,
                       "avg_hours_held": float(o[3]) if o[3] is not None else None}
    finally:
        conn.close()
    return {"open_count": len(open_rows), "open": open_rows,
            "overall_closed": overall, "by_style_direction": groups,
            "note": "cc#748 TC outcome sim — verdict=STRONG entries, +/-3% exits, 5-trading-day TIME cap."}
