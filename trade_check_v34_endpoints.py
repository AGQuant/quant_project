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
    """RETIRED cc#1982 (TC V2 migration, founder ruling 10-Sep, cc_task_logs 6228: "V2 everywhere").
    tc_screener_cache is frozen since 08-Sep. Repo-wide search on 10-Sep-2026 found no frontend
    file calling this route, so per the card's "migrate, or retire if the only consumers are gone"
    rule it is retired rather than migrated. Left wired, not deleted (no route deleted on a P0
    card). Live equivalent: GET /api/mobile/check/scan?universe=... (app_check_endpoints.py, reads
    tc_universe_ticks via tc_resolver)."""
    return {"cached": False, "retired": True,
            "note": "retired 10-Sep-2026 (no consumer found); see tc_universe_ticks via "
                    "/api/mobile/check/scan",
            "universe": universe, "side": side, "top": top}


@router.get("/api/trade-check/movers")
def movers(universe: str = "futures", side: str = "LONG"):
    """cc#1982: migrated off tc_screener_cache onto tc_universe_ticks (TC V2, canonical; founder
    ruling 10-Sep, cc_task_logs 6228: "V2 everywhere"). Diffs the best-of-bucket score100 at the
    latest tick of the two most recent trading days present in tc_universe_ticks, for the
    requested side (LONG maps to the table's BUY, SHORT to SELL). "Best of bucket" is the same
    definition app_check_endpoints.py uses for the app Check tab: highest score100 among that
    side's style buckets at the latest tick. pivot_zone and failed_rules do not exist in
    tc_universe_ticks and are dropped rather than faked; `bucket` (which style won) is added.
    Categorizes each symbol into new_pass / dropped / score_up / score_down / verdict_flip.
    Returns baseline_only=true until a 2nd trading day of ticks exists.
    Verdict rank: STRONG > VALID > WATCH > REJECT (PASS = STRONG or VALID)."""
    side = (side or "LONG").upper()
    tc_side = "SELL" if side == "SHORT" else "BUY"   # LONG/SHORT -> tc_universe_ticks' BUY/SELL
    PASS = {"STRONG", "VALID"}
    conn = psycopg.connect(_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date
                           FROM tc_universe_ticks ORDER BY 1 DESC LIMIT 2""")
            dates = [r[0] for r in cur.fetchall()]
            if not dates:
                return {"baseline_only": True, "universe": universe, "side": side,
                        "message": "No tc_universe_ticks data yet.",
                        "new_pass": [], "dropped": [], "score_up": [], "score_down": [], "verdict_flip": []}
            today = dates[0]
            if len(dates) < 2:
                return {"baseline_only": True, "run_date": str(today), "universe": universe, "side": side,
                        "message": "Building baseline — movers available from tomorrow",
                        "new_pass": [], "dropped": [], "score_up": [], "score_down": [], "verdict_flip": []}
            prev = dates[1]
            # best-scoring bucket per symbol at each day's latest tick, one query for both days
            cur.execute("""
                SELECT symbol, d, score100, verdict10, bucket, cmp FROM (
                    SELECT symbol, (ts AT TIME ZONE 'Asia/Kolkata')::date AS d,
                           score100, verdict10, bucket, cmp,
                           ROW_NUMBER() OVER (PARTITION BY symbol, (ts AT TIME ZONE 'Asia/Kolkata')::date
                                               ORDER BY ts DESC, score100 DESC) AS rn
                    FROM tc_universe_ticks
                    WHERE side = %s AND (ts AT TIME ZONE 'Asia/Kolkata')::date IN (%s, %s)
                ) x WHERE rn = 1""", (tc_side, today, prev))
            tcols = [d[0] for d in cur.description]
            all_rows = [dict(zip(tcols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    today_rows = {r["symbol"]: r for r in all_rows if r["d"] == today}
    prev_rows = {r["symbol"]: {"score": _f(r["score100"]), "verdict": r["verdict10"]}
                 for r in all_rows if r["d"] == prev}

    new_pass, dropped, score_up, score_down, verdict_flip = [], [], [], [], []
    for sym, t in today_rows.items():
        tv, ts = t.get("verdict10"), _f(t.get("score100"))
        row = {"symbol": sym, "score": ts, "verdict": tv,
               "cmp": _f(t.get("cmp")), "bucket": t.get("bucket")}
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
    """RETIRED cc#1982 (TC V2 migration, founder ruling 10-Sep, cc_task_logs 6228: "V2 everywhere,
    do not re-activate bg_tc_screener_precompute"). No longer writes tc_screener_cache from this
    route. run_tc_screener_precompute() itself is left untouched -- scheduler.py:1944 still wires
    it to the bg_tc_screener_precompute job row (active=false in scheduler_master); no writer is
    enabled or disabled here (do_not_touch, cc#1982 spec). tc_universe_ticks has its own 5-min
    writer (bg_tc_universe_tick) and needs no manual trigger."""
    return {"ok": False, "retired": True,
            "note": "retired 10-Sep-2026; tc_universe_ticks is written every 5 min by "
                    "bg_tc_universe_tick, no manual trigger needed"}
