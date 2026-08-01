"""cc#700: T+1 result-scrape universe gate.

BOSS RULE (founder 26-Jul, session_log id=9178):
V1 (SCRAPE_UNIVERSE_TOP500_NSE_V1): scrape universe = NSE-LISTED companies in the
TOP 500 BY MARKET CAP (from screener_raw.market_cap; NSE code must be non-numeric).
V2 (universe_v2_inclusive, cc#701): INCLUSIVE UNION — a symbol qualifies if it is
(a) NSE non-numeric AND top-500 by market_cap, OR (b) already present in
sector_ops_metrics (any row) — so ops-tracked smalls (e.g. AMC_Wealth names below
rank 500) stay scrape-eligible. ~613 symbols total. Everything else is NEVER queued
for a scrape; its Result Analysis serves from the screener_raw fallback ("limited
review"). BSE-only numeric codes are always excluded.

Scope: scrape stack is PROTOTYPE tier — quality over coverage. Full-universe production
data arrives with the vendor transition (CMOTS, post metrics-freeze). This module is the
single gate every T+1-scrape enqueue path calls at insert time (result_corner lifecycle
reconcile + ops_metrics_pipeline.run_t1_refresh). Ops-metrics EXTRACTION entry (the
Saturday detector over already-stored doc_texts) stays open-flow and does NOT call this.
"""

TOP_N = 500


def in_scrape_universe(cur, symbol: str) -> bool:
    """True iff `symbol` qualifies for the T+1 scrape (cc#701 inclusive union): NSE-listed
    (non-numeric nse_code) AND in the top-500 by market_cap in screener_raw, OR already present
    in sector_ops_metrics (any row). Purely-numeric codes (BSE-only) are always excluded. Ranking
    is evaluated live so the top-500 tranche tracks the latest screener upload."""
    sym = (symbol or "").strip().upper()
    if not sym or sym.isdigit():
        return False
    cur.execute("""
        WITH ranked AS (
            SELECT UPPER(nse_code) AS code
            FROM screener_raw
            WHERE nse_code IS NOT NULL AND nse_code <> '' AND nse_code !~ '^[0-9]+$'
              AND market_cap IS NOT NULL
            ORDER BY market_cap DESC NULLS LAST
            LIMIT %s)
        SELECT 1
        WHERE EXISTS (SELECT 1 FROM ranked WHERE code=%s)
           OR EXISTS (SELECT 1 FROM sector_ops_metrics WHERE UPPER(symbol)=%s)
    """, (TOP_N, sym, sym))
    return cur.fetchone() is not None


def universe_symbols(cur) -> set:
    """cc#741: the FULL scrape-eligible set (cc#701 inclusive union) in ONE query — for enqueue-side
    pre-filtering. Callers filter their todo list against this instead of re-implementing the ranking
    inline (the per-symbol in_scrape_universe() stays the authoritative single-symbol gate). Same
    definition as in_scrape_universe: NSE non-numeric top-500 by market_cap UNION any sector_ops_metrics
    symbol; numeric BSE-only codes excluded by the non-numeric filter."""
    cur.execute("""
        WITH ranked AS (
            SELECT UPPER(nse_code) AS code
            FROM screener_raw
            WHERE nse_code IS NOT NULL AND nse_code <> '' AND nse_code !~ '^[0-9]+$'
              AND market_cap IS NOT NULL
            ORDER BY market_cap DESC NULLS LAST
            LIMIT %s)
        SELECT code FROM ranked
        UNION
        SELECT UPPER(symbol) FROM sector_ops_metrics WHERE symbol IS NOT NULL
    """, (TOP_N,))
    return {r[0] for r in cur.fetchall()}


# cc#774 / UNIVERSE_DENOMINATOR_RULE (session_log id=207, locked 01-Aug): the SQL-joinable form of the
# SAME definition as universe_symbols() above, for queries that must JOIN the universe rather than
# pre-filter a Python list. Use this in EVERY coverage/backlog/missing query — the 01-Aug false alarm
# (reported "48% coverage / 214 missing"; truth 97.8% / 6) came from a query hand-rolling
# `EXISTS (SELECT 1 FROM screener_raw ...)`, which is the ~1801-row screener, NOT this ~616 universe.
#
#   Usage:  cur.execute("WITH " + UNIVERSE_CTE + ", reporters AS (... JOIN scrape_universe u ON ...) ...")
#
# NEVER quote a coverage number without stating denominator + window.
UNIVERSE_CTE = f"""
    scrape_universe AS (
        SELECT UPPER(nse_code) AS symbol FROM (
            SELECT nse_code FROM screener_raw
            WHERE nse_code IS NOT NULL AND nse_code <> '' AND nse_code !~ '^[0-9]+$'
              AND market_cap IS NOT NULL
            ORDER BY market_cap DESC NULLS LAST
            LIMIT {TOP_N}) r
        UNION
        SELECT UPPER(symbol) FROM sector_ops_metrics WHERE symbol IS NOT NULL
    )"""


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
