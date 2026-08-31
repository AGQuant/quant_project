"""
qb_finz_dividend_select.py — FINZ Dividend Basket selection/proposal engine.
cc#1533 seeded this equal-weight, flagged at the time as a fallback pending real weight data.
cc#1534 corrects it from the founder's actual Live Portfolio sheet's "DIVIDEND ON FINKHOZ"
table (NOT the older "Dividend_Basket" table, which includes LIQUIDCASE and doesn't match the
8 symbols already live here) — real per-stock weights ranging 8.9%-19.3%, fully deployed.

DRY-RUN, FIXED LIST, real weights (not equal). Sums to exactly 100% — no cash residual.
"""

BASKET = "finz_dividend"
CAPITAL = 500000.0

# (symbol, weight_pct) — source: founder Live Portfolio sheet, "DIVIDEND ON FINKHOZ" table
# (the table matching these 8 symbols exactly), read 31-Aug-2026 (cc#1534 correction;
# cc#1533's equal-weight seed was wrong).
_HOLDINGS = [
    ("NXST", 14.2), ("ABSLAMC", 15.0), ("CHENNPETRO", 19.3), ("PFC", 9.8),
    ("VEDL", 11.8), ("NMDC", 11.0), ("BANKBARODA", 10.0), ("RECLTD", 8.9),
]


def propose_rebalance(conn=None):
    entries = [{"symbol": sym, "slot_value": round(CAPITAL * pct / 100.0, 2)}
               for sym, pct in _HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ Dividend, fixed list per Finkhoz_Basket_Research_Protocol_v1.2, "
                           "corrected 31-Aug-2026 (cc#1534, real weights from the live portfolio's "
                           "\"DIVIDEND ON FINKHOZ\" table, fully deployed, not equal)",
    }
