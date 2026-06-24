"""
tc_lite_scanner.py — TC Lite intraday SCREENER (cc_task #77).

NOT a trading system. A live 5-min filter that flags futures stocks passing a
simple 5-check TC v3.6-lite gate (LONG or SHORT), saves one signal per
symbol/side/day, then just tracks where price went (intraday high / low /
pivots / % change). NO P&L, NO stop-loss, NO target, NO exit/close logic,
NO alerts, NO paper trade.

Source data (all already computed every 5-min — zero extra engine load):
  v8_metrics (gvm_score, sector_week, day_1d, dma_50, dma_200)
  + v8_paper_pivots (pp/r1/s1 levels only) + cmp_prices (CMP).

LONG  (all AND): C1 gvm>=7.0 · C2 sector_week>0 · C3 day_1d>0 ·
                 C4 dma_50>0 · C5 PP < CMP <= R1 (pivot zone).
SHORT (all AND): C1 gvm>=7.0 · C2 sector_week<0 · C3 day_1d<0 ·
                 C4 dma_200<=2.0 · C5 S1 <= CMP < PP (pivot zone below PP).

Dedup: tc_intraday_signals UNIQUE(symbol, direction, signal_date) — INSERT
ON CONFLICT DO NOTHING. One signal per stock per side per day.
"""

import os
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Json
from fastapi import APIRouter

router = APIRouter()

_DB = os.getenv("DATABASE_URL", "")

GVM_MIN     = 7.0
DMA200_MAX  = 2.0   # SHORT C4: near/below 200 DMA


def _ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _market_open(now=None):
    """09:30–15:15 IST, Mon–Fri."""
    now = now or _ist()
    mins = now.hour * 60 + now.minute
    return now.weekday() < 5 and 570 <= mins <= 915


def _long_checks(r):
    return {
        "C1": (r["gvm"] is not None and r["gvm"] >= GVM_MIN),
        "C2": (r["sec_w"] is not None and r["sec_w"] > 0),
        "C3": (r["day1d"] is not None and r["day1d"] > 0),
        "C4": (r["dma50"] is not None and r["dma50"] > 0),
        "C5": (r["pp"] is not None and r["r1"] is not None and r["cmp"] is not None
               and r["pp"] < r["cmp"] <= r["r1"]),
    }


def _short_checks(r):
    return {
        "C1": (r["gvm"] is not None and r["gvm"] >= GVM_MIN),
        "C2": (r["sec_w"] is not None and r["sec_w"] < 0),
        "C3": (r["day1d"] is not None and r["day1d"] < 0),
        "C4": (r["dma200"] is not None and r["dma200"] <= DMA200_MAX),
        "C5": (r["s1"] is not None and r["pp"] is not None and r["cmp"] is not None
               and r["s1"] <= r["cmp"] < r["pp"]),
    }


def scan_tc_lite():
    """Run one screening pass over the active futures universe. Saves any fresh
    LONG/SHORT signals to tc_intraday_signals. Returns a small summary dict.
    Gated to market hours (09:30–15:15 IST) — call freely from the 5-min tick."""
    now = _ist()
    if not _market_open(now):
        return {"ok": True, "skipped": "outside market hours (09:30-15:15 IST)",
                "ts": now.strftime("%d-%b %H:%M IST")}

    try:
        conn = psycopg.connect(_DB)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}

    inserted = {"LONG": 0, "SHORT": 0}
    scanned = 0
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f.symbol, m.gvm_score, m.sector_week, m.day_1d,
                       m.dma_50, m.dma_200, p.pp, p.r1, p.s1, c.cmp
                FROM futures_universe f
                JOIN v8_metrics m ON m.symbol = f.symbol
                  AND m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                LEFT JOIN v8_paper_pivots p ON p.symbol = f.symbol
                  AND p.pivot_date = (SELECT MAX(pivot_date) FROM v8_paper_pivots)
                LEFT JOIN cmp_prices c ON c.symbol = f.symbol
                WHERE f.is_active = TRUE
            """)
            rows = cur.fetchall()

            to_insert = []
            for row in rows:
                scanned += 1
                r = {"symbol": row[0], "gvm": _f(row[1]), "sec_w": _f(row[2]),
                     "day1d": _f(row[3]), "dma50": _f(row[4]), "dma200": _f(row[5]),
                     "pp": _f(row[6]), "r1": _f(row[7]), "s1": _f(row[8]),
                     "cmp": _f(row[9])}
                if r["cmp"] is None:
                    continue
                for direction, checks in (("LONG", _long_checks(r)),
                                          ("SHORT", _short_checks(r))):
                    if all(checks.values()):
                        to_insert.append((r["symbol"], direction, r["cmp"], r["cmp"],
                                          r["pp"], r["r1"], r["s1"], r["gvm"],
                                          r["sec_w"], r["day1d"],
                                          Json(checks)))

            for rec in to_insert:
                cur.execute("""
                    INSERT INTO tc_intraday_signals
                      (symbol, direction, entry_price, cmp, pp, r1, s1,
                       gvm_score, sector_week, day_1d, checks)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, direction, signal_date) DO NOTHING
                    RETURNING direction
                """, rec)
                got = cur.fetchone()
                if got:
                    inserted[got[0]] += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        conn.close()

    return {"ok": True, "ts": now.strftime("%d-%b %H:%M IST"),
            "scanned": scanned, "new_long": inserted["LONG"],
            "new_short": inserted["SHORT"]}


@router.get("/api/scanners/tc_lite")
def tc_lite_signals():
    """Today's TC Lite signals with LIVE intraday state (high/low/% only)."""
    out = {"date": str(_ist().date()), "count": 0, "signals": []}
    try:
        conn = psycopg.connect(_DB)
    except Exception as e:
        out["_error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.symbol, s.direction,
                       to_char(s.signal_ts AT TIME ZONE 'Asia/Kolkata', 'DD-Mon HH24:MI'),
                       s.entry_price, s.pp, s.r1, s.s1, s.gvm_score,
                       i.hi, i.lo, i.last_close
                FROM tc_intraday_signals s
                LEFT JOIN LATERAL (
                    SELECT MAX(high) AS hi, MIN(low) AS lo,
                           (SELECT close FROM intraday_prices
                            WHERE symbol = s.symbol AND ts::date = CURRENT_DATE
                            ORDER BY ts DESC LIMIT 1) AS last_close
                    FROM intraday_prices
                    WHERE symbol = s.symbol AND ts::date = CURRENT_DATE
                ) i ON TRUE
                WHERE s.signal_date = CURRENT_DATE
                ORDER BY s.signal_ts DESC
            """)
            rows = cur.fetchall()
    finally:
        conn.close()

    def pct(a, b):
        a, b = _f(a), _f(b)
        if a is None or b is None or b == 0:
            return None
        return round((a - b) / b * 100, 2)

    sigs = []
    for r in rows:
        entry = _f(r[3])
        hi, lo, cmp_v = _f(r[8]), _f(r[9]), _f(r[10])
        sigs.append({
            "symbol": r[0], "direction": r[1], "signal_ts": r[2],
            "entry_price": entry, "cmp": cmp_v,
            "current_high": hi, "current_low": lo,
            "high_pct": pct(hi, entry), "low_pct": pct(lo, entry),
            "cmp_chg_pct": pct(cmp_v, entry),
            "pp": _f(r[4]), "r1": _f(r[5]), "s1": _f(r[6]),
            "gvm_score": _f(r[7]),
        })
    out["count"] = len(sigs)
    out["signals"] = sigs
    return out


@router.post("/api/scanners/tc_lite/scan")
def tc_lite_scan_now():
    """Manual trigger — run one screening pass now (off-hours returns skipped)."""
    return scan_tc_lite()
