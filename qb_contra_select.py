"""
qb_contra_select.py — Contra Value basket (sixth QB) selection/proposal engine.
cc#560, canonical spec session_log id=6104 (CONTRA_VALUE_BASKET_V1_LOCKED) + the 19-Jul min-10
amendment (G>=7, M<=6.5, mcap>1000; V>=7.5 and sector>6 preserved).

DRY-RUN ONLY. Proposes the monthly-rebalance entries for `contra_value` — quality (G) trading cheap
(V) in a healthy sector, momentum washed out (low M) but the turn just starting (above 20-DMA).
Execution stays founder-confirmed. Reads universe_technicals / gvm_scores / raw_prices only.

Spec id=6104 rules (amended) encoded here:
  universe    full universe (universe_technicals latest) JOIN gvm_scores (latest)
  screen      GVM>7 AND G>=7 AND V>=7.5 AND M<=6.5 AND sector-avg-GVM>6 (AVG gvm_score per segment)
              AND above 20-DMA (latest close > AVG(close, last 20 trading days), raw_prices)
              AND mcap>1000 Cr (gvm_scores.market_cap). NO month_index (drops the contra thesis).
  selection   Max 10. N>10 -> top 10 by V SCORE desc (deepest value first; gvm desc tiebreak);
              N<=10 -> hold all. Rs 50k/slot; empty slots = cash.
  exits       (qb_eod_checker, contra_value only) HS1 -20% + GVM<6.8 quality exit + M>=8 profit-take
              (M_RECOVERED — thesis complete, hand off to the momentum baskets); NO HS2, no rank.
"""
import os
import logging

import psycopg

log = logging.getLogger("qb_contra_select")

BASKET      = "contra_value"
CAPITAL     = 500000.0
SLOT_VALUE  = 50000.0
TOP_N       = 10
GVM_MIN     = 7.0     # strict >
G_MIN       = 7.0     # >=
V_MIN       = 7.5     # >=
M_MAX       = 6.5     # <=
SEG_MIN     = 6.0     # strict >  (segment avg gvm_score)
MCAP_MIN    = 1000    # strict >  (Cr, gvm_scores.market_cap)

# gates on gvm_scores + segment-avg CTE + mcap, then above-20DMA computed inline from raw_prices
# over the (small) surviving set. Ordered V desc, then GVM desc (quality tiebreak), then symbol.
_SCREEN_SQL = """
WITH latest AS (
    SELECT symbol, gvm_score, g_score, v_score, m_score, segment, market_cap
    FROM gvm_scores WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
),
segavg AS (
    SELECT segment, AVG(gvm_score) AS seg_avg FROM latest GROUP BY segment
),
base AS (
    SELECT l.symbol, l.v_score, l.g_score, l.m_score, l.gvm_score, l.market_cap, l.segment
    FROM latest l
    JOIN universe_technicals ut ON ut.symbol = l.symbol
                               AND ut.score_date = (SELECT MAX(score_date) FROM universe_technicals)
    JOIN segavg s ON s.segment = l.segment
    WHERE l.gvm_score > %(gvm)s AND l.g_score >= %(g)s AND l.v_score >= %(v)s
      AND l.m_score <= %(m)s AND s.seg_avg > %(seg)s AND l.market_cap > %(mcap)s
),
dma20 AS (   -- above 20-DMA: latest close vs AVG(close, last 20 trading days), candidate set only
    SELECT x.symbol,
           (array_agg(x.close ORDER BY x.price_date DESC))[1]::float AS latest_close,
           AVG(x.close)::float AS avg20
    FROM (
        SELECT r.symbol, r.close, r.price_date,
               ROW_NUMBER() OVER (PARTITION BY r.symbol ORDER BY r.price_date DESC) AS rn
        FROM raw_prices r JOIN base b ON b.symbol = r.symbol
        WHERE r.close IS NOT NULL
    ) x
    WHERE x.rn <= 20
    GROUP BY x.symbol
)
SELECT b.symbol, b.v_score, b.g_score, b.m_score, b.gvm_score, b.market_cap, b.segment,
       (d.latest_close - d.avg20) AS above_20dma
FROM base b
JOIN dma20 d ON d.symbol = b.symbol
WHERE (d.latest_close - d.avg20) > 0
ORDER BY b.v_score DESC, b.gvm_score DESC, b.symbol
"""


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    return round(float(v), 3) if v is not None else None


def propose_rebalance(conn=None, as_of=None):
    """Contra Value entry proposal (dry-run, founder-confirmed to execute). Returns qualifiers +
    the top-10-by-V selection + Rs 50k/slot sizing + current holdings (reference only)."""
    own = conn is None
    if own:
        conn = _conn()
    if as_of is None:
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_DATE")
            as_of = cur.fetchone()[0]
    try:
        with conn.cursor() as cur:
            cur.execute(_SCREEN_SQL, {"gvm": GVM_MIN, "g": G_MIN, "v": V_MIN,
                                      "m": M_MAX, "seg": SEG_MIN, "mcap": MCAP_MIN})
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            qualifiers = [{
                "symbol": r["symbol"], "v": _f(r["v_score"]), "g": _f(r["g_score"]),
                "m": _f(r["m_score"]), "gvm": _f(r["gvm_score"]), "mcap": _f(r["market_cap"]),
                "segment": r["segment"], "above_20dma": _f(r["above_20dma"]),
            } for r in rows]
            n_qual = len(qualifiers)

            # Max 10 slots: top-10 by V (already sorted V desc, gvm desc, symbol) when >10, else all
            entries = qualifiers[:TOP_N]
            for e in entries:
                e["slot_value"] = SLOT_VALUE
            n_sel = len(entries)
            cash = round(CAPITAL - SLOT_VALUE * n_sel, 2)
            note = (f"N={n_qual} > {TOP_N} -> top {TOP_N} by V score"
                    if n_qual > TOP_N else f"N={n_qual} <= {TOP_N} -> hold all")

            cur.execute("SELECT symbol FROM quant_paper_positions "
                        "WHERE basket_name=%s AND status='open'", (BASKET,))
            holdings = [row[0] for row in cur.fetchall()]

        return {
            "as_of": str(as_of), "basket": BASKET, "capital": CAPITAL,
            "slot_value": SLOT_VALUE, "top_n": TOP_N,
            "qualifiers": qualifiers, "n_qualified": n_qual,
            "entries": entries, "n_selected": n_sel, "cash_value": cash,
            "selection_note": note, "holdings": holdings, "entry_only": False,
            "rules": {
                "universe": "universe_technicals (full) JOIN gvm_scores",
                "screen": "GVM>7 AND G>=7 AND V>=7.5 AND M<=6.5 AND seg_avg_GVM>6 AND above_20DMA AND mcap>1000Cr",
                "thesis": "quality (G) cheap (V) in a healthy sector, momentum washed out (low M), turn starting (above 20-DMA)",
                "selection": f"max {TOP_N}; N>{TOP_N} -> top {TOP_N} by V desc (gvm tiebreak); else all",
                "sizing": f"Rs {int(SLOT_VALUE)}/slot; empty slots = cash",
                "exits": "HS1 -20% + GVM<6.8 quality exit + M>=8 profit-take (M_RECOVERED); no HS2, no rank",
            },
        }
    finally:
        if own:
            conn.close()
