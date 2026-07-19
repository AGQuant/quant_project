"""
qb_breakout_select.py — 52-Week Breakout basket (fifth QB) selection/proposal engine.
cc#559, canonical spec session_log id=6103 (BREAKOUT_52W_BASKET_V1_LOCKED).

DRY-RUN ONLY. Proposes the monthly-rebalance entries for the `breakout_52w` basket — a fast-capture
price-structure signal (near 52w high + at month high + volume accumulation), quality-gated by GVM.
Execution stays founder-confirmed; nothing here auto-buys/sells. Reads universe_technicals /
gvm_scores / raw_prices only.

Spec id=6103 rules encoded here:
  universe    full universe (universe_technicals latest) JOIN gvm_scores (latest)
  screen      GVM >= 7.5 AND week_index_52 >= 90 AND month_index >= 90 AND mcap > 1000 Cr
              (mcap = gvm_scores.market_cap) AND vol_ratio_21 >= 1.0
  vol_ratio   latest volume / AVG(volume over last 21 trading days), from raw_prices — computed
              inline here until cc#558 lands a persisted vol_ratio_21 column.
  selection   N>10 -> top 10 by 1-YEAR RETURN desc; 5<=N<=10 -> hold all N; N<5 -> FULLY CASH.
  sizing      Rs 50,000 per slot always (5L/10); N<10 leaves cash slots.
  exits       (in qb_eod_checker, breakout_52w only) HS1 -15% from entry + GVM<7.2 quality exit;
              NO HS2, no rank exit, no trailing.
"""
import os
import logging

import psycopg

log = logging.getLogger("qb_breakout_select")

BASKET      = "breakout_52w"
CAPITAL     = 500000.0
SLOT_VALUE  = 50000.0     # Rs 5L/10, always
TOP_N       = 10
MIN_N       = 5           # below this -> fully cash (breakouts in a dead tape are traps)
GVM_MIN     = 7.5
WK52_MIN    = 90
MONTH_MIN   = 90
MCAP_MIN    = 1000        # Cr, gvm_scores.market_cap
VOL_RATIO_MIN = 1.0

# universe_technicals + gvm_scores screen, then vol_ratio_21 computed inline from raw_prices over
# the (small) surviving candidate set. Ordered by 1-year return desc (the selection ranking).
_SCREEN_SQL = """
WITH cand AS (
    SELECT ut.symbol, ut.year_return, ut.week_index_52, ut.month_index,
           g.gvm_score, g.market_cap
    FROM universe_technicals ut
    JOIN gvm_scores g ON g.symbol = ut.symbol
                     AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
    WHERE ut.score_date = (SELECT MAX(score_date) FROM universe_technicals)
      AND g.gvm_score >= %(gvm)s
      AND ut.week_index_52 >= %(wk)s
      AND ut.month_index >= %(mon)s
      AND g.market_cap > %(mcap)s
),
vol AS (   -- vol_ratio_21 = latest volume / AVG(last 21 trading-day volumes), candidate set only
    SELECT x.symbol,
           (array_agg(x.volume ORDER BY x.price_date DESC))[1]::float AS latest_vol,
           AVG(x.volume)::float AS avg21
    FROM (
        SELECT r.symbol, r.price_date, r.volume,
               ROW_NUMBER() OVER (PARTITION BY r.symbol ORDER BY r.price_date DESC) AS rn
        FROM raw_prices r JOIN cand c ON c.symbol = r.symbol
        WHERE r.volume IS NOT NULL
    ) x
    WHERE x.rn <= 21
    GROUP BY x.symbol
)
SELECT c.symbol, c.year_return, c.week_index_52, c.month_index, c.gvm_score, c.market_cap,
       (v.latest_vol / NULLIF(v.avg21, 0)) AS vol_ratio_21
FROM cand c
LEFT JOIN vol v ON v.symbol = c.symbol
WHERE (v.latest_vol / NULLIF(v.avg21, 0)) >= %(vr)s
ORDER BY c.year_return DESC NULLS LAST, c.symbol
"""


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    return round(float(v), 3) if v is not None else None


def propose_rebalance(conn=None, as_of=None):
    """52W Breakout entry proposal (dry-run, founder-confirmed to execute).

    Returns:
      {
        as_of, basket, capital, slot_value, top_n,
        qualifiers:  every name passing the full screen (gvm+wk52+month+mcap+vol), 1y-return desc,
        n_qualified: len(qualifiers),
        entries:     the selected cohort per the N-rules (top-10 / all / none), each with
                     {symbol, year_return, gvm, mcap, vol_ratio_21, week_index_52, month_index, slot_value},
        n_selected:  len(entries),
        cash_value:  capital - 50k*n_selected,
        selection_note: which N-branch fired,
        holdings:    current open positions (reference only),
        rules:       encoded spec-6103 thresholds,
      }
    """
    own = conn is None
    if own:
        conn = _conn()
    if as_of is None:
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_DATE")
            as_of = cur.fetchone()[0]
    try:
        with conn.cursor() as cur:
            cur.execute(_SCREEN_SQL, {"gvm": GVM_MIN, "wk": WK52_MIN, "mon": MONTH_MIN,
                                      "mcap": MCAP_MIN, "vr": VOL_RATIO_MIN})
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            qualifiers = [{
                "symbol": r["symbol"], "year_return": _f(r["year_return"]),
                "gvm": _f(r["gvm_score"]), "mcap": _f(r["market_cap"]),
                "vol_ratio_21": _f(r["vol_ratio_21"]),
                "week_index_52": _f(r["week_index_52"]), "month_index": _f(r["month_index"]),
            } for r in rows]
            n_qual = len(qualifiers)

            # selection: N<5 -> cash; 5<=N<=10 -> all; N>10 -> top 10 by 1y return (already sorted)
            if n_qual < MIN_N:
                entries, note = [], f"N={n_qual} < {MIN_N} -> FULLY CASH (dead-tape trap guard)"
            elif n_qual <= TOP_N:
                entries, note = list(qualifiers), f"N={n_qual} in [{MIN_N},{TOP_N}] -> hold all N"
            else:
                entries, note = qualifiers[:TOP_N], f"N={n_qual} > {TOP_N} -> top {TOP_N} by 1y return"

            for e in entries:
                e["slot_value"] = SLOT_VALUE
            n_sel = len(entries)
            cash = round(CAPITAL - SLOT_VALUE * n_sel, 2)

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
                "screen": "GVM>=7.5 AND week_index_52>=90 AND month_index>=90 AND mcap>1000Cr AND vol_ratio_21>=1.0",
                "vol_ratio_21": "latest volume / AVG(last 21 trading-day volumes), raw_prices (inline)",
                "selection": f"N>{TOP_N} -> top {TOP_N} by 1y return; {MIN_N}<=N<={TOP_N} -> all; N<{MIN_N} -> cash",
                "sizing": f"Rs {int(SLOT_VALUE)}/slot always (5L/{TOP_N}); N<{TOP_N} leaves cash",
                "exits": "HS1 -15% from entry + GVM<7.2 quality exit; no HS2, no rank/trailing",
            },
        }
    finally:
        if own:
            conn.close()
