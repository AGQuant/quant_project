"""
Intraday paper engine endpoints (id=374; debug build 18-Jun-2026).
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
    Dashboard endpoint. Tries real DB; falls back to a labeled test payload
    so we can distinguish a Python/DB failure from a JS rendering failure.
    """
    now = _ist_now()
    out = {
        "ts": now.strftime("%d-%b %H:%M IST"),
        "cache_ts": None,
        "cache_rows": 0,
        "sides": {},
        "_debug": None,
        "_source": "db",
    }
    try:
        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*), MAX(computed_at) FROM tc_cache")
        row = cur.fetchone()
        if row:
            out["cache_rows"] = int(row[0] or 0)
            if row[1]:
                try:
                    ts = row[1]
                    ist = ts + timedelta(hours=5, minutes=30)
                    out["cache_ts"] = ist.strftime("%d-%b %H:%M")
                except Exception:
                    out["cache_ts"] = str(row[1])[:16]

        for side in ("LONG", "SHORT"):
            cur.execute("SELECT COUNT(*) FROM tc_cache WHERE side=%s", (side,))
            universe = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM tc_cache WHERE side=%s AND score>=10", (side,))
            tc10 = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM tc_intraday_positions WHERE side=%s AND status='OPEN'", (side,))
            n_open = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM tc_intraday_trades WHERE side=%s AND exit_ts::date=CURRENT_DATE", (side,))
            n_closed = int(cur.fetchone()[0] or 0)

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
                    "symbol": sym, "entry_price": entry,
                    "cmp": round(cmp, 2) if cmp else None,
                    "pnl_pct": pnl, "target": _f(target), "stop": _f(stop),
                })

            cur.execute("""
                SELECT symbol, entry_price, exit_price, return_pct, result
                FROM tc_intraday_trades
                WHERE side=%s AND exit_ts::date=CURRENT_DATE
                ORDER BY exit_ts DESC LIMIT 50
            """, (side,))
            trade_rows = []
            for sym, entry, exit_px, ret, result in cur.fetchall():
                ret = _f(ret)
                trade_rows.append({
                    "symbol": sym, "entry_price": _f(entry),
                    "exit_price": _f(exit_px), "pnl_pct": ret,
                    "result": _pill(result, ret), "exit_reason": result,
                })

            n_tr = len(trade_rows)
            wins = sum(1 for t in trade_rows if (t["pnl_pct"] or 0) > 0)
            total_pnl = round(sum(t["pnl_pct"] or 0 for t in trade_rows), 2)
            avg_pnl = round(total_pnl / n_tr, 2) if n_tr else 0

            out["sides"][side] = {
                "funnel": {"universe": universe, "tc10": tc10, "open": n_open, "closed": n_closed},
                "stats": {"trades": n_tr, "win_rate": round(wins/n_tr*100, 1) if n_tr else 0,
                          "avg_pnl": avg_pnl, "total_pnl": total_pnl},
                "open": open_rows,
                "trades": trade_rows,
            }

        cur.close()
        conn.close()

    except Exception as e:
        # DB failed — fall back to test payload so we can confirm JS renders
        out["_debug"] = f"{type(e).__name__}: {str(e)[:300]}"
        out["_source"] = "fallback_test"
        out["cache_rows"] = -1
        out["sides"] = {
            "LONG": {
                "funnel": {"universe": 208, "tc10": 114, "open": 0, "closed": 10},
                "stats": {"trades": 10, "win_rate": 90.0, "avg_pnl": 0.49, "total_pnl": 4.88},
                "open": [],
                "trades": [
                    {"symbol": "ADANIGREEN", "entry_price": 1484.0, "exit_price": 1506.26,
                     "pnl_pct": 1.5, "result": "WIN", "exit_reason": "TARGET"},
                    {"symbol": "HDFCLIFE", "entry_price": 588.75, "exit_price": 591.35,
                     "pnl_pct": 0.44, "result": "WIN", "exit_reason": "SQUARE_OFF"},
                    {"symbol": "COFORGE", "entry_price": 1484.4, "exit_price": 1481.1,
                     "pnl_pct": -0.22, "result": "LOSS", "exit_reason": "SQUARE_OFF"},
                ],
            },
            "SHORT": {
                "funnel": {"universe": 208, "tc10": 3, "open": 0, "closed": 2},
                "stats": {"trades": 2, "win_rate": 0.0, "avg_pnl": -0.34, "total_pnl": -0.69},
                "open": [],
                "trades": [
                    {"symbol": "TCS", "entry_price": 2191.4, "exit_price": 2205.4,
                     "pnl_pct": -0.64, "result": "LOSS", "exit_reason": "SQUARE_OFF"},
                    {"symbol": "GODREJCP", "entry_price": 1009.4, "exit_price": 1009.9,
                     "pnl_pct": -0.05, "result": "LOSS", "exit_reason": "SQUARE_OFF"},
                ],
            },
        }

    return out


@router.post("/api/intraday/tick")
def intraday_tick():
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
