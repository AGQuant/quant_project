"""
qb_finz_helios_select.py — FINZ Helios Basket selection/proposal engine.
cc#1533 built this with the wrong holdings (an Aug-2026 factsheet + screener import that had
since gone stale — only RRKABEL overlapped with reality). cc#1534 corrects it from the
founder's actual Live Portfolio sheet, which shows the basket was rebalanced 31-Aug-2026.

DRY-RUN, FIXED LIST, real weights (not equal). LIQUIDCASE is 46.1% of the basket per the sheet
but does not resolve through cmp_resolver/raw_prices (confirmed, cc#1534) — it is the same
unresolved symbol cc#1517's screener import already excluded pending founder clarification.
Left as unallocated cash rather than renormalizing the resolvable stocks up to 100%, which
would misrepresent the basket's real cash position as fully invested.
"""

BASKET = "finz_helios"
CAPITAL = 500000.0

# (symbol, weight_pct) — source: founder Live Portfolio sheet, Helios table, read 31-Aug-2026.
# LIQUIDCASE 46.1% omitted (unresolved) — stays unallocated cash, not renormalized into these 7.
_HOLDINGS = [
    ("SKYGOLD", 9.3), ("RRKABEL", 5.6), ("MOTHERSON", 9.6), ("MARKSANS", 9.3),
    ("GLAND", 5.6), ("AZAD", 5.5), ("ASKAUTOLTD", 8.9),
]


def propose_rebalance(conn=None):
    entries = [{"symbol": sym, "slot_value": round(CAPITAL * pct / 100.0, 2)}
               for sym, pct in _HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ Helios, fixed list per Finkhoz_Basket_Research_Protocol_v1.2, "
                           "corrected 31-Aug-2026 (cc#1534, real weights from the live portfolio) "
                           "— LIQUIDCASE 46.1% unresolved, held as unallocated cash",
    }
