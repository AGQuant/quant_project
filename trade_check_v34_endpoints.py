"""
Trade Check v3.4 endpoints — FastAPI router.
"""

import os
from datetime import datetime, timedelta

import psycopg
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

import trade_check_v34 as tc
import native_trade_check as ntc
import tc_intraday as tci

router = APIRouter()

_DB = os.getenv("DATABASE_URL", "")


def _ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


class CheckRequest(BaseModel):
    symbol: str
    side: str = "LONG"
    gate_5min: bool = False
    gate_1day: bool = False
    render: bool = True


class PromoteRequest(BaseModel):
    symbol: str
    side: str = "LONG"
    gate_5min: bool = False
    gate_1day: bool = False
    qty: int
    entry_price: float
    notes: Optional[str] = ""


@router.post("/api/trade-check/v34")
def check(req: CheckRequest):
    result = tc.trade_check(req.symbol, req.side, req.gate_5min, req.gate_1day)
    if req.render and "error" not in result:
        result["table"] = tc.render_table(result)
    return result


@router.post("/api/trade-check/v34/promote")
def promote(req: PromoteRequest):
    result = tc.trade_check(req.symbol, req.side, req.gate_5min, req.gate_1day)
    if "error" in result:
        return result
    promo = tc.promote_to_personal_journal(result, req.qty, req.entry_price, req.notes or "")
    return {"check": result, "promote": promo}


@router.get("/api/trade-check/v34/health")
def health():
    return {
        "version": tc.VERSION, "parent_spec": tc.SPEC_PARENT,
        "max_weighted": tc.MAX_WEIGHTED,
        "thresholds": {"STRONG": tc.STRONG_MIN, "VALID": tc.VALID_MIN},
        "status": "ok",
    }


@router.get("/api/trade-check/screen-nifty50")
def screen_nifty50(n: int = 50, top: int = 10):
    n = max(10, min(n, 210)); top = max(1, min(top, 20))
    return ntc.screen_top50(n=n, top=top)


@router.post("/api/trade-check/tc-cache/refresh")
def tc_cache_refresh(n: int = 210):
    n = max(10, min(n, 210))
    return tci.refresh_tc_cache(n=n)


@router.get("/api/trade-check/intraday-scan")
def intraday_scan(side: str = "LONG"):
    side = "SHORT" if side.upper() == "SHORT" else "LONG"
    return tci.intraday_scan(side=side)


@router.get("/api/trade-check/intraday-paper/status")
def intraday_paper_status():
    return tci.intraday_paper_status()


@router.post("/api/trade-check/intraday-paper/run")
def intraday_paper_run():
    rc = tci.refresh_tc_cache()
    en = tci.run_intraday_paper_entry()
    ex = tci.run_intraday_paper_exit()
    return {"ok": True, "cache_written": rc.get("written"),
            "entered": en.get("entered"), "closed": ex.get("closed"),
            "square_off": ex.get("square_off"), "ts": en.get("ts")}


# ── /api/intraday/dashboard  ── self-contained, no module deps ────────────────
# Does NOT call tci.intraday_dashboard() — uses raw psycopg so module import
# chain issues cannot cause silent failures.

@router.get("/api/intraday/dashboard")
def intraday_dashboard():
    now = _ist()
    out = {"ts": now.strftime("%d-%b %H:%M IST"),
           "cache_ts": None, "cache_rows": 0, "sides": {}}
    try:
        conn = psycopg.connect(_DB)
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM tc_cache")
        r = cur.fetchone()
        out["cache_rows"] = int(r[0]) if r else 0

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
            opens = []
            for row in cur.fetchall():
                e = _f(row[1]); cmp = _f(row[4])
                pnl = round(((cmp/e - 1) if side == "LONG" else (e/cmp - 1)) * 100, 2) if e and cmp else None
                opens.append({"symbol": row[0], "entry_price": e,
                               "cmp": round(cmp, 2) if cmp else None,
                               "pnl_pct": pnl, "target": _f(row[2]), "stop": _f(row[3])})

            cur.execute("""
                SELECT symbol, entry_price, exit_price, return_pct, result
                FROM tc_intraday_trades
                WHERE side=%s AND exit_ts::date=CURRENT_DATE
                ORDER BY exit_ts DESC LIMIT 50
            """, (side,))
            trades = []
            for row in cur.fetchall():
                ret = _f(row[3]); res = row[4]
                pill = "WIN" if res == "TARGET" else ("LOSS" if res == "SL" else
                        ("WIN" if (ret or 0) > 0 else ("LOSS" if (ret or 0) < 0 else "FLAT")))
                trades.append({"symbol": row[0], "entry_price": _f(row[1]),
                                "exit_price": _f(row[2]), "pnl_pct": ret,
                                "result": pill, "exit_reason": res})

            n = len(trades)
            wins = sum(1 for t in trades if (t["pnl_pct"] or 0) > 0)
            total = round(sum(t["pnl_pct"] or 0 for t in trades), 2)

            out["sides"][side] = {
                "funnel": {"universe": universe, "tc10": tc10,
                           "open": n_open, "closed": n_closed},
                "stats": {"trades": n, "win_rate": round(wins/n*100, 1) if n else 0,
                          "avg_pnl": round(total/n, 2) if n else 0, "total_pnl": total},
                "open": opens,
                "trades": trades,
            }

        cur.close()
        conn.close()

    except Exception as e:
        out["_error"] = f"{type(e).__name__}: {str(e)}"

    return out


@router.post("/api/intraday/tick")
def intraday_tick():
    rc = tci.refresh_tc_cache()
    en = tci.run_intraday_paper_entry()
    ex = tci.run_intraday_paper_exit()
    return {"ok": True, "cache_written": rc.get("written"),
            "new_entries": en.get("positions", []),
            "closed": ex.get("closed"),
            "square_off": ex.get("square_off"),
            "ts": en.get("ts") or ex.get("ts")}
