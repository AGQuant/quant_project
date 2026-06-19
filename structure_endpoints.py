"""
Trade Structure Analyzer — Scorr (19-Jun-2026)
Spec: session_log id=407  TRADE_STRUCTURE_ANALYZER_SPEC_V1 (LOCKED)

GET /api/structure/{symbol}?direction=LONG|SHORT

Server-side computation only (pure Python + Railway DB, no Claude API):
  Gate 1 (5-min)  — EMA20, trend (HH/HL vs LH/LL), bar pattern, score 0-3, pass if >=2
  Room   (1D)     — key support/resistance levels from EOD + pivots + 52w + swings
                    distance, MAJOR/MINOR strength, verdict (GOOD_ROOM/TIGHT/AT_LEVEL)
  Action          — HOLD | COVER | EXIT | ADD
"""

import os
from typing import Optional, List, Dict, Any

import numpy as np
import pandas as pd
import psycopg
from scipy.signal import argrelextrema
from fastapi import APIRouter, HTTPException

structure_router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _f(v):
    """numeric/Decimal -> float (None-safe)."""
    return None if v is None else float(v)


# ───────────────────────────── data fetch ─────────────────────────────
def _fetch(symbol: str):
    sym = symbol.strip().upper()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT price_date, open, high, low, close
            FROM raw_prices
            WHERE symbol = %s AND price_date >= CURRENT_DATE - 365
            ORDER BY price_date ASC
        """, (sym,))
        eod = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

        cur.execute("""
            SELECT pp, r1, r2, s1, s2
            FROM v8_paper_pivots
            WHERE symbol = %s
            ORDER BY pivot_date DESC
            LIMIT 1
        """, (sym,))
        pr = cur.fetchone()
        pivots = dict(zip([d[0] for d in cur.description], pr)) if pr else {}

        cur.execute("""
            SELECT gvm_score, rsi_month, rsi_weekly, daily_rsi, dma_50, dma_200,
                   week_return, month_return, sector_week, sector_month
            FROM v8_metrics
            WHERE symbol = %s AND score_date = (SELECT MAX(score_date) FROM v8_metrics)
            LIMIT 1
        """, (sym,))
        mr = cur.fetchone()
        metrics = dict(zip([d[0] for d in cur.description], mr)) if mr else {}

        # latest <=100 5-min bars over last ~2 sessions, returned ascending
        cur.execute("""
            SELECT ts, open, high, low, close, volume FROM (
                SELECT ts, open, high, low, close, volume
                FROM intraday_prices
                WHERE symbol = %s
                  AND ts::date >= CURRENT_DATE - 2
                  AND ts::time >= '09:15:00'
                ORDER BY ts DESC
                LIMIT 100
            ) sub
            ORDER BY ts ASC
        """, (sym,))
        intra = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    return eod, pivots, metrics, intra


# ───────────────────────────── Gate 1 (5-min) ─────────────────────────────
def _gate1(direction: str, bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    closes = [_f(b["close"]) for b in bars]
    highs = [_f(b["high"]) for b in bars]
    lows = [_f(b["low"]) for b in bars]
    opens = [_f(b["open"]) for b in bars]

    if len(closes) < 2:
        return {"pass": False, "ema20": None, "score": 0,
                "trend": "UNKNOWN", "bar_pattern": "--", "price_vs_ma": "UNKNOWN",
                "note": "insufficient 5-min data"}

    # EMA20 of last 20 closes
    last20 = closes[-20:]
    ema20 = float(pd.Series(last20).ewm(span=20, adjust=False).mean().iloc[-1])
    price = closes[-1]
    price_vs_ma = "ABOVE" if price > ema20 else "BELOW"

    # Trend from last 5 bars: HH+HL vs LH+LL
    n = min(5, len(closes))
    hh_hl = lh_ll = 0
    for i in range(len(closes) - n + 1, len(closes)):
        if highs[i] > highs[i - 1] and lows[i] > lows[i - 1]:
            hh_hl += 1
        elif highs[i] < highs[i - 1] and lows[i] < lows[i - 1]:
            lh_ll += 1
    if hh_hl >= 3:
        trend = "BULLISH"
    elif lh_ll >= 3:
        trend = "BEARISH"
    else:
        trend = "FLAT"

    # Bar pattern: last 3 bars close vs open
    last3 = [("UP" if closes[i] >= opens[i] else "DOWN") for i in range(max(0, len(closes) - 3), len(closes))]
    bar_pattern = "-".join(last3)
    ups = last3.count("UP")
    downs = last3.count("DOWN")

    score = 0
    if direction == "LONG":
        if trend == "BULLISH":
            score += 1
        if price_vs_ma == "ABOVE":
            score += 1
        if ups >= 2:
            score += 1
    else:  # SHORT
        if trend == "BEARISH":
            score += 1
        if price_vs_ma == "BELOW":
            score += 1
        if downs >= 2:
            score += 1

    return {"pass": score >= 2, "ema20": round(ema20, 2), "score": score,
            "trend": trend, "bar_pattern": bar_pattern, "price_vs_ma": price_vs_ma}


# ───────────────────────────── Room (1D) ─────────────────────────────
def _clusters(prices: List[float], tol: float = 0.01, pick: str = "min"):
    """Merge prices within tol (1%) into clusters. Returns [(ref_price, count)]."""
    out = []
    for p in sorted(prices):
        if out and out[-1]["ref"] and abs(p - out[-1]["ref"]) / out[-1]["ref"] <= tol:
            out[-1]["items"].append(p)
            out[-1]["ref"] = min(out[-1]["items"]) if pick == "min" else max(out[-1]["items"])
        else:
            out.append({"ref": p, "items": [p]})
    return [(c["ref"], len(c["items"])) for c in out]


def _strength_near(price: float, swing_prices: List[float], tol: float = 0.01) -> str:
    """MAJOR if 2+ swing points sit within 1% of this level, else MINOR."""
    if not price:
        return "MINOR"
    near = sum(1 for s in swing_prices if abs(s - price) / price <= tol)
    return "MAJOR" if near >= 2 else "MINOR"


def _level(label, price, ltype, current, direction, strength):
    price = _f(price)
    if direction == "SHORT":      # downside room: positive when level is below current
        dist_pts = current - price
    else:                          # upside room: positive when level is above current
        dist_pts = price - current
    dist_pct = (dist_pts / current * 100) if current else 0.0
    return {"label": label, "price": round(price, 2),
            "distance_pts": round(dist_pts, 2), "distance_pct": round(dist_pct, 2),
            "strength": strength, "type": ltype}


def _room(direction: str, current: float, eod: List[Dict[str, Any]],
          pivots: Dict[str, Any]) -> Dict[str, Any]:
    if not eod or not current:
        return {"levels": [], "verdict": "NO_DATA", "analysis": "no EOD data",
                "total_room_pct": None, "total_room_pts": None}

    highs = np.array([_f(r["high"]) for r in eod])
    lows = np.array([_f(r["low"]) for r in eod])

    pinned: List[Dict[str, Any]] = []   # S1/S2 or R1/R2 — always shown first
    rest: List[Dict[str, Any]] = []

    if direction == "SHORT":
        ltype = "SUPPORT"
        # swing lows (local minima, 3 bars each side)
        idx = argrelextrema(lows, np.less, order=3)[0]
        swing_pts = [float(lows[i]) for i in idx]
        swing_clusters = _clusters(swing_pts, pick="min")
        # pinned pivots S1, S2 (always)
        for lbl, key in [("Pivot S1", "s1"), ("Pivot S2", "s2")]:
            if pivots.get(key) is not None:
                p = _f(pivots[key])
                pinned.append(_level(lbl, p, ltype, current, direction,
                                     _strength_near(p, swing_pts)))
        # swing lows below current
        for ref, cnt in swing_clusters:
            if ref < current * 0.999:
                rest.append(_level("Swing Low", ref, ltype, current, direction,
                                   "MAJOR" if cnt >= 2 else _strength_near(ref, swing_pts)))
        # 52-week low
        low52 = float(lows.min())
        rest.append(_level("52w Low", low52, ltype, current, direction,
                           _strength_near(low52, swing_pts)))
    else:
        ltype = "RESISTANCE"
        idx = argrelextrema(highs, np.greater, order=3)[0]
        swing_pts = [float(highs[i]) for i in idx]
        swing_clusters = _clusters(swing_pts, pick="max")
        for lbl, key in [("Pivot R1", "r1"), ("Pivot R2", "r2")]:
            if pivots.get(key) is not None:
                p = _f(pivots[key])
                pinned.append(_level(lbl, p, ltype, current, direction,
                                     _strength_near(p, swing_pts)))
        for ref, cnt in swing_clusters:
            if ref > current * 1.001:
                rest.append(_level("Swing High", ref, ltype, current, direction,
                                   "MAJOR" if cnt >= 2 else _strength_near(ref, swing_pts)))
        high52 = float(highs.max())
        rest.append(_level("52w High", high52, ltype, current, direction,
                           _strength_near(high52, swing_pts)))

    # dedup near-identical levels in `rest` (within 0.2%), keep nearest
    rest.sort(key=lambda l: (l["distance_pct"] < 0, abs(l["distance_pct"])))
    deduped: List[Dict[str, Any]] = []
    for lv in rest:
        if any(abs(lv["price"] - d["price"]) / lv["price"] <= 0.002 for d in deduped):
            continue
        deduped.append(lv)

    levels = pinned + deduped

    # nearest obstacle on the correct side -> total room + verdict
    forward = [l for l in levels if l["distance_pct"] > 0]
    if forward:
        nearest = min(forward, key=lambda l: l["distance_pct"])
        total_pct = nearest["distance_pct"]
        total_pts = nearest["distance_pts"]
    else:
        nearest = min(levels, key=lambda l: abs(l["distance_pct"])) if levels else None
        total_pct = abs(nearest["distance_pct"]) if nearest else None
        total_pts = abs(nearest["distance_pts"]) if nearest else None

    if total_pct is None:
        verdict = "NO_DATA"
    elif total_pct > 3:
        verdict = "GOOD_ROOM"
    elif total_pct >= 1:
        verdict = "TIGHT"
    else:
        verdict = "AT_LEVEL"

    side_word = "fall" if direction == "SHORT" else "rise"
    if nearest:
        analysis = (f"~{abs(total_pct):.1f}% room to {side_word} to nearest "
                    f"{nearest['type'].lower()} ({nearest['label']} @ {nearest['price']:.1f}).")
    else:
        analysis = "no key levels found."

    return {"levels": levels, "verdict": verdict, "analysis": analysis,
            "total_room_pct": None if total_pct is None else round(total_pct, 2),
            "total_room_pts": None if total_pts is None else round(total_pts, 2)}


def _action(direction: str, gate1: Dict[str, Any], room: Dict[str, Any]) -> str:
    verdict = room.get("verdict")
    if verdict == "AT_LEVEL":
        return "COVER" if direction == "SHORT" else "EXIT"
    if gate1.get("pass") and verdict == "GOOD_ROOM":
        return "ADD"
    return "HOLD"


# ───────────────────────────── endpoint ─────────────────────────────
@structure_router.get("/api/structure/{symbol}")
def structure(symbol: str, direction: str = "LONG"):
    direction = (direction or "LONG").strip().upper()
    if direction not in ("LONG", "SHORT"):
        raise HTTPException(status_code=400, detail="direction must be LONG or SHORT")

    eod, pivots, metrics, intra = _fetch(symbol)
    if not eod and not intra:
        raise HTTPException(status_code=404, detail=f"no data for {symbol.upper()}")

    # current price: last intraday close, else last EOD close
    if intra:
        current = _f(intra[-1]["close"])
    else:
        current = _f(eod[-1]["close"])

    gate1 = _gate1(direction, intra)
    room = _room(direction, current, eod, pivots)
    action = _action(direction, gate1, room)

    intraday_bars = [{
        "time": b["ts"].strftime("%Y-%m-%d %H:%M") if hasattr(b["ts"], "strftime") else str(b["ts"]),
        "open": _f(b["open"]), "high": _f(b["high"]),
        "low": _f(b["low"]), "close": _f(b["close"]),
        "volume": int(b["volume"]) if b["volume"] is not None else None,
    } for b in intra]

    return {
        "symbol": symbol.strip().upper(),
        "direction": direction,
        "current_price": None if current is None else round(current, 2),
        "gate1": gate1,
        "room": room,
        "action": action,
        "metrics": {k: _f(v) for k, v in metrics.items()},
        "pivots": {k: _f(v) for k, v in pivots.items()},
        "intraday_bars": intraday_bars,
    }
