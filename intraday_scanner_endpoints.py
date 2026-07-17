"""
Intraday Scanner V2 — Scorr (LOCKED 19-Jun-2026, spec INTRADAY_SCANNER_SPEC_V2)
==============================================================================
On-demand 2-tier intraday scanner. No scheduler integration — called from the
Scanners page or manually. v8_signal_writer keeps intraday_prices fresh (5-min).

Tier 1 — pre-bucket: pass n-1 filters of ANY ONE of 4 buckets:
  buy_reversal (9/10) · buy_momentum (9/10) · buy_s1_bounce (6/7) · TC 14-check (13/14)
Tier 2 — core filters (BOTH must pass after Tier 1):
  Gate A  MA hierarchy   — 5min > 1hr > 1day > 3day, not overextended (<3%)
  Gate B  Room to run    — (15d_high - CMP)/CMP > 2.5%

TC = 14 checks (C1-C14), min 13 of 14 (n-1). C14 (basis) skipped if NULL → 12 of 13.
Time gate: 09:30–15:15 IST only. CMP at signal time = last today intraday close.
Passing signals auto-record to intraday_watchlist (ON CONFLICT symbol+date DO NOTHING).

Endpoints:
  GET /api/scanners/intraday              — full BUY scan, records passes
  GET /api/scanners/intraday/tc/{symbol}  — 14-check TC for one symbol now
  GET /api/scanners/intraday/watchlist    — today's recorded signals + live PnL

Bars are uniform 5-min, so "last 3 bars" = 15min, "last 12 bars" = 1hr.
Bar math uses ORDER BY ts DESC LIMIT N (timezone-safe); absolute time uses IST.
"""

import os
import math
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Json
from fastapi import APIRouter, HTTPException

# Reuse locked filter logic from the V8 engine (single source of truth)
from v8_endpoints import FILTER_CONFIG, _get_buy_reversal_live_filters, _passes_filter

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")
DATABASE_URL = os.getenv("DATABASE_URL", "")

WINDOW_START = dtime(9, 30)
WINDOW_END = dtime(15, 15)


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ist_now() -> datetime:
    return datetime.now(IST)


def _in_window(now: Optional[datetime] = None) -> bool:
    now = now or _ist_now()
    if now.weekday() >= 5:
        return False
    return WINDOW_START <= now.time() <= WINDOW_END


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _nifty_rsi() -> Optional[float]:
    """Market gate RSI14 (EWM) on NIFTY50 daily closes — mirrors v8 _s1b logic."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT close FROM raw_prices
                WHERE symbol='NIFTY50' AND price_date < CURRENT_DATE
                ORDER BY price_date DESC LIMIT 30
            """)
            closes = [float(r[0]) for r in cur.fetchall()]
        if len(closes) < 15:
            return None
        closes.reverse()
        import pandas as _pd
        s = _pd.Series(closes)
        delta = s.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = 100 - (100 / (1 + rs))
        v = rsi.iloc[-1]
        return float(v) if v == v else None
    except Exception:
        return None


# ── Gate 1: V8 pre-bucket ────────────────────────────────────────────────────

def _bucket_pass_count(row: dict, config: dict) -> int:
    return sum(
        1 for metric, bounds in config.items()
        if _passes_filter(row.get(metric), *(bounds if isinstance(bounds, list) else (bounds[0], bounds[1])))
    )


def _s1b_filters_eval(row: dict, nifty_rsi: Optional[float]) -> int:
    """buy_s1_bounce: 7 filters (clarifying_answers). Returns pass count."""
    cmp = _f(row.get("live_close"))
    op = _f(row.get("day_open"))
    lo2 = _f(row.get("lo_2d"))
    lo5 = _f(row.get("lo_5d"))
    tlow = _f(row.get("today_low"))
    s1 = _f(row.get("s1"))
    recovery_2d = ((cmp - lo2) / lo2 * 100) if (cmp and lo2 and lo2 > 0) else None
    day_ret = ((cmp - op) / op * 100) if (cmp and op and op > 0) else None
    wl = [x for x in (lo5, tlow) if x is not None]
    week_low = min(wl) if wl else None
    checks = [
        nifty_rsi is not None and nifty_rsi >= 55.0,                       # 1 market gate
        _passes_filter(row.get("week_return"), 0.0, 3.0),                  # 2
        _passes_filter(row.get("dma_50"), 0.0, None),                      # 3
        _passes_filter(row.get("vol_ratio"), 1.5, None),                   # 4
        _passes_filter(recovery_2d, 2.0, 8.0),                             # 5
        _passes_filter(day_ret, 0.5, None),                                # 6
        week_low is not None and s1 is not None and week_low <= s1,        # 7
    ]
    return sum(1 for c in checks if c)


def _gate1(row: dict, reversal_cfg: dict, nifty_rsi: Optional[float], tc: dict) -> dict:
    """Tier 1: pass n-1 of ANY ONE of 4 buckets (incl. TC 14-check as bucket 4)."""
    mom_cfg = FILTER_CONFIG["buy_momentum"]
    # cc#481 change_1: Tier-1 relaxed n-1 -> n-2 (need = max(1, evaluated-2)) for all buckets.
    buckets = [
        ("buy_momentum", _bucket_pass_count(row, mom_cfg), len(mom_cfg), max(1, len(mom_cfg) - 2)),
        ("buy_reversal", _bucket_pass_count(row, reversal_cfg), len(reversal_cfg), max(1, len(reversal_cfg) - 2)),
        ("buy_s1_bounce", _s1b_filters_eval(row, nifty_rsi), 7, max(1, 7 - 2)),
        ("tc", tc["score"], tc["evaluated"], tc["need"]),
    ]
    passed = [b for b in buckets if b[1] >= b[3]]
    if passed:
        # among passing buckets, label with the highest pass ratio
        name, sc, tot, need = max(passed, key=lambda b: (b[1] / b[2]) if b[2] else 0)
        return {"pass": True, "bucket": name, "score": sc, "total": tot}
    name, sc, tot, need = max(buckets, key=lambda b: b[1] - b[3])
    return {"pass": False, "bucket": name, "score": sc, "total": tot}


# ── Gate 2: MA hierarchy ─────────────────────────────────────────────────────

def _gate2(row: dict) -> dict:
    a3 = _f(row.get("avg_3bars"))     # 5-min (last 3 bars)
    a12 = _f(row.get("avg_12bars"))   # 1-hr  (last 12 bars)
    aday = _f(row.get("avg_today"))   # 1-day (all today bars)
    a3day = _f(row.get("avg_3day"))   # 3-day (last 3 daily closes)
    if None in (a3, a12, aday, a3day) or a3day == 0:
        return {"pass": False, "c1": False, "c2": False, "c3": False, "c4": False}
    c1 = a3 > a12
    c2 = a12 > aday
    c3 = aday > a3day
    c4 = (aday - a3day) / a3day * 100 < 3.0
    return {"pass": all([c1, c2, c3, c4]), "c1": c1, "c2": c2, "c3": c3, "c4": c4}


# ── TC bucket: 14-check (BUY) ────────────────────────────────────────────────

def _vol_timenorm(row: dict) -> Optional[float]:
    num = _f(row.get("vol_today"))
    den = _f(row.get("avg_vol_to_t"))
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _tc_eval(row: dict, adr: Optional[float]) -> dict:
    """BUY 14-check TC (spec V2). C14 (basis) skipped if NULL. need = evaluated-1 (n-1)."""
    cmp = _f(row.get("live_close"))
    pp, r1 = _f(row.get("pp")), _f(row.get("r1"))
    today_high = _f(row.get("today_high"))
    lb_open, lb_close = _f(row.get("lb_open")), _f(row.get("lb_close"))
    rsi_w, rsi_m = _f(row.get("rsi_weekly")), _f(row.get("rsi_month"))
    basis = _f(row.get("basis"))
    vtn = _vol_timenorm(row)

    pivot_room = (cmp is not None and pp is not None and r1 is not None
                  and pp < cmp <= r1 and (r1 - cmp) / cmp * 100 > 0.5)
    day_high_room = (cmp is not None and today_high is not None
                     and (today_high - cmp) / cmp * 100 > 0.3)
    room_to_r1 = (cmp is not None and pp is not None and r1 is not None
                  and (r1 - pp) > 0 and (r1 - cmp) >= 0.5 * (r1 - pp))

    results = {
        "C1_adr_ge_1": (None if adr is None else adr >= 1.0),                        # skippable
        "C2_sector_day_pos": _passes_filter(row.get("sector_day"), 1e-9, None),
        "C3_sector_week_pos": _passes_filter(row.get("sector_week"), 1e-9, None),
        "C4_pivot_zone": bool(pivot_room),
        "C5_vol_ge_1_25": (vtn is not None and vtn >= 1.25),
        "C6_last_bar_green": (lb_open is not None and lb_close is not None and lb_close > lb_open),
        "C7_day_high_room": bool(day_high_room),
        "C8_week_return_pos": _passes_filter(row.get("week_return"), 1e-9, None),
        "C9_rsi_weekly_50_75": _passes_filter(rsi_w, 50.0, 75.0),
        "C10_rsi_month_gt_55": (rsi_m is not None and rsi_m > 55.0),
        "C11_day_1d_pos": _passes_filter(row.get("day_1d"), 1e-9, None),
        "C12_mom_2d_pos": _passes_filter(row.get("mom_2d"), 1e-9, None),
        "C13_room_to_r1": bool(room_to_r1),
        "C14_basis_ge_0": (None if basis is None else basis >= 0),                   # skippable
    }
    evaluated = [v for v in results.values() if v is not None]
    passed = sum(1 for v in evaluated if v)
    need = max(1, len(evaluated) - 2)   # cc#481: n-2 of available (14->12, basis NULL 13->11)
    return {"pass": passed >= need, "score": passed, "evaluated": len(evaluated),
            "need": need, "checks": results}


# ── Tier 2 Gate B: room to run (15-trading-day high, > 2.5%) ─────────────────

def _gate4(row: dict) -> dict:
    cmp = _f(row.get("live_close"))
    week_high = _f(row.get("week_high"))   # MAX(high) last 15 trading days
    if cmp is None or week_high is None or cmp <= 0:
        return {"pass": False, "room_pct": None}
    room = (week_high - cmp) / cmp * 100
    return {"pass": room > 2.5, "room_pct": round(room, 2)}


# ── Main scan SQL ────────────────────────────────────────────────────────────

_SCAN_SQL = """
WITH t AS (
    SELECT MAX(ts)::time AS ttime FROM intraday_prices WHERE ts::date = CURRENT_DATE
)
SELECT
    m.symbol,
    m.gvm_score, m.dma_50, m.dma_200, m.rsi_month, m.rsi_weekly,
    m.month_return, m.week_return, m.mom_2d,
    m.sector_day, m.sector_week, m.sector_month, m.vol_ratio, m.day_1d,
    m.daily_rsi, m.week_index_52,
    p.pp, p.r1, p.s1, p.r2,
    td.day_open, td.live_close, td.lb_open, td.lb_close,
    td.today_high, td.today_low, td.avg_today, td.vol_today,
    a3.avg_3bars, a12.avg_12bars,
    h.lo_2d, h.lo_5d, h.week_high, h.week_low, h.avg_3day,
    vden.avg_vol_to_t,
    fb.basis
FROM v8_metrics m
JOIN futures_universe f ON f.symbol = m.symbol AND f.is_active = TRUE
LEFT JOIN v8_paper_pivots p ON p.symbol = m.symbol
    AND p.pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)
LEFT JOIN LATERAL (
    SELECT
        (SELECT open  FROM intraday_prices WHERE symbol=m.symbol AND ts::date=CURRENT_DATE ORDER BY ts ASC  LIMIT 1) AS day_open,
        (SELECT close FROM intraday_prices WHERE symbol=m.symbol AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 1) AS live_close,
        (SELECT open  FROM intraday_prices WHERE symbol=m.symbol AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 1) AS lb_open,
        (SELECT close FROM intraday_prices WHERE symbol=m.symbol AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 1) AS lb_close,
        MAX(high) AS today_high, MIN(low) AS today_low,
        AVG(close) AS avg_today, SUM(volume) AS vol_today
    FROM intraday_prices WHERE symbol=m.symbol AND ts::date=CURRENT_DATE
) td ON true
LEFT JOIN LATERAL (
    SELECT AVG(close) AS avg_3bars FROM (
        SELECT close FROM intraday_prices WHERE symbol=m.symbol AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 3
    ) z
) a3 ON true
LEFT JOIN LATERAL (
    SELECT AVG(close) AS avg_12bars FROM (
        SELECT close FROM intraday_prices WHERE symbol=m.symbol AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 12
    ) z
) a12 ON true
LEFT JOIN LATERAL (
    SELECT MIN(low)  FILTER (WHERE rn<=2)  AS lo_2d,
           MIN(low)  FILTER (WHERE rn<=5)  AS lo_5d,
           MAX(high) FILTER (WHERE rn<=15) AS week_high,
           MIN(low)  FILTER (WHERE rn<=15) AS week_low,
           MAX(high) FILTER (WHERE rn<=3)  AS high_3d,
           AVG(close) FILTER (WHERE rn<=3) AS avg_3day
    FROM (
        SELECT low, high, close, ROW_NUMBER() OVER (ORDER BY price_date DESC) AS rn
        FROM raw_prices WHERE symbol=m.symbol AND price_date < CURRENT_DATE
    ) x WHERE rn<=15
) h ON true
LEFT JOIN LATERAL (
    SELECT AVG(daysum) AS avg_vol_to_t FROM (
        SELECT ts::date AS d, SUM(volume) AS daysum
        FROM intraday_prices
        WHERE symbol=m.symbol
          AND ts::date BETWEEN CURRENT_DATE-8 AND CURRENT_DATE-1
          AND ts::time <= (SELECT ttime FROM t)
        GROUP BY ts::date
    ) z
) vden ON true
LEFT JOIN LATERAL (
    SELECT basis FROM futures_basis
    WHERE symbol=m.symbol AND ts::date=CURRENT_DATE ORDER BY ts DESC LIMIT 1
) fb ON true
WHERE m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
"""


def _evaluate(row: dict, reversal_cfg: dict, nifty_rsi: Optional[float], adr: Optional[float]) -> dict:
    tc = _tc_eval(row, adr)                       # 14-check TC (also Tier-1 bucket 4)
    g1 = _gate1(row, reversal_cfg, nifty_rsi, tc)  # Tier 1: any of 4 buckets
    g2 = _gate2(row)                              # Tier 2 Gate A: MA hierarchy
    g4 = _gate4(row)                              # Tier 2 Gate B: room to run
    tier1 = g1["pass"]
    tier2 = g2["pass"] and g4["pass"]
    vtn = _vol_timenorm(row)
    return {
        "symbol": row["symbol"],
        "basket": g1["bucket"],
        "signal": tier1 and tier2,
        "tier1_pass": tier1, "tier1_score": g1["score"], "tier1_total": g1["total"],
        "gate1_score": g1["score"],
        "tier2_pass": tier2,
        "gate2_ma_pass": g2["pass"], "gate2": {k: v for k, v in g2.items() if k != "pass"},
        "gate3_tc_score": tc["score"], "tc_pass": tc["pass"],
        "gate3_need": tc["need"], "gate3_evaluated": tc["evaluated"],
        "gate4_room_pct": g4["room_pct"], "gate4_pass": g4["pass"],
        "room_ref": _f(row.get("week_high")),   # 15-trading-day high (Gate B reference)
        "vol_ratio_timenorm": round(vtn, 3) if vtn is not None else None,
        "basis": _f(row.get("basis")),
        "entry_price": _f(row.get("live_close")),
        "checks": tc["checks"],
    }


# ── SHORT side (spec INTRADAY_SCANNER_SELL_SPEC_V1) ──────────────────────────

def _sell_overbought_eval(row: dict) -> int:
    """sell_overbought_v2: 5 filters (2 pivot/computed). Returns pass count."""
    rsi_w, rsi_m = _f(row.get("rsi_weekly")), _f(row.get("rsi_month"))
    sec_w = _f(row.get("sector_week"))
    wh, r1, r2 = _f(row.get("week_high")), _f(row.get("r1")), _f(row.get("r2"))
    cmp, h3 = _f(row.get("live_close")), _f(row.get("high_3d"))
    fall_3d = ((cmp - h3) / h3 * 100) if (cmp and h3 and h3 > 0) else None
    near_resist = wh is not None and ((r1 is not None and wh > 0.9 * r1) or (r2 is not None and wh > 0.9 * r2))
    checks = [
        rsi_w is not None and rsi_w >= 80.0,
        rsi_m is not None and rsi_m >= 70.0,
        sec_w is not None and sec_w < 0,
        bool(near_resist),
        fall_3d is not None and fall_3d < -3.0,
    ]
    return sum(1 for c in checks if c)


def _sell_tc_eval(row: dict, adr: Optional[float]) -> dict:
    """SHORT 14-check TC (mirror of BUY). C14 (basis) skipped if NULL. need = evaluated-1."""
    cmp = _f(row.get("live_close"))
    pp, s1 = _f(row.get("pp")), _f(row.get("s1"))
    today_low = _f(row.get("today_low"))
    lb_open, lb_close = _f(row.get("lb_open")), _f(row.get("lb_close"))
    rsi_w, rsi_m = _f(row.get("rsi_weekly")), _f(row.get("rsi_month"))
    daily_rsi = _f(row.get("daily_rsi"))
    basis = _f(row.get("basis"))

    pivot_room = (cmp is not None and pp is not None and s1 is not None
                  and s1 < cmp < pp and (cmp - s1) / cmp * 100 > 0.5)
    day_low_room = (cmp is not None and today_low is not None
                    and (cmp - today_low) / cmp * 100 > 0.3)
    room_to_s1 = (cmp is not None and pp is not None and s1 is not None
                  and (pp - s1) > 0 and (cmp - s1) >= 0.5 * (pp - s1))

    results = {
        "C1_adr_lt_1": (None if adr is None else adr < 1.0),                         # skippable
        "C2_sector_day_neg": _passes_filter(row.get("sector_day"), None, -1e-9),
        "C3_sector_week_neg": _passes_filter(row.get("sector_week"), None, -1e-9),
        "C4_pivot_zone": bool(pivot_room),
        "C5_daily_rsi_gt_30": (daily_rsi is not None and daily_rsi > 30.0),
        "C6_last_bar_red": (lb_open is not None and lb_close is not None and lb_close < lb_open),
        "C7_day_low_room": bool(day_low_room),
        "C8_week_return_neg": _passes_filter(row.get("week_return"), None, -1e-9),
        "C9_rsi_weekly_25_50": _passes_filter(rsi_w, 25.0, 50.0),
        "C10_rsi_month_lt_45": (rsi_m is not None and rsi_m < 45.0),
        "C11_day_1d_neg": _passes_filter(row.get("day_1d"), None, -1e-9),
        "C12_mom_2d_neg": _passes_filter(row.get("mom_2d"), None, -1e-9),
        "C13_room_to_s1": bool(room_to_s1),
        "C14_basis_le_0": (None if basis is None else basis <= 0),                   # skippable
    }
    evaluated = [v for v in results.values() if v is not None]
    passed = sum(1 for v in evaluated if v)
    need = max(1, len(evaluated) - 2)   # cc#481: n-2 (mirror of BUY)
    return {"pass": passed >= need, "score": passed, "evaluated": len(evaluated),
            "need": need, "checks": results}


def _gate1_short(row: dict, tc: dict) -> dict:
    """Tier 1 SHORT: pass n-2 of ANY ONE of 4 sell buckets (incl. TC-14). cc#481: relaxed n-1->n-2."""
    sr_cfg, sm_cfg = FILTER_CONFIG["sell_reversal"], FILTER_CONFIG["sell_momentum"]
    buckets = [
        ("sell_reversal", _bucket_pass_count(row, sr_cfg), len(sr_cfg), max(1, len(sr_cfg) - 2)),
        ("sell_momentum", _bucket_pass_count(row, sm_cfg), len(sm_cfg), max(1, len(sm_cfg) - 2)),
        ("sell_overbought", _sell_overbought_eval(row), 5, max(1, 5 - 2)),
        ("tc", tc["score"], tc["evaluated"], tc["need"]),
    ]
    passed = [b for b in buckets if b[1] >= b[3]]
    if passed:
        name, sc, tot, need = max(passed, key=lambda b: (b[1] / b[2]) if b[2] else 0)
        return {"pass": True, "bucket": name, "score": sc, "total": tot}
    name, sc, tot, need = max(buckets, key=lambda b: b[1] - b[3])
    return {"pass": False, "bucket": name, "score": sc, "total": tot}


def _gate2_short(row: dict) -> dict:
    """Tier 2 Gate A SHORT (inverted MA): 5min < 1hr < 1day < 3day, not over-extended down."""
    a3, a12 = _f(row.get("avg_3bars")), _f(row.get("avg_12bars"))
    aday, a3day = _f(row.get("avg_today")), _f(row.get("avg_3day"))
    if None in (a3, a12, aday, a3day) or a3day == 0:
        return {"pass": False, "c1": False, "c2": False, "c3": False, "c4": False}
    c1, c2, c3 = a3 < a12, a12 < aday, aday < a3day
    c4 = (a3day - aday) / a3day * 100 < 3.0
    return {"pass": all([c1, c2, c3, c4]), "c1": c1, "c2": c2, "c3": c3, "c4": c4}


def _gate4_short(row: dict) -> dict:
    """Tier 2 Gate B SHORT: room to fall — (CMP - 15d_low)/CMP > 2.5%."""
    cmp, week_low = _f(row.get("live_close")), _f(row.get("week_low"))
    if cmp is None or week_low is None or cmp <= 0:
        return {"pass": False, "room_pct": None}
    room = (cmp - week_low) / cmp * 100
    return {"pass": room > 2.5, "room_pct": round(room, 2)}


def _evaluate_short(row: dict, adr: Optional[float]) -> dict:
    tc = _sell_tc_eval(row, adr)
    g1 = _gate1_short(row, tc)
    g2 = _gate2_short(row)
    g4 = _gate4_short(row)
    tier1 = g1["pass"]
    tier2 = g2["pass"] and g4["pass"]
    vtn = _vol_timenorm(row)
    return {
        "symbol": row["symbol"],
        "basket": g1["bucket"],
        "signal": tier1 and tier2,
        "tier1_pass": tier1, "tier1_score": g1["score"], "tier1_total": g1["total"],
        "gate1_score": g1["score"],
        "tier2_pass": tier2,
        "gate2_ma_pass": g2["pass"], "gate2": {k: v for k, v in g2.items() if k != "pass"},
        "gate3_tc_score": tc["score"], "tc_pass": tc["pass"],
        "gate3_need": tc["need"], "gate3_evaluated": tc["evaluated"],
        "gate4_room_pct": g4["room_pct"], "gate4_pass": g4["pass"],
        "room_ref": _f(row.get("week_low")),    # 15-trading-day low (Gate B reference)
        "vol_ratio_timenorm": round(vtn, 3) if vtn is not None else None,
        "basis": _f(row.get("basis")),
        "entry_price": _f(row.get("live_close")),
        "checks": tc["checks"],
    }


def _target_stop(entry, direction):
    """cc#481 change_3: display-only ±3% levels stamped at signal (LONG: +3%/-3%; SHORT mirror).
    No paper engine, no auto-exit — the screener stays a screener."""
    e = _f(entry)
    if e is None or e <= 0:
        return None, None
    if direction == "SHORT":
        return round(e * 0.97, 2), round(e * 1.03, 2)   # target below, stop above
    return round(e * 1.03, 2), round(e * 0.97, 2)         # LONG: target above, stop below


def _record_watchlist(signals: list, signal_ts: datetime, direction: str = "LONG"):
    if not signals:
        return 0
    rows = []
    for s in signals:
        tgt, sl = _target_stop(s["entry_price"], direction)
        rows.append((s["symbol"], s["basket"], direction, signal_ts, s["entry_price"], s["basket"],
                     s["gate1_score"], s["gate2_ma_pass"], s["gate3_tc_score"], s["gate4_room_pct"],
                     Json(s["checks"]), tgt, sl))
    with _conn() as conn, conn.cursor() as cur:
        # cc#481 change_3: app-side ADD COLUMN (never the run_sql lock path).
        cur.execute("ALTER TABLE intraday_watchlist ADD COLUMN IF NOT EXISTS target NUMERIC, "
                    "ADD COLUMN IF NOT EXISTS stop_loss NUMERIC")
        cur.executemany("""
            INSERT INTO intraday_watchlist
                (symbol, basket, direction, signal_ts, entry_price, gate1_bucket,
                 gate1_score, gate2_ma_pass, gate3_tc_score, gate4_room_pct, checks, target, stop_loss)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, signal_date, direction) DO NOTHING
        """, rows)
        recorded = cur.rowcount
        conn.commit()
    return recorded


# ── Near-miss (Tier1 pass, Tier2 fail) + signal enrichment ───────────────────

def _near_failed(s: dict) -> str:
    """Which Tier-2 gate failed for a Tier-1 passer."""
    a = not s["gate2_ma_pass"]   # Gate A — MA hierarchy
    b = not s["gate4_pass"]      # Gate B — room to run
    if a and b:
        return "Both"
    if a:
        return "Gate A (MA)"
    if b:
        return "Gate B (Room)"
    return "—"


def _build_near_misses(scored: list, direction: str) -> list:
    """Stocks that cleared Tier 1 (any bucket n-1) but failed one/both Tier-2 gates."""
    nm = [s for s in scored if s["tier1_pass"] and not s["tier2_pass"]]
    nm.sort(key=lambda s: (s["tier1_score"], s.get("gate4_room_pct") or 0), reverse=True)
    return [{
        "symbol": s["symbol"], "direction": direction, "basket": s["basket"],
        "tier1_score": s["tier1_score"], "tier1_total": s["tier1_total"],
        "failed": _near_failed(s),
        "gate2_ma_pass": s["gate2_ma_pass"], "gate4_pass": s["gate4_pass"],
        "room_pct": s.get("gate4_room_pct"), "room_ref": s.get("room_ref"),
        "entry_price": s.get("entry_price"),
    } for s in nm]


def _enrich(rows: list, direction: str):
    """In place: add recorded entry, live CMP, direction-aware PnL%, signal time,
    and native Future-Scan TC (tc_cache) with a pass flag. direction = LONG | SHORT."""
    syms = sorted({r["symbol"] for r in rows})
    if not syms:
        return
    fs_need = 12 if direction == "LONG" else 11   # spec #14: >=12/15 LONG, >=11 SHORT
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT symbol, MIN(entry_price) AS e, MIN(signal_ts) AS t
                       FROM intraday_watchlist
                       WHERE signal_date=CURRENT_DATE AND direction=%s AND symbol = ANY(%s)
                       GROUP BY symbol""", (direction, syms))
        wl = {r[0]: (_f(r[1]), r[2]) for r in cur.fetchall()}
        cur.execute("SELECT symbol, cmp FROM cmp_prices WHERE symbol = ANY(%s)", (syms,))
        cmp_map = {r[0]: _f(r[1]) for r in cur.fetchall()}
        cur.execute("SELECT symbol, score, total FROM tc_cache WHERE side=%s AND symbol = ANY(%s)",
                    (direction, syms))
        tc_map = {r[0]: (_f(r[1]), r[2]) for r in cur.fetchall()}
    sign = -1 if direction == "SHORT" else 1
    for r in rows:
        sym = r["symbol"]
        rec_entry, rec_ts = wl.get(sym, (None, None))
        entry = rec_entry if rec_entry is not None else r.get("entry_price")
        cmp = cmp_map.get(sym)
        r["recorded_entry"] = round(rec_entry, 2) if rec_entry is not None else None
        r["signal_ts"] = rec_ts.astimezone(IST).strftime("%H:%M") if rec_ts else None
        r["cmp"] = round(cmp, 2) if cmp is not None else None
        r["pnl_pct"] = (round(sign * (cmp - entry) / entry * 100, 2)
                        if (entry and cmp) else None)
        ns, nt = tc_map.get(sym, (None, None))
        r["fs_score"] = ns
        r["fs_total"] = nt
        r["future_scan_pass"] = bool(ns is not None and ns >= fs_need)


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/api/scanners/intraday")
def scanner_intraday(limit: int = 40):
    """V2 BUY scan (Tier1 4 buckets -> Tier2 MA + room) over the active futures universe."""
    now = _ist_now()
    if not _in_window(now):
        return {"scanner": "intraday", "status": "outside_window",
                "window": "09:30-15:15 IST", "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
                "signals": [], "count": 0, "near_misses": [], "near_miss_count": 0}

    reversal_cfg = _get_buy_reversal_live_filters()[0]
    nifty_rsi = _nifty_rsi()

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT adr FROM adr_daily WHERE price_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
        ar = cur.fetchone()
        adr = float(ar[0]) if ar and ar[0] is not None else None
        cur.execute("SET LOCAL jit = off")   # ~800ms JIT overhead on the many-LATERAL scan; data access is ~400ms
        cur.execute(_SCAN_SQL)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    scored = [_evaluate(r, reversal_cfg, nifty_rsi, adr) for r in rows]
    signals = [s for s in scored if s["signal"]]
    signals.sort(key=lambda s: (s["gate3_tc_score"], s["gate1_score"]), reverse=True)

    recorded = _record_watchlist(signals, now)

    near_misses = _build_near_misses(scored, "LONG")
    top_signals = signals[:max(1, limit)]
    top_near = near_misses[:max(1, limit)]
    _enrich(top_signals, "LONG")
    _enrich(top_near, "LONG")

    return {
        "scanner": "intraday", "side": "BUY", "spec": "V2", "status": "ok",
        "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "nifty_rsi": round(nifty_rsi, 1) if nifty_rsi is not None else None,
        "adr": adr, "universe": len(rows),
        "count": len(signals), "recorded_to_watchlist": recorded,
        "signals": top_signals,
        "near_misses": top_near, "near_miss_count": len(near_misses),
    }


@router.get("/api/scanners/intraday/short")
def scanner_intraday_short(limit: int = 40):
    """V2 SHORT scan (Tier1 4 sell buckets -> Tier2 inverted MA + room-to-fall)."""
    now = _ist_now()
    if not _in_window(now):
        return {"scanner": "intraday_short", "side": "SHORT", "status": "outside_window",
                "window": "09:30-15:15 IST", "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
                "signals": [], "count": 0, "near_misses": [], "near_miss_count": 0}

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT adr FROM adr_daily WHERE price_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
        ar = cur.fetchone()
        adr = float(ar[0]) if ar and ar[0] is not None else None
        cur.execute("SET LOCAL jit = off")   # ~800ms JIT overhead on the many-LATERAL scan; data access is ~400ms
        cur.execute(_SCAN_SQL)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    scored = [_evaluate_short(r, adr) for r in rows]
    signals = [s for s in scored if s["signal"]]
    signals.sort(key=lambda s: (s["gate3_tc_score"], s["gate1_score"]), reverse=True)
    recorded = _record_watchlist(signals, now, direction="SHORT")

    near_misses = _build_near_misses(scored, "SHORT")
    top_signals = signals[:max(1, limit)]
    top_near = near_misses[:max(1, limit)]
    _enrich(top_signals, "SHORT")
    _enrich(top_near, "SHORT")

    return {
        "scanner": "intraday_short", "side": "SHORT", "spec": "V1", "status": "ok",
        "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "adr": adr, "universe": len(rows),
        "count": len(signals), "recorded_to_watchlist": recorded,
        "signals": top_signals,
        "near_misses": top_near, "near_miss_count": len(near_misses),
    }


@router.get("/api/scanners/intraday/tc/{symbol}")
def scanner_intraday_tc(symbol: str, side: str = "long"):
    """Run the 14-check TC + Tier1/Tier2 evaluation for a single symbol now (side=long|short)."""
    symbol = symbol.upper()
    side = side.lower()
    now = _ist_now()
    if not _in_window(now):
        return {"symbol": symbol, "status": "outside_window",
                "window": "09:30-15:15 IST", "now": now.strftime("%Y-%m-%d %H:%M:%S IST")}

    reversal_cfg = _get_buy_reversal_live_filters()[0]
    nifty_rsi = _nifty_rsi()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT adr FROM adr_daily WHERE price_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
        ar = cur.fetchone()
        adr = float(ar[0]) if ar and ar[0] is not None else None
        cur.execute(_SCAN_SQL + " AND m.symbol = %s", (symbol,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"No live metrics for {symbol} today")
        row = dict(zip(cols, row))

    res = _evaluate_short(row, adr) if side == "short" else _evaluate(row, reversal_cfg, nifty_rsi, adr)
    return {
        "symbol": symbol, "side": ("SHORT" if side == "short" else "BUY"), "spec": "V2", "status": "ok",
        "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "signal": res["signal"], "basket": res["basket"],
        "tier1": {"pass": res["tier1_pass"], "bucket": res["basket"],
                  "score": res["tier1_score"], "total": res["tier1_total"]},
        "tier2": {"pass": res["tier2_pass"],
                  "ma": {"pass": res["gate2_ma_pass"], **res["gate2"]},
                  "room": {"pass": res["gate4_pass"], "room_pct": res["gate4_room_pct"]}},
        "tc": {"pass": res["tc_pass"], "score": res["gate3_tc_score"],
               "need": res["gate3_need"], "evaluated": res["gate3_evaluated"], "checks": res["checks"]},
        "vol_ratio_timenorm": res["vol_ratio_timenorm"],
        "basis": res["basis"],
        "entry_price": res["entry_price"],
    }


@router.get("/api/scanners/intraday/watchlist")
def scanner_intraday_watchlist(date: Optional[str] = None, direction: Optional[str] = None,
                                basket: Optional[str] = None):
    """Today's recorded signals (BUY+SHORT) with live CMP, PnL%, time since signal.

    PnL% is direction-aware: SHORT profits when price falls.
    cc#484: basket filter added (additive, display-layer only) so ORB AG's own tab
    can pull just its rows from the shared intraday_watchlist table.
    """
    clauses = ["w.signal_date = %s" if date else "w.signal_date = CURRENT_DATE"]
    params = [date] if date else []
    if direction:
        clauses.append("w.direction = %s")
        params.append(direction.upper())
    if basket:
        clauses.append("w.basket = %s")
        params.append(basket)
    where = " AND ".join(clauses)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE intraday_watchlist ADD COLUMN IF NOT EXISTS target NUMERIC, "
                    "ADD COLUMN IF NOT EXISTS stop_loss NUMERIC")   # cc#481: safe before SELECT on fresh DB
        conn.commit()
        cur.execute(f"""
            SELECT w.symbol, w.basket, w.direction, w.signal_date, w.signal_ts, w.entry_price,
                   w.gate1_score, w.gate2_ma_pass, w.gate3_tc_score, w.gate4_room_pct,
                   w.target, w.stop_loss AS stop, c.cmp,
                   ROUND(CASE WHEN w.entry_price>0
                        THEN (CASE WHEN w.direction='SHORT' THEN -1 ELSE 1 END)
                             * (COALESCE(c.cmp, w.entry_price) - w.entry_price)/w.entry_price*100
                        ELSE 0 END::numeric, 2) AS pnl_pct,
                   EXTRACT(EPOCH FROM (NOW() - w.signal_ts))/60.0 AS mins_since
            FROM intraday_watchlist w
            LEFT JOIN cmp_prices c ON c.symbol = w.symbol
            WHERE {where}
            ORDER BY w.signal_ts DESC
        """, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for r in rows:
        if r.get("mins_since") is not None:
            r["mins_since"] = round(float(r["mins_since"]), 1)
        for k in ("entry_price", "cmp", "gate4_room_pct", "target", "stop"):
            if r.get(k) is not None:
                r[k] = float(r[k])

    return {"scanner": "intraday_watchlist",
            "date": date or "today", "count": len(rows), "rows": rows}


# ── ORB AG strategy (cc#484) ──────────────────────────────────────────────────
# Additive, standalone bucket — distinct from the Tier1/Tier2 buckets above and
# from TC Scanner (tc_scanner_endpoints.py). Does not touch either. Confirmed
# with Arpit (17-Jul): condition 3+4 (pivot) is a POSITIONAL band (PP < CMP <
# R1), not a fresh same-day cross.
#
# All 6 conditions required:
#   1. CMP > 20-DMA (v8_metrics.dma_20)
#   2. CMP > 9-DMA (computed here — v8_metrics only carries 20/50/200)
#   3+4. PP < CMP < R1 (daily pivot positional band, v8_paper_pivots)
#   5. Sector month return > 0 (v8_metrics.sector_month)
#   6. Opening Range Breakout: range = 09:15-10:00 IST (45min — deliberately
#      wider than V14's 09:15-09:30 ORB, a different window, not the same signal).
#      Breakout = CMP clears the range HIGH; only evaluated after 10:00.
#   7. Breakout-bar volume >= 1.2x the historical average volume for that SAME
#      5-min time-of-day slot over the last 8 trading days — this is what makes
#      it "time-proportional" (an early bar is compared against other early
#      bars, not a flat full-day average, since it fires early in the session).

ORB_WINDOW_START = dtime(9, 15)
ORB_WINDOW_END   = dtime(10, 0)
ORB_SCAN_START   = dtime(10, 0)   # breakout only evaluable once the range has closed
ORB_SCAN_END     = dtime(15, 20)

_ORB_SQL = """
WITH orb AS (
    SELECT symbol, MAX(high) AS orb_high, MIN(low) AS orb_low
    FROM intraday_prices
    WHERE source='fyers_fut' AND timeframe='5m' AND ts::date=CURRENT_DATE
      AND ts::time >= %(orb_start)s AND ts::time < %(orb_end)s
    GROUP BY symbol
),
dma9 AS (
    SELECT symbol, AVG(close) AS dma_9
    FROM (
        SELECT symbol, close, ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
        FROM raw_prices WHERE price_date < CURRENT_DATE
    ) x WHERE rn <= 9
    GROUP BY symbol
)
SELECT
    m.symbol, m.dma_20, m.sector_month,
    p.pp, p.r1,
    o.orb_high, o.orb_low,
    d9.dma_9,
    cur.cmp, cur.cur_ts, cur.cur_vol,
    hbv.hist_bar_vol
FROM v8_metrics m
JOIN futures_universe f ON f.symbol = m.symbol AND f.is_active = TRUE
LEFT JOIN v8_paper_pivots p ON p.symbol = m.symbol
    AND p.pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)
LEFT JOIN orb o ON o.symbol = m.symbol
LEFT JOIN dma9 d9 ON d9.symbol = m.symbol
LEFT JOIN LATERAL (
    SELECT close AS cmp, ts AS cur_ts, volume AS cur_vol
    FROM intraday_prices
    WHERE symbol=m.symbol AND source='fyers_fut' AND timeframe='5m' AND ts::date=CURRENT_DATE
    ORDER BY ts DESC LIMIT 1
) cur ON true
LEFT JOIN LATERAL (
    SELECT AVG(volume) AS hist_bar_vol
    FROM intraday_prices
    WHERE symbol=m.symbol AND source='fyers_fut' AND timeframe='5m'
      AND ts::date BETWEEN CURRENT_DATE-8 AND CURRENT_DATE-1
      AND ts::time = cur.cur_ts::time
) hbv ON true
WHERE m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
"""


def _orb_ag_eval(row: dict) -> dict:
    cmp   = _f(row.get("cmp"))
    dma20 = _f(row.get("dma_20"))
    dma9  = _f(row.get("dma_9"))
    pp, r1 = _f(row.get("pp")), _f(row.get("r1"))
    sector_month = _f(row.get("sector_month"))
    orb_high = _f(row.get("orb_high"))
    cur_vol  = _f(row.get("cur_vol"))
    hist_bar_vol = _f(row.get("hist_bar_vol"))

    checks = {
        "above_dma20":     cmp is not None and dma20 is not None and cmp > dma20,
        "above_dma9":      cmp is not None and dma9 is not None and cmp > dma9,
        "pivot_band_pp_r1": cmp is not None and pp is not None and r1 is not None and pp < cmp < r1,
        "sector_month_positive": sector_month is not None and sector_month > 0,
        "orb_breakout":    cmp is not None and orb_high is not None and cmp > orb_high,
        "volume_1.2x_timeprop": (cur_vol is not None and hist_bar_vol is not None
                                  and hist_bar_vol > 0 and cur_vol >= 1.2 * hist_bar_vol),
    }
    score = sum(1 for v in checks.values() if v)
    return {
        "symbol": row["symbol"], "signal": all(checks.values()), "checks": checks,
        "score": score, "total": len(checks),
        "cmp": cmp, "dma_20": dma20, "dma_9": dma9, "pp": pp, "r1": r1,
        "sector_month": sector_month, "orb_high": orb_high,
        "cur_vol": cur_vol, "hist_bar_vol": hist_bar_vol,
    }


@router.get("/api/scanners/orb_ag")
def scanner_orb_ag(limit: int = 40):
    """cc#484: ORB AG — standalone additive strategy. All 6 conditions must pass.
    Evaluated only 10:00-15:20 IST (opening range 09:15-10:00 must be closed first).
    Recorded to the shared intraday_watchlist table with basket='orb_ag',
    direction='LONG' — reuses the existing recording/PnL/target-stop infra
    (_record_watchlist, _target_stop) unchanged; nothing here touches Tier1/Tier2
    or TC Scanner."""
    now = _ist_now()
    if now.weekday() >= 5 or not (ORB_SCAN_START <= now.time() <= ORB_SCAN_END):
        return {"scanner": "orb_ag", "status": "outside_window",
                "window": "10:00-15:20 IST (range=09:15-10:00)",
                "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
                "signals": [], "count": 0}

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(_ORB_SQL, {"orb_start": ORB_WINDOW_START, "orb_end": ORB_WINDOW_END})
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    scored = [_orb_ag_eval(r) for r in rows]
    signals = [s for s in scored if s["signal"]]
    signals.sort(key=lambda s: s["score"], reverse=True)

    to_record = [{"symbol": s["symbol"], "basket": "orb_ag", "gate1_score": s["score"],
                  "gate2_ma_pass": True, "gate3_tc_score": s["score"], "gate4_room_pct": None,
                  "checks": s["checks"], "entry_price": s["cmp"]} for s in signals]
    recorded = _record_watchlist(to_record, now, direction="LONG")

    top_signals = signals[:max(1, limit)]
    return {
        "scanner": "orb_ag", "side": "LONG", "spec": "cc#484 v1", "status": "ok",
        "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "universe": len(rows), "count": len(signals), "recorded_to_watchlist": recorded,
        "signals": top_signals,
    }
