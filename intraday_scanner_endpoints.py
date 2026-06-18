"""
Intraday Scanner V1 — Scorr (LOCKED 18-Jun-2026, spec INTRADAY_SCANNER_V1_003)
==============================================================================
On-demand 4-gate intraday scanner. No scheduler integration — called from the
Scanners page or manually. v8_signal_writer keeps intraday_prices fresh (5-min).

Gates (all strict AND):
  Gate 1  V8 pre-bucket   — pass n-1 filters of ANY ONE of 3 buy buckets
  Gate 2  MA hierarchy    — 5min > 1hr > 1day > 3day, not overextended (<3%)
  Gate 3  10-check TC     — min 9 of 10 (ADR skipped if NULL → one miss allowed)
  Gate 4  Room to run     — (week_high - CMP)/CMP > 0.5%

Time gate: 09:30–15:15 IST only. CMP at signal time = last today intraday close.
Passing signals auto-record to intraday_watchlist (ON CONFLICT symbol+date DO NOTHING).

Endpoints:
  GET /api/scanners/intraday              — full scan, records passes
  GET /api/scanners/intraday/tc/{symbol}  — 10-check TC for one symbol now
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


def _gate1(row: dict, reversal_cfg: dict, nifty_rsi: Optional[float]) -> dict:
    """Best of 3 buckets. Pass if any bucket reaches its n-1 threshold."""
    buckets = []
    mom_score = _bucket_pass_count(row, FILTER_CONFIG["buy_momentum"])
    buckets.append(("buy_momentum", mom_score, len(FILTER_CONFIG["buy_momentum"]), len(FILTER_CONFIG["buy_momentum"]) - 1))
    rev_score = _bucket_pass_count(row, reversal_cfg)
    buckets.append(("buy_reversal", rev_score, len(reversal_cfg), len(reversal_cfg) - 1))
    s1b_score = _s1b_filters_eval(row, nifty_rsi)
    buckets.append(("buy_s1_bounce", s1b_score, 7, 6))

    passed = [(name, sc, tot, need) for (name, sc, tot, need) in buckets if sc >= need]
    if passed:
        # pick the bucket with the highest absolute pass count
        name, sc, tot, need = max(passed, key=lambda b: b[1])
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


# ── Gate 3: 10-check TC ──────────────────────────────────────────────────────

def _vol_timenorm(row: dict) -> Optional[float]:
    num = _f(row.get("vol_today"))
    den = _f(row.get("avg_vol_to_t"))
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _rsi_month_floor(bucket: Optional[str]) -> float:
    return {"buy_reversal": 55.0, "buy_momentum": 70.0, "buy_s1_bounce": 50.0}.get(bucket, 55.0)


def _gate3(row: dict, adr: Optional[float], bucket: Optional[str]) -> dict:
    cmp = _f(row.get("live_close"))
    pp, r1 = _f(row.get("pp")), _f(row.get("r1"))
    today_high = _f(row.get("today_high"))
    lb_open, lb_close = _f(row.get("lb_open")), _f(row.get("lb_close"))
    rsi_w = _f(row.get("rsi_weekly"))
    rsi_m = _f(row.get("rsi_month"))
    vtn = _vol_timenorm(row)

    pivot_room = (cmp is not None and pp is not None and r1 is not None
                  and pp < cmp <= r1 and (r1 - cmp) / cmp * 100 > 0.5)
    day_high_room = (cmp is not None and today_high is not None
                     and (today_high - cmp) / cmp * 100 > 0.3)

    results = {
        "adr_ge_1": (None if adr is None else adr >= 1.0),                          # 1 (skippable)
        "sector_day_pos": _passes_filter(row.get("sector_day"), 0.000001, None),    # 2 (>0)
        "sector_week_pos": _passes_filter(row.get("sector_week"), 0.000001, None),  # 3 (>0)
        "pivot_zone": bool(pivot_room),                                             # 4
        "vol_timenorm_ge_0_8": (vtn is not None and vtn >= 0.8),                    # 5
        "last_bar_green": (lb_open is not None and lb_close is not None and lb_close > lb_open),  # 6
        "day_high_room": bool(day_high_room),                                       # 7
        "week_return_pos": _passes_filter(row.get("week_return"), 0.000001, None),  # 8
        "rsi_weekly_50_75": _passes_filter(rsi_w, 50.0, 75.0),                      # 9
        "rsi_month_floor": (rsi_m is not None and rsi_m > _rsi_month_floor(bucket)),  # 10
    }
    evaluated = [v for v in results.values() if v is not None]
    passed = sum(1 for v in evaluated if v)
    # min 9 of 10; if a check is skipped (ADR NULL), still allow exactly one miss
    need = 9 if len(evaluated) == 10 else (len(evaluated) - 1)
    return {"pass": passed >= need, "score": passed, "evaluated": len(evaluated),
            "need": need, "checks": results}


# ── Gate 4: room to run ──────────────────────────────────────────────────────

def _gate4(row: dict) -> dict:
    cmp = _f(row.get("live_close"))
    week_high = _f(row.get("week_high"))
    if cmp is None or week_high is None or cmp <= 0:
        return {"pass": False, "room_pct": None}
    room = (week_high - cmp) / cmp * 100
    return {"pass": room > 0.5, "room_pct": round(room, 2)}


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
    p.pp, p.r1, p.s1,
    td.day_open, td.live_close, td.lb_open, td.lb_close,
    td.today_high, td.today_low, td.avg_today, td.vol_today,
    a3.avg_3bars, a12.avg_12bars,
    h.lo_2d, h.lo_5d, h.week_high, h.avg_3day,
    vden.avg_vol_to_t
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
    SELECT MIN(low)  FILTER (WHERE rn<=2) AS lo_2d,
           MIN(low)  FILTER (WHERE rn<=5) AS lo_5d,
           MAX(high) FILTER (WHERE rn<=7) AS week_high,
           AVG(close) FILTER (WHERE rn<=3) AS avg_3day
    FROM (
        SELECT low, high, close, ROW_NUMBER() OVER (ORDER BY price_date DESC) AS rn
        FROM raw_prices WHERE symbol=m.symbol AND price_date < CURRENT_DATE
    ) x WHERE rn<=7
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
WHERE m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
"""


def _evaluate(row: dict, reversal_cfg: dict, nifty_rsi: Optional[float], adr: Optional[float]) -> dict:
    g1 = _gate1(row, reversal_cfg, nifty_rsi)
    g2 = _gate2(row)
    g3 = _gate3(row, adr, g1["bucket"])
    g4 = _gate4(row)
    signal = g1["pass"] and g2["pass"] and g3["pass"] and g4["pass"]
    return {
        "symbol": row["symbol"],
        "basket": g1["bucket"],
        "signal": signal,
        "gate1_pass": g1["pass"], "gate1_score": g1["score"], "gate1_total": g1["total"],
        "gate2_ma_pass": g2["pass"], "gate2": {k: v for k, v in g2.items() if k != "pass"},
        "gate3_tc_score": g3["score"], "gate3_pass": g3["pass"],
        "gate3_need": g3["need"], "gate3_evaluated": g3["evaluated"],
        "gate4_room_pct": g4["room_pct"], "gate4_pass": g4["pass"],
        "vol_ratio_timenorm": round(_vol_timenorm(row), 3) if _vol_timenorm(row) is not None else None,
        "entry_price": _f(row.get("live_close")),
        "checks": g3["checks"],
    }


def _record_watchlist(signals: list, signal_ts: datetime):
    if not signals:
        return 0
    rows = [
        (s["symbol"], s["basket"], signal_ts, s["entry_price"], s["basket"],
         s["gate1_score"], s["gate2_ma_pass"], s["gate3_tc_score"], s["gate4_room_pct"],
         Json(s["checks"]))
        for s in signals
    ]
    with _conn() as conn, conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO intraday_watchlist
                (symbol, basket, signal_ts, entry_price, gate1_bucket,
                 gate1_score, gate2_ma_pass, gate3_tc_score, gate4_room_pct, checks)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, signal_date) DO NOTHING
        """, rows)
        recorded = cur.rowcount
        conn.commit()
    return recorded


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/api/scanners/intraday")
def scanner_intraday(limit: int = 40):
    """4-gate intraday scan over the active futures universe."""
    now = _ist_now()
    if not _in_window(now):
        return {"scanner": "intraday", "status": "outside_window",
                "window": "09:30-15:15 IST", "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
                "signals": [], "count": 0}

    reversal_cfg = _get_buy_reversal_live_filters()[0]
    nifty_rsi = _nifty_rsi()

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT adr FROM adr_daily WHERE price_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
        ar = cur.fetchone()
        adr = float(ar[0]) if ar and ar[0] is not None else None
        cur.execute(_SCAN_SQL)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    scored = [_evaluate(r, reversal_cfg, nifty_rsi, adr) for r in rows]
    signals = [s for s in scored if s["signal"]]
    signals.sort(key=lambda s: (s["gate3_tc_score"], s["gate1_score"]), reverse=True)

    recorded = _record_watchlist(signals, now)

    return {
        "scanner": "intraday", "status": "ok",
        "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "nifty_rsi": round(nifty_rsi, 1) if nifty_rsi is not None else None,
        "adr": adr, "universe": len(rows),
        "count": len(signals), "recorded_to_watchlist": recorded,
        "signals": signals[:max(1, limit)],
    }


@router.get("/api/scanners/intraday/tc/{symbol}")
def scanner_intraday_tc(symbol: str):
    """Run the 10-check TC for a single symbol at current time."""
    symbol = symbol.upper()
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

    res = _evaluate(row, reversal_cfg, nifty_rsi, adr)
    return {
        "symbol": symbol, "status": "ok",
        "now": now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "signal": res["signal"], "basket": res["basket"],
        "gate1": {"pass": res["gate1_pass"], "score": res["gate1_score"], "total": res["gate1_total"]},
        "gate2": {"pass": res["gate2_ma_pass"], **res["gate2"]},
        "gate3": {"pass": res["gate3_pass"], "score": res["gate3_tc_score"],
                  "need": res["gate3_need"], "evaluated": res["gate3_evaluated"], "checks": res["checks"]},
        "gate4": {"pass": res["gate4_pass"], "room_pct": res["gate4_room_pct"]},
        "vol_ratio_timenorm": res["vol_ratio_timenorm"],
        "entry_price": res["entry_price"],
    }


@router.get("/api/scanners/intraday/watchlist")
def scanner_intraday_watchlist(date: Optional[str] = None):
    """Today's recorded signals with live CMP, PnL%, time since signal."""
    where_date = "w.signal_date = %s" if date else "w.signal_date = CURRENT_DATE"
    params = (date,) if date else ()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"""
            SELECT w.symbol, w.basket, w.signal_date, w.signal_ts, w.entry_price,
                   w.gate1_score, w.gate2_ma_pass, w.gate3_tc_score, w.gate4_room_pct,
                   c.cmp,
                   ROUND(CASE WHEN w.entry_price>0
                        THEN (COALESCE(c.cmp, w.entry_price) - w.entry_price)/w.entry_price*100
                        ELSE 0 END::numeric, 2) AS pnl_pct,
                   EXTRACT(EPOCH FROM (NOW() - w.signal_ts))/60.0 AS mins_since
            FROM intraday_watchlist w
            LEFT JOIN cmp_prices c ON c.symbol = w.symbol
            WHERE {where_date}
            ORDER BY w.signal_ts DESC
        """, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for r in rows:
        if r.get("mins_since") is not None:
            r["mins_since"] = round(float(r["mins_since"]), 1)
        for k in ("entry_price", "cmp", "gate4_room_pct"):
            if r.get(k) is not None:
                r[k] = float(r[k])

    return {"scanner": "intraday_watchlist",
            "date": date or "today", "count": len(rows), "rows": rows}
