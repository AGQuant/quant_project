"""
Momentum Daily Engine - Scorr
==============================
Recomputes the 8 momentum (M) parameters DAILY from raw_prices (fresh OHLC),
writes a dated row per stock into momentum_scores. This is the price-driven
half of GVM that moves every day; G+V ride the weekly screener upload.

Formula (v3 - 8 params):
  ret_1y       -> 2-FACTOR (absolute + peer_relative)/2   peer=gvm_segment
  ret_3y       -> 2-FACTOR (absolute + peer_relative)/2   peer=gvm_segment
  ret_1m       -> 2-FACTOR (absolute + peer_relative)/2   peer=gvm_segment
  dma_50       -> ABSOLUTE (DMA dev thresholds)
  dma_200      -> ABSOLUTE (DMA dev thresholds)
  ret_52w_vs_index -> ABSOLUTE (return thresholds)
  rsi_month    -> ABSOLUTE (6-period RSI on monthly candles)
  vol_trend    -> ABSOLUTE (20d avg vol / 60d avg vol ratio)    [NEW v3]
  m_score = mean of the 8 ratings; missing params fall back to 5.0 (neutral).
  m_missing_count records how many of the 8 fell to fallback.

Thresholds:
  RETURN absolute (1Y/3Y/52w):  <0 ->2.5 | 0-5 ->5 | 5-15 ->7.5 | >=15 ->10
  RETURN absolute (1M):         <0 ->2.5 | 0-3 ->5 | 3-8 ->7.5 | >=8 ->10
  DMA absolute:                 <0 ->2.5 | 0-3 ->5 | 3-10 ->7.5 | >=10 ->10
  RSI_month absolute:           <50->2.5 | 50-60->5 | 60-70->7.5 | >=70->10
  Vol trend (20d/60d ratio):    <0.8->2.5 | 0.8-1.0->5 | 1.0-1.2->7.5 | >=1.2->10
  Relative (ratio vs peer trimmed-mean): >125 ->10 | >100 ->7.5 | >75 ->5 | else 2.5

Lookbacks (calendar days, nearest prior trading bar):
  1M = 30d, 1Y = 365d, 3Y = 1095d. Uses adjusted_close.
  ret_52w_vs_index = stock_1y_return - NIFTY50_1y_return.
  rsi_month: monthly close series (last close per calendar month), Wilder 6-period.
  vol_trend: mean(volume last 20 bars) / mean(volume last 60 bars). Requires >= 60 bars.
"""

import os
import logging
from datetime import date
from typing import Optional, Dict

import psycopg
import pandas as pd
import numpy as np

log = logging.getLogger("scorr.momentum_daily")

DATABASE_URL = os.getenv("DATABASE_URL")
INDEX_SYMBOL = "NIFTY50"
RSI_MONTHLY_PERIOD = 6  # 6-period RSI on monthly candles


def _conn():
    return psycopg.connect(DATABASE_URL)


# ---------- scoring primitives ----------
def score_return_absolute(v):
    """Absolute return bands for 1Y / 3Y / 52w-vs-index."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if v < 0: return 2.5
    if v < 5: return 5.0
    if v < 15: return 7.5
    return 10.0


def score_ret1m_absolute(v):
    """Absolute return bands for 1M (smaller horizon, own cutoffs)."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if v < 0: return 2.5
    if v < 3: return 5.0
    if v < 8: return 7.5
    return 10.0


def score_dma_absolute(dev):
    if dev is None or (isinstance(dev, float) and np.isnan(dev)):
        return None
    if dev < 0: return 2.5
    if dev < 3: return 5.0
    if dev < 10: return 7.5
    return 10.0


def score_rsi_month_absolute(rsi):
    """6-period monthly RSI bands."""
    if rsi is None or (isinstance(rsi, float) and np.isnan(rsi)):
        return None
    if rsi < 50: return 2.5
    if rsi < 60: return 5.0
    if rsi < 70: return 7.5
    return 10.0


def score_vol_trend(ratio):
    """Volume trend: 20d avg / 60d avg. Confirms or denies price momentum.
    <0.8 = volume drying up (weak). >=1.2 = strong surge (confirming).
    """
    if ratio is None or (isinstance(ratio, float) and np.isnan(ratio)):
        return None
    if ratio < 0.8:  return 2.5
    if ratio < 1.0:  return 5.0
    if ratio < 1.2:  return 7.5
    return 10.0


def score_relative(stock_val, peer_avg):
    """Peer-relative half, mirrors gvm_engine.score_relative."""
    if stock_val is None or (isinstance(stock_val, float) and np.isnan(stock_val)):
        return None
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


def score_two_factor(abs_rating, rel_rating):
    """Average the absolute and relative halves. Each may be None (missing)."""
    parts = [x for x in (abs_rating, rel_rating) if x is not None]
    if not parts:
        return None
    return round(sum(parts) / len(parts), 2)


# ---------- price-series math ----------
def _load_prices() -> pd.DataFrame:
    with _conn() as conn:
        df = pd.read_sql_query(
            "SELECT symbol, price_date, close, adjusted_close, volume FROM raw_prices ORDER BY symbol, price_date",
            conn,
        )
    df["price_date"] = pd.to_datetime(df["price_date"])
    df["px"] = df["adjusted_close"].where(df["adjusted_close"].notna(), df["close"])
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
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


def _monthly_rsi(g: pd.DataFrame, target_ts, period: int = RSI_MONTHLY_PERIOD) -> Optional[float]:
    """6-period Wilder RSI on monthly closes (last bar of each calendar month)."""
    sub = g[g["price_date"] <= target_ts]
    if sub.empty:
        return None
    monthly = (
        sub.set_index("price_date")["px"]
        .resample("ME").last()
        .dropna()
    )
    if len(monthly) < period + 1:
        return None
    delta = monthly.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def _vol_trend(g: pd.DataFrame, target_ts) -> Optional[float]:
    """Volume trend: mean(last 20 bars) / mean(last 60 bars).
    Requires >= 60 bars with valid volume. Returns the ratio."""
    sub = g[g["price_date"] <= target_ts]["volume"].dropna()
    if len(sub) < 60:
        return None
    avg20 = float(sub.iloc[-20:].mean())
    avg60 = float(sub.iloc[-60:].mean())
    if avg60 == 0:
        return None
    return round(avg20 / avg60, 4)


def _compute_raw_params(prices: pd.DataFrame, target_date: date) -> pd.DataFrame:
    target_ts = pd.Timestamp(target_date)
    d1m = target_ts - pd.Timedelta(days=30)
    d1y = target_ts - pd.Timedelta(days=365)
    d3y = target_ts - pd.Timedelta(days=1095)

    idx_g = prices[prices["symbol"] == INDEX_SYMBOL]
    idx_now = _price_on_or_before(idx_g, target_ts)
    idx_1y  = _price_on_or_before(idx_g, d1y)
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

        sma50  = float(np.mean(closes[-50:]))  if len(closes) >= 50  else None
        sma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else None
        dma50  = ((px_now / sma50  - 1) * 100) if sma50  else None
        dma200 = ((px_now / sma200 - 1) * 100) if sma200 else None

        p1m = _price_on_or_before(g, d1m)
        p1y = _price_on_or_before(g, d1y)
        p3y = _price_on_or_before(g, d3y)
        ret1m  = ((px_now / p1m - 1) * 100) if p1m else None
        ret1y  = ((px_now / p1y - 1) * 100) if p1y else None
        ret3y  = ((px_now / p3y - 1) * 100) if p3y else None
        ret52w_idx = (ret1y - idx_1y_ret) if (ret1y is not None and idx_1y_ret is not None) else None
        rsi_m  = _monthly_rsi(g, target_ts)
        vol_tr = _vol_trend(g, target_ts)

        rows.append({
            "symbol": sym, "latest_price": round(px_now, 2),
            "ret_1m":  round(ret1m,  2) if ret1m  is not None else None,
            "ret_1y":  round(ret1y,  2) if ret1y  is not None else None,
            "ret_3y":  round(ret3y,  2) if ret3y  is not None else None,
            "dma_50":  round(dma50,  2) if dma50  is not None else None,
            "dma_200": round(dma200, 2) if dma200 is not None else None,
            "ret_52w_vs_index": round(ret52w_idx, 2) if ret52w_idx is not None else None,
            "rsi_month":  round(rsi_m,  2) if rsi_m  is not None else None,
            "vol_trend":  round(vol_tr,  4) if vol_tr  is not None else None,
        })
    return pd.DataFrame(rows)


def _peer_trimmed_mean(df: pd.DataFrame, col: str, seg_map: Dict[str, str]) -> Dict[str, float]:
    """Trimmed-mean (10-90 pctile, min 3) of `col` per gvm_segment."""
    df = df.copy()
    df["seg"] = df["symbol"].map(lambda s: seg_map.get(s, "Unknown"))
    out = {}
    for seg, grp in df.groupby("seg"):
        vals = pd.to_numeric(grp[col], errors="coerce").dropna()
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

    peer1m = _peer_trimmed_mean(raw, "ret_1m", seg_map)
    peer1y = _peer_trimmed_mean(raw, "ret_1y", seg_map)
    peer3y = _peer_trimmed_mean(raw, "ret_3y", seg_map)

    FB = 5.0  # neutral fallback
    out_rows = []
    for _, r in raw.iterrows():
        seg = seg_map.get(r["symbol"], "Unknown")

        # 2-factor params (absolute + relative)
        r1m = score_two_factor(score_ret1m_absolute(r["ret_1m"]),
                               score_relative(r["ret_1m"], peer1m.get(seg)))
        r1y = score_two_factor(score_return_absolute(r["ret_1y"]),
                               score_relative(r["ret_1y"], peer1y.get(seg)))
        r3y = score_two_factor(score_return_absolute(r["ret_3y"]),
                               score_relative(r["ret_3y"], peer3y.get(seg)))
        # absolute-only params
        d50  = score_dma_absolute(r["dma_50"])
        d200 = score_dma_absolute(r["dma_200"])
        r52  = score_return_absolute(r["ret_52w_vs_index"])
        rsi  = score_rsi_month_absolute(r["rsi_month"])
        vt   = score_vol_trend(r.get("vol_trend"))

        ratings = [r1m, r1y, r3y, d50, d200, r52, rsi, vt]
        missing = sum(1 for x in ratings if x is None)
        filled  = [x if x is not None else FB for x in ratings]
        m_score = round(sum(filled) / 8, 2)

        out_rows.append((
            r["symbol"], target_date, r["latest_price"],
            r["ret_1m"], r["ret_1y"], r["ret_3y"], r["dma_50"], r["dma_200"],
            r["ret_52w_vs_index"], r["rsi_month"], r.get("vol_trend"),
            filled[0], filled[1], filled[2], filled[3], filled[4],
            filled[5], filled[6], filled[7],
            m_score, missing,
        ))

    # Ensure new columns exist (safe migration)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE momentum_scores
            ADD COLUMN IF NOT EXISTS vol_trend NUMERIC,
            ADD COLUMN IF NOT EXISTS vol_trend_rating NUMERIC
        """)
        conn.commit()

    with _conn() as conn, conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO momentum_scores
              (symbol, score_date, latest_price,
               ret_1m, ret_1y, ret_3y, dma_50, dma_200,
               ret_52w_vs_index, rsi_month, vol_trend,
               ret_1m_rating, ret_1y_rating, ret_3y_rating, dma_50_rating, dma_200_rating,
               ret_52w_idx_rating, rsi_month_rating, vol_trend_rating,
               m_score, m_missing_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
              latest_price=EXCLUDED.latest_price,
              ret_1m=EXCLUDED.ret_1m, ret_1y=EXCLUDED.ret_1y, ret_3y=EXCLUDED.ret_3y,
              dma_50=EXCLUDED.dma_50, dma_200=EXCLUDED.dma_200,
              ret_52w_vs_index=EXCLUDED.ret_52w_vs_index, rsi_month=EXCLUDED.rsi_month,
              vol_trend=EXCLUDED.vol_trend,
              ret_1m_rating=EXCLUDED.ret_1m_rating, ret_1y_rating=EXCLUDED.ret_1y_rating,
              ret_3y_rating=EXCLUDED.ret_3y_rating, dma_50_rating=EXCLUDED.dma_50_rating,
              dma_200_rating=EXCLUDED.dma_200_rating,
              ret_52w_idx_rating=EXCLUDED.ret_52w_idx_rating,
              rsi_month_rating=EXCLUDED.rsi_month_rating,
              vol_trend_rating=EXCLUDED.vol_trend_rating,
              m_score=EXCLUDED.m_score, m_missing_count=EXCLUDED.m_missing_count
        """, out_rows)
        conn.commit()

    return {"status": "ok", "score_date": str(target_date), "scored": len(out_rows), "params": 8}
