"""cc#700: T+1 result-scrape universe gate.

BOSS RULE (founder 26-Jul, session_log id=9178):
V1 (SCRAPE_UNIVERSE_TOP500_NSE_V1 — rule id kept verbatim as the locked session_log
identifier; the CUTOFF it names is superseded below): scrape universe = NSE-LISTED
companies in the TOP 750 BY MARKET CAP (from screener_raw.market_cap; NSE code must be
non-numeric). cc#814 (founder-frozen 02-Aug-2026) raised the cutoff 500 -> 750.
V2 (universe_v2_inclusive, cc#701): INCLUSIVE UNION — a symbol qualifies if it is
(a) NSE non-numeric AND top-750 by market_cap, OR (b) already present in
sector_ops_metrics (any row) — so ops-tracked smalls (e.g. AMC_Wealth names below
rank 750) stay scrape-eligible.
V3 (SCRAPE_UNIVERSE_TOP750_CANON_V1, cc#2091, founder-ruled 14-Sep-2026 — verbatim:
"now lets keep everything consistent which is remembrable quarterly result scrape 750
by mcap, financial data history 750 by mcap, gvm history 750 by mcap rest all cleanup"):
the cc#701 sector_ops_metrics UNION is DROPPED. The universe is a CLEAN top-750 by
market cap, nothing else — one number, one definition, reused by every table that
claims to track "the universe": this scrape gate, fundamentals_history retention, and
gvm_history retention/backfill (cc#2092). If a future question starts with "how many
companies", the answer is 750. Ops-tracked smalls outside the top 750 are no longer
scrape-eligible via this gate — a real, intentional narrowing, not an oversight.

Scope: scrape stack is PROTOTYPE tier — quality over coverage. Full-universe production
data arrives with the vendor transition (CMOTS, post metrics-freeze). This module is the
single gate every T+1-scrape enqueue path calls at insert time (result_corner lifecycle
reconcile + ops_metrics_pipeline.run_t1_refresh). Ops-metrics EXTRACTION entry (the
Saturday detector over already-stored doc_texts) stays open-flow and does NOT call this.

RETENTION IS ONE-WAY (cc#2091 item 4): this gate controls what is newly SCRAPED and
COMPUTED going forward — a symbol that drops out of the live top-750 simply stops being
enqueued here, automatically, by the LIMIT/ORDER BY re-evaluating live. It does NOT mean
that symbol's EXISTING history should be deleted — deleting it would destroy exactly the
point-in-time record a backtest needs and reintroduce survivorship bias by another door.
No job may delete fundamentals_history / gvm_history rows on a "symbol currently outside
top-750" test alone. The one-time cc#2091 cleanup (14-Sep-2026) is the sole exception,
founder-authorized, and is not a pattern to repeat — see universe_retention_report()
below for the read-only, no-delete way to monitor drift going forward.
"""

TOP_N = 750


def in_scrape_universe(cur, symbol: str) -> bool:
    """True iff `symbol` qualifies for the T+1 scrape (cc#2091 V3, clean top-750, no union):
    NSE-listed (non-numeric nse_code) AND in the top-750 by market_cap in screener_raw.
    Purely-numeric codes (BSE-only) are always excluded. Ranking is evaluated live so the
    top-750 tranche tracks the latest screener upload."""
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
        SELECT 1 WHERE EXISTS (SELECT 1 FROM ranked WHERE code=%s)
    """, (TOP_N, sym))
    return cur.fetchone() is not None


def universe_symbols(cur) -> set:
    """cc#741/cc#2091: the FULL scrape-eligible set (cc#2091 V3, clean top-750) in ONE query —
    for enqueue-side pre-filtering. Callers filter their todo list against this instead of
    re-implementing the ranking inline (the per-symbol in_scrape_universe() stays the
    authoritative single-symbol gate). Same definition as in_scrape_universe: NSE non-numeric
    top-750 by market_cap; numeric BSE-only codes excluded by the non-numeric filter."""
    cur.execute("""
        SELECT UPPER(nse_code) AS code
        FROM screener_raw
        WHERE nse_code IS NOT NULL AND nse_code <> '' AND nse_code !~ '^[0-9]+$'
          AND market_cap IS NOT NULL
        ORDER BY market_cap DESC NULLS LAST
        LIMIT %s
    """, (TOP_N,))
    return {r[0] for r in cur.fetchall()}


# cc#774 / UNIVERSE_DENOMINATOR_RULE (session_log id=207, locked 01-Aug): the SQL-joinable form of the
# SAME definition as universe_symbols() above, for queries that must JOIN the universe rather than
# pre-filter a Python list. Use this in EVERY coverage/backlog/missing query — the 01-Aug false alarm
# (reported "48% coverage / 214 missing"; truth 97.8% / 6) came from a query hand-rolling
# `EXISTS (SELECT 1 FROM screener_raw ...)`, which is the ~1801-row screener, NOT this universe.
# cc#2091 (14-Sep): dropped the sector_ops_metrics UNION — clean top-750 only, matching
# in_scrape_universe/universe_symbols above exactly (one definition, three call shapes).
#
#   Usage:  cur.execute("WITH " + UNIVERSE_CTE + ", reporters AS (... JOIN scrape_universe u ON ...) ...")
#
# NEVER quote a coverage number without stating denominator + window.
UNIVERSE_CTE = f"""
    scrape_universe AS (
        SELECT UPPER(nse_code) AS symbol
        FROM screener_raw
        WHERE nse_code IS NOT NULL AND nse_code <> '' AND nse_code !~ '^[0-9]+$'
          AND market_cap IS NOT NULL
        ORDER BY market_cap DESC NULLS LAST
        LIMIT {TOP_N}
    )"""


def universe_retention_report(cur) -> dict:
    """cc#2091 item 4 (RETENTION GUARD) — read-only monitoring, never a delete. Reuses
    universe_symbols() (the ONE definition, never re-derived) to report:
      - universe_count: the live scrape-eligible count (should read 750 barring a screener gap).
      - fh_symbols / gh_symbols: distinct symbols currently in fundamentals_history / gvm_history.
      - fh_outside / gh_outside: how many of those are OUTSIDE the current universe — EXPECTED
        and healthy for a symbol that has since dropped out of the top-750 (its history is kept
        by design, see the module docstring's RETENTION IS ONE-WAY note) — informational, not an
        alarm on its own.
      - under_covered: current-universe symbols with ZERO fundamentals_history rows at all — this
        IS the number worth watching; a top-750 company the scraper has never once reached.
    No row is touched. Call this from a health page or a periodic log line; it never deletes."""
    universe = universe_symbols(cur)
    cur.execute("SELECT DISTINCT UPPER(symbol) FROM fundamentals_history")
    fh_symbols = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT UPPER(symbol) FROM gvm_history")
    gh_symbols = {r[0] for r in cur.fetchall()}
    return {
        "universe_count": len(universe),
        "fh_symbols": len(fh_symbols),
        "fh_outside_universe": len(fh_symbols - universe),
        "gh_symbols": len(gh_symbols),
        "gh_outside_universe": len(gh_symbols - universe),
        "under_covered": sorted(universe - fh_symbols),
        "under_covered_count": len(universe - fh_symbols),
    }


def log_universe_skip(cur, symbol: str, ex_date=None, source: str = "") -> bool:
    """Record an out-of-universe scrape-skip ONCE per symbol (not per night). Returns True the
    first time a symbol is skipped (freshly logged), False on repeat skips. Backed by the
    scrape_universe_skips table so the log-once contract survives across runs."""
    # cc#814: the reason string is DERIVED from TOP_N rather than hardcoded, so it can never again
    # drift out of step with the gate it describes. Rows already written keep 'universe_top500' —
    # that is historically accurate for the cutoff in force when they were skipped, and rewriting
    # them would falsify the log.
    reason = f"universe_top{TOP_N}"
    cur.execute("""CREATE TABLE IF NOT EXISTS scrape_universe_skips (
        symbol TEXT PRIMARY KEY,
        reason TEXT,
        ex_date DATE,
        source TEXT,
        first_skipped_at TIMESTAMPTZ DEFAULT NOW())""")
    cur.execute("""INSERT INTO scrape_universe_skips (symbol, reason, ex_date, source)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (symbol) DO NOTHING
                   RETURNING 1""", ((symbol or "").strip().upper(), reason, ex_date, source))
    return cur.fetchone() is not None
