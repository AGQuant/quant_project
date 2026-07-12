"""
v14_replay.py — V14 BT7-style 1-year replay harness (cc#442 build_4, spec id=3060).
=================================================================================
Episode-level replay of each V14 setup over the Phase A 5-min warehouse (intraday_prices
source='fyers_hist'), costs baked in, producing a per-tag WR/EV table for the viability gate
(ship only setups clearing 55% WR after costs at 1:1).

*** DO NOT RUN until Phase A (#389) completes AND the founder triggers it. ***
This module is NOT scheduled and NOT wired to any auto path — it is a manual command only.

Replay scope + honest limitations: the Phase A warehouse holds 5-min PRICE bars only, so the
price-derived rules (opening-range, VWAP, VolX, ATR, pivots-from-history, day%, session
high/low, fall/rise, room-to-pivot) replay faithfully. Gates that need a historical snapshot NOT
warehoused per-bar — basis/OI (futures_basis history), the V10 NIFTY regime (G1), and live sector
day% — are RELAXED in replay and flagged in the output, so replayed WR is an upper bound on those
gates. Re-tighten once those histories are warehoused.
"""

import logging
from datetime import date
from typing import Dict, List, Optional

import psycopg

import v14_engine as E

log = logging.getLogger("scorr.v14.replay")

# uniform-exit + setup constants mirror the live engine (single source of truth)
ATR_MULT = E.ATR_MULT
COST_SLIPPAGE = E.COST_SLIPPAGE


def _sessions(cur, sym: str, start: date, end: date) -> List[date]:
    cur.execute("""SELECT DISTINCT ts::date FROM intraday_prices
                   WHERE symbol=%s AND source='fyers_hist' AND timeframe='5m'
                     AND ts::date BETWEEN %s AND %s ORDER BY ts::date""", (sym, start, end))
    return [r[0] for r in cur.fetchall()]


def _day_bars(cur, sym: str, d: date):
    cur.execute("""SELECT ts, open, high, low, close, volume FROM intraday_prices
                   WHERE symbol=%s AND source='fyers_hist' AND timeframe='5m'
                     AND ts::date=%s AND ts::time>='09:15:00' ORDER BY ts ASC""", (sym, d))
    return cur.fetchall()


def _simulate_day(cur, sym: str, d: date, prior_days: List[date]) -> List[Dict]:
    """Walk a session bar-by-bar; open at most one trade/tag/day on the first price-rule trigger
    inside a clock window; exit via the uniform bracket. Returns closed episodes for that day."""
    bars = _day_bars(cur, sym, d)
    if len(bars) < 6:
        return []
    # prior-session VolX denominators (cum volume to same time-of-day)
    prior_bars = {pd_: _day_bars(cur, sym, pd_) for pd_ in prior_days[:5]}
    episodes: List[Dict] = []
    open_by_tag: Dict[str, Dict] = {}
    or_hi, or_lo = E._opening_range(bars)

    for i in range(3, len(bars)):
        window = bars[:i + 1]
        ts = bars[i][0]; t = ts.time()
        hm = (t.hour, t.minute)
        in_clock = any(a <= hm < b for (a, b) in E.CLOCK_WINDOWS)
        cur_c = E._f(bars[i][4])
        if cur_c is None:
            continue
        vwap = E._vwap(window)
        atr = E._atr(window)
        # manage open trades (bracket) on this bar
        for tag, tr in list(open_by_tag.items()):
            side = tr["side"]; entry = tr["entry"]
            move = ((cur_c - entry) / entry * 100) if side == "long" else ((entry - cur_c) / entry * 100)
            if move >= E.TRAIL_TRIGGER:
                tr["stop"] = entry if side == "long" else entry   # breakeven
            reason = None
            if (side == "long" and cur_c >= tr["target"]) or (side == "short" and cur_c <= tr["target"]):
                reason = "target"
            elif (side == "long" and cur_c <= tr["stop"]) or (side == "short" and cur_c >= tr["stop"]):
                reason = "stop"
            elif (i - tr["bar"]) >= 6 and move < E.TIME_STOP_PCT:   # ~30 min = 6 x 5m bars
                reason = "time"
            elif hm >= E.SQUAREOFF:
                reason = "squareoff"
            if reason:
                pnl_pts = (cur_c - entry) if side == "long" else (entry - cur_c)
                pnl_pct = pnl_pts / entry * 100 - COST_SLIPPAGE
                episodes.append({"tag": tag, "side": side, "pnl_pts": round(pnl_pts, 2),
                                 "net_pct": round(pnl_pct, 3), "reason": reason})
                open_by_tag.pop(tag, None)

        if not in_clock or hm >= E.SQUAREOFF or vwap is None or atr is None:
            continue
        # price-rule triggers (basis/OI/V10/sector gates RELAXED in replay — see module docstring)
        sess_hi = max((E._f(b[2]) for b in window if E._f(b[2]) is not None), default=None)
        volx = _replay_volx(window, prior_bars, t)
        for tag, side, ok in _triggers(window, cur_c, vwap, or_hi, or_lo, sess_hi, volx):
            if not ok or tag in open_by_tag:
                continue
            target = cur_c + ATR_MULT * atr if side == "long" else cur_c - ATR_MULT * atr
            stop = cur_c - (target - cur_c) if side == "long" else cur_c + (cur_c - target)
            open_by_tag[tag] = {"side": side, "entry": cur_c, "target": target, "stop": stop, "bar": i}
    return episodes


def _replay_volx(window, prior_bars, t) -> Optional[float]:
    today_cum = sum((E._f(b[5]) or 0.0) for b in window)
    if today_cum <= 0:
        return None
    pcs = []
    for pb in prior_bars.values():
        cum = sum((E._f(b[5]) or 0.0) for b in pb if b[0].time() <= t)
        if cum > 0:
            pcs.append(cum)
    if not pcs:
        return None
    avg = sum(pcs) / len(pcs)
    return round(today_cum / avg, 2) if avg else None


def _triggers(window, cur_c, vwap, or_hi, or_lo, sess_hi, volx):
    """Price-only trigger checks per tag (basis/OI/regime/sector relaxed)."""
    out = []
    # ORB
    if None not in (or_hi, or_lo, volx):
        out.append(("ORB", "long", cur_c > or_hi and volx >= 1.5 and cur_c > vwap))
        out.append(("ORB", "short", cur_c < or_lo and volx >= 1.5 and cur_c < vwap))
    # VWAP-RECLAIM
    if volx is not None and len(window) >= 2:
        above = sum(1 for b in window if (E._f(b[4]) or 0) >= vwap) / len(window)
        pc = E._f(window[-2][4]); plo = E._f(window[-2][3]); phi = E._f(window[-2][2])
        long_ok = (above >= 0.60 and plo is not None and plo < vwap and (vwap - plo) / vwap * 100 <= 0.6
                   and pc is not None and pc < vwap and cur_c > vwap and volx >= 1.0)
        short_ok = ((1 - above) >= 0.60 and phi is not None and phi > vwap and (phi - vwap) / vwap * 100 <= 0.6
                    and pc is not None and pc > vwap and cur_c < vwap and volx >= 1.0)
        out.append(("VWAP-RECLAIM", "long", long_ok))
        out.append(("VWAP-RECLAIM", "short", short_ok))
    # R1-REJ needs pivots; approximated by session-high rejection (>=0.3-1.5% fall, below VWAP)
    if sess_hi:
        fall = (sess_hi - cur_c) / sess_hi * 100
        out.append(("R1-REJ", "short", 0.3 <= fall <= 1.5 and cur_c < vwap))
    return out


def run_replay(symbols: Optional[List[str]] = None, start: Optional[str] = None,
               end: Optional[str] = None, db_url: Optional[str] = None) -> Dict:
    """Manual entry point. Replay the 3 setups across the fyers_hist warehouse and return a per-tag
    WR/EV table. NEVER auto-invoked — call explicitly after Phase A completes + founder trigger."""
    import os
    dsn = db_url or os.getenv("DATABASE_URL", "")
    agg: Dict[str, Dict] = {t: {"trades": 0, "wins": 0, "pts": 0.0, "net_pct": 0.0} for t in E.TAGS}
    total_days = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        if not symbols:
            cur.execute("SELECT DISTINCT symbol FROM intraday_prices WHERE source='fyers_hist' AND timeframe='5m'")
            symbols = [r[0] for r in cur.fetchall()]
        s = date.fromisoformat(start) if start else date(2000, 1, 1)
        e = date.fromisoformat(end) if end else date(2999, 1, 1)
        for sym in symbols:
            sess = _sessions(cur, sym, s, e)
            for idx, d in enumerate(sess):
                priors = sess[max(0, idx - 5):idx][::-1]
                for ep in _simulate_day(cur, sym, d, priors):
                    a = agg[ep["tag"]]
                    a["trades"] += 1
                    a["wins"] += 1 if ep["net_pct"] > 0 else 0
                    a["pts"] += ep["pnl_pts"]; a["net_pct"] += ep["net_pct"]
                total_days += 1
    table = []
    for t, a in agg.items():
        n = a["trades"]
        table.append({"tag": t, "trades": n, "win_rate": round(100 * a["wins"] / n, 1) if n else 0.0,
                      "net_points": round(a["pts"], 1), "avg_net_pct": round(a["net_pct"] / n, 3) if n else 0.0,
                      "ships_55pct": (n > 0 and (100 * a["wins"] / n) >= 55.0)})
    return {"symbols": len(symbols), "sessions_scanned": total_days, "by_tag": table,
            "note": "Replay RELAXES basis/OI/V10-regime/sector gates (not warehoused per-bar) — WR is an upper bound."}


if __name__ == "__main__":
    # Manual CLI: python v14_replay.py  — prints the per-tag table. Guarded off any auto path.
    import json
    print(json.dumps(run_replay(), indent=2))
