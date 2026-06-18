"""
Intraday paper engine endpoints (id=374; rewritten 18-Jun-2026).

Single source of truth: tc_intraday_* tables (written by scheduler every 5-min).
Dashboard logic is INLINE — no dependency on tc_intraday or intraday_engine modules.

  GET  /api/intraday/dashboard   — instant-read dashboard for /intraday page
  POST /api/intraday/tick        — manual tick (enter + exit)
  GET  /api/intraday/open        — open positions (?side=)
  GET  /api/intraday/trades      — today's closed trades (?side=)
"""

import os
from datetime import datetime, timedelta

import psycopg
from fastapi import APIRouter

import tc_intraday as tci
import intraday_engine as ie

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _pill(result, ret):
    if result == "TARGET":
        return "WIN"
    if result == "SL":
        return "LOSS"
    r = ret or 0
    return "WIN" if r > 0 else ("LOSS" if r < 0 else "FLAT")


@router.get("/api/intraday/dashboard")
def intraday_dashboard():
    """
    Inline dashboard — reads tc_intraday_* + tc_cache directly.
    Returns {ts, cache_ts, cache_rows, sides:{LONG/SHORT:{funnel,stats,open,trades}}}.
    """
    now = _ist_now()
    out = {
        "ts": now.strftime("%d-%b %H:%M IST"),
        "cache_ts": None,
        "cache_rows": 0,
        "sides": {},
        "_debug": None,
    }
    try:
        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()

        # ── cache freshness ──────────────────────────────────────────────────
        cur.execute("SELECT COUNT(*), MAX(computed_at) FROM tc_cache")
        row = cur.fetchone()
        if row:
            out["cache_rows"] = int(row[0] or 0)
            if row[1]:
                try:
                    ts = row[1]
                    if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                        # timezone-aware: convert to IST manually
                        ist = ts + timedelta(hours=5, minutes=30)
                        out["cache_ts"] = ist.strftime("%d-%b %H:%M")
                    else:
                        out["cache_ts"] = (ts + timedelta(hours=5, minutes=30)).strftime("%d-%b %H:%M")
                except Exception:
                    out["cache_ts"] = str(row[1])[:16]

        # ── per-side payload ─────────────────────────────────────────────────
        for side in ("LONG", "SHORT"):

            # funnel
            cur.execute("SELECT COUNT(*) FROM tc_cache WHERE side=%s", (side,))
            universe = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM tc_cache WHERE side=%s AND score>=10", (side,))
            tc10 = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM tc_intraday_positions WHERE side=%s AND status='OPEN'", (side,))
            n_open = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM tc_intraday_trades WHERE side=%s AND exit_ts::date=CURRENT_DATE", (side,))
            n_closed = int(cur.fetchone()[0] or 0)

            # open positions
            cur.execute("""
                SELECT p.symbol, p.entry_price, p.target, p.stop_loss, c.cmp
                FROM tc_intraday_positions p
                LEFT JOIN cmp_prices c ON c.symbol = p.symbol
                WHERE p.side=%s AND p.status='OPEN'
                ORDER BY p.entry_ts DESC
            """, (side,))
            open_rows = []
            for sym, entry, target, stop, cmp in cur.fetchall():
                entry = _f(entry); cmp = _f(cmp)
                pnl = None
                if entry and cmp:
                    pnl = round(((cmp/entry - 1) if side == "LONG" else (entry/cmp - 1)) * 100, 2)
                open_rows.append({
                    "symbol": sym,
                    "entry_price": entry,
                    "cmp": round(cmp, 2) if cmp else None,
                    "pnl_pct": pnl,
                    "target": _f(target),
                    "stop": _f(stop),
                })

            # today's trades
            cur.execute("""
                SELECT symbol, entry_price, exit_price, return_pct, result
                FROM tc_intraday_trades
                WHERE side=%s AND exit_ts::date=CURRENT_DATE
                ORDER BY exit_ts DESC
                LIMIT 50
            """, (side,))
            trade_rows = []
            for sym, entry, exit_px, ret, result in cur.fetchall():
                ret = _f(ret)
                trade_rows.append({
                    "symbol": sym,
                    "entry_price": _f(entry),
                    "exit_price": _f(exit_px),
                    "pnl_pct": ret,
                    "result": _pill(result, ret),
                    "exit_reason": result,
                })

            # stats
            n_tr = len(trade_rows)
            wins = sum(1 for t in trade_rows if (t["pnl_pct"] or 0) > 0)
            total_pnl = round(sum(t["pnl_pct"] or 0 for t in trade_rows), 2)
            avg_pnl = round(total_pnl / n_tr, 2) if n_tr else 0

            out["sides"][side] = {
                "funnel": {"universe": universe, "tc10": tc10, "open": n_open, "closed": n_closed},
                "stats": {"trades": n_tr, "win_rate": round(wins/n_tr*100,1) if n_tr else 0,
                          "avg_pnl": avg_pnl, "total_pnl": total_pnl},
                "open": open_rows,
                "trades": trade_rows,
            }

        cur.close()
        conn.close()

    except Exception as e:
        out["_debug"] = f"{type(e).__name__}: {str(e)[:200]}"

    return out


@router.post("/api/intraday/tick")
def intraday_tick():
    """Manual engine tick: cache refresh + scan + enter + exit/square-off."""
    rc = tci.refresh_tc_cache()
    en = tci.run_intraday_paper_entry()
    ex = tci.run_intraday_paper_exit()
    return {
        "ok": True,
        "cache_written": rc.get("written"),
        "new_entries": en.get("positions", []),
        "closed": ex.get("closed"),
        "square_off": ex.get("square_off"),
        "ts": en.get("ts") or ex.get("ts"),
    }


@router.get("/api/intraday/open")
def intraday_open(side: str = None):
    return {"open": ie.get_open(side.upper() if side else None)}


@router.get("/api/intraday/trades")
def intraday_trades(side: str = None, limit: int = 50):
    return {"trades": ie.get_trades(side.upper() if side else None, limit)}
