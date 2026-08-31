"""
qb_finz_dividend_select.py — FINZ Dividend Basket selection/proposal engine.
cc#1533, Finkhoz_Basket_Research_Protocol_v1.2 (12-Aug-2026), basket FNZ-DIV.

DRY-RUN, FIXED LIST, equal weight. The protocol's approved count is 9 holdings (100%/9 =
11.11%), but only 8 are confirmed (single source: this week's screener import, not
cross-checked against a factsheet) — seeded with the 8 confirmed, per cc#1533's explicit
instruction not to invent a missing symbol.
"""

BASKET = "finz_dividend"
CAPITAL = 500000.0
HOLDINGS = ["NXST", "ABSLAMC", "CHENNPETRO", "PFC", "VEDL", "NMDC", "BANKBARODA", "RECLTD"]


def propose_rebalance(conn=None):
    slot = round(CAPITAL / len(HOLDINGS), 2)
    entries = [{"symbol": sym, "slot_value": slot} for sym in HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ Dividend, fixed list per Finkhoz_Basket_Research_Protocol_v1.2, "
                           "seeded 31-Aug-2026, equal weight over the 8 CONFIRMED holdings "
                           "(protocol's approved count is 9 — the 9th is unconfirmed, not seeded)",
    }
