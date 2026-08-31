"""
qb_finz_defence_select.py — FINZ Defence Basket selection/proposal engine.
cc#1533, Finkhoz_Basket_Research_Protocol_v1.2 (12-Aug-2026), basket FNZ-DEF.

DRY-RUN, FIXED LIST, equal weight. The protocol's approved count is 7 holdings (100%/7 =
14.29%), but only 6 are confirmed (single source: this week's screener import, not
cross-checked against a factsheet) — seeded with the 6 confirmed, per cc#1533's explicit
instruction not to invent a missing symbol.
"""

BASKET = "finz_defence"
CAPITAL = 500000.0
HOLDINGS = ["AEQUS", "APOLLO", "AZAD", "BHARATFORG", "DATAPATTNS", "HAL"]


def propose_rebalance(conn=None):
    slot = round(CAPITAL / len(HOLDINGS), 2)
    entries = [{"symbol": sym, "slot_value": slot} for sym in HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ Defence, fixed list per Finkhoz_Basket_Research_Protocol_v1.2, "
                           "seeded 31-Aug-2026, equal weight over the 6 CONFIRMED holdings "
                           "(protocol's approved count is 7 — the 7th is unconfirmed, not seeded)",
    }
