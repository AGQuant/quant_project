"""
V6 Filter Engine — Scorr / Project Quant
=========================================
Shadow engine to V5. Reads from v6_filters (backtest-optimized thresholds),
writes to v6_qualified. Compute logic reused from v5_engine — same metrics,
different thresholds — so v5_metrics is the shared source of truth.

Paper-trade comparison: compare_v5_v6() returns side-by-side qualified stocks
per signal type. Run for 2 weeks before deciding live cutover.

Source: v6_filters seeded from run_20260525_000300 FINAL_OPTIMIZED config.
"""

import logging
import json as _json
from datetime import date
from typing import Optional, Dict, List, Tuple

from v5_engine import compute_metrics_for_symbol, store_metrics, evaluate_stock

log = logging.getLogger("scorr.v6")

V6_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS v6_filters (
    id SERIAL PRIMARY KEY,
    signal_type TEXT NOT NULL,
    metric TEXT NOT NULL,
    min_val NUMERIC,
    max_val NUMERIC,
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(signal_type, metric)
);

CREATE TABLE IF NOT EXISTS v6_qualified (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    score_date DATE NOT NULL,
    gvm_score NUMERIC,
    cmp NUMERIC,
    metrics JSONB,
    qualified_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol, signal_type, score_date)
);
CREATE INDEX IF NOT EXISTS idx_v6_qual_date_type ON v6_qualified(score_date DESC, signal_type);
"""


def load_v6_filters(conn) -> Dict[str, Dict[str, Tuple[Optional[float], Optional[float]]]]:
    """Load filter config from v6_filters table. Returns {signal_type: {metric: (min, max)}}."""
    filters = {}
    with conn.cursor() as cur:
        cur.execute("SELECT signal_type, metric, min_val, max_val FROM v6_filters")
        for sig, metric, mn, mx in cur.fetchall():
            filters.setdefault(sig, {})[metric] = (
                float(mn) if mn is not None else None,
                float(mx) if mx is not None else None,
            )
    return filters


def store_v6_qualified(conn, symbol: str, signal_type: str, score_date: date,
                        metrics: Dict, cmp: Optional[float] = None):
    """Insert/update qualified stock into v6_qualified."""
    with conn.cursor() as cur:
        clean_metrics = {k: float(v) if isinstance(v, (int, float)) and v is not None else None
                         for k, v in metrics.items()
                         if k not in ("symbol", "score_date")}
        cur.execute("""
            INSERT INTO v6_qualified (symbol, signal_type, score_date, gvm_score, cmp, metrics)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (symbol, signal_type, score_date) DO UPDATE SET
                gvm_score = EXCLUDED.gvm_score, cmp = EXCLUDED.cmp, metrics = EXCLUDED.metrics
        """, (symbol, signal_type, score_date, metrics.get("gvm_score"), cmp,
              _json.dumps(clean_metrics)))
        conn.commit()


def _load_metrics_from_db(conn, symbol: str, target_date: date) -> Optional[Dict]:
    """Read pre-computed metrics from v5_metrics. Returns None if absent."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT gvm_score, dma_50, dma_200, rsi_month, rsi_weekly,
                   month_return, week_return, year_return,
                   sector_day, sector_week, month_index, week_index_52, range_1d
            FROM v5_metrics WHERE symbol = %s AND score_date = %s
        """, (symbol, target_date))
        row = cur.fetchone()
    if not row:
        return None
    keys = ["gvm_score", "dma_50", "dma_200", "rsi_month", "rsi_weekly",
            "month_return", "week_return", "year_return",
            "sector_day", "sector_week", "month_index", "week_index_52", "range_1d"]
    out = {"symbol": symbol, "score_date": target_date}
    for i, k in enumerate(keys):
        out[k] = float(row[i]) if row[i] is not None else None
    return out


def run_v6_engine(conn, symbols: List[str] = None, target_date: date = None,
                  recompute: bool = False) -> Dict:
    """
    Run V6 AND-gate against v6_filters thresholds.
    
    Default: reads from v5_metrics (must already be computed for target_date).
    recompute=True: recomputes from raw_prices via v5_engine.compute_metrics_for_symbol.
    """
    target_date = target_date or date.today()

    if symbols is None:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM v5_signals ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]

    filters = load_v6_filters(conn)
    if not filters:
        return {"error": "v6_filters table empty", "engine": "v6"}

    with conn.cursor() as cur:
        cur.execute("DELETE FROM v6_qualified WHERE score_date = %s", (target_date,))
        conn.commit()

    cmp_map = {}
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, cmp FROM cmp_prices")
        cmp_map = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}

    results = {"date": str(target_date), "engine": "v6",
               "source_mode": "recompute" if recompute else "v5_metrics",
               "symbols_processed": 0, "symbols_skipped": 0,
               "Buy_Reversal": 0, "Buy_Momentum": 0,
               "Sell_Reversal": 0, "Sell_Momentum": 0,
               "errors": []}

    for sym in symbols:
        try:
            if recompute:
                metrics = compute_metrics_for_symbol(conn, sym, target_date)
                store_metrics(conn, metrics)
            else:
                metrics = _load_metrics_from_db(conn, sym, target_date)
                if metrics is None:
                    results["symbols_skipped"] += 1
                    continue
            
            results["symbols_processed"] += 1

            for signal_type, sig_filters in filters.items():
                if evaluate_stock(metrics, sig_filters):
                    store_v6_qualified(conn, sym, signal_type, target_date,
                                        metrics, cmp_map.get(sym))
                    results[signal_type] = results.get(signal_type, 0) + 1
        except Exception as e:
            results["errors"].append(f"{sym}: {str(e)[:100]}")
            log.warning(f"V6 engine error on {sym}: {e}")

    log.info(f"V6 engine done: {results}")
    return results


def compare_v5_v6(conn, score_date: date = None) -> Dict:
    """
    Paper-trade comparison: V5 vs V6 qualified stocks per signal type.
    Returns counts, intersection, and exclusive sets per signal.
    """
    score_date = score_date or date.today()
    
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 'v5' AS engine, signal_type, symbol FROM v5_qualified WHERE score_date = %s
            UNION ALL
            SELECT 'v6' AS engine, signal_type, symbol FROM v6_qualified WHERE score_date = %s
        """, (score_date, score_date))
        rows = cur.fetchall()

    by_sig: Dict[str, Dict[str, set]] = {}
    for engine, sig, sym in rows:
        by_sig.setdefault(sig, {"v5": set(), "v6": set()})[engine].add(sym)

    out = {"date": str(score_date), "summary": {}}
    all_sigs = ["Buy_Momentum", "Buy_Reversal", "Sell_Momentum", "Sell_Reversal"]
    for sig in all_sigs:
        sets = by_sig.get(sig, {"v5": set(), "v6": set()})
        v5_syms = sets["v5"]
        v6_syms = sets["v6"]
        out["summary"][sig] = {
            "v5_count": len(v5_syms),
            "v6_count": len(v6_syms),
            "common_count": len(v5_syms & v6_syms),
            "v5_only_count": len(v5_syms - v6_syms),
            "v6_only_count": len(v6_syms - v5_syms),
            "common": sorted(v5_syms & v6_syms),
            "v5_only": sorted(v5_syms - v6_syms),
            "v6_only": sorted(v6_syms - v5_syms),
        }
    return out
