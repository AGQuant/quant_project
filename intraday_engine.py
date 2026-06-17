"""
Intraday Paper Engine — Phase 1 prototype (17-Jun-2026, spec id=374).

Full paper-trading engine (NOT a scanner). Reuses tc_intraday.intraday_scan()
for the entry shortlist (TC>=10 cached + live filters), then manages paper
positions with fixed 1.5%/1.5% target/stop and 15:15 square-off.

ENTRY:  qualifier from intraday_scan + before 15:00 + not already traded today
        -> INSERT intraday_positions at live CMP, 1 lot.
EXIT:   target +1.5% OR stop -1.5% (CMP-based) OR 15:15 square-off
        -> INSERT intraday_trades, mark position CLOSED.
GUARDS: 1 entry / symbol / side / day (UNIQUE constraint). Flat by 15:15.

Writer is STANDALONE/manual in phase 1. Scheduler wiring = phase 1.5 — NEVER on
the v8_signal_writer heartbeat. Context isolation id=244 — separate from v8_paper
and personal_journal.
"""

import os
from datetime import datetime, timedelta, time as dtime
import psycopg

import tc_intraday as tci

DATABASE_URL = os.getenv("DATABASE_URL", "")

TARGET_PCT = 1.5
STOP_PCT = 1.5
ENTRY_CUTOFF = dtime(15, 0)    # no new entries after 15:00 IST
SQUARE_OFF = dtime(15, 15)     # hard close all open at 15:15 IST


def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _live_cmp(cur, symbol):
    cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (symbol,))
    r = cur.fetchone()
    return _f(r[0]) if r else None


# ─────────────────────────────────────────────── ENTRY ───

def _try_enter(cur, sym, side, cmp):
    """Insert a paper position if not already traded this symbol/side today."""
    if cmp is None or cmp <= 0:
        return False
    if side == "LONG":
        target = round(cmp * (1 + TARGET_PCT / 100), 2)
        stop = round(cmp * (1 - STOP_PCT / 100), 2)
    else:
        target = round(cmp * (1 - TARGET_PCT / 100), 2)
        stop = round(cmp * (1 + STOP_PCT / 100), 2)
    try:
        cur.execute("""
            INSERT INTO intraday_positions
                (symbol, side, entry_price, entry_ts, qty, cmp, pnl_pct,
                 target, stop, status, trade_date)
            VALUES (%s,%s,%s,%s,1,%s,0,%s,%s,'OPEN',CURRENT_DATE)
            ON CONFLICT (symbol, side, trade_date) DO NOTHING""",
            (sym, side, cmp, _ist_now(), cmp, target, stop))
        return cur.rowcount > 0
    except Exception:
        return False


# ─────────────────────────────────────────────── EXIT / MARK ───

def _pnl_pct(side, entry, cmp):
    if not entry:
        return 0.0
    raw = (cmp / entry - 1) * 100
    return round(raw if side == "LONG" else -raw, 3)


def _close(cur, pos_id, sym, side, entry, exit_price, entry_ts, reason):
    pnl = _pnl_pct(side, entry, exit_price)
    result = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
    cur.execute("""
        INSERT INTO intraday_trades
            (symbol, side, entry_price, exit_price, entry_ts, exit_ts, qty,
             pnl_pct, result, exit_reason, trade_date)
        VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,CURRENT_DATE)""",
        (sym, side, entry, exit_price, entry_ts, _ist_now(), pnl, result, reason))
    cur.execute("UPDATE intraday_positions SET status='CLOSED', cmp=%s, pnl_pct=%s WHERE id=%s",
                (exit_price, pnl, pos_id))


def _manage_open(cur, force_squareoff=False):
    """Mark open positions, exit on target/stop, or square-off all if past 15:15."""
    cur.execute("""SELECT id, symbol, side, entry_price, entry_ts, target, stop
                   FROM intraday_positions WHERE status='OPEN' AND trade_date=CURRENT_DATE""")
    rows = cur.fetchall()
    closed, marked = 0, 0
    for pid, sym, side, entry, ets, target, stop in rows:
        entry = _f(entry); target = _f(target); stop = _f(stop)
        cmp = _live_cmp(cur, sym)
        if cmp is None:
            continue
        if force_squareoff:
            _close(cur, pid, sym, side, entry, cmp, ets, "SQUARE_OFF_1515")
            closed += 1
            continue
        hit = None
        if side == "LONG":
            if cmp >= target:
                hit = ("TARGET", target)
            elif cmp <= stop:
                hit = ("STOP", stop)
        else:
            if cmp <= target:
                hit = ("TARGET", target)
            elif cmp >= stop:
                hit = ("STOP", stop)
        if hit:
            _close(cur, pid, sym, side, entry, hit[1], ets, hit[0])
            closed += 1
        else:
            cur.execute("UPDATE intraday_positions SET cmp=%s, pnl_pct=%s WHERE id=%s",
                        (cmp, _pnl_pct(side, entry, cmp), pid))
            marked += 1
    return closed, marked


# ─────────────────────────────────────────────── WRITER TICK ───

def run_tick():
    """One engine tick: manage open, then enter new qualifiers (both sides)
    if before cutoff. Square-off everything if past 15:15. Standalone/manual."""
    started = _ist_now()
    now_t = started.time()
    square = now_t >= SQUARE_OFF
    can_enter = now_t < ENTRY_CUTOFF

    entered, errors = [], []
    funnel = {"LONG": {}, "SHORT": {}}

    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        # 1) manage / exit open positions (or square-off all)
        closed, marked = _manage_open(cur, force_squareoff=square)
        conn.commit()

        # 2) entries (skip if square-off window or past cutoff)
        if can_enter and not square:
            for side in ("LONG", "SHORT"):
                try:
                    scan = tci.intraday_scan(side=side)
                    funnel[side] = {
                        "shortlist_tc10": scan.get("shortlist", 0),
                        "passed_filters": scan.get("matched", 0),
                    }
                    for r in scan.get("rows", []):
                        sym = r["symbol"]
                        cmp = _live_cmp(cur, sym) or _f(r.get("cmp"))
                        if _try_enter(cur, sym, side, cmp):
                            entered.append({"symbol": sym, "side": side, "entry": cmp})
                    conn.commit()
                except Exception as e:
                    errors.append({"side": side, "error": f"{type(e).__name__}: {str(e)[:80]}"})
        else:
            for side in ("LONG", "SHORT"):
                funnel[side] = {"shortlist_tc10": 0, "passed_filters": 0,
                                "note": "square-off window" if square else "past entry cutoff"}

    return {"ok": True, "ts": _ist_now().strftime("%d-%b %H:%M IST"),
            "square_off": square, "entries_allowed": can_enter and not square,
            "closed": closed, "marked_open": marked,
            "new_entries": entered, "funnel": funnel, "errors": errors[:10],
            "elapsed_sec": round((_ist_now() - started).total_seconds(), 1)}


# ─────────────────────────────────────────────── PAGE READERS ───

def get_open(side=None):
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        q = """SELECT symbol, side, entry_price, entry_ts, cmp, pnl_pct, target, stop
               FROM intraday_positions WHERE status='OPEN' AND trade_date=CURRENT_DATE"""
        params = []
        if side:
            q += " AND side=%s"; params.append(side.upper())
        q += " ORDER BY entry_ts DESC"
        cur.execute(q, params)
        cols = ["symbol", "side", "entry_price", "entry_ts", "cmp", "pnl_pct", "target", "stop"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_trades(side=None, limit=50):
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        q = """SELECT symbol, side, entry_price, exit_price, entry_ts, exit_ts,
                      pnl_pct, result, exit_reason FROM intraday_trades
               WHERE trade_date=CURRENT_DATE"""
        params = []
        if side:
            q += " AND side=%s"; params.append(side.upper())
        q += " ORDER BY exit_ts DESC LIMIT %s"; params.append(limit)
        cur.execute(q, params)
        cols = ["symbol", "side", "entry_price", "exit_price", "entry_ts", "exit_ts",
                "pnl_pct", "result", "exit_reason"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_dashboard():
    """Full /intraday page payload: funnel + open + trade log + stats, per side."""
    now = _ist_now()
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        # cache freshness (stage-1 source)
        cur.execute("SELECT MAX(computed_at), COUNT(*) FROM tc_cache")
        cm = cur.fetchone()
        cache_ts = cm[0].strftime("%d-%b %H:%M") if cm and cm[0] else "never"
        cache_n = cm[1] if cm else 0

        # TC>=10 universe counts (funnel stage 1)
        cur.execute("SELECT side, COUNT(*) FROM tc_cache WHERE score>=10 GROUP BY side")
        tc10 = {r[0]: r[1] for r in cur.fetchall()}

        out = {"ts": now.strftime("%d-%b %H:%M IST"),
               "cache_ts": cache_ts, "cache_rows": cache_n, "sides": {}}

        for side in ("LONG", "SHORT"):
            cur.execute("""SELECT COUNT(*) FILTER (WHERE status='OPEN'),
                                  COUNT(*) FILTER (WHERE status='CLOSED')
                           FROM intraday_positions WHERE side=%s AND trade_date=CURRENT_DATE""", (side,))
            pc = cur.fetchone()
            cur.execute("""SELECT COUNT(*), COUNT(*) FILTER (WHERE result='WIN'),
                                  ROUND(AVG(pnl_pct)::numeric,3), ROUND(SUM(pnl_pct)::numeric,3)
                           FROM intraday_trades WHERE side=%s AND trade_date=CURRENT_DATE""", (side,))
            tc = cur.fetchone()
            ntr = tc[0] or 0
            out["sides"][side] = {
                "funnel": {
                    "universe": cache_n // 2 if cache_n else 0,
                    "tc10": tc10.get(side, 0),
                    "open": pc[0] or 0,
                    "closed": pc[1] or 0,
                },
                "open": get_open(side),
                "trades": get_trades(side, 50),
                "stats": {
                    "trades": ntr,
                    "wins": tc[1] or 0,
                    "win_rate": round((tc[1] or 0) / ntr * 100, 1) if ntr else 0,
                    "avg_pnl": _f(tc[2]) or 0,
                    "total_pnl": _f(tc[3]) or 0,
                },
            }
        return out
