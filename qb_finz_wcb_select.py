"""
qb_finz_wcb_select.py — FINZ Wealth Compounder Basket selection/proposal engine.
cc#1533 seeded this equal-weight, flagged at the time as a fallback pending real weight data.
cc#1534 corrects it from the founder's actual Live Portfolio sheet, which gives real per-stock
weights ranging 2.7%-13.1% — not equal.

DRY-RUN, FIXED LIST, real weights (not equal, despite the protocol matrix's 100%/12 formula —
the live portfolio has since diverged from that formula through active management).
"""

BASKET = "finz_wcb"
CAPITAL = 500000.0

# (symbol, weight_pct) — source: founder Live Portfolio sheet, Wealth Compounder table,
# read 31-Aug-2026 (cc#1534 correction; cc#1533's equal-weight seed was wrong).
_HOLDINGS = [
    ("SAILIFE", 12.8), ("KRISHNADEF", 4.4), ("WAAREEENER", 11.4), ("STAR", 9.0),
    ("RRKABEL", 12.9), ("GPPL", 5.2), ("CARTRADE", 13.1), ("FIEMIND", 8.9),
    ("SURYODAY", 2.7), ("MASFIN", 5.4), ("KARURVYSYA", 6.1), ("ANANTRAJ", 7.9),
]


def propose_rebalance(conn=None):
    entries = [{"symbol": sym, "slot_value": round(CAPITAL * pct / 100.0, 2)}
               for sym, pct in _HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ Wealth Compounder, fixed list per "
                           "Finkhoz_Basket_Research_Protocol_v1.2, corrected 31-Aug-2026 "
                           "(cc#1534, real weights from the live portfolio, not equal)",
    }
