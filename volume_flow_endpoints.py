"""cc#1368 — VOLUME FLOW: green-bar volume vs red-bar volume over the last N 5-minute ticks.

WHAT THIS MEASURES, STATED HONESTLY. Over the last N equity 5-min bars, the volume that traded
on bars closing UP (green) versus bars closing DOWN (red). A high green share reads as
accumulation, a high red share as distribution. It is a VOLUME read, never an order-book read —
we do not capture bids/asks, so nothing here is called "order flow" (founder rule, this card).

DATA HONESTY RULES BUILT IN:
  * Universe is REGISTRY-DERIVED — futures_universe WHERE is_active — never a hardcoded list
    (ENGINE_LIVENESS_RULE corollary).
  * A symbol with fewer than N/2 bars in the window, or zero total volume across it, is
    EXCLUDED — a flow ratio over a thin or dead window is a fabricated signal.
  * rvol (cc#1438, upgraded cc#1440): today's cumulative volume at the symbol's latest tick ÷
    rvol_profiles.avg_cum_vol at that SAME slot — served by rvol_engine.live_rvol_batch. The
    profile math is the engine's (build_profiles owns it); a symbol whose profile is missing or
    has < MIN_SESSIONS sessions is null — never a fake pace. cc#1440 retired Sprint A's inline
    SQL SUM here for the engine's cumulative-aware batch, removing the one deviation Sprint A
    had to state.
  * vol_p (cc#1440, CANON V2): yesterday's RVOL at the closing slot — the same profile formula
    as rvol, read for the last completed session. Served by rvol_engine.closing_rvol_batch.
  * deliv_ratio (cc#1438): latest delivery_eod.deliv_qty ÷ its trailing 20-day average — the
    EXACT QSR vold derivation (qsr_engine dl CTE, extracted verbatim: rn=1 vs AVG rn 2..21),
    not a third copy of the formula. Null when either side is absent.
  * (killed by cc#1438: vol_x same-clock pace vs yesterday, vol_d D-1/D-2 — both retired by
    VOLUME_METRICS_CANON_V1's kill list.)
  * as_of is the latest tick actually used, so a surface can never present stale as live.

Read-only on intraday_prices + raw_prices + futures_universe + rvol_profiles + delivery_eod.
No engine, no scheduler, no writes.
"""

import os
from datetime import date, datetime

import psycopg
from fastapi import APIRouter, HTTPException

router = APIRouter()


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


_ALLOWED_TICKS = (25, 50, 100, 500)


@router.get("/api/volume-flow")
def volume_flow(ticks: int = 100):
    """Bullish/bearish volume-flow lists over the last `ticks` 5-min bars per symbol.

    Response: {as_of, ticks, universe, shown, excluded_thin, bullish: [...], bearish: [...]}
    Each row: {symbol, flow_ratio, day_chg_pct, week_chg_pct, fut_chg_pct, rvol, vol_p, deliv_ratio,
               green_vol, red_vol}. Qualifiers: bullish flow_ratio >= 0.60 (desc),
    bearish flow_ratio <= 0.40 (asc). Symbols between the bands are computed but not listed —
    a 50/50 tape is noise, not signal. (cc#1438: vol_x/vol_d keys retired for rvol/deliv_ratio
    per VOLUME_METRICS_CANON_V1.1; response shape otherwise unchanged.)
    """
    if ticks not in _ALLOWED_TICKS:
        raise HTTPException(400, f"ticks must be one of {_ALLOWED_TICKS}")
    try:
        with _conn() as conn, conn.cursor() as cur:
            # registry-derived universe, never a hardcoded list
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active ORDER BY symbol")
            syms = [r[0] for r in cur.fetchall()]
            if not syms:
                return {"as_of": None, "ticks": ticks, "universe": 0, "shown": 0,
                        "bullish": [], "bearish": []}

            # the eq sessions the day/futures maths anchor on: today + the prior trading date.
            # (cc#1438: the D-2 date died with vol_d — only rvol/day/fut reads remain, and none
            # of them look further back than the previous session.)
            cur.execute("""
                SELECT DISTINCT ts::date AS d FROM intraday_prices
                WHERE source = 'fyers_eq' ORDER BY d DESC LIMIT 2
            """)
            dts = [r[0] for r in cur.fetchall()]
            today = dts[0] if dts else None
            prev_d = dts[1] if len(dts) > 1 else None

            # ── flow window: last N bars per symbol, one set-based pass ──────────────────────
            cur.execute("""
                WITH b AS (
                    SELECT symbol, ts, open, close, volume,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
                    FROM intraday_prices
                    WHERE source = 'fyers_eq' AND symbol = ANY(%(syms)s)
                )
                SELECT symbol,
                       COUNT(*)                                        AS bars,
                       MAX(ts)                                         AS last_ts,
                       MAX(close) FILTER (WHERE rn = 1)                AS last_close,
                       COALESCE(SUM(volume) FILTER (WHERE close > open), 0) AS green_vol,
                       COALESCE(SUM(volume) FILTER (WHERE close < open), 0) AS red_vol,
                       COALESCE(SUM(volume), 0)                        AS total_vol
                FROM b WHERE rn <= %(n)s
                GROUP BY symbol
            """, {"syms": syms, "n": ticks})
            flow = {r[0]: {"bars": r[1], "last_ts": r[2], "last_close": r[3],
                           "green": int(r[4]), "red": int(r[5]), "total": int(r[6])}
                    for r in cur.fetchall()}

            as_of = max((v["last_ts"] for v in flow.values() if v["last_ts"]), default=None)

            # ── day / week reference closes from the daily table (prev session + 5 back) ─────
            cur.execute("""
                WITH d AS (
                    SELECT DISTINCT price_date FROM raw_prices
                    WHERE price_date < CURRENT_DATE
                    ORDER BY price_date DESC LIMIT 5
                )
                SELECT symbol,
                       MAX(close) FILTER (WHERE price_date = (SELECT MAX(price_date) FROM d)) AS prev_close,
                       MAX(close) FILTER (WHERE price_date = (SELECT MIN(price_date) FROM d)) AS week_close
                FROM raw_prices
                WHERE symbol = ANY(%(syms)s)
                  AND price_date IN (SELECT price_date FROM d)
                GROUP BY symbol
            """, {"syms": syms})
            refs = {r[0]: {"prev": r[1], "week": r[2]} for r in cur.fetchall()}

            # ── futures day change: latest fut bar today vs last fut bar of the prev session ─
            fut = {}
            if today is not None:
                cur.execute("""
                    WITH f AS (
                        SELECT symbol, ts, close,
                               ROW_NUMBER() OVER (PARTITION BY symbol, ts::date
                                                  ORDER BY ts DESC) AS rn
                        FROM intraday_prices
                        WHERE source = 'fyers_fut' AND symbol = ANY(%(syms)s)
                          AND ts::date IN (%(today)s, %(prev)s)
                    )
                    SELECT symbol,
                           MAX(close) FILTER (WHERE ts::date = %(today)s AND rn = 1) AS fut_now,
                           MAX(close) FILTER (WHERE ts::date = %(prev)s  AND rn = 1) AS fut_prev
                    FROM f GROUP BY symbol
                """, {"syms": syms, "today": today, "prev": prev_d})
                fut = {r[0]: {"now": r[1], "prev": r[2]} for r in cur.fetchall()}

            # ── cc#1438 RVOL / cc#1440 VOL P: both read through rvol_engine's own batch forms
            # (live_rvol_batch / closing_rvol_batch — CANON V2, session_log 33843: one formula,
            # two read points). cc#1440 also RETIRES this endpoint's Sprint-A inline SQL (a plain
            # SUM): the engine batch is cumulative-aware (cc#680), which removes the one deviation
            # Sprint A had to state. Profile math stays the engine's; nothing recomputed here.
            from rvol_engine import live_rvol_batch, closing_rvol_batch
            rvl = live_rvol_batch(cur, syms)
            vpb = closing_rvol_batch(cur, syms)
            vpl = {s: (v["value"] if v else None) for s, v in vpb.items()}

            # ── cc#1438 DELIV: latest deliv_qty ÷ trailing 20-day avg — the EXACT QSR vold
            # derivation (qsr_engine dl CTE, rn=1 vs AVG rn 2..21), extracted, not re-derived.
            cur.execute("""
                SELECT symbol,
                       MAX(CASE WHEN rn = 1 THEN deliv_qty END)::numeric AS d0,
                       AVG(deliv_qty) FILTER (WHERE rn BETWEEN 2 AND 21)  AS d20avg
                FROM (
                    SELECT symbol, deliv_qty,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY d DESC) AS rn
                    FROM delivery_eod
                    WHERE deliv_qty IS NOT NULL AND symbol = ANY(%(syms)s)
                ) y WHERE rn <= 21 GROUP BY symbol
            """, {"syms": syms})
            dlv = {}
            for r in cur.fetchall():
                d0, d20 = (float(r[1]) if r[1] is not None else None,
                           float(r[2]) if r[2] is not None else None)
                dlv[r[0]] = round(d0 / d20, 2) if (d0 is not None and d20 and d20 > 0) else None

        rows, thin = [], 0
        for sym in syms:
            f = flow.get(sym)
            if not f or f["bars"] < ticks / 2 or f["total"] == 0:
                thin += 1
                continue
            decided = f["green"] + f["red"]     # doji bars (close == open) carry no direction
            if decided == 0:
                thin += 1
                continue
            ratio = f["green"] / decided
            ref = refs.get(sym) or {}
            lc = float(f["last_close"]) if f["last_close"] is not None else None
            day = (lc / float(ref["prev"]) - 1) * 100 if (lc and ref.get("prev")) else None
            week = (lc / float(ref["week"]) - 1) * 100 if (lc and ref.get("week")) else None
            fu = fut.get(sym) or {}
            fchg = ((float(fu["now"]) / float(fu["prev"]) - 1) * 100
                    if (fu.get("now") and fu.get("prev")) else None)
            rows.append({
                "symbol": sym,
                "flow_ratio": round(ratio, 4),
                "day_chg_pct": round(day, 2) if day is not None else None,
                "week_chg_pct": round(week, 2) if week is not None else None,
                "fut_chg_pct": round(fchg, 2) if fchg is not None else None,
                "rvol": rvl.get(sym),               # cc#1438: canon metric 1 (was vol_x)
                "vol_p": vpl.get(sym),              # cc#1440 (canon V2): yesterday's RVOL at close
                "deliv_ratio": dlv.get(sym),        # cc#1438: canon metric 4 (was vol_d)
                "green_vol": f["green"], "red_vol": f["red"],
            })

        bullish = sorted((r for r in rows if r["flow_ratio"] >= 0.60),
                         key=lambda r: -r["flow_ratio"])
        bearish = sorted((r for r in rows if r["flow_ratio"] <= 0.40),
                         key=lambda r: r["flow_ratio"])
        return {"as_of": as_of.isoformat() if as_of else None, "ticks": ticks,
                "universe": len(syms), "shown": len(rows), "excluded_thin": thin,
                "bullish": bullish, "bearish": bearish}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"volume_flow failed: {e}")
