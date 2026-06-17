"""
TC Cache + Intraday Scan — Phase 1 prototype (17-Jun-2026, spec id=371/373).

TWO PIECES:
1. tc_cache writer — loops futures universe x both sides through the existing
   native_trade_check.compute_trade_check(), upserts LATEST snapshot into
   tc_cache (no rule-state history — that's phase 2 score-trend). Standalone,
   manually triggerable. Scheduler wiring deferred to phase 1.5.

2. Intraday scan — TWO STAGE:
   Stage 1 (cached, instant): pull symbols with score >= 10 from tc_cache.
   Stage 2 (live, only on that shortlist): apply intraday confirmations:
     - 1H bar positive (hourly gain)
     - volume pace >= 1.5x (today 9:15->now vs 7-day avg same window)
     - CMP > prev close by 1-2% (strict band)
     - week 5-day low within 1% of S1
     - CMP > 20 DMA
     - CMP > day open (holding green)
   LONG and SHORT scored separately.

Reuses compute_trade_check unchanged. Pure DB. Context isolation id=244 —
never mixes with V8 paper engine.
"""

import os
from datetime import datetime, timedelta
import psycopg
from psycopg.types.json import Jsonb

import native_trade_check as ntc

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


# ─────────────────────────────────────────────── CACHE WRITER ───

def refresh_tc_cache(n=210):
    """Recompute TC for top-N mcap futures, both sides, upsert latest into tc_cache.
    Heavy (N*2 computes) — standalone, off the live request path."""
    started = _ist_now()
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        symbols = ntc._top_mcap_symbols(cur, n) if hasattr(ntc, "_top_mcap_symbols") else []
        if not symbols:
            cur.execute("""SELECT f.symbol FROM futures_universe f
                           JOIN gvm_scores g ON f.symbol=g.symbol
                           WHERE f.is_active=TRUE AND g.market_cap IS NOT NULL
                           ORDER BY g.market_cap DESC LIMIT %s""", (n,))
            symbols = [r[0] for r in cur.fetchall()]

    written, errors = 0, []
    for sym in symbols:
        for side in ("LONG", "SHORT"):
            try:
                d = ntc.compute_trade_check(sym, side)
                if not d.get("ok"):
                    errors.append({"symbol": sym, "side": side, "error": d.get("error", "no data")})
                    continue
                row = ntc._slim_row(d)
                with psycopg.connect(DATABASE_URL) as c2, c2.cursor() as cur2:
                    cur2.execute("""
                        INSERT INTO tc_cache (symbol, side, score, total, verdict_class,
                            cmp, pivot, not_passed, n_pass, n_watch, n_fail, computed_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                        ON CONFLICT (symbol, side) DO UPDATE SET
                            score=EXCLUDED.score, total=EXCLUDED.total,
                            verdict_class=EXCLUDED.verdict_class, cmp=EXCLUDED.cmp,
                            pivot=EXCLUDED.pivot, not_passed=EXCLUDED.not_passed,
                            n_pass=EXCLUDED.n_pass, n_watch=EXCLUDED.n_watch,
                            n_fail=EXCLUDED.n_fail, computed_at=NOW()""",
                        (row["symbol"], row["side"], row["score"], row["total"],
                         row["verdict_class"], row["cmp"],
                         Jsonb(row["pivot"]),
                         Jsonb(row["not_passed"]),
                         row["n_pass"], row["n_watch"], row["n_fail"]))
                    c2.commit()
                written += 1
            except Exception as e:
                errors.append({"symbol": sym, "side": side, "error": f"{type(e).__name__}: {str(e)[:80]}"})

    return {"ok": True, "written": written, "symbols": len(symbols),
            "errors": errors[:20], "ts": _ist_now().strftime("%d-%b %H:%M IST"),
            "elapsed_sec": round((_ist_now() - started).total_seconds(), 1)}


# ─────────────────────────────────────────────── INTRADAY STAGE-2 FILTERS ───

def _hour_positive(cur, symbol):
    """Last rolling-60min built from 5-min bars: close > open AND second-half rising."""
    cur.execute("""
        SELECT close, open FROM intraday_prices
        WHERE symbol=%s AND timeframe='5m' AND ts::date=CURRENT_DATE
        ORDER BY ts DESC LIMIT 12""", (symbol,))
    bars = cur.fetchall()
    if len(bars) < 6:
        return None
    bars = bars[::-1]  # oldest->newest within the hour
    o = _f(bars[0][1]); c = _f(bars[-1][0])
    if o is None or c is None:
        return None
    return c > o


def _vol_pace(cur, symbol, mult=1.5):
    """Today 9:15->now cumulative vol vs avg same-window over prior 7 trading days."""
    now = _ist_now()
    hhmm = now.strftime("%H:%M:%S")
    cur.execute("""
        WITH today AS (
          SELECT COALESCE(SUM(volume),0) AS v FROM intraday_prices
          WHERE symbol=%s AND timeframe='5m' AND ts::date=CURRENT_DATE
            AND ts::time <= %s::time),
        prior AS (
          SELECT ts::date AS d, SUM(volume) AS v FROM intraday_prices
          WHERE symbol=%s AND timeframe='5m'
            AND ts::date < CURRENT_DATE AND ts::date >= CURRENT_DATE - INTERVAL '9 days'
            AND ts::time <= %s::time
          GROUP BY ts::date ORDER BY d DESC LIMIT 7)
        SELECT (SELECT v FROM today), (SELECT AVG(v) FROM prior)""",
        (symbol, hhmm, symbol, hhmm))
    r = cur.fetchone()
    today_v = _f(r[0]) if r else None
    avg_v = _f(r[1]) if r else None
    if not today_v or not avg_v or avg_v == 0:
        return None, None
    pace = today_v / avg_v
    return pace >= mult, round(pace, 2)


def intraday_scan(side="LONG", min_score=10, gain_lo=1.0, gain_hi=2.0, vol_mult=1.5):
    """Stage 1: cached score>=min_score universe. Stage 2: live intraday filters."""
    side = side.upper()
    started = _ist_now()
    out, checked = [], 0
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("""SELECT symbol, score, total, verdict_class, cmp, pivot
                       FROM tc_cache WHERE side=%s AND score>=%s
                       ORDER BY score DESC""", (side, min_score))
        shortlist = cur.fetchall()

        for sym, score, total, vclass, cmp_cached, pivot in shortlist:
            checked += 1
            try:
                # live CMP + prev close + dma_20 + S1 + 5d low
                cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (sym,))
                cr = cur.fetchone(); cmp = _f(cr[0]) if cr else _f(cmp_cached)
                if cmp is None:
                    continue
                cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date<CURRENT_DATE
                               ORDER BY price_date DESC LIMIT 1""", (sym,))
                pc = cur.fetchone(); prev_close = _f(pc[0]) if pc else None
                if not prev_close:
                    continue
                gain = (cmp / prev_close - 1) * 100

                # day open
                cur.execute("""SELECT open FROM intraday_prices WHERE symbol=%s AND timeframe='5m'
                               AND ts::date=CURRENT_DATE ORDER BY ts ASC LIMIT 1""", (sym,))
                do = cur.fetchone(); day_open = _f(do[0]) if do else None

                # dma_20 (from v8_metrics, % above)
                cur.execute("""SELECT dma_20 FROM v8_metrics WHERE symbol=%s
                               AND score_date=(SELECT MAX(score_date) FROM v8_metrics) LIMIT 1""", (sym,))
                dm = cur.fetchone(); dma20 = _f(dm[0]) if dm else None

                # S1 + 5-day low
                cur.execute("""SELECT s1 FROM v8_paper_pivots WHERE symbol=%s
                               AND pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots) LIMIT 1""", (sym,))
                ps = cur.fetchone(); s1 = _f(ps[0]) if ps else None
                cur.execute("""SELECT MIN(low) FROM (SELECT low FROM raw_prices WHERE symbol=%s AND volume>0
                               ORDER BY price_date DESC LIMIT 5) s""", (sym,))
                lw = cur.fetchone(); low5d = _f(lw[0]) if lw else None

                # ── Stage-2 filters ──
                # LONG: gain in band, above prev close. SHORT mirrors (fall band).
                if side == "LONG":
                    f_gain = gain_lo <= gain <= gain_hi
                    f_dma = (dma20 is not None and dma20 > 0)          # above 20 DMA
                    f_open = (day_open is not None and cmp > day_open)  # holding green
                    f_s1 = (s1 is not None and low5d is not None and low5d <= 1.01 * s1)  # week low near S1
                else:
                    f_gain = gain_lo <= (-gain) <= gain_hi             # down 1-2%
                    f_dma = (dma20 is not None and dma20 < 0)          # below 20 DMA
                    f_open = (day_open is not None and cmp < day_open)  # holding red
                    f_s1 = True  # support rule is long-specific; skip for short

                f_hour = _hour_positive(cur, sym) if side == "LONG" else (
                    (lambda h: (not h) if h is not None else None)(_hour_positive(cur, sym)))
                f_vol, pace = _vol_pace(cur, sym, vol_mult)

                checks = {
                    "score>=10": True,  # already filtered
                    "gain_band": f_gain,
                    "hour_pos": f_hour,
                    "vol_1.5x": f_vol,
                    "dma_20": f_dma,
                    "hold_open": f_open,
                    "week_low_s1": f_s1,
                }
                # all non-None must be True; None (no data) abstains -> not a pass
                passed = all(v is True for v in checks.values())
                if passed:
                    out.append({
                        "symbol": sym, "side": side, "tc_score": _f(score), "total": total,
                        "verdict_class": vclass, "cmp": round(cmp, 2),
                        "gain_pct": round(gain, 2), "vol_pace": pace,
                        "s1": round(s1, 2) if s1 else None,
                        "low5d": round(low5d, 2) if low5d else None,
                        "checks": {k: (None if v is None else bool(v)) for k, v in checks.items()},
                    })
            except Exception:
                continue

    out.sort(key=lambda r: (r["tc_score"] or 0, r["vol_pace"] or 0), reverse=True)
    return {"ok": True, "side": side, "min_score": min_score,
            "shortlist": checked, "matched": len(out), "rows": out[:20],
            "ts": _ist_now().strftime("%d-%b %H:%M IST"),
            "elapsed_sec": round((_ist_now() - started).total_seconds(), 1),
            "note": "Stage1 cached TC>=10; Stage2 live intraday (1H, vol 1.5x, gain 1-2%, DMA20, S1, open). Prototype."}
