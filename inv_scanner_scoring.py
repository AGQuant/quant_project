"""cc#1284 · INVESTMENT SCANNER ENGINE 2/3 — TWO-TRACK SCORING (spec session_log 30147,
INVESTMENT_SCANNER_SCORE_AND_RULES_V1 — the formulas below are THE spec, implemented verbatim).

Two /100 scores per universe symbol per run_date:

MOMENTUM /100                                REVERSAL /100
  quality (composite GVM)   w50 graded         quality ex-M ((G+V)/2)  w50 graded
  52-week position          w15 continuous     pullback depth          w14 banded
  1-month dM                w10 continuous     washed monthly RSI      w12 banded
  DMA alignment (thirds)    w10                S1 touch+reclaim        w12 (IC-V2 verbatim)
  monthly RSI band          w8                 accumulation-under-weakness w12
  volume ratio              w7

S1 IS NOT REIMPLEMENTED: this module imports invest_check_v2._bars/_s1_reclaim and multiplies
the SAME 0/0.5/1 credit by 12. IC-V2's own behaviour is untouched by construction — nothing in
its file changes (regression anchor: AFFLE score10 stays 7.5).

INSUFFICIENT INPUTS are excluded from the computable weight and the score renormalized
(IC-V2 excluded_components pattern) — a missing leg is never a fake zero. components jsonb
carries every leg's raw input, credit, weight and excluded flag so any score can be re-derived
by hand from its own row.

Hand-validation against 30147's live 24-Aug numbers BEFORE this file was written:
  DCBBANK mom  = 50 + 14.6 + 10 + 10 + 8 + 7 = 99.6   (spec: 99.6)
  BSE     rev  = 50 + 14 + 6 + 12 + s1:0     = 82      (spec: 82, S1 credit 0 — CMP<PP)
  KRISHNADEF rev = 50 + 14 + 12 + 0 + 0      = 76      (spec: 76, quality_ex_m 8.62)

Bands both tracks: STRONG_BUY>=84 · ACCUMULATE>=65 · WATCH>=50 · AVOID<50.
Context isolation (27979): shares grammar with TC/V8, never rules or data paths.
"""

import os
import json
import logging
from typing import Optional

import psycopg
from psycopg.types.json import Json
from fastapi import APIRouter, Header, HTTPException

from invest_check_v2 import _bars, _s1_reclaim   # shared S1 — IC-V2's own function, not a copy

log = logging.getLogger("scorr.inv_scanner_scoring")
router = APIRouter(tags=["investment_scanner"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


DDL = """
CREATE TABLE IF NOT EXISTS investment_scanner_scores (
    symbol   TEXT NOT NULL,
    run_date DATE NOT NULL,
    mom_score NUMERIC,
    rev_score NUMERIC,
    mom_components JSONB,
    rev_components JSONB,
    computable_weight_mom NUMERIC,
    computable_weight_rev NUMERIC,
    band_mom TEXT,
    band_rev TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, run_date)
);
"""


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _band(score):
    if score is None:
        return None
    if score >= 84: return "STRONG_BUY"
    if score >= 65: return "ACCUMULATE"
    if score >= 50: return "WATCH"
    return "AVOID"


def _mom_legs(gvm, dm, wk52, dma20, dma50, dma200, rsi_m, vol21):
    """Momentum legs, 30147 verbatim. Each returns (weight, credit_points|None, raw)."""
    legs = {}
    # quality_composite_gvm_w50: graded
    if gvm is None:
        legs["quality_composite_gvm"] = (50, None, gvm)
    else:
        q = 50 if gvm >= 8 else 42 if gvm >= 7.5 else 35 if gvm >= 7 else 15
        legs["quality_composite_gvm"] = (50, float(q), gvm)
    # wk52_position_w15: continuous 15 * LEAST(wk52/100, 1)
    legs["wk52_position"] = (15, None if wk52 is None else 15.0 * min(wk52 / 100.0, 1.0), wk52)
    # dm_1mo_w10: continuous 10 * LEAST(dm/2, 1) if dm>0 else 0
    legs["dm_1mo"] = (10, None if dm is None else (10.0 * min(dm / 2.0, 1.0) if dm > 0 else 0.0), dm)
    # dma_alignment_w10: 10 * (legs above 20/50/200)/3, pct-distance sign
    if dma20 is None and dma50 is None and dma200 is None:
        legs["dma_alignment"] = (10, None, None)
    else:
        ups = sum(1 for d in (dma20, dma50, dma200) if d is not None and d > 0)
        legs["dma_alignment"] = (10, 10.0 * ups / 3.0, {"dma_20": dma20, "dma_50": dma50, "dma_200": dma200})
    # rsi_monthly_w8: 55-72 -> 8; 45-55 or 72-78 -> 4; else 0
    if rsi_m is None:
        legs["rsi_monthly"] = (8, None, rsi_m)
    else:
        r = 8 if 55 <= rsi_m <= 72 else 4 if (45 <= rsi_m < 55 or 72 < rsi_m <= 78) else 0
        legs["rsi_monthly"] = (8, float(r), rsi_m)
    # volume_w7: 7 * LEAST(vol/1.5, 1) if >=0.9 else 0
    legs["volume"] = (7, None if vol21 is None else (7.0 * min(vol21 / 1.5, 1.0) if vol21 >= 0.9 else 0.0), vol21)
    return legs


def _rev_legs(g, v, wk52, rsi_m, vol21, month_ret, s1):
    """Reversal legs, 30147 verbatim. s1 is the IC-V2 _s1_reclaim dict or None."""
    legs = {}
    # quality_ex_momentum_w50: (G+V)/2 graded
    if g is None or v is None:
        legs["quality_ex_momentum"] = (50, None, None)
    else:
        q = (g + v) / 2.0
        pts = 50 if q >= 7.75 else 42 if q >= 7.25 else 30 if q >= 6.75 else 12
        legs["quality_ex_momentum"] = (50, float(pts), q)
    # pullback_depth_w14: wk52 35-65 -> 14; 65-75 -> 7; <35 -> 4; >75 -> 0
    if wk52 is None:
        legs["pullback_depth"] = (14, None, wk52)
    else:
        p = 14 if 35 <= wk52 <= 65 else 7 if 65 < wk52 <= 75 else 4 if wk52 < 35 else 0
        legs["pullback_depth"] = (14, float(p), wk52)
    # washed_rsi_w12: 35-50 -> 12; 50-58 -> 6; <35 -> 3; >58 -> 0
    if rsi_m is None:
        legs["washed_rsi"] = (12, None, rsi_m)
    else:
        w = 12 if 35 <= rsi_m <= 50 else 6 if 50 < rsi_m <= 58 else 3 if rsi_m < 35 else 0
        legs["washed_rsi"] = (12, float(w), rsi_m)
    # s1_touch_reclaim_w12: IC-V2 credit (0/0.5/1) * 12 — THE trigger
    if s1 is None:
        legs["s1_touch_reclaim"] = (12, None, None)
    else:
        legs["s1_touch_reclaim"] = (12, 12.0 * float(s1["credit"]),
                                    {"credit": s1["credit"], "touched_sessions_ago": s1["touched_sessions_ago"],
                                     "above_pp": s1["above_pp"], "pp": s1["pp"], "cmp": s1["cmp"]})
    # accumulation_under_weakness_w12: vol>=1.1 AND month<=0 -> 12; vol>=1.1 AND month<=5 -> 6; else 0
    if vol21 is None or month_ret is None:
        legs["accumulation_under_weakness"] = (12, None, {"vol_ratio_21": vol21, "month_return": month_ret})
    else:
        a = 12 if (vol21 >= 1.1 and month_ret <= 0) else 6 if (vol21 >= 1.1 and month_ret <= 5) else 0
        legs["accumulation_under_weakness"] = (12, float(a), {"vol_ratio_21": vol21, "month_return": month_ret})
    return legs


def _score(legs):
    """Renormalized /100 over computable weight (IC-V2 excluded pattern). Returns
    (score, computable_weight, components_json)."""
    earned = computable = 0.0
    comp = {}
    for name, (w, pts, raw) in legs.items():
        excluded = pts is None
        if not excluded:
            earned += pts
            computable += w
        comp[name] = {"weight": w, "credit": None if excluded else round(pts, 3),
                      "input": raw, "excluded": excluded}
    score = round(earned / computable * 100.0, 1) if computable > 0 else None
    return score, computable, comp


def compute(conn=None) -> dict:
    own = conn is None
    if own:
        conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("SELECT MAX(run_date) FROM investment_scanner_universe")
            d = cur.fetchone()[0]
            if d is None:
                return {"status": "skip", "reason": "no universe run to score"}
            cur.execute("""
                SELECT u.symbol, u.gvm, u.g, u.v, u.dm_1mo,
                       ut.week_index_52, ut.dma_20, ut.dma_50, ut.dma_200,
                       ut.rsi_month, ut.vol_ratio_21, ut.month_return
                FROM investment_scanner_universe u
                LEFT JOIN universe_technicals ut ON ut.symbol = u.symbol
                     AND ut.score_date = (SELECT MAX(score_date) FROM universe_technicals)
                WHERE u.run_date = %s ORDER BY u.symbol""", (d,))
            rows = cur.fetchall()
            n = 0
            tech_missing = []
            for (sym, gvm, g, v, dm, wk52, dma20, dma50, dma200, rsi_m, vol21, month_ret) in rows:
                gvm, g, v, dm = _f(gvm), _f(g), _f(v), _f(dm)
                wk52, dma20, dma50, dma200 = _f(wk52), _f(dma20), _f(dma50), _f(dma200)
                rsi_m, vol21, month_ret = _f(rsi_m), _f(vol21), _f(month_ret)
                if wk52 is None and rsi_m is None and vol21 is None:
                    tech_missing.append(sym)
                s1 = None
                try:
                    bars = _bars(cur, sym)
                    s1 = _s1_reclaim(bars)             # None when < 12 bars — leg excluded
                except Exception as e:                  # noqa: BLE001 — one symbol never kills the run
                    log.warning(f"s1 {sym}: {e}")
                mom, cw_m, comp_m = _score(_mom_legs(gvm, dm, wk52, dma20, dma50, dma200, rsi_m, vol21))
                rev, cw_r, comp_r = _score(_rev_legs(g, v, wk52, rsi_m, vol21, month_ret, s1))
                cur.execute("""
                    INSERT INTO investment_scanner_scores
                        (symbol, run_date, mom_score, rev_score, mom_components, rev_components,
                         computable_weight_mom, computable_weight_rev, band_mom, band_rev)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, run_date) DO UPDATE SET
                        mom_score=EXCLUDED.mom_score, rev_score=EXCLUDED.rev_score,
                        mom_components=EXCLUDED.mom_components, rev_components=EXCLUDED.rev_components,
                        computable_weight_mom=EXCLUDED.computable_weight_mom,
                        computable_weight_rev=EXCLUDED.computable_weight_rev,
                        band_mom=EXCLUDED.band_mom, band_rev=EXCLUDED.band_rev
                """, (sym, d, mom, rev, Json(comp_m), Json(comp_r), cw_m, cw_r, _band(mom), _band(rev)))
                n += 1
        conn.commit()
        out = {"status": "ok", "run_date": str(d), "scored": n,
               "technicals_missing": tech_missing[:20], "technicals_missing_n": len(tech_missing)}
        log.info(f"inv_scanner_scoring: {out}")
        return out
    finally:
        if own:
            conn.close()


@router.post("/api/admin/run-inv-scanner-scoring")
def admin_run(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return compute()


@router.get("/api/inv-scanner/scores")
def get_scores(run_date: Optional[str] = None, track: str = "momentum", limit: int = 50):
    """Ranked scores for a run_date (default latest). track = momentum | reversal."""
    col = "mom_score" if track != "reversal" else "rev_score"
    band = "band_mom" if track != "reversal" else "band_rev"
    with _conn() as conn, conn.cursor() as cur:
        if not run_date:
            cur.execute("SELECT MAX(run_date) FROM investment_scanner_scores")
            r = cur.fetchone()
            run_date = str(r[0]) if r and r[0] else None
        if not run_date:
            return {"run_date": None, "rows": []}
        cur.execute(f"""
            SELECT s.symbol, s.mom_score, s.rev_score, s.{band}, u.tags, u.insufficient_history,
                   s.computable_weight_mom, s.computable_weight_rev
            FROM investment_scanner_scores s
            JOIN investment_scanner_universe u ON u.symbol = s.symbol AND u.run_date = s.run_date
            WHERE s.run_date = %s AND s.{col} IS NOT NULL
            ORDER BY s.{col} DESC, s.symbol LIMIT %s""", (run_date, max(1, min(limit, 250))))
        rows = [{"symbol": r[0],
                 "mom_score": _f(r[1]), "rev_score": _f(r[2]), "band": r[3],
                 "tags": r[4], "insufficient_history": r[5],
                 "computable_weight_mom": _f(r[6]), "computable_weight_rev": _f(r[7])}
                for r in cur.fetchall()]
    return {"run_date": run_date, "track": track, "count": len(rows), "rows": rows}
