"""
qb_finz_etf_select.py — FINZ ETF Basket selection/proposal engine.
cc#1534, Finkhoz_Basket_Research_Protocol_v1.2 (12-Aug-2026), basket FNZ-ETF.

DRY-RUN, FIXED LIST, real weights (not equal). Blocked at cc#1533 (composition unconfirmed —
the original Dec-2025 build held 5 ETFs, every Aug-2026 source said 4 but not which was
dropped). Resolved by cc#1534 from the founder's Live Portfolio sheet ETF_Basket table: a 5th
row (LTGILTBEES) exists in the sheet with no Buy Price/Qty/Current Price filled in — not
currently held, dropped from the original composition. Seeded here as the 4 actually-held ETFs.
"""

BASKET = "finz_etf"
CAPITAL = 500000.0

# (symbol, weight_pct) — source: founder Live Portfolio sheet, ETF_Basket table, read 31-Aug-2026.
_HOLDINGS = [
    ("MID150BEES", 19.11), ("GOLDBEES", 20.06), ("NIFTYBEES", 43.26), ("SILVERBEES", 17.58),
]


def propose_rebalance(conn=None):
    entries = [{"symbol": sym, "slot_value": round(CAPITAL * pct / 100.0, 2)}
               for sym, pct in _HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ ETF Basket, fixed list per Finkhoz_Basket_Research_Protocol_v1.2, "
                           "seeded 31-Aug-2026 (cc#1534, unblocked from cc#1533), real weights "
                           "from the live portfolio, 4 ETFs (LTGILTBEES dropped, not currently held)",
    }
