"""
qb_finz_helios_select.py — FINZ Helios Basket selection/proposal engine.
cc#1533, Finkhoz_Basket_Research_Protocol_v1.2 (12-Aug-2026), basket FNZ-HRB.

DRY-RUN, FIXED LIST, equal weight. The protocol's approved count is 11 holdings (100%/11 =
9.09%), but only 10 are confirmed (factsheet + screener import both agree on these 10; no
source names an 11th) — seeded with the 10 confirmed, per cc#1533's explicit instruction not
to invent a missing symbol. Weight is still capital/10 (equal split of the confirmed set), not
a forced 9.09% that would leave an unexplained cash slot.
"""

BASKET = "finz_helios"
CAPITAL = 500000.0
HOLDINGS = ["AVALON", "PAYTM", "AETHER", "SONACOMS", "SAILIFE", "RRKABEL",
            "TITAN", "MINDACORP", "BELRISE", "EMCURE"]


def propose_rebalance(conn=None):
    slot = round(CAPITAL / len(HOLDINGS), 2)
    entries = [{"symbol": sym, "slot_value": slot} for sym in HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ Helios, fixed list per Finkhoz_Basket_Research_Protocol_v1.2, "
                           "seeded 31-Aug-2026, equal weight over the 10 CONFIRMED holdings "
                           "(protocol's approved count is 11 — the 11th is unconfirmed, not seeded)",
    }
