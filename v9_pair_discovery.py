"""
V9 Pair Strategy — Pair Discovery
===================================
Finds all valid pairs from the 209 futures universe using 2025 EOD data.

Steps:
  1. Load eligible universe (GVM >= 6, >= 200 trading days in 2025)
  2. Group by segment (same-segment pairs only)
  3. For each pair: Pearson correlation >= 0.75
  4. For passing pairs: Engle-Granger cointegration test p-value < 0.05
  5. Compute OLS hedge ratio (beta)
  6. Store results in pair_universe table

Output table: pair_universe
  symbol_a, symbol_b, segment, correlation, coint_pvalue,
  hedge_ratio, hedge_intercept, discovery_date, is_active

Usage:
  python v9_pair_discovery.py
  or via endpoint: POST /api/v9/discover
"""

import os
import logging
import numpy as np
import pandas as pd
from datetime import date, datetime
from itertools import combinations
from typing import List, Tuple, Optional

import psycopg2
from statsmodels.tsa.stattools import coint
from scipy import stats

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('v9_discovery')

DATABASE_URL = os.environ.get('DATABASE_URL')

# ── Constants (locked) ────────────────────────────────────────────────────────
BACKTEST_START = '2025-01-01'
BACKTEST_END   = '2025-12-31'
GVM_MIN        = 6.0
MIN_DAYS       = 200
CORR_MIN       = 0.75
COINT_PVALUE   = 0.05

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pair_universe (
    id              SERIAL PRIMARY KEY,
    symbol_a        TEXT NOT NULL,
    symbol_b        TEXT NOT NULL,
    segment         TEXT NOT NULL,
    correlation     NUMERIC(6,4),
    coint_pvalue    NUMERIC(8,6),
    hedge_ratio     NUMERIC(10,6),
    hedge_intercept NUMERIC(10,4),
    mean_spread     NUMERIC(10,4),
    std_spread      NUMERIC(10,4),
    discovery_date  DATE NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(symbol_a, symbol_b, discovery_date)
);
CREATE INDEX IF NOT EXISTS idx_pair_universe_segment ON pair_universe(segment);
CREATE INDEX IF NOT EXISTS idx_pair_universe_active  ON pair_universe(is_active, discovery_date DESC);
"""


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()
    log.info("pair_universe table ready")


def load_eligible_universe(conn) -> pd.DataFrame:
    sql = """
        SELECT fu.symbol, gs.segment, gs.gvm_score,
               COUNT(r.price_date) AS trading_days
        FROM futures_universe fu
        JOIN gvm_scores gs ON gs.symbol = fu.symbol
        JOIN raw_prices r  ON r.symbol = fu.symbol
            AND r.price_date BETWEEN %s AND %s
        WHERE fu.is_active = TRUE AND gs.gvm_score >= %s
        GROUP BY fu.symbol, gs.segment, gs.gvm_score
        HAVING COUNT(r.price_date) >= %s
        ORDER BY gs.segment, gs.gvm_score DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (BACKTEST_START, BACKTEST_END, GVM_MIN, MIN_DAYS))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    df = pd.DataFrame(rows, columns=cols)
    log.info(f"Eligible universe: {len(df)} stocks across {df['segment'].nunique()} segments")
    return df


def load_price_series(conn, symbols: List[str]) -> pd.DataFrame:
    sql = """
        SELECT symbol, price_date, close FROM raw_prices
        WHERE symbol = ANY(%s) AND price_date BETWEEN %s AND %s
        ORDER BY price_date ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (symbols, BACKTEST_START, BACKTEST_END))
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=['symbol', 'price_date', 'close'])
    df['price_date'] = pd.to_datetime(df['price_date'])
    pivot = df.pivot(index='price_date', columns='symbol', values='close').dropna(how='all')
    log.info(f"Price matrix: {pivot.shape[0]} days × {pivot.shape[1]} symbols")
    return pivot


def compute_correlation(s_a: pd.Series, s_b: pd.Series) -> float:
    aligned = pd.concat([s_a, s_b], axis=1).dropna()
    if len(aligned) < MIN_DAYS:
        return 0.0
    return float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))


def compute_cointegration(s_a: pd.Series, s_b: pd.Series) -> Tuple[float, float]:
    """Engle-Granger cointegration. Returns (p_value, hedge_ratio beta)."""
    aligned = pd.concat([s_a, s_b], axis=1).dropna()
    if len(aligned) < MIN_DAYS:
        return 1.0, 1.0
    a_vals = aligned.iloc[:, 0].values
    b_vals = aligned.iloc[:, 1].values
    slope, _, _, _, _ = stats.linregress(b_vals, a_vals)
    try:
        _, p_value, _ = coint(a_vals, b_vals)
    except Exception:
        return 1.0, float(slope)
    return float(p_value), float(slope)


def compute_spread_stats(s_a: pd.Series, s_b: pd.Series,
                         beta: float) -> Tuple[float, float, float]:
    """Returns (mean_spread, std_spread, intercept)."""
    aligned = pd.concat([s_a, s_b], axis=1).dropna()
    a_vals  = aligned.iloc[:, 0].values
    b_vals  = aligned.iloc[:, 1].values
    _, intercept, _, _, _ = stats.linregress(b_vals, a_vals)
    spread = a_vals - beta * b_vals
    return float(np.mean(spread)), float(np.std(spread)), float(intercept)


def discover_pairs(conn) -> List[dict]:
    universe    = load_eligible_universe(conn)
    all_symbols = universe['symbol'].tolist()
    prices      = load_price_series(conn, all_symbols)

    results      = []
    total_tested = 0
    corr_passed  = 0
    coint_passed = 0

    for segment, group in universe.groupby('segment'):
        syms = group['symbol'].tolist()
        if len(syms) < 2:
            continue
        log.info(f"Segment: {segment} — {len(syms)} stocks, "
                 f"{len(syms)*(len(syms)-1)//2} pairs")

        for sym_a, sym_b in combinations(syms, 2):
            total_tested += 1
            if sym_a not in prices.columns or sym_b not in prices.columns:
                continue
            aligned = pd.concat([prices[sym_a], prices[sym_b]], axis=1).dropna()
            if len(aligned) < MIN_DAYS:
                continue

            s_a = aligned.iloc[:, 0]
            s_b = aligned.iloc[:, 1]

            corr = compute_correlation(s_a, s_b)
            if corr < CORR_MIN:
                continue
            corr_passed += 1

            p_value, beta = compute_cointegration(s_a, s_b)
            if p_value >= COINT_PVALUE:
                continue
            coint_passed += 1

            mean_spread, std_spread, intercept = compute_spread_stats(s_a, s_b, beta)

            results.append({
                'symbol_a':        sym_a,
                'symbol_b':        sym_b,
                'segment':         segment,
                'correlation':     round(corr, 4),
                'coint_pvalue':    round(p_value, 6),
                'hedge_ratio':     round(beta, 6),
                'hedge_intercept': round(intercept, 4),
                'mean_spread':     round(mean_spread, 4),
                'std_spread':      round(std_spread, 4),
                'discovery_date':  date(2025, 1, 1),
            })
            log.info(f"  VALID: {sym_a}/{sym_b} | corr={corr:.3f} "
                     f"p={p_value:.4f} β={beta:.4f}")

    log.info(f"\nDiscovery: tested={total_tested} "
             f"corr_pass={corr_passed} coint_pass={coint_passed} "
             f"valid={len(results)}")
    return results


def store_pairs(conn, pairs: List[dict]) -> int:
    if not pairs:
        log.warning("No valid pairs to store")
        return 0
    with conn.cursor() as cur:
        for p in pairs:
            cur.execute("""
                INSERT INTO pair_universe
                (symbol_a,symbol_b,segment,correlation,coint_pvalue,
                 hedge_ratio,hedge_intercept,mean_spread,std_spread,
                 discovery_date,is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                ON CONFLICT (symbol_a,symbol_b,discovery_date) DO UPDATE SET
                    correlation=EXCLUDED.correlation,
                    coint_pvalue=EXCLUDED.coint_pvalue,
                    hedge_ratio=EXCLUDED.hedge_ratio,
                    hedge_intercept=EXCLUDED.hedge_intercept,
                    mean_spread=EXCLUDED.mean_spread,
                    std_spread=EXCLUDED.std_spread,
                    is_active=TRUE
            """, (p['symbol_a'],p['symbol_b'],p['segment'],
                  p['correlation'],p['coint_pvalue'],p['hedge_ratio'],
                  p['hedge_intercept'],p['mean_spread'],p['std_spread'],
                  p['discovery_date']))
    conn.commit()
    log.info(f"Stored {len(pairs)} valid pairs in pair_universe")
    return len(pairs)


def run_discovery() -> dict:
    conn = get_conn()
    ensure_schema(conn)
    log.info(f"V9 Pair Discovery | {BACKTEST_START} to {BACKTEST_END}")
    pairs  = discover_pairs(conn)
    stored = store_pairs(conn, pairs)
    summary = {}
    for p in pairs:
        summary[p['segment']] = summary.get(p['segment'], 0) + 1
    conn.close()
    return {
        "status":      "ok",
        "valid_pairs": stored,
        "by_segment":  summary,
        "period":      f"{BACKTEST_START} to {BACKTEST_END}",
        "filters":     {"gvm_min": GVM_MIN, "min_days": MIN_DAYS,
                        "corr_min": CORR_MIN, "coint_pvalue": COINT_PVALUE},
    }


if __name__ == '__main__':
    print(run_discovery())
