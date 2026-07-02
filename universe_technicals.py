"""
universe_technicals.py — Scorr V12 (cc#154)
=============================================
Nightly technicals for the FULL GVM universe (~1766 symbols), not just the
209 futures_universe symbols v8_metrics covers. Fills the ~88pct NULL gap on
the V12 screener for non-futures stocks (session_log id=1168 v12_gaps Option A).

Reuses, does NOT redefine, the canonical calculations:
  - RSI/DMA/returns/mom_2d/week_index_52/month_index: v8_engine.compute_metrics_for_symbol()
    (RSI periods Month=6/Week=8/Daily=14 live inside that function — imported by call,
    not copied, so this can never drift from v8_metrics).
  - Rolling-5d pivots PP/R1/R2/S1/S2: same formula + constants (PIVOT_WINDOW,
    PIVOT_MIN_DAYS) as v8_paper.compute_pivots(), imported from v8_paper.

Universe = DISTINCT symbol FROM gvm_scores at latest score_date (the full
scored universe), not futures_universe.

Retention: DAILY HISTORY (symbol, score_date), not latest-only — cc#154
CLAUDE_DECISIONS: ~640K rows/yr, negligible, enables cash-universe backtests.

live 5-min freshness is untouched: v12_endpoints._BASE_SQL still prefers
v8_metrics (5-min-fresh, futures-only) via COALESCE, falling back to this
table's EOD-frozen technicals only where v8_metrics has no row.

TC score stays FUTURES-ONLY per Arpit's 02-Jul decision (cc#154
tc_score_decision) — no TC columns here.
"""
import json
import logging
import time
from datetime import date
from typing import Optional

import v8_engine
from v8_paper import PIVOT_WINDOW, PIVOT_MIN_DAYS

log = logging.getLogger("scorr.universe_technicals")

RUNTIME_ALERT_SECS = 300     # >5min triggers an alert row
MATCH_PCT_ALERT = 99         # <99% overlap-match triggers an alert row
OVERLAP_TOLERANCE = 0.01     # cc#154 correctness test tolerance

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS universe_technicals (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    score_date DATE NOT NULL,
    dma_20 NUMERIC, dma_50 NUMERIC, dma_200 NUMERIC,
    rsi_month NUMERIC, rsi_weekly NUMERIC, daily_rsi NUMERIC,
    week_return NUMERIC, month_return NUMERIC, year_return NUMERIC,
    mom_2d NUMERIC, week_index_52 NUMERIC, month_index NUMERIC,
    pp NUMERIC, r1 NUMERIC, r2 NUMERIC, s1 NUMERIC, s2 NUMERIC,
    computed_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol, score_date)
);
CREATE INDEX IF NOT EXISTS idx_universe_technicals_symbol_date ON universe_technicals(symbol, score_date DESC);
CREATE INDEX IF NOT EXISTS idx_universe_technicals_date ON universe_technicals(score_date DESC);
"""


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def _get_universe(conn):
    """Full GVM-scored universe at its latest score_date (not futures_universe)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT symbol FROM gvm_scores
            WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
        """)
        return sorted(r[0] for r in cur.fetchall())


def _compute_pivot(conn, symbol: str, for_date: date) -> Optional[dict]:
    """Same rolling-window formula/constants as v8_paper.compute_pivots(),
    generalized to any symbol (that function is hardcoded to futures_universe)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT price_date, high, low, close FROM raw_prices
            WHERE symbol=%s AND price_date < %s ORDER BY price_date DESC LIMIT %s
        """, (symbol, for_date, PIVOT_WINDOW))
        rows = [r for r in cur.fetchall() if r[1] and r[2] and r[3]]
    if len(rows) < PIVOT_MIN_DAYS:
        return None
    bh = max(float(r[1]) for r in rows)
    bl = min(float(r[2]) for r in rows)
    bc = float(rows[0][3])
    pp = (bh + bl + bc) / 3.0
    r1 = 2 * pp - bl; s1 = 2 * pp - bh
    r2 = pp + (bh - bl); s2 = pp - (bh - bl)
    return {"pp": round(pp, 2), "r1": round(r1, 2), "r2": round(r2, 2),
            "s1": round(s1, 2), "s2": round(s2, 2)}


def _upsert(conn, row: dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO universe_technicals
                (symbol, score_date, dma_20, dma_50, dma_200,
                 rsi_month, rsi_weekly, daily_rsi,
                 week_return, month_return, year_return,
                 mom_2d, week_index_52, month_index,
                 pp, r1, r2, s1, s2)
            VALUES (%(symbol)s, %(score_date)s, %(dma_20)s, %(dma_50)s, %(dma_200)s,
                    %(rsi_month)s, %(rsi_weekly)s, %(daily_rsi)s,
                    %(week_return)s, %(month_return)s, %(year_return)s,
                    %(mom_2d)s, %(week_index_52)s, %(month_index)s,
                    %(pp)s, %(r1)s, %(r2)s, %(s1)s, %(s2)s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                dma_20=EXCLUDED.dma_20, dma_50=EXCLUDED.dma_50, dma_200=EXCLUDED.dma_200,
                rsi_month=EXCLUDED.rsi_month, rsi_weekly=EXCLUDED.rsi_weekly, daily_rsi=EXCLUDED.daily_rsi,
                week_return=EXCLUDED.week_return, month_return=EXCLUDED.month_return,
                year_return=EXCLUDED.year_return, mom_2d=EXCLUDED.mom_2d,
                week_index_52=EXCLUDED.week_index_52, month_index=EXCLUDED.month_index,
                pp=EXCLUDED.pp, r1=EXCLUDED.r1, r2=EXCLUDED.r2, s1=EXCLUDED.s1, s2=EXCLUDED.s2,
                computed_at=NOW()
        """, row)
    conn.commit()


def _overlap_match_check(conn, target_date: date):
    """cc#154 correctness test: for futures_universe symbols (where both tables
    have a row), ut values must equal v8_metrics EOD-frozen values within
    OVERLAP_TOLERANCE. Returns match pct, or None if no overlap rows exist yet."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ut.symbol, ut.dma_50, vm.dma_50, ut.rsi_weekly, vm.rsi_weekly,
                   ut.week_return, vm.week_return, ut.mom_2d, vm.mom_2d
            FROM universe_technicals ut
            JOIN v8_metrics vm ON vm.symbol = ut.symbol AND vm.score_date = ut.score_date
            WHERE ut.score_date = %s
              AND ut.symbol IN (SELECT symbol FROM futures_universe WHERE is_active = TRUE)
        """, (target_date,))
        rows = cur.fetchall()
    if not rows:
        return None
    ok = 0
    for symbol, ut_dma50, vm_dma50, ut_rsiw, vm_rsiw, ut_wr, vm_wr, ut_m2d, vm_m2d in rows:
        pairs = [(ut_dma50, vm_dma50), (ut_rsiw, vm_rsiw), (ut_wr, vm_wr), (ut_m2d, vm_m2d)]
        matched = all(
            (a is None and b is None) or
            (a is not None and b is not None and abs(float(a) - float(b)) <= OVERLAP_TOLERANCE)
            for a, b in pairs
        )
        if matched:
            ok += 1
    return round(ok / len(rows) * 100, 2)


def run_universe_technicals(conn, target_date: date = None) -> dict:
    ensure_schema(conn)
    target_date = target_date or date.today()
    t0 = time.time()

    symbols = _get_universe(conn)
    computed, null_dma200, errors = 0, 0, []
    for sym in symbols:
        try:
            m = v8_engine.compute_metrics_for_symbol(conn, sym, target_date)
            piv = _compute_pivot(conn, sym, target_date) or {}
            row = {
                "symbol": sym, "score_date": target_date,
                "dma_20": m.get("dma_20"), "dma_50": m.get("dma_50"), "dma_200": m.get("dma_200"),
                "rsi_month": m.get("rsi_month"), "rsi_weekly": m.get("rsi_weekly"),
                "daily_rsi": m.get("daily_rsi"),
                "week_return": m.get("week_return"), "month_return": m.get("month_return"),
                "year_return": m.get("year_return"),
                "mom_2d": m.get("mom_2d"), "week_index_52": m.get("week_index_52"),
                "month_index": m.get("month_index"),
                "pp": piv.get("pp"), "r1": piv.get("r1"), "r2": piv.get("r2"),
                "s1": piv.get("s1"), "s2": piv.get("s2"),
            }
            _upsert(conn, row)
            computed += 1
            if row["dma_200"] is None:
                null_dma200 += 1
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:80]}")

    runtime_secs = round(time.time() - t0, 1)
    match_pct = _overlap_match_check(conn, target_date)
    alert = (runtime_secs > RUNTIME_ALERT_SECS) or (match_pct is not None and match_pct < MATCH_PCT_ALERT)

    details = {
        "score_date": str(target_date),
        "symbols_total": len(symbols),
        "computed": computed,
        "nulls_dma200": null_dma200,
        "errors": len(errors),
        "runtime_secs": runtime_secs,
        "overlap_match_pct": match_pct,
        "sample_errors": errors[:10],
    }
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops_log (session_date, session_ts, category, title, details)
            VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)
        """, ("alert" if alert else "universe_technicals_run",
              "universe_technicals nightly run" + (" - ALERT" if alert else ""),
              json.dumps(details, default=str)))
    conn.commit()
    log.info(f"universe_technicals: {details}")
    return details
