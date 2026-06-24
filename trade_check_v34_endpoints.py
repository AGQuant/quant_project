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
import trade_check_v36 as tc36
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


# ── Trade Check v3.6 (session_log id=600) — canonical /api/trade-check ─────────
# New 11-rule (LONG) / 10-rule (SHORT) Tier-1 gate. Runs side-by-side with the
# v34 weighted engine above; /api/trade-check/v34 is left unchanged.

class CheckV36Request(BaseModel):
    symbol: str
    side: str = "LONG"
    render: bool = True


@router.post("/api/trade-check")
def trade_check_v36_post(req: CheckV36Request):
    result = tc36.trade_check_v36(req.symbol, req.side)
    if req.render and "error" not in result:
        result["table"] = tc36.render_table(result)
    return result


@router.get("/api/trade-check")
def trade_check_v36_get(symbol: str, side: str = "LONG", render: bool = True):
    result = tc36.trade_check_v36(symbol, side)
    if render and "error" not in result:
        result["table"] = tc36.render_table(result)
    return result


@router.get("/api/trade-check/v36/health")
def health_v36():
    return {
        "version": tc36.VERSION, "spec_ref": tc36.SPEC_REF,
        "max_score": {"LONG": tc36.MAX_LONG, "SHORT": tc36.MAX_SHORT},
        "advance_threshold": tc36.ADVANCE_MIN, "status": "ok",
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


# ── TC screener nightly pre-compute cache (task #43) ───────────────────────────
# Kills the 60-90s live-screen spinner: score the full futures universe once
# after market close, store in tc_screener_cache, serve instantly from cache.

TC_UNIVERSE = "futures"


def _pivot_zone(cmp_v, pp, r1, s1):
    if cmp_v is None or pp is None:
        return None
    if r1 is not None and cmp_v >= r1:
        return "Above R1"
    if cmp_v >= pp:
        return "PP-R1"
    if s1 is not None and cmp_v >= s1:
        return "S1-PP"
    return "Below S1"


def run_tc_screener_precompute():
    """Score the full active futures universe (LONG + SHORT) via trade_check_v34
    and replace today's rows in tc_screener_cache. Heavy (~universe x2 scorer
    calls) — nightly @16:00 IST or manual POST /api/admin/run-tc-screener only."""
    run_date = _ist().date()
    conn = psycopg.connect(_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS tc_screener_cache (
                id SERIAL PRIMARY KEY, run_date DATE, universe VARCHAR(20), side VARCHAR(10),
                symbol VARCHAR(20), score NUMERIC, verdict VARCHAR(20), cmp NUMERIC,
                pivot_zone TEXT, failed_rules TEXT[], computed_at TIMESTAMPTZ DEFAULT NOW())""")
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT symbol, cmp FROM cmp_prices")
            cmp_map = {r[0]: _f(r[1]) for r in cur.fetchall()}
            cur.execute("""SELECT symbol, pp, r1, s1 FROM v8_paper_pivots
                           WHERE pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)""")
            piv_map = {r[0]: (_f(r[1]), _f(r[2]), _f(r[3])) for r in cur.fetchall()}
        conn.commit()

        out_rows = []
        for sym in symbols:
            cmp_v = cmp_map.get(sym)
            pp, r1, s1 = piv_map.get(sym, (None, None, None))
            zone = _pivot_zone(cmp_v, pp, r1, s1)
            for side in ("LONG", "SHORT"):
                res = tc.trade_check(sym, side)
                if not isinstance(res, dict) or res.get("error"):
                    continue
                failed = [r.get("rule") for r in res.get("rules", [])
                          if r.get("status") in ("FAIL", "VETO") and r.get("rule")]
                out_rows.append((run_date, TC_UNIVERSE, side, sym,
                                 res.get("earned"), res.get("verdict"), cmp_v, zone, failed))

        with conn.cursor() as cur:
            cur.execute("DELETE FROM tc_screener_cache WHERE run_date = %s AND universe = %s",
                        (run_date, TC_UNIVERSE))
            cur.executemany("""INSERT INTO tc_screener_cache
                (run_date, universe, side, symbol, score, verdict, cmp, pivot_zone, failed_rules)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", out_rows)
        conn.commit()
        longs = sum(1 for r in out_rows if r[2] == "LONG")
        shorts = sum(1 for r in out_rows if r[2] == "SHORT")
        return {"ok": True, "run_date": str(run_date), "symbols": len(symbols),
                "rows": len(out_rows), "long": longs, "short": shorts}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        conn.close()


@router.get("/api/trade-check/screen-cached")
def screen_cached(universe: str = "nifty50", side: Optional[str] = None, top: int = 10):
    """Fast read of the nightly tc_screener_cache — top LONG + top SHORT for today.
    Falls back to live ntc.screen_top50 if today's cache is empty (first day/weekend)."""
    top = max(1, min(top, 50))
    run_date = _ist().date()
    conn = psycopg.connect(_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM tc_screener_cache WHERE run_date = %s", (run_date,))
            n_today = int(cur.fetchone()[0] or 0)
            if n_today == 0:
                return {"cached": False, "run_date": str(run_date), "universe": universe,
                        "note": "cache empty for today — live fallback",
                        "live": ntc.screen_top50(n=50, top=top)}
            sides = ["LONG", "SHORT"] if not side else [side.upper()]
            result = {}
            for sd in sides:
                cur.execute("""SELECT symbol, score, verdict, cmp, pivot_zone, failed_rules
                               FROM tc_screener_cache
                               WHERE run_date = %s AND side = %s
                               ORDER BY score DESC NULLS LAST LIMIT %s""",
                            (run_date, sd, top))
                cols = [d[0] for d in cur.description]
                result[sd.lower()] = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"cached": True, "run_date": str(run_date), "universe": universe, "top": top, **result}


@router.get("/api/trade-check/movers")
def movers(universe: str = "futures", side: str = "LONG"):
    """CC-48: diff today's tc_screener_cache vs the most recent prior run_date.
    Categorizes each symbol into new_pass / dropped / score_up / score_down /
    verdict_flip. Returns baseline_only=true until a 2nd snapshot exists.
    Verdict rank: STRONG > VALID > WATCH (PASS = STRONG or VALID)."""
    side = (side or "LONG").upper()
    PASS = {"STRONG", "VALID"}
    conn = psycopg.connect(_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT run_date FROM tc_screener_cache ORDER BY run_date DESC LIMIT 2")
            dates = [r[0] for r in cur.fetchall()]
            if not dates:
                return {"baseline_only": True, "universe": universe, "side": side,
                        "message": "No screener cache yet — run the nightly TC screener first.",
                        "new_pass": [], "dropped": [], "score_up": [], "score_down": [], "verdict_flip": []}
            today = dates[0]
            if len(dates) < 2:
                return {"baseline_only": True, "run_date": str(today), "universe": universe, "side": side,
                        "message": "Building baseline — movers available from tomorrow",
                        "new_pass": [], "dropped": [], "score_up": [], "score_down": [], "verdict_flip": []}
            prev = dates[1]
            cur.execute("""SELECT symbol, score, verdict, cmp, pivot_zone
                           FROM tc_screener_cache WHERE run_date=%s AND side=%s""", (today, side))
            tcols = [d[0] for d in cur.description]
            today_rows = {r[0]: dict(zip(tcols, r)) for r in cur.fetchall()}
            cur.execute("""SELECT symbol, score, verdict FROM tc_screener_cache
                           WHERE run_date=%s AND side=%s""", (prev, side))
            prev_rows = {r[0]: {"score": _f(r[1]), "verdict": r[2]} for r in cur.fetchall()}
    finally:
        conn.close()

    new_pass, dropped, score_up, score_down, verdict_flip = [], [], [], [], []
    for sym, t in today_rows.items():
        tv, ts = t.get("verdict"), _f(t.get("score"))
        row = {"symbol": sym, "score": ts, "verdict": tv,
               "cmp": _f(t.get("cmp")), "pivot_zone": t.get("pivot_zone")}
        p = prev_rows.get(sym)
        if p is None:
            if tv in PASS:
                new_pass.append(row)
            continue
        pv, ps = p.get("verdict"), p.get("score")
        row["prev_verdict"], row["prev_score"] = pv, ps
        if tv != pv:
            if tv in PASS and pv not in PASS:
                new_pass.append(row)
            elif pv in PASS and tv not in PASS:
                dropped.append(row)
            else:
                verdict_flip.append(row)
        elif ts is not None and ps is not None:
            if ts > ps:
                score_up.append(row)
            elif ts < ps:
                score_down.append(row)

    # symbols that were PASS yesterday but absent from today's cache
    for sym, p in prev_rows.items():
        if sym not in today_rows and p.get("verdict") in PASS:
            dropped.append({"symbol": sym, "score": None, "verdict": None,
                            "prev_verdict": p.get("verdict"), "prev_score": p.get("score")})

    new_pass.sort(key=lambda x: -(x["score"] or 0))
    score_up.sort(key=lambda x: -((x.get("score") or 0) - (x.get("prev_score") or 0)))
    score_down.sort(key=lambda x: (x.get("score") or 0) - (x.get("prev_score") or 0))
    return {"baseline_only": False, "run_date": str(today), "prev_date": str(prev),
            "universe": universe, "side": side,
            "new_pass": new_pass, "dropped": dropped, "score_up": score_up,
            "score_down": score_down, "verdict_flip": verdict_flip}


@router.post("/api/admin/run-tc-screener")
def run_tc_screener():
    """Manually seed today's tc_screener_cache (synchronous; off-hours use)."""
    return run_tc_screener_precompute()
