"""
Native Trade Check endpoints — v3.3, zero-token, pure Railway DB.
No Claude, no ADMIN_TOKEN, no MCP. Engine: native_trade_check v3 (all-auto).

POST /api/check                      composite — full Tier1+Tier2 card
                                     side=INVEST -> fundamental buy-and-hold card
GET  /api/check/rule/{rule}          single parameter (R1..R12, F1..F7)
GET  /api/check/health
GET  /api/trade-check/fibcheck       on-demand Fibonacci retracement (cc_task #131)
"""

import os
import psycopg
from datetime import datetime, timedelta
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from native_trade_check import compute_trade_check, compute_single_rule
from invest_check import compute_invest_check

router = APIRouter()


class CheckRequest(BaseModel):
    symbol: str
    side: Optional[str] = "LONG"
    gate1: Optional[bool] = None   # optional human override for R10
    gate2: Optional[bool] = None   # optional human override for R12


@router.post("/api/check")
def api_check(req: CheckRequest):
    side = (req.side or "LONG").upper()
    if side == "INVEST":
        return compute_invest_check(req.symbol)
    if side not in ("LONG", "SHORT"):
        side = "LONG"
    return compute_trade_check(req.symbol, side, req.gate1, req.gate2)


@router.get("/api/check/rule/{rule}")
def api_check_rule(rule: str, symbol: str, side: str = "LONG"):
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        side = "LONG"
    return compute_single_rule(symbol, side, rule)


@router.get("/api/check/health")
def api_check_health():
    return {"status": "ok", "engine": "native_v3.3_all_auto_v3", "cost": "$0",
            "auto_params": 18, "gates": "optional overrides for R10/R12",
            "modes": ["LONG", "SHORT", "INVEST"],
            "needs_admin_token": False, "needs_claude": False}


# ── Fibcheck (cc_task #131) — on-demand Fibonacci retracement ────────────────
# Pull-based only: caller passes a symbol (+ optional reference entry_price).
# Swing high = max(high) over the last 120 EOD bars; swing low = min(low) on/after
# the swing-high date (the down-leg). Fib level = low + pct*(high-low). CMP is the
# live intraday close during market hours, else the latest EOD close. Technicals
# (dma_50/200, rsi_m/w, sector_w/m) come from the latest v8_metrics row. Returns a
# plain-language commentary in the same tone as the manual 30-Jun HDFCBANK review.
_FIB_PCTS = [0.0, 23.6, 38.2, 50.0, 61.8, 78.6, 100.0]


def _fib_conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _ist_market_hours():
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    mins = ist.hour * 60 + ist.minute
    return (ist.weekday() < 5) and (555 <= mins <= 930)


# cc#269: horizon selector — calendar-day windows (NOT bar-count LIMITs), consistent with
# the 52w calendar-not-barcount principle for this feature. 3m is the default.
_FIB_HORIZONS = {"1w": 7, "1m": 30, "3m": 90, "5m": 150, "12m": 365}


@router.get("/api/trade-check/fibcheck")
def api_fibcheck(symbol: str, entry_price: Optional[float] = None, lookback: str = "3m"):
    sym = (symbol or "").upper().strip()
    if not sym:
        return {"error": "symbol required"}
    # cc#269: map the horizon to a calendar-day window; anything unrecognised defaults to 3m.
    days = _FIB_HORIZONS.get((lookback or "3m").lower().strip(), _FIB_HORIZONS["3m"])
    try:
        with _fib_conn() as conn, conn.cursor() as cur:
            # EOD bars within the selected calendar-day window, oldest->newest. The date filter
            # bounds the set (no LIMIT); everything downstream is window-size-agnostic.
            cur.execute("""
                SELECT price_date, high, low, close
                FROM raw_prices
                WHERE symbol=%s AND high IS NOT NULL AND low IS NOT NULL
                  AND price_date >= CURRENT_DATE - (%s * INTERVAL '1 day')
                ORDER BY price_date ASC
            """, (sym, days))
            rows = cur.fetchall()
            if len(rows) < 2:
                return {"error": f"Not enough price history for {sym}"}

            series = [{"date": str(r[0]), "close": float(r[3]) if r[3] is not None else None}
                      for r in rows]

            # swing high = max(high); swing low = min(low) on/after the high's date
            hi_i = max(range(len(rows)), key=lambda i: float(rows[i][1]))
            swing_high = float(rows[hi_i][1]); swing_high_date = str(rows[hi_i][0])
            tail = rows[hi_i:]  # high bar onward (the down-leg)
            lo_j = min(range(len(tail)), key=lambda j: float(tail[j][2]))
            swing_low = float(tail[lo_j][2]); swing_low_date = str(tail[lo_j][0])

            span = swing_high - swing_low
            fib_levels = [{"pct": p,
                           "price": round(swing_low + (p / 100.0) * span, 2)}
                          for p in _FIB_PCTS]

            # CMP: live intraday during market hours, else latest EOD close
            cmp_val = None
            if _ist_market_hours():
                cur.execute("""
                    SELECT close FROM intraday_prices
                    WHERE symbol=%s AND ts::date=CURRENT_DATE
                    ORDER BY ts DESC LIMIT 1
                """, (sym,))
                ir = cur.fetchone()
                if ir and ir[0] is not None:
                    cmp_val = float(ir[0])
            if cmp_val is None:
                cmp_val = float(rows[-1][3]) if rows[-1][3] is not None else None

            # latest supporting technicals
            cur.execute("""
                SELECT dma_50, dma_200, rsi_month, rsi_weekly, sector_week, sector_month
                FROM v8_metrics WHERE symbol=%s
                ORDER BY score_date DESC LIMIT 1
            """, (sym,))
            tr = cur.fetchone()
        tech = {}
        if tr:
            for k, v in zip(["dma_50", "dma_200", "rsi_month", "rsi_weekly",
                             "sector_week", "sector_month"], tr):
                tech[k] = float(v) if v is not None else None

        def _retr(px):
            return round((px - swing_low) / span * 100, 1) if (span and px is not None) else None

        cmp_retr = _retr(cmp_val)
        entry_retr = _retr(entry_price) if entry_price is not None else None
        dist_cmp_entry = (round((entry_price - cmp_val) / cmp_val * 100, 2)
                          if (entry_price is not None and cmp_val) else None)

        # nearest fib level above CMP = next resistance
        above = [f for f in fib_levels if cmp_val is not None and f["price"] > cmp_val]
        nearest_above = min(above, key=lambda f: f["price"]) if above else None

        commentary = _fib_commentary(sym, swing_high, swing_high_date, swing_low,
                                     swing_low_date, cmp_val, cmp_retr, nearest_above,
                                     entry_price, dist_cmp_entry, tech)

        return {
            "symbol": sym,
            "lookback": (lookback or "3m").lower().strip() if (lookback or "3m").lower().strip() in _FIB_HORIZONS else "3m",  # cc#269: echo the resolved horizon
            "lookback_days": len(rows),
            "swing_high": {"price": swing_high, "date": swing_high_date},
            "swing_low": {"price": swing_low, "date": swing_low_date},
            "fib_levels": fib_levels,
            "cmp": round(cmp_val, 2) if cmp_val is not None else None,
            "cmp_retracement_pct": cmp_retr,
            "entry_price": entry_price,
            "entry_retracement_pct": entry_retr,
            "pct_distance_cmp_to_entry": dist_cmp_entry,
            "nearest_level_above": nearest_above,
            "technicals": tech,
            "series": series,
            "commentary": commentary,
            "is_live": _ist_market_hours(),
        }
    except Exception as e:
        return {"error": f"fibcheck failed: {e}"}


def _fib_commentary(sym, hi, hi_d, lo, lo_d, cmp_val, cmp_retr, nearest_above,
                    entry_price, dist, tech):
    """3-5 plain sentences, same tone as the manual HDFCBANK fib review."""
    parts = []
    parts.append(f"{sym} swung from a high of {hi:.2f} on {hi_d} down to a low of "
                 f"{lo:.2f} on {lo_d}, and the Fibonacci ladder is drawn across that leg.")
    if cmp_val is not None and cmp_retr is not None:
        parts.append(f"CMP {cmp_val:.2f} sits at the {cmp_retr:.1f}% retracement of that move.")
        if nearest_above:
            parts.append(f"The nearest level overhead is {nearest_above['pct']:.1f}% at "
                         f"{nearest_above['price']:.2f}, which acts as the next resistance.")
        else:
            parts.append("Price is at or above the 100% level — the prior swing high is the "
                         "reference, with no fib resistance left above.")
    if entry_price is not None and cmp_val is not None and dist is not None:
        rs = entry_price - cmp_val
        direction = "above" if rs > 0 else "below"
        parts.append(f"A reference entry at {entry_price:.2f} is {abs(dist):.2f}% "
                     f"({abs(rs):.2f} pts) {direction} CMP.")
    d200 = tech.get("dma_200"); rm = tech.get("rsi_month")
    if d200 is not None or rm is not None:
        trend = ("above its 200-DMA (structurally bullish)" if (d200 is not None and d200 > 0)
                 else "below its 200-DMA (structurally weak)" if d200 is not None else "")
        rsi_state = (f"monthly RSI {rm:.0f}" + (" (overbought)" if rm >= 70 else
                     " (oversold)" if rm <= 30 else " (neutral)")) if rm is not None else ""
        tail = " and ".join([t for t in [trend, rsi_state] if t])
        if tail:
            parts.append(f"Context: the stock is {tail}.")
    return " ".join(parts)
