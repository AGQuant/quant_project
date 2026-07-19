"""
qb_alpha_select.py — Alpha Multicap V2 FINAL selection/proposal engine.
cc#553, canonical spec session_log id=6086 (ALPHA_MULTICAP_V2_FINAL_DGVM_LOCKED).

DRY-RUN ONLY. This proposes the monthly-rebalance entries + exits for the `alpha_multicap`
basket. Entry/exit EXECUTION stays founder-confirmed (existing QB design) — nothing here
auto-buys or auto-sells. It only READS gvm_scores / gvm_history / nifty500_universe /
quant_paper_positions and returns a proposal that a manual SQL replication reproduces exactly.

Spec id=6086 rules encoded here:
  universe    Nifty 500 (nifty500_universe)
  score       alpha_score = 0.5*gvm_score + 0.5*m_score   (latest gvm_scores)
  hard gates  GVM >= 7.5 AND V >= 7.5 AND M > 7 AND (gvm_now - gvm_180d_ago) > +0.5
              dGVM 180d lookback from gvm_history: nearest score in [as_of-200, as_of-180]
  selection   top-12 by alpha_score among qualifiers, equal weight; < 12 qualify -> cash slots
              (return convention: full 12-slot / Rs 5L capital, cash earns 0)
  exit        monthly (6th): MAX 3 holdings ranked OUTSIDE composite top-25, worst rank first;
              refills must pass ALL entry gates incl dGVM
  stops       HS1 -20% from entry only (HS2 removed for alpha in qb_eod_checker.py); no trail
"""
import os
import logging

import psycopg

log = logging.getLogger("qb_alpha_select")

BASKET      = "alpha_multicap"
TOP_N       = 12      # equal-weight slots (~Rs 41.6k/slot on Rs 5L)
KEEP_RANK   = 25      # holdings ranked worse than this are exit-eligible
MAX_EXITS   = 3       # per monthly rebalance, worst rank first
GVM_MIN     = 7.5
V_MIN       = 7.5
M_MIN       = 7.0     # strict > (encoded as m_score > M_MIN)
DGVM_MIN    = 0.5     # strict > : (gvm_now - gvm_180d_ago) > 0.5
CAPITAL     = 500000.0

# Full Nifty-500 eligible set ranked by alpha_score. `passes_gates` is the entry-qualifier flag;
# the alpha_score ORDER is the "composite" rank basis used for BOTH selection and the exit test.
_RANK_SQL = """
WITH latest AS (
    SELECT symbol, gvm_score, v_score, m_score
    FROM gvm_scores
    WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
),
dgvm AS (   -- gvm 180d ago: nearest gvm_history score in [as_of-200, as_of-180]
    SELECT DISTINCT ON (symbol) symbol, gvm_score AS gvm_180
    FROM gvm_history
    WHERE score_date BETWEEN %(asof)s::date - 200 AND %(asof)s::date - 180
    ORDER BY symbol, score_date DESC
),
univ AS (SELECT symbol FROM nifty500_universe)
SELECT l.symbol,
       l.gvm_score, l.v_score, l.m_score,
       d.gvm_180,
       (l.gvm_score - d.gvm_180)                    AS dgvm,
       (0.5 * l.gvm_score + 0.5 * l.m_score)        AS alpha_score,
       (l.gvm_score >= %(gvm)s AND l.v_score >= %(v)s AND l.m_score > %(m)s
        AND d.gvm_180 IS NOT NULL
        AND (l.gvm_score - d.gvm_180) > %(dgvm)s)    AS passes_gates
FROM latest l
JOIN univ u ON u.symbol = l.symbol
LEFT JOIN dgvm d ON d.symbol = l.symbol
ORDER BY alpha_score DESC NULLS LAST, l.symbol
"""


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    return float(v) if v is not None else None


def _ranked_universe(cur, as_of):
    """Return the full Nifty-500 eligible set, alpha_score-desc, each row a dict with a 1-based
    composite `rank` (the same ranking that defines both top-12 entries and the top-25 keep zone)."""
    cur.execute(_RANK_SQL, {"asof": as_of, "gvm": GVM_MIN, "v": V_MIN, "m": M_MIN, "dgvm": DGVM_MIN})
    cols = [c.name for c in cur.description]
    rows = []
    for i, r in enumerate(cur.fetchall(), start=1):
        d = dict(zip(cols, r))
        d["rank"] = i
        for k in ("gvm_score", "v_score", "m_score", "gvm_180", "dgvm", "alpha_score"):
            d[k] = round(_f(d[k]), 3) if d[k] is not None else None
        rows.append(d)
    return rows


def propose_rebalance(conn=None, as_of=None):
    """Full monthly-rebalance proposal for alpha_multicap (dry-run, founder-confirmed to execute).

    Returns:
      {
        as_of, basket, capital, slot_size,
        entries:      top-12 qualifiers (equal weight), each {symbol, gvm, v, m, dgvm, alpha_score, rank},
        cash_slots:   12 - len(entries)  (>0 only when fewer than 12 names pass all gates),
        holdings:     current open positions with their live composite rank,
        exits:        MAX 3 held names ranked outside top-25, worst rank first,
        refills:      best qualifiers (passing ALL gates) not currently held, to fill freed/empty slots,
        rules:        the encoded spec-6086 thresholds (for the UI/provenance),
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
            ranked = _ranked_universe(cur, as_of)
            rank_of = {r["symbol"]: r["rank"] for r in ranked}
            by_symbol = {r["symbol"]: r for r in ranked}

            # entries: top-12 by alpha_score among gate-qualifiers, equal weight
            qualifiers = [r for r in ranked if r["passes_gates"]]
            entries = qualifiers[:TOP_N]
            cash_slots = max(0, TOP_N - len(entries))

            # current holdings + their live composite rank
            cur.execute("SELECT symbol FROM quant_paper_positions "
                        "WHERE basket_name=%s AND status='open'", (BASKET,))
            held = [row[0] for row in cur.fetchall()]
            holdings = [{"symbol": s, "rank": rank_of.get(s),
                         "alpha_score": (by_symbol.get(s) or {}).get("alpha_score")} for s in held]

            # exits: held names ranked OUTSIDE top-25 (or fully unranked), worst rank first, max 3
            def _rank_key(h):
                # unranked (no live score) sorts worst
                return h["rank"] if h["rank"] is not None else 10**9
            exit_pool = [h for h in holdings if _rank_key(h) > KEEP_RANK]
            exit_pool.sort(key=_rank_key, reverse=True)
            exits = exit_pool[:MAX_EXITS]

            # refills: best gate-qualifiers not currently held, to fill freed slots (and any empty
            # slots when currently below 12). Refills must pass ALL gates incl dGVM (they come from
            # `qualifiers`). Count = exits freed + slots short of 12.
            held_set = set(held)
            freed = len(exits)
            short_of_full = max(0, TOP_N - len(held_set))
            refill_slots = max(freed, short_of_full)
            refills = [q for q in qualifiers if q["symbol"] not in held_set][:refill_slots]

        return {
            "as_of": str(as_of),
            "basket": BASKET,
            "capital": CAPITAL,
            "slot_size": round(CAPITAL / TOP_N, 2),
            "entries": entries,
            "cash_slots": cash_slots,
            "qualified_pool": len(qualifiers),
            "holdings": holdings,
            "exits": exits,
            "refills": refills,
            "rules": {
                "universe": "Nifty 500 (nifty500_universe)",
                "score": "0.5*GVM + 0.5*M",
                "hard_gates": "GVM>=7.5 AND V>=7.5 AND M>7 AND dGVM_180d>+0.5",
                "dgvm_lookback": "gvm_history nearest score in [as_of-200, as_of-180]",
                "selection": f"top-{TOP_N} by alpha_score, equal weight; fewer -> cash slots",
                "exit": f"monthly 6th: max {MAX_EXITS} held names ranked outside top-{KEEP_RANK}, worst first",
                "stops": "HS1 -20% from entry only (no HS2, no trailing)",
                "return_convention": f"full {int(TOP_N)}-slot / Rs {int(CAPITAL)} capital, cash slots earn 0",
            },
        }
    finally:
        if own:
            conn.close()
