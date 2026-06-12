"""
gvm_universe_pivots.py — Rolling-5d PP/R1/S1 for the ENTIRE universe (1720+).

Mirrors v8_paper.compute_pivots logic exactly, but iterates over every symbol
in raw_prices (not just futures_universe). Writes to the same v8_paper_pivots
table — schema is shared. The v8 paper engine only consumes pivots for symbols
present in v8_metrics qualified set, so extra rows for non-futures are
harmless (it ignores them).

This is GVM-page-scoped (Model 2) — designed so the company report can render
a pivot range for any stock, not only the 210 futures.

Endpoints:
  POST /api/admin/build_universe_pivots[?for_date=YYYY-MM-DD]
       Builds pivots for all stocks for the given date (default today).
       Auth via X-Admin-Token header if ADMIN_TOKEN is set.
"""

import os
import logging
from datetime import date
from typing import Optional, List, Dict

import psycopg
from fastapi import APIRouter, HTTPException, Header

log = logging.getLogger("scorr.gvm_universe_pivots")

router = APIRouter(tags=["gvm_universe_pivots"])

PIVOT_WINDOW = 5
PIVOT_MIN_DAYS = 3
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _check_admin(token: Optional[str]):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


def compute_universe_pivots(for_date: Optional[date] = None,
                            symbol_filter: Optional[List[str]] = None) -> Dict:
    """
    Build rolling-5d PP/R1/S1 for every symbol in raw_prices (or the filtered
    subset). Inserts on conflict (symbol, pivot_date) so re-running for the
    same day is safe.

    Algorithm matches v8_paper.compute_pivots verbatim:
        bh = MAX(high), bl = MIN(low), bc = most recent close over T-1..T-5
        PP = (bh + bl + bc) / 3
        R1 = 2*PP - bl    S1 = 2*PP - bh
        R2 = PP + (bh-bl) S2 = PP - (bh-bl)
    """
    for_date = for_date or date.today()
    built = 0
    skipped: List[str] = []
    sym_count = 0

    with _conn() as conn:
        # Pull the symbol universe
        with conn.cursor() as cur:
            if symbol_filter:
                cur.execute(
                    "SELECT DISTINCT symbol FROM raw_prices WHERE symbol = ANY(%s) ORDER BY symbol",
                    (symbol_filter,),
                )
            else:
                cur.execute(
                    "SELECT DISTINCT symbol FROM raw_prices ORDER BY symbol"
                )
            symbols = [r[0] for r in cur.fetchall()]
        sym_count = len(symbols)

        # Build one symbol at a time — same shape as v8_paper.compute_pivots.
        for sym in symbols:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT price_date, high, low, close
                    FROM raw_prices
                    WHERE symbol = %s AND price_date < %s
                    ORDER BY price_date DESC
                    LIMIT %s
                    """,
                    (sym, for_date, PIVOT_WINDOW),
                )
                rows = [r for r in cur.fetchall()
                        if r[1] is not None and r[2] is not None and r[3] is not None]

            if len(rows) < PIVOT_MIN_DAYS:
                skipped.append(sym)
                continue

            wend, wstart = rows[0][0], rows[-1][0]
            bh = max(float(r[1]) for r in rows)
            bl = min(float(r[2]) for r in rows)
            bc = float(rows[0][3])
            pp = (bh + bl + bc) / 3.0
            r1 = 2 * pp - bl
            s1 = 2 * pp - bh
            r2 = pp + (bh - bl)
            s2 = pp - (bh - bl)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO v8_paper_pivots
                        (symbol, pivot_date, window_start, window_end,
                         pp, r1, s1, r2, s2,
                         base_high, base_low, base_close, base_days, built_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (symbol, pivot_date) DO UPDATE SET
                        window_start = EXCLUDED.window_start,
                        window_end   = EXCLUDED.window_end,
                        pp = EXCLUDED.pp, r1 = EXCLUDED.r1, s1 = EXCLUDED.s1,
                        r2 = EXCLUDED.r2, s2 = EXCLUDED.s2,
                        base_high = EXCLUDED.base_high, base_low = EXCLUDED.base_low,
                        base_close = EXCLUDED.base_close, base_days = EXCLUDED.base_days,
                        built_at = NOW()
                    """,
                    (sym, for_date, wstart, wend,
                     round(pp, 2), round(r1, 2), round(s1, 2),
                     round(r2, 2), round(s2, 2),
                     bh, bl, bc, len(rows)),
                )
                conn.commit()
            built += 1

    log.info(f"universe pivots built {built}/{sym_count} for {for_date} "
             f"(skipped {len(skipped)})")
    return {
        "status": "ok",
        "pivot_date": str(for_date),
        "symbols_total": sym_count,
        "built": built,
        "skipped": len(skipped),
        "skipped_sample": skipped[:10],
    }


@router.post("/api/admin/build_universe_pivots")
def build_universe_pivots_endpoint(for_date: Optional[str] = None,
                                   x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    d = None
    if for_date:
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(for_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(400, "for_date must be YYYY-MM-DD")
    return compute_universe_pivots(d)
