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
  * deliv_ratio (cc#1444, amends cc#1438): AVG(deliv_qty) over the most recent 3 trading days ÷
    AVG(deliv_qty) over the 20 trading days immediately BEFORE those 3 (rn 4..23 —
    NON-OVERLAPPING by design: if the baseline kept days 1-3, a genuine delivery spike would
    inflate both sides at once and partially cancel itself). Null when either side is absent.
    (The original 1-day/rn 2..21 form came from QSR's dl CTE; QSR itself is retired, cc#1442.)
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


def _flow_window(cur, symbols, ticks):
    """cc#1455: the green/red flow window, extracted from volume_flow() so the Quality Bullish
    deck reuses THIS derivation at ticks=50 instead of a second copy. Last `ticks` 5-min bars
    per symbol, one set-based pass. {symbol: {bars, last_ts, last_close, green, red, total}}."""
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
    """, {"syms": list(symbols), "n": ticks})
    return {r[0]: {"bars": r[1], "last_ts": r[2], "last_close": r[3],
                   "green": int(r[4]), "red": int(r[5]), "total": int(r[6])}
            for r in cur.fetchall()}


def deliv_ratio_batch(cur, symbols):
    """cc#1452 push 5: the cc#1444 Delivery Ratio derivation, extracted so other surfaces (the
    GVM/CIO VolumePanel) reuse THIS function instead of growing a copy. {symbol: ratio-or-None} —
    AVG(deliv_qty) over the most recent 3 trading days ÷ AVG over the 20 days immediately BEFORE
    those 3 (rn 1..3 vs 4..23, non-overlapping; see module docstring). Null-safe both sides."""
    cur.execute("""
        SELECT symbol,
               AVG(deliv_qty) FILTER (WHERE rn BETWEEN 1 AND 3)  AS d3avg,
               AVG(deliv_qty) FILTER (WHERE rn BETWEEN 4 AND 23) AS d20avg
        FROM (
            SELECT symbol, deliv_qty,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY d DESC) AS rn
            FROM delivery_eod
            WHERE deliv_qty IS NOT NULL AND symbol = ANY(%(syms)s)
        ) y WHERE rn <= 23 GROUP BY symbol
    """, {"syms": list(symbols)})
    out = {}
    for r in cur.fetchall():
        d3, d20 = (float(r[1]) if r[1] is not None else None,
                   float(r[2]) if r[2] is not None else None)
        out[r[0]] = round(d3 / d20, 2) if (d3 is not None and d20 and d20 > 0) else None
    return out


@router.get("/api/volume-flow")
def volume_flow(ticks: int = 100):
    """Bullish/bearish volume-flow lists over the last `ticks` 5-min bars per symbol.

    Response: {as_of, ticks, universe, shown, excluded_thin, bullish: [...], bearish: [...]}
    Each row: {symbol, flow_ratio, day_chg_pct, week_chg_pct, fut_chg_pct, rvol, vol_p, deliv_ratio,
               green_vol, red_vol}. Qualifiers (cc#1450): bullish = flow_ratio >= 0.60 AND day AND
    week returns both POSITIVE (desc); bearish = flow_ratio <= 0.40 AND day AND week both NEGATIVE
    (asc); a NULL day/week return excludes the row from either list. Symbols between the bands are
    computed but not listed — a 50/50 tape is noise, not signal. (cc#1438: vol_x/vol_d keys retired
    for rvol/deliv_ratio per VOLUME_METRICS_CANON_V1.1.)
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

            # ── flow window via the shared helper (cc#1455 extracted it — one derivation) ────
            flow = _flow_window(cur, syms, ticks)

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

            # ── cc#1444 DELIV via the shared deliv_ratio_batch above (cc#1452 extracted it so the
            # VolumePanel reuses the same function — one derivation, no copies).
            dlv = deliv_ratio_batch(cur, syms)

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

        # cc#1450 (founder, 30-Aug): the flow window spans 100 5-min bars and can cross calendar
        # days, so a high green share could list a stock whose TODAY is red. Both lists now also
        # require Day AND Week returns to agree with the direction; a NULL on either side excludes
        # the row from BOTH lists (missing data never passes a filter). These rows are filtered,
        # not "thin" — excluded_thin keeps counting data-quality exclusions only.
        def _agree(r, up):
            d, w = r["day_chg_pct"], r["week_chg_pct"]
            if d is None or w is None:
                return False
            return (d > 0 and w > 0) if up else (d < 0 and w < 0)
        bullish = sorted((r for r in rows if r["flow_ratio"] >= 0.60 and _agree(r, True)),
                         key=lambda r: -r["flow_ratio"])
        bearish = sorted((r for r in rows if r["flow_ratio"] <= 0.40 and _agree(r, False)),
                         key=lambda r: r["flow_ratio"])
        return {"as_of": as_of.isoformat() if as_of else None, "ticks": ticks,
                "universe": len(syms), "shown": len(rows), "excluded_thin": thin,
                "bullish": bullish, "bearish": bearish}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"volume_flow failed: {e}")


@router.get("/api/quality-bullish-basis")
def quality_bullish_basis():
    """cc#1455 — Quality Bullish + Basis, two lists for the /m/v10 2-card deck.

    Funnel (founder-validated against live data before the card was filed):
      50-bar flow_ratio >= 0.60 (the SAME _flow_window derivation volume_flow uses — no copy)
      AND day_chg_pct > 0 (same prev-close basis as volume_flow's DAY)
      AND v8_metrics.month_return > 0
      AND v8_metrics.sector_month > 0 (ABSOLUTE positivity, not vs Nifty)
      then split by futures_basis latest-tick basis sign. basis NULL or exactly 0 joins NEITHER
      list (missing/flat never counts as a direction). A zero-row list is a legitimate outcome
      (basis_neg was 0 on the day this shipped) — the card renders that honestly.
    Rows (refinement, task log 11:39): DISPLAY columns cmp (futures close, Buildup's own CMP
    framing) / day_pct / sector_day (v8_metrics live peer day) / oi_chg_pct (the EXACT Buildup
    formula, session-last tick); the gating values (flow50, month_return, sector_month, basis)
    ride along in the payload for transparency + the |basis| default sort but are not shown."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT symbol FROM futures_universe WHERE is_active ORDER BY symbol")
            syms = [r[0] for r in cur.fetchall()]
            if not syms:
                return {"as_of": None, "quality_bullish_basis_pos": [],
                        "quality_bullish_basis_neg": [], "universe": 0}

            flow = _flow_window(cur, syms, 50)
            as_of = max((v["last_ts"] for v in flow.values() if v["last_ts"]), default=None)

            # cc#1462: Vol R rides on every row — rvol_engine's own batch read, never a new copy.
            from rvol_engine import live_rvol_batch
            rvl = live_rvol_batch(cur, syms)

            # prev-session close for day% — same anchor volume_flow's DAY column uses
            cur.execute("""
                SELECT symbol, close FROM (
                    SELECT symbol, close,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY price_date DESC) AS rn
                    FROM raw_prices WHERE symbol = ANY(%(syms)s) AND price_date < CURRENT_DATE
                ) z WHERE rn = 1
            """, {"syms": syms})
            prevs = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}

            # cc#1455 refinement (task log 11:39): sector_day joins the read — it is a DISPLAY
            # column now; month_return/sector_month stay filter-only.
            cur.execute("""
                SELECT DISTINCT ON (symbol) symbol, month_return, sector_month, sector_day
                FROM v8_metrics WHERE symbol = ANY(%(syms)s)
                ORDER BY symbol, score_date DESC
            """, {"syms": syms})
            mets = {r[0]: {"mo": (float(r[1]) if r[1] is not None else None),
                           "sec": (float(r[2]) if r[2] is not None else None),
                           "secday": (float(r[3]) if r[3] is not None else None)}
                    for r in cur.fetchall()}

            # cc#1455 refinement: CMP (futures close — the same framing Futures Buildup's FUT
            # column uses), basis, and OI change %% (the EXACT Buildup formula:
            # oi_chg/NULLIF(oi_prev,0)*100 at the session's last tick) in one session-bounded
            # read, the same sess/l shape v10_buildup itself queries.
            cur.execute("""
                WITH sess AS (
                    SELECT MAX(ts::date) AS d FROM futures_basis
                    WHERE ts::time BETWEEN '09:15' AND '15:30'
                )
                SELECT DISTINCT ON (symbol) symbol, basis, futures_close,
                       ROUND(oi_chg::numeric / NULLIF(oi_prev, 0) * 100, 1) AS oi_chg_pct
                FROM futures_basis, sess
                WHERE symbol = ANY(%(syms)s) AND ts::date = sess.d
                  AND ts::time BETWEEN '09:15' AND '15:30'
                ORDER BY symbol, ts DESC
            """, {"syms": syms})
            fut = {r[0]: {"basis": (float(r[1]) if r[1] is not None else None),
                          "cmp": (float(r[2]) if r[2] is not None else None),
                          "oi": (float(r[3]) if r[3] is not None else None)}
                   for r in cur.fetchall()}

        pos, neg = [], []
        for sym in syms:
            f = flow.get(sym)
            if not f or f["bars"] < 25 or f["total"] == 0:   # same thin-window honesty as volume_flow (n/2)
                continue
            decided = f["green"] + f["red"]
            if decided == 0:
                continue
            fr = f["green"] / decided
            if fr < 0.60:
                continue
            lc = float(f["last_close"]) if f["last_close"] is not None else None
            pv = prevs.get(sym)
            day = ((lc / pv - 1) * 100) if (lc and pv) else None
            if day is None or day <= 0:
                continue
            m = mets.get(sym) or {}
            if m.get("mo") is None or m["mo"] <= 0 or m.get("sec") is None or m["sec"] <= 0:
                continue
            fb = fut.get(sym) or {}
            b = fb.get("basis")
            if b is None or b == 0:
                continue
            # cc#1455 refinement: DISPLAY columns are cmp / day_pct / sector_day / oi_chg_pct;
            # the gating values (flow50, month_return, sector_month, basis) stay in the payload
            # for transparency and the |basis| default sort, but are not row columns any more.
            row = {"symbol": sym,
                   "cmp": fb.get("cmp"), "day_pct": round(day, 2),
                   "sector_day": (round(m["secday"], 2) if m.get("secday") is not None else None),
                   "oi_chg_pct": fb.get("oi"),
                   "rvol": rvl.get(sym),                       # cc#1462: Vol R (shared derivation)
                   "flow50": round(fr, 4), "month_return": round(m["mo"], 2),
                   "sector_month": round(m["sec"], 2), "basis": round(b, 2)}
            (pos if b > 0 else neg).append(row)

        pos.sort(key=lambda r: -abs(r["basis"]))
        neg.sort(key=lambda r: -abs(r["basis"]))
        return {"as_of": as_of.isoformat() if as_of else None, "universe": len(syms),
                "quality_bullish_basis_pos": pos, "quality_bullish_basis_neg": neg}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"quality_bullish_basis failed: {e}")
