"""cc#700: T+1 result-scrape universe gate.

BOSS RULE (founder 26-Jul, session_log id=9178, SCRAPE_UNIVERSE_TOP500_NSE_V1):
the T+1 result-scrape universe = NSE-LISTED companies in the TOP 500 BY MARKET CAP
only (from screener_raw.market_cap; NSE code must be non-numeric — BSE-only numeric
codes are excluded). Everything outside the top-500 NSE set is NEVER queued for a
scrape; its Result Analysis serves from the screener_raw fallback ("limited review").

Scope: scrape stack is PROTOTYPE tier — quality over coverage. Full-universe production
data arrives with the vendor transition (CMOTS, post metrics-freeze). This module is the
single gate every T+1-scrape enqueue path calls at insert time (result_corner lifecycle
reconcile + ops_metrics_pipeline.run_t1_refresh). Ops-metrics EXTRACTION entry (the
Saturday detector over already-stored doc_texts) stays open-flow and does NOT call this.
"""

TOP_N = 500


def in_scrape_universe(cur, symbol: str) -> bool:
    """True iff `symbol` is NSE-listed (non-numeric nse_code) AND ranks within the top-500
    by market_cap in screener_raw at call time. Purely-numeric codes (BSE-only) and rank>500
    are excluded. Ranking is evaluated live so the universe tracks the latest screener upload."""
    sym = (symbol or "").strip().upper()
    if not sym or sym.isdigit():
        return False
    cur.execute("""
        WITH ranked AS (
            SELECT UPPER(nse_code) AS code,
                   ROW_NUMBER() OVER (ORDER BY market_cap DESC NULLS LAST) AS rnk
            FROM screener_raw
            WHERE nse_code IS NOT NULL AND nse_code <> '' AND nse_code !~ '^[0-9]+$'
              AND market_cap IS NOT NULL)
        SELECT 1 FROM ranked WHERE code=%s AND rnk <= %s
    """, (sym, TOP_N))
    return cur.fetchone() is not None


def log_universe_skip(cur, symbol: str, ex_date=None, source: str = "") -> bool:
    """Record an out-of-universe scrape-skip ONCE per symbol (not per night). Returns True the
    first time a symbol is skipped (freshly logged), False on repeat skips. Backed by the
    scrape_universe_skips table so the log-once contract survives across runs."""
    cur.execute("""CREATE TABLE IF NOT EXISTS scrape_universe_skips (
        symbol TEXT PRIMARY KEY,
        reason TEXT DEFAULT 'universe_top500',
        ex_date DATE,
        source TEXT,
        first_skipped_at TIMESTAMPTZ DEFAULT NOW())""")
    cur.execute("""INSERT INTO scrape_universe_skips (symbol, reason, ex_date, source)
                   VALUES (%s,'universe_top500',%s,%s)
                   ON CONFLICT (symbol) DO NOTHING
                   RETURNING 1""", ((symbol or "").strip().upper(), ex_date, source))
    return cur.fetchone() is not None
