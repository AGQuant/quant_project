"""
qb_finz_wcb_select.py — FINZ Wealth Compounder Basket selection/proposal engine.
cc#1533, Finkhoz_Basket_Research_Protocol_v1.2 (12-Aug-2026), basket FNZ-WCB.

DRY-RUN, FIXED LIST, equal weight (100% / 12 = 8.33%, per the protocol's basket matrix).
Not an algorithmic screen — a Finkhoz analyst/committee-curated list, confirmed twice
independently (Aug-2026 factsheet + this week's screener import) per cc#1533's evidence.
"""

BASKET = "finz_wcb"
CAPITAL = 500000.0
HOLDINGS = ["SAILIFE", "GPPL", "RRKABEL", "KARURVYSYA", "CARTRADE", "ANANTRAJ",
            "STAR", "WAAREEENER", "MASFIN", "SURYODAY", "FIEMIND", "KRISHNADEF"]


def propose_rebalance(conn=None):
    slot = round(CAPITAL / len(HOLDINGS), 2)
    entries = [{"symbol": sym, "slot_value": slot} for sym in HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ Wealth Compounder, fixed list per "
                           "Finkhoz_Basket_Research_Protocol_v1.2, seeded 31-Aug-2026, "
                           "equal weight (12 @ 8.33%)",
    }
