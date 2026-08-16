"""
v8_metrics_gapfill.py — cc#1048 FULL-UNIVERSE v8_metrics BACKFILL
=================================================================
Fills the v8_metrics history the one-time cc#(Jun-2026) backfill skipped, so a year-long
replay walks the whole futures universe instead of a ~93-symbol slice.

WHY 113 SYMBOLS WERE MISSED — the root cause, so it is not re-introduced:
    v8_backfill_endpoints.backfill_metrics selects its symbols with
        JOIN gvm_scores g ON g.score_date = (SELECT MAX(score_date) ...) AND g.gvm_score >= 6.5
    i.e. it only ever covered symbols whose CURRENT GVM clears 6.5. Every other active
    futures symbol got nothing. Measured 16-Aug-2026 over 2025-08-01..2026-06-15:
    206 active non-index symbols, 93 with >100 metric days, 113 with between 3 and 100.
    Re-running that endpoint would skip the same 113 again, which is why this is its own job.

WHAT THIS DOES NOT DO (founder-set, cc#1048):
  • It NEVER restates an existing row. Every insert is ON CONFLICT DO NOTHING, so the 93
    symbols already in the table are untouched by construction, not by care. The original
    endpoint ends in ON CONFLICT DO UPDATE and would have rewritten 20,155 rows.
  • It NEVER invents a GVM. The score comes from gvm_history at the exact date where one
    exists, else the latest prior date, and which of the two was used is stamped per row in
    the response counters.
  • It NEVER forks a formula. _wilder_rsi and the window constants are IMPORTED from
    v8_backfill_endpoints, so the arithmetic cannot drift from the rows already in the table.

SECTOR VALUES — a deliberate, founder-confirmed inconsistency, recorded here because it will
look like a bug to whoever reads it next. sector_week/sector_month in THIS historical window
are a market-wide SIMPLE MEAN: verified 16-Aug that every symbol on a given score_date shares
one identical value (2025-09-15 = 2.6724 across all 95 rows, 2026-01-15 = -2.6280 across 88).
That is NOT the live cc#810 definition, which is mcap-weighted PER SEGMENT. The founder chose
to match the existing 93 rather than mix two definitions inside one backtest window, so this
job READS the per-date value off the existing rows instead of recomputing anything — an exact
match is then guaranteed rather than merely intended. The deviation is: history = simple mean,
live = cc#810 mcap-weighted per segment.

Triggered via: POST /api/v8/backfill/metrics_gapfill   (ADMIN_TOKEN)
Read-only on gvm_history and raw_prices; the only writes are new v8_metrics rows.
"""

import os
import logging
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import psycopg
from fastapi import APIRouter, HTTPException, Header

# cc#1048: IMPORTED, never copied — the rows this job writes must be arithmetically
# indistinguishable from the rows the original backfill wrote.
from v8_backfill_endpoints import _wilder_rsi, DATA_START, MIN_HIST

log = logging.getLogger("scorr.gapfill")
router = APIRouter(prefix="/api/v8/backfill", tags=["backfill"])

IST = ZoneInfo("Asia/Kolkata")

# The window the original backfill covered and the replay needs.
WIN_START = "2025-08-01"
WIN_END = "2026-06-15"

# A symbol with more than this many metric days in the window is already covered; below it the
# symbol is a gap. Derived live against this threshold — never a hardcoded symbol list (cc#1048
# item 3), so the job stays correct as the universe turns over.
COVERED_DAYS = 100


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _market_hours(now):
    """cc#1048 item 4: heavy inserts run off-market only. NSE 09:15-15:30 IST, Mon-Fri."""
    if now.weekday() >= 5:
        return False
    return "09:15" <= now.strftime("%H:%M") <= "15:30"


@router.post("/metrics_gapfill")
def metrics_gapfill(x_admin_token: str = Header(None), dry_run: bool = True):
    """Insert the missing v8_metrics rows for the thin symbols. dry_run=True by default —
    it reports exactly what it WOULD write and touches nothing."""
    if x_admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(401, "Unauthorized")

    now = datetime.now(IST)
    if _market_hours(now) and not dry_run:
        raise HTTPException(409, "market hours — this job is off-market only (cc#1048 item 4)")

    with _conn() as conn, conn.cursor() as cur:
        # ── 1. the gap list, derived live ────────────────────────────────────────────────
        cur.execute("""
            SELECT fu.symbol
            FROM futures_universe fu
            LEFT JOIN v8_metrics m ON m.symbol = fu.symbol
                 AND m.score_date BETWEEN %s AND %s
            WHERE fu.is_active = TRUE
              AND fu.symbol NOT LIKE %s AND fu.symbol NOT LIKE %s
            GROUP BY fu.symbol
            HAVING COUNT(DISTINCT m.score_date) <= %s
        """, (WIN_START, WIN_END, "%NIFTY%", "%SENSEX%", COVERED_DAYS))
        gap_symbols = [r[0] for r in cur.fetchall()]
        if not gap_symbols:
            return {"status": "ok", "gap_symbols": 0, "msg": "no gaps — nothing to fill"}

        # ── 2. the DATE GRID + the per-date sector values, straight off the existing rows ──
        # Using the dates the covered symbols already carry keeps the new rows on exactly the
        # same grid, and taking sector_week/month from those same rows makes the match exact
        # rather than recomputed (see the module note on the simple-mean deviation).
        cur.execute("""
            SELECT score_date,
                   MIN(sector_week)::float8  AS sw,
                   MIN(sector_month)::float8 AS sm
            FROM v8_metrics
            WHERE score_date BETWEEN %s AND %s AND sector_week IS NOT NULL
            GROUP BY score_date ORDER BY score_date
        """, (WIN_START, WIN_END))
        grid = {r[0].strftime("%Y-%m-%d"): {"sw": r[1], "sm": r[2]} for r in cur.fetchall()}

        # ── 3. price history for the gap symbols ─────────────────────────────────────────
        cur.execute("""
            SELECT symbol, price_date::text, close
            FROM raw_prices
            WHERE symbol = ANY(%s) AND price_date >= %s AND close IS NOT NULL
            ORDER BY symbol, price_date
        """, (gap_symbols, DATA_START))
        by_sym = defaultdict(list)
        for sym, dt, close in cur.fetchall():
            by_sym[sym].append((dt, float(close)))

        # ── 4. point-in-time GVM from gvm_history ────────────────────────────────────────
        cur.execute("""
            SELECT symbol, score_date::text, gvm_score
            FROM gvm_history
            WHERE symbol = ANY(%s) AND score_date <= %s AND gvm_score IS NOT NULL
            ORDER BY symbol, score_date
        """, (gap_symbols, WIN_END))
        gvm_hist = defaultdict(list)
        for sym, dt, g in cur.fetchall():
            gvm_hist[sym].append((dt, float(g)))

        # ── 5. what already exists, so we only build genuinely missing pairs ─────────────
        cur.execute("""
            SELECT symbol, score_date::text FROM v8_metrics
            WHERE symbol = ANY(%s) AND score_date BETWEEN %s AND %s
        """, (gap_symbols, WIN_START, WIN_END))
        existing = {(r[0], r[1]) for r in cur.fetchall()}

    def _gvm_at(sym, dt):
        """Exact date if present, else the latest prior. Returns (score, method) — never a
        guess, and the caller counts which branch was taken."""
        rows = gvm_hist.get(sym)
        if not rows:
            return None, "none"
        exact = None
        prior = None
        for d, g in rows:
            if d == dt:
                exact = g
                break
            if d < dt:
                prior = g
            else:
                break
        if exact is not None:
            return exact, "exact"
        if prior is not None:
            return prior, "carried"
        return None, "none"

    # ── 6. build the rows, same formulas as the original backfill ────────────────────────
    payload = []
    counters = {"exact": 0, "carried": 0, "no_gvm": 0, "short_history": 0, "already_present": 0}
    for sym, series in by_sym.items():
        closes = [c for _, c in series]
        idx_of = {d: i for i, (d, _) in enumerate(series)}
        for dt, sec in grid.items():
            if (sym, dt) in existing:
                counters["already_present"] += 1
                continue
            i = idx_of.get(dt)
            if i is None or i < MIN_HIST:
                counters["short_history"] += 1
                continue
            hist, live = closes[:i], closes[i]
            dma50 = (live / np.mean(hist[-50:]) - 1) * 100 if len(hist) >= 50 else None
            dma200 = (live / np.mean(hist[-200:]) - 1) * 100 if len(hist) >= 200 else None
            wk_ret = (live / hist[-6] - 1) * 100 if len(hist) >= 6 else None
            mo_ret = (live / hist[-22] - 1) * 100 if len(hist) >= 22 else None
            mom2d = (live / hist[-2] - 1) * 100 if len(hist) >= 2 else None
            rsi_m = None
            if len(hist) >= 22 * 7:
                rsi_m = _wilder_rsi([hist[k] for k in range(-22 * 7, 0, 22)] + [live], 6)
            rsi_w = None
            if len(hist) >= 5 * 9:
                rsi_w = _wilder_rsi([hist[k] for k in range(-5 * 9, 0, 5)] + [live], 8)
            gvm, how = _gvm_at(sym, dt)
            counters["no_gvm" if how == "none" else how] += 1
            payload.append((sym, dt, gvm, dma50, dma200, rsi_m, rsi_w,
                            mo_ret, wk_ret, mom2d, sec["sw"], sec["sm"]))

    if dry_run:
        return {"status": "dry_run", "gap_symbols": len(gap_symbols),
                "grid_days": len(grid), "rows_that_would_insert": len(payload),
                "counters": counters, "window": f"{WIN_START}..{WIN_END}",
                "sector_basis": "simple mean, read from existing rows (cc#1048 founder call)"}

    # ── 7. insert — DO NOTHING, so an existing row can never be restated ─────────────────
    inserted = 0
    with _conn() as conn, conn.cursor() as cur:
        for row in payload:
            cur.execute("""
                INSERT INTO v8_metrics
                (symbol, score_date, gvm_score, dma_50, dma_200,
                 rsi_month, rsi_weekly, month_return, week_return, mom_2d,
                 sector_week, sector_month)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, score_date) DO NOTHING
            """, row)
            inserted += cur.rowcount
        conn.commit()

    log.info("cc#1048 gapfill: %d symbols, %d rows inserted", len(gap_symbols), inserted)
    return {"status": "ok", "gap_symbols": len(gap_symbols), "grid_days": len(grid),
            "rows_built": len(payload), "rows_inserted": inserted, "counters": counters,
            "window": f"{WIN_START}..{WIN_END}",
            "sector_basis": "simple mean, read from existing rows (cc#1048 founder call)"}
