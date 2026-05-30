"""
Momentum Daily Engine - Scorr
==============================
Recomputes the 5 momentum (M) parameters DAILY from raw_prices (fresh OHLC),
writes a dated row per stock into momentum_scores. This is the price-driven
half of GVM that moves every day; G+V ride the weekly screener upload.

Verified formula (reproduced from the live 21-May momentum_scores baseline):
  dma_50, dma_200        -> ABSOLUTE  (DMA dev thresholds)
  ret_52w_vs_index       -> ABSOLUTE  (return thresholds)
  ret_1y                 -> ABSOLUTE  (return thresholds)
  ret_3y                 -> RELATIVE  ((absolute + peer_relative)/2), peer=gvm_segment
  m_score = mean of the 5 ratings

Thresholds:
  RETURN absolute:  <0 ->2.5 | 0-5 ->5 | 5-15 ->7.5 | >=15 ->10
  DMA absolute:     <0 ->2.5 | 0-3 ->5 | 3-10 ->7.5 | >=10 ->10

Lookbacks (calendar days, nearest prior trading bar):
  1Y = 365d, 3Y = 1095d. Uses adjusted_close.
  ret_52w_vs_index = stock_1y_return - NIFTY50_1y_return.
"""

import os
import logging
from datetime import date
from typing import Optional, Dict, List

import psycopg
import pandas as pd
import numpy as np

log = logging.getLogger("scorr.momentum_daily")

DATABASE_URL = os.getenv("DATABASE_URL")
INDEX_SYMBOL = "NIFTY50"


def _conn():
    return psycopg.connect(DATABASE_URL)


# ---------- scoring primitives (verified against live baseline) ----------
def score_return_absolute(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 5.0
    if v < 0: return 2.5
    if v < 5: return 5.0
    if v < 15: return 7.5
    return 10.0


def score_dma_absolute(dev):
    if dev is None or (isinstance(dev, float) and np.isnan(dev)):
        return 5.0
    if dev < 0: return 2.5
    if dev < 3: return 5.0
    if dev < 10: return 7.5
    return 10.0


def score_relative(stock_val, peer_avg):
    """Peer-relative half, mirrors gvm_engine.score_relative."""
    if peer_avg is None or peer_avg == 0 or (isinstance(peer_avg, float) and np.isnan(peer_avg)):
        return 5.0
    s, p = float(stock_val), float(peer_avg)
    if s > 0 and p < 0: return 10.0
    if s < 0 and p > 0: return 2.5
    if s < 0 and p < 0:
        ratio = (p / s) * 100
    else:
        ratio = (s / p) * 100
    if ratio > 125: return 10.0
    if ratio > 100: return 7.5
    if ratio > 75: return 5.0
    return 2.5


def score_ret3y_relative(stock_val, peer_avg):
    """ret_3y = (absolute + relative) / 2."""
    if stock_val is None or (isinstance(stock_val, float) and np.isnan(stock_val)):
        return 5.0
    return round((score_return_absolute(stock_val) + score_relative(stock_val, peer_avg)) / 2, 2)


# ---------- price-series math ----------
def _load_prices() -> pd.DataFrame:
    with _conn() as conn:
        df = pd.read_sql_query(
            "SELECT symbol, price_date, close, adjusted_close FROM raw_prices ORDER BY symbol, price_date",
            conn,
        )
    df["price_date"] = pd.to_datetime(df["price_date"])
    # prefer adjusted_close; fall back to close
    df["px"] = df["adjusted_close"].where(df["adjusted_close"].notna(), df["close"])
    return df


def _segment_map() -> Dict[str, str]:
    with _conn() as conn:
        seg = pd.read_sql_query("SELECT nse_code, gvm_segment FROM input_raw", conn)
    seg["nse_code"] = seg["nse_code"].astype(str).str.strip()
    seg["gvm_segment"] = seg["gvm_segment"].astype(str).str.strip().replace({"nan": "Unknown", "": "Unknown"})
    return dict(zip(seg["nse_code"], seg["gvm_segment"]))


def _price_on_or_before(g: pd.DataFrame, target_ts) -> Optional[float]:
    sub = g[g["price_date"] <= target_ts]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["px"])


def _compute_raw_params(prices: pd.DataFrame, target_date: date) -> pd.DataFrame:
    target_ts = pd.Timestamp(target_date)
    d1y = target_ts - pd.Timedelta(days=365)
    d3y = target_ts - pd.Timedelta(days=1095)

    # index 1y return
    idx_g = prices[prices["symbol"] == INDEX_SYMBOL]
    idx_now = _price_on_or_before(idx_g, target_ts)
    idx_1y = _price_on_or_before(idx_g, d1y)
    idx_1y_ret = ((idx_now / idx_1y - 1) * 100) if (idx_now and idx_1y) else None

    rows = []
    for sym, g in prices.groupby("symbol"):
        if sym == INDEX_SYMBOL:
            continue
        g = g.reset_index(drop=True)
        cur = g[g["price_date"] <= target_ts]
        if cur.empty:
            continue
        px_now = float(cur.iloc[-1]["px"])
        closes = cur["px"].values

        sma50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else None
        sma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else None
        dma50 = ((px_now / sma50 - 1) * 100) if sma50 else None
        dma200 = ((px_now / sma200 - 1) * 100) if sma200 else None

        p1y = _price_on_or_before(g, d1y)
        p3y = _price_on_or_before(g, d3y)
        ret1y = ((px_now / p1y - 1) * 100) if p1y else None
        ret3y = ((px_now / p3y - 1) * 100) if p3y else None
        ret52w_idx = (ret1y - idx_1y_ret) if (ret1y is not None and idx_1y_ret is not None) else None

        rows.append({
            "symbol": sym, "latest_price": round(px_now, 2),
            "ret_1y": round(ret1y, 2) if ret1y is not None else None,
            "ret_3y": round(ret3y, 2) if ret3y is not None else None,
            "dma_50": round(dma50, 2) if dma50 is not None else None,
            "dma_200": round(dma200, 2) if dma200 is not None else None,
            "ret_52w_vs_index": round(ret52w_idx, 2) if ret52w_idx is not None else None,
        })
    return pd.DataFrame(rows)


def _peer_ret3y(df: pd.DataFrame, seg_map: Dict[str, str]) -> Dict[str, float]:
    df = df.copy()
    df["seg"] = df["symbol"].map(lambda s: seg_map.get(s, "Unknown"))
    out = {}
    for seg, grp in df.groupby("seg"):
        vals = pd.to_numeric(grp["ret_3y"], errors="coerce").dropna()
        if len(vals) >= 3:
            lo, hi = vals.quantile(0.10), vals.quantile(0.90)
            t = vals[(vals >= lo) & (vals <= hi)]
            out[seg] = round(t.mean(), 4) if len(t) else round(vals.mean(), 4)
        elif len(vals):
            out[seg] = round(vals.mean(), 4)
        else:
            out[seg] = None
    return out


def compute_momentum(target_date: Optional[date] = None) -> Dict:
    target_date = target_date or date.today()
    prices = _load_prices()
    if prices.empty:
        return {"status": "warn", "message": "raw_prices empty", "scored": 0}

    seg_map = _segment_map()
    raw = _compute_raw_params(prices, target_date)
    if raw.empty:
        return {"status": "warn", "message": "no params computed", "scored": 0}

    peer3y = _peer_ret3y(raw, seg_map)

    out_rows = []
    for _, r in raw.iterrows():
        seg = seg_map.get(r["symbol"], "Unknown")
        r1y_rt = score_return_absolute(r["ret_1y"])
        r3y_rt = score_ret3y_relative(r["ret_3y"], peer3y.get(seg))
        d50_rt = score_dma_absolute(r["dma_50"])
        d200_rt = score_dma_absolute(r["dma_200"])
        r52_rt = score_return_absolute(r["ret_52w_vs_index"])
        m_score = round((r1y_rt + r3y_rt + d50_rt + d200_rt + r52_rt) / 5, 2)
        out_rows.append((
            r["symbol"], target_date, r["latest_price"],
            r["ret_1y"], r["ret_3y"], r["dma_50"], r["dma_200"], r["ret_52w_vs_index"],
            r1y_rt, r3y_rt, d50_rt, d200_rt, r52_rt, m_score,
        ))

    with _conn() as conn, conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO momentum_scores
              (symbol, score_date, latest_price, ret_1y, ret_3y, dma_50, dma_200, ret_52w_vs_index,
               ret_1y_rating, ret_3y_rating, dma_50_rating, dma_200_rating, ret_52w_idx_rating, m_score)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
              latest_price=EXCLUDED.latest_price, ret_1y=EXCLUDED.ret_1y, ret_3y=EXCLUDED.ret_3y,
              dma_50=EXCLUDED.dma_50, dma_200=EXCLUDED.dma_200, ret_52w_vs_index=EXCLUDED.ret_52w_vs_index,
              ret_1y_rating=EXCLUDED.ret_1y_rating, ret_3y_rating=EXCLUDED.ret_3y_rating,
              dma_50_rating=EXCLUDED.dma_50_rating, dma_200_rating=EXCLUDED.dma_200_rating,
              ret_52w_idx_rating=EXCLUDED.ret_52w_idx_rating, m_score=EXCLUDED.m_score
        """, out_rows)
        conn.commit()

    return {"status": "ok", "score_date": str(target_date), "scored": len(out_rows)}
