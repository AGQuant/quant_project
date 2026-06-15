"""
V8 Backfill Endpoints — One-time historical metric backfill
===========================================================
Saves 1-year EOD computed metrics (Jun 2025 - Jun 2026) into v8_metrics.
80 GVM>=6.5 futures symbols, 258 trading days, ~20,560 rows.

Triggered via: POST /api/v8/backfill/metrics
Auth: ADMIN_TOKEN required

Added: 15-Jun-2026 (buy_reversal_simulator.py validated these metrics)
"""

import os, logging
import numpy as np
import psycopg
from fastapi import APIRouter, HTTPException, Header
from collections import defaultdict

log = logging.getLogger("scorr.backfill")
router = APIRouter(prefix="/api/v8/backfill", tags=["backfill"])

GVM_MIN    = 6.5
DATA_START = "2023-06-01"
BT_START   = "2025-06-02"
BT_END     = "2026-06-12"
MIN_HIST   = 200

def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))

def _wilder_rsi(closes, period):
    if len(closes) < period + 1: return None
    c = np.array(closes, dtype=float); delta = np.diff(c)
    gain = np.where(delta>0,delta,0.0); loss = np.where(delta<0,-delta,0.0)
    ag = gain[:period].mean(); al_ = loss[:period].mean()
    for i in range(period, len(delta)):
        ag  = (ag  * (period-1) + gain[i]) / period
        al_ = (al_ * (period-1) + loss[i]) / period
    return 100.0 if al_==0 else 100-100/(1+ag/al_)


@router.post("/metrics")
def backfill_metrics(x_admin_token: str = Header(None)):
    """One-time backfill: compute + insert v8_metrics for Jun 2025 - Jun 2026."""
    if x_admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(401, "Unauthorized")

    log.info("Starting v8_metrics backfill...")

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT r.symbol, r.price_date::text, r.high, r.low, r.close, g.gvm_score
            FROM raw_prices r
            JOIN futures_universe fu ON fu.symbol=r.symbol AND fu.is_active=TRUE
            JOIN gvm_scores g ON g.symbol=r.symbol
                AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                AND g.gvm_score >= %s
            WHERE r.price_date >= %s
            ORDER BY r.symbol, r.price_date
        """, (GVM_MIN, DATA_START))
        rows = cur.fetchall()

    by_sym = defaultdict(list); gvm_map = {}
    for sym, dt, h, l, c, gvm in rows:
        by_sym[sym].append({"dt":dt,"high":float(h),"low":float(l),"close":float(c)})
        gvm_map[sym] = float(gvm)

    all_dates = sorted(set(r["dt"] for sd in by_sym.values() for r in sd))
    bt_dates  = [d for d in all_dates if BT_START <= d <= BT_END]
    sym_idx   = {sym:{r["dt"]:i for i,r in enumerate(sd)} for sym,sd in by_sym.items()}

    all_metrics = {}
    for sym, sdata in by_sym.items():
        closes_all = [r["close"] for r in sdata]
        for dt in bt_dates:
            idx = sym_idx[sym].get(dt)
            if idx is None or idx < MIN_HIST: continue
            hist = closes_all[:idx]; live = closes_all[idx]
            dma50  = (live/np.mean(hist[-50:]) -1)*100 if len(hist)>=50  else None
            dma200 = (live/np.mean(hist[-200:])-1)*100 if len(hist)>=200 else None
            wk_ret = (live/hist[-6] -1)*100            if len(hist)>=6   else None
            mo_ret = (live/hist[-22]-1)*100            if len(hist)>=22  else None
            mom2d  = (live/hist[-2] -1)*100            if len(hist)>=2   else None
            rsi_m = None
            if len(hist)>=22*7:
                mc=[hist[i] for i in range(-22*7,0,22)]+[live]
                rsi_m=_wilder_rsi(mc,6)
            rsi_w = None
            if len(hist)>=5*9:
                wc=[hist[i] for i in range(-5*9,0,5)]+[live]
                rsi_w=_wilder_rsi(wc,8)
            all_metrics[(sym,dt)]={
                "gvm":gvm_map[sym],"dma50":dma50,"dma200":dma200,
                "mo_ret":mo_ret,"wk_ret":wk_ret,
                "rsi_m":rsi_m,"rsi_w":rsi_w,"mom2d":mom2d,
            }

    sector = {}
    for dt in bt_dates:
        wv=[all_metrics[(s,dt)]["wk_ret"] for s in by_sym if (s,dt) in all_metrics and all_metrics[(s,dt)]["wk_ret"] is not None]
        mv=[all_metrics[(s,dt)]["mo_ret"] for s in by_sym if (s,dt) in all_metrics and all_metrics[(s,dt)]["mo_ret"] is not None]
        sector[dt]={"sw":float(np.mean(wv)) if wv else None,"sm":float(np.mean(mv)) if mv else None}

    inserted = 0
    with _conn() as conn, conn.cursor() as cur:
        for (sym,dt),m in all_metrics.items():
            sec=sector.get(dt,{})
            try:
                cur.execute("""
                    INSERT INTO v8_metrics
                    (symbol,score_date,gvm_score,dma_50,dma_200,
                     rsi_month,rsi_weekly,month_return,week_return,mom_2d,
                     sector_week,sector_month)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol,score_date) DO UPDATE SET
                        gvm_score=EXCLUDED.gvm_score,dma_50=EXCLUDED.dma_50,
                        dma_200=EXCLUDED.dma_200,rsi_month=EXCLUDED.rsi_month,
                        rsi_weekly=EXCLUDED.rsi_weekly,month_return=EXCLUDED.month_return,
                        week_return=EXCLUDED.week_return,mom_2d=EXCLUDED.mom_2d,
                        sector_week=EXCLUDED.sector_week,sector_month=EXCLUDED.sector_month
                """, (sym,dt,m["gvm"],m["dma50"],m["dma200"],
                      m["rsi_m"],m["rsi_w"],m["mo_ret"],m["wk_ret"],m["mom2d"],
                      sec.get("sw"),sec.get("sm")))
                inserted+=1
            except Exception as e:
                log.warning(f"backfill {sym} {dt}: {e}")
        conn.commit()

    return {"status":"ok","symbols":len(by_sym),"bt_days":len(bt_dates),
            "rows_inserted":inserted,"date_range":f"{BT_START} to {BT_END}"}
