"""
Buy Reversal Simulator — Standalone EOD Backtest Engine
========================================================
Saved: 15-Jun-2026 | Author: Arpit Goel / Scorr

PURPOSE:
  Reusable 1-year EOD backtest for buy_reversal basket.
  Validated against 6-day 5-min intraday sim results.
  Foundation for all future strategy optimisation.

KEY FINDINGS (Jun 2025 - Jun 2026, 80 symbols GVM>=6.5):
  Static old config  : 547 signals, 50.3% WR, -0.15% exp, -81.52% P&L
  Static new config  : 223 signals, 59.6% WR, +0.16% exp, +35.53% P&L
  Dynamic Nifty-linked: 194 signals, 63.4% WR, +0.25% exp, +48.91% P&L

DYNAMIC FILTER LOGIC (Nifty 1-month return as regime gate):
  BULL    (Nifty 1M > +2%) : week_ret<=3, rsi_month<=67, sector_week<=4
  NEUTRAL (Nifty 1M  0-2%) : week_ret<=2, rsi_month<=62, sector_week<=3
  BEAR    (Nifty 1M < 0%)  : week_ret<=1, rsi_month<=58, sector_week<=2

PER-REGIME PERFORMANCE (Dynamic config):
  BULL   : 112 trades, WR=71.4%, Exp=+0.59%, P&L=+66.57%
  NEUTRAL:  25 trades, WR=48.0%, Exp=-0.35%, P&L=-8.78%
  BEAR   :  57 trades, WR=54.4%, Exp=-0.16%, P&L=-8.87%
  Note: NEUTRAL/BEAR losses absorbed by live mood gate (ADR+Nifty D/W/M)

PATTERN DISCOVERIES:
  1. RSI Month [52-62] wins most (55-56% WR). Above 62 win rate falls.
  2. Week Return [0-2] best. Above 2% stocks are chased not reverting.
  3. Sector Week [1-3] sweet spot. Above 3% sector overheated = fail.
  4. Nifty monthly return r=+0.841 with WR. Strongest predictor found.
  5. Smart money regime: when avg RSI-Weekly >65 across universe = bad month.

BASE FILTERS (fixed across all regimes):
  gvm_score    : [6.5, 10.0]
  dma_200      : [1.5, 20.0]
  dma_50       : [1.5,  8.0]
  month_return : [-2.0, 7.2]
  rsi_weekly   : [50.0, 62.0]
  mom_2d       : [0.0,  2.4]
  sector_month : [0.0,  6.0]

DYNAMIC FILTERS (3 thresholds vary by regime):
  week_return  : max 3.0 / 2.0 / 1.0  (BULL/NEUTRAL/BEAR)
  rsi_month    : max 67.0 / 62.0 / 58.0
  sector_week  : max 4.0 / 3.0 / 2.0

ENTRY RULE: EOD close in pivot zone
  Pivot: rolling 5-day PP=(H+L+C)/3, R1=2*PP-L, S1=2*PP-H
  Entry: PP < close <= R1 AND (R1-close) >= 0.5*(R1-PP)

EXIT RULE:
  WIN:  next-day close >= R1
  LOSS: next-day close <= S1
  MAX HOLD: 5 days, then exit at close (W if above entry, L if below)

USAGE:
  python buy_reversal_simulator.py
  Requires: raw_prices + gvm_scores + futures_universe in Railway DB
  Data: Pull 3yr history (from 2023-06-01) for 200-bar warmup

NEXT OPTIMISATIONS:
  - dma_50 >= 1.5 hard gate (pending 1-month live observation)
  - BULL-only mode (skip NEUTRAL/BEAR entirely)
  - Extend to Sell Reversal, Buy Momentum, Sell Momentum
  - Add live intraday 5-min backtest engine (30-day data from 15-Jun-2026)
"""

import os
import json
import logging
import numpy as np
import psycopg
from collections import defaultdict
from datetime import datetime, date

log = logging.getLogger("scorr.buy_reversal_sim")

# ── Config ────────────────────────────────────────────────────────────────────

BACKTEST_START = "2025-06-02"
BACKTEST_END   = "2026-06-12"
DATA_START     = "2023-06-01"   # 3yr history for 200-bar DMA warmup
MIN_HISTORY    = 200
MAX_HOLD_DAYS  = 5
SCORE_THRESHOLD = 8             # min filters passing out of 10
GVM_MIN         = 6.5

# Nifty 1-month return thresholds
BULL_THRESHOLD   = 2.0    # Nifty 1M > 2% = BULL
BEAR_THRESHOLD   = 0.0    # Nifty 1M < 0% = BEAR

# Fixed base filters
BASE_FILTERS = {
    "gvm_score":    (6.5,  10.0),
    "dma_200":      (1.5,  20.0),
    "dma_50":       (1.5,   8.0),
    "month_return": (-2.0,  7.2),
    "rsi_weekly":   (50.0, 62.0),
    "mom_2d":       (0.0,   2.4),
    "sector_month": (0.0,   6.0),
}

# Dynamic filter thresholds per regime
DYNAMIC_THRESHOLDS = {
    "BULL":    {"week_return": 3.0, "rsi_month": 67.0, "sector_week": 4.0},
    "NEUTRAL": {"week_return": 2.0, "rsi_month": 62.0, "sector_week": 3.0},
    "BEAR":    {"week_return": 1.0, "rsi_month": 58.0, "sector_week": 2.0},
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_regime(nifty_1m_return: float) -> str:
    if nifty_1m_return > BULL_THRESHOLD:   return "BULL"
    elif nifty_1m_return >= BEAR_THRESHOLD: return "NEUTRAL"
    else:                                   return "BEAR"

def get_filters(regime: str) -> dict:
    dyn = DYNAMIC_THRESHOLDS[regime]
    filters = dict(BASE_FILTERS)
    filters["week_return"] = (0.0,  dyn["week_return"])
    filters["rsi_month"]   = (52.0, dyn["rsi_month"])
    filters["sector_week"] = (1.0,  dyn["sector_week"])
    return filters

def passes(value, mn, mx) -> bool:
    if value is None: return False
    v = float(value)
    if mn is not None and v < mn: return False
    if mx is not None and v > mx: return False
    return True

def wilder_rsi(closes: list, period: int):
    if len(closes) < period + 1: return None
    c = np.array(closes, dtype=float)
    delta = np.diff(c)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = gain[:period].mean()
    al_ = loss[:period].mean()
    for i in range(period, len(delta)):
        ag  = (ag  * (period - 1) + gain[i]) / period
        al_ = (al_ * (period - 1) + loss[i]) / period
    return 100.0 if al_ == 0 else 100 - 100 / (1 + ag / al_)

def compute_pivot_5d(price_rows: list, idx: int):
    """Rolling 5-day pivot: PP=(H+L+C)/3, R1=2*PP-L, S1=2*PP-H"""
    if idx < 5: return None
    window = price_rows[idx-5:idx]
    h = max(float(r['high'])  for r in window)
    l = min(float(r['low'])   for r in window)
    c = float(window[-1]['close'])
    pp = (h + l + c) / 3
    return {"pp": pp, "r1": 2*pp - l, "s1": 2*pp - h}

def pivot_room_ok(close: float, pp: float, r1: float) -> bool:
    band = r1 - pp
    return band > 0 and pp < close <= r1 and (r1 - close) >= 0.5 * band


# ── Data loading ──────────────────────────────────────────────────────────────

def load_price_data(conn) -> dict:
    """Load 3yr EOD OHLC for GVM>=6.5 futures universe symbols."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT r.symbol, r.price_date::text AS dt,
                   r.open, r.high, r.low, r.close, r.volume,
                   g.gvm_score
            FROM raw_prices r
            JOIN futures_universe fu ON fu.symbol=r.symbol AND fu.is_active=TRUE
            JOIN gvm_scores g ON g.symbol=r.symbol
                AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                AND g.gvm_score >= %s
            WHERE r.price_date >= %s
            ORDER BY r.symbol, r.price_date
        """, (GVM_MIN, DATA_START))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

    by_sym = defaultdict(list)
    gvm_map = {}
    for row in rows:
        r = dict(zip(cols, row))
        by_sym[r['symbol']].append(r)
        gvm_map[r['symbol']] = float(r['gvm_score'])
    return dict(by_sym), gvm_map

def compute_nifty_monthly_returns(conn) -> dict:
    """Compute Nifty 1-month return for each calendar month."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH ranked AS (
                SELECT price_date, close,
                       TO_CHAR(price_date,'YYYY-MM') AS month,
                       ROW_NUMBER() OVER (PARTITION BY TO_CHAR(price_date,'YYYY-MM') ORDER BY price_date ASC)  AS rn_asc,
                       ROW_NUMBER() OVER (PARTITION BY TO_CHAR(price_date,'YYYY-MM') ORDER BY price_date DESC) AS rn_desc
                FROM raw_prices WHERE symbol='NIFTY50' AND price_date >= %s
            )
            SELECT e.month,
                   ROUND(((e.close/s.close-1)*100)::numeric,2) AS month_ret
            FROM (SELECT month, close FROM ranked WHERE rn_desc=1) e
            JOIN (SELECT month, close FROM ranked WHERE rn_asc=1) s ON s.month=e.month
            ORDER BY e.month
        """, (DATA_START,))
        return {r[0]: float(r[1]) for r in cur.fetchall()}


# ── Metric computation ────────────────────────────────────────────────────────

def compute_all_metrics(by_sym: dict, gvm_map: dict, bt_dates: list) -> dict:
    """Compute all filter metrics for each (symbol, date) in backtest window."""
    all_metrics = {}
    sym_idx = {sym: {r['dt']: i for i, r in enumerate(sd)} for sym, sd in by_sym.items()}

    for sym, sdata in by_sym.items():
        closes_all = [float(r['close']) for r in sdata]
        dt2i = sym_idx[sym]
        for dt in bt_dates:
            idx = dt2i.get(dt)
            if idx is None or idx < MIN_HISTORY: continue
            hist = closes_all[:idx]
            live = closes_all[idx]

            dma50  = (live/np.mean(hist[-50:]) -1)*100 if len(hist)>=50  else None
            dma200 = (live/np.mean(hist[-200:])-1)*100 if len(hist)>=200 else None
            wk_ret = (live/hist[-6] -1)*100            if len(hist)>=6   else None
            mo_ret = (live/hist[-22]-1)*100            if len(hist)>=22  else None
            mom2d  = (live/hist[-2] -1)*100            if len(hist)>=2   else None

            rsi_m = None
            if len(hist) >= 22*7:
                mc = [hist[i] for i in range(-22*7, 0, 22)] + [live]
                rsi_m = wilder_rsi(mc, 6)

            rsi_w = None
            if len(hist) >= 5*9:
                wc = [hist[i] for i in range(-5*9, 0, 5)] + [live]
                rsi_w = wilder_rsi(wc, 8)

            all_metrics[(sym, dt)] = {
                "gvm_score":    gvm_map.get(sym),
                "dma_200":      dma200,
                "dma_50":       dma50,
                "month_return": mo_ret,
                "week_return":  wk_ret,
                "rsi_month":    rsi_m,
                "rsi_weekly":   rsi_w,
                "mom_2d":       mom2d,
                "close":        live,
            }
    return all_metrics, sym_idx


# ── Core backtest ─────────────────────────────────────────────────────────────

def run_backtest(by_sym: dict, all_metrics: dict, sym_idx: dict,
                 bt_dates: list, nifty_monthly: dict) -> list:
    """
    Run buy_reversal EOD backtest with dynamic Nifty-linked filters.
    Returns list of trade dicts.
    """
    trades = []

    for dt in bt_dates:
        mo = dt[:7]
        nifty_1m = nifty_monthly.get(mo, 0.0)
        regime   = get_regime(nifty_1m)
        filters  = get_filters(regime)

        # Compute universe-wide sector averages for this day
        wk_vals = [all_metrics[(s,dt)]["week_return"]  for s in by_sym if (s,dt) in all_metrics and all_metrics[(s,dt)]["week_return"]  is not None]
        mo_vals = [all_metrics[(s,dt)]["month_return"] for s in by_sym if (s,dt) in all_metrics and all_metrics[(s,dt)]["month_return"] is not None]
        sec_wk = float(np.mean(wk_vals)) if wk_vals else 0.0
        sec_mo = float(np.mean(mo_vals)) if mo_vals else 0.0

        traded_today = set()

        for sym in sorted(by_sym.keys()):
            m = all_metrics.get((sym, dt))
            if m is None: continue
            m["sector_week"]  = sec_wk
            m["sector_month"] = sec_mo

            # Score all 10 filters
            score = sum(1 for f, (mn, mx) in filters.items() if passes(m.get(f), mn, mx))
            if score < SCORE_THRESHOLD: continue

            # Pivot-room gate
            idx = sym_idx[sym].get(dt)
            if idx is None: continue
            pv = compute_pivot_5d(by_sym[sym], idx)
            if pv is None: continue

            close = m["close"]
            if not pivot_room_ok(close, pv["pp"], pv["r1"]): continue

            if sym in traded_today: continue
            traded_today.add(sym)

            # Find exit (max 5-day hold)
            sdata  = by_sym[sym]
            r1, s1 = pv["r1"], pv["s1"]
            result = None; exit_pnl = 0.0

            for fwd in range(1, MAX_HOLD_DAYS + 1):
                fi = idx + fwd
                if fi >= len(sdata): break
                fc = float(sdata[fi]['close'])
                if fc >= r1:   result = "WIN";  exit_pnl = (r1 - close) / close * 100; break
                elif fc <= s1: result = "LOSS"; exit_pnl = (s1 - close) / close * 100; break

            if result is None:
                fi = min(idx + MAX_HOLD_DAYS, len(sdata) - 1)
                fc = float(sdata[fi]['close'])
                result   = "WIN" if fc > close else "LOSS"
                exit_pnl = (fc - close) / close * 100

            trades.append({
                "symbol":  sym,
                "date":    dt,
                "month":   mo,
                "regime":  regime,
                "result":  result,
                "pnl":     exit_pnl,
                "entry":   close,
                "r1":      r1,
                "s1":      s1,
                "score":   score,
                "nifty1m": nifty_1m,
            })

    return trades


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(trades: list, bt_dates: list):
    wins   = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    total  = len(trades)
    wr     = len(wins) / total * 100 if total else 0
    aw     = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
    al     = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    exp    = (wr/100*aw) + ((1-wr/100)*al)
    n_days = len(bt_dates)

    print("=" * 62)
    print("BUY REVERSAL — EOD BACKTEST RESULTS")
    print("=" * 62)
    print("Signals        : %d (%.1f/day)" % (total, total/n_days))
    print("W / L          : %d / %d" % (len(wins), len(losses)))
    print("Win Rate       : %.1f%%" % wr)
    print("Avg Win        : +%.2f%%" % aw)
    print("Avg Loss       : %.2f%%" % al)
    print("Expectancy     : %+.2f%%" % exp)
    print("Total P&L      : %+.2f%%" % sum(t["pnl"] for t in trades))

    print("\n%-10s %-8s %4s %3s %3s %6s %8s" % ("Month","Regime","Sig","W","L","WR%","P&L"))
    print("-" * 52)
    by_mo = defaultdict(list)
    for t in trades: by_mo[t["month"]].append(t)
    for mo in sorted(by_mo):
        mt  = by_mo[mo]
        tot = len(mt)
        w   = sum(1 for t in mt if t["result"] == "WIN")
        wr_m = w / tot * 100 if tot else 0
        regime = mt[0]["regime"]
        flag = " *" if wr_m >= 65 else (" X" if wr_m < 40 else "  ")
        print("%-10s %-8s %4d %3d %3d %5.1f%% %+8.2f%%%s" % (
            mo, regime, tot, w, tot-w, wr_m,
            sum(t["pnl"] for t in mt), flag))

    print("\n-- Per Regime --")
    for regime in ["BULL", "NEUTRAL", "BEAR"]:
        rt = [t for t in trades if t["regime"] == regime]
        if not rt: continue
        rw  = [t for t in rt if t["result"] == "WIN"]
        rl  = [t for t in rt if t["result"] == "LOSS"]
        rwr = len(rw) / len(rt) * 100
        raw_ = sum(t["pnl"] for t in rw) / len(rw) if rw else 0
        ral  = sum(t["pnl"] for t in rl) / len(rl) if rl else 0
        rexp = (rwr/100*raw_) + ((1-rwr/100)*ral)
        print("  %-8s: %4d trades  WR=%.1f%%  Exp=%+.2f%%  P&L=%+.2f%%" % (
            regime, len(rt), rwr, rexp, sum(t["pnl"] for t in rt)))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL not set")

    with psycopg.connect(db_url) as conn:
        print("Loading price data...")
        by_sym, gvm_map = load_price_data(conn)
        print(f"  {len(by_sym)} symbols loaded")

        print("Loading Nifty monthly returns...")
        nifty_monthly = compute_nifty_monthly_returns(conn)
        print(f"  {len(nifty_monthly)} months loaded")

    all_dates = sorted(set(r['dt'] for sym_rows in by_sym.values() for r in sym_rows))
    bt_dates  = [d for d in all_dates if BACKTEST_START <= d <= BACKTEST_END]
    print(f"Backtest: {len(bt_dates)} trading days ({bt_dates[0]} to {bt_dates[-1]})")

    print("Computing metrics...")
    all_metrics, sym_idx = compute_all_metrics(by_sym, gvm_map, bt_dates)
    print(f"  {len(all_metrics)} symbol-day metrics computed")

    print("Running backtest...")
    trades = run_backtest(by_sym, all_metrics, sym_idx, bt_dates, nifty_monthly)

    print_report(trades, bt_dates)
    return trades


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
