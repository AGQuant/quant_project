"""
qb_finz_defence_select.py — FINZ Defence Basket selection/proposal engine.
cc#1533 seeded this equal-weight, flagged at the time as a fallback pending real weight data.
cc#1534 corrects it from the founder's actual Live Portfolio sheet, which gives real per-stock
weights ranging 6.9%-21.5% — not equal.

DRY-RUN, FIXED LIST, real weights (not equal). LIQUIDCASE is 12.9% of the basket per the sheet
but does not resolve through cmp_resolver/raw_prices (confirmed, cc#1534) — left as
unallocated cash rather than renormalizing the resolvable stocks up to 100%.
"""

BASKET = "finz_defence"
CAPITAL = 500000.0

# (symbol, weight_pct) — source: founder Live Portfolio sheet, Defence table, read 31-Aug-2026
# (cc#1534 correction; cc#1533's equal-weight seed was wrong).
# LIQUIDCASE 12.9% omitted (unresolved) — stays unallocated cash, not renormalized into these 6.
_HOLDINGS = [
    ("AEQUS", 7.6), ("APOLLO", 6.9), ("AZAD", 12.8), ("BHARATFORG", 18.2),
    ("DATAPATTNS", 20.1), ("HAL", 21.5),
]


def propose_rebalance(conn=None):
    entries = [{"symbol": sym, "slot_value": round(CAPITAL * pct / 100.0, 2)}
               for sym, pct in _HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ Defence, fixed list per Finkhoz_Basket_Research_Protocol_v1.2, "
                           "corrected 31-Aug-2026 (cc#1534, real weights from the live portfolio) "
                           "— LIQUIDCASE 12.9% unresolved, held as unallocated cash",
    }
