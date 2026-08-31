"""
qb_finz_stable_select.py — FINZ Stable Portfolio selection/proposal engine.
cc#1533, Finkhoz_Basket_Research_Protocol_v1.2 (12-Aug-2026), basket FNZ-STB.

DRY-RUN, FIXED LIST. Not an algorithmic screen like Scorr's own 6 baskets — this basket's
holdings are Finkhoz's own analyst/committee-curated list, sourced from the master sheet's
"Stable Portfolio (Live) since Nov 21 2024" tab. propose_rebalance() returns that fixed list
verbatim; nothing here queries gvm_scores/nifty500_universe/etc.

WEIGHTS ARE REAL, NOT EQUAL. The protocol's basket matrix gives Stable no formula weight —
unlike Wealth Compounder/Helios/Dividend/Defence (100%/holdings_count each) — so this selector
uses the live portfolio's actual current weights (uneven, 4.4%-9.0%, reflecting drift since the
Nov-2024 inception), not a computed equal split. See cc#1533's own evidence note for the
inception=vs-current-weight distinction.
"""

BASKET = "finz_stable"
CAPITAL = 500000.0

# (symbol, weight_pct) — source: master sheet "Stable Portfolio (Live)", read 31-Aug-2026.
_HOLDINGS = [
    ("KARURVYSYA", 5.3), ("YATHARTH", 6.8), ("NMDC", 8.4), ("AUBANK", 7.3),
    ("IPCALAB", 6.6), ("HINDCOPPER", 4.4), ("TIPSMUSIC", 8.6), ("ADANIPORTS", 5.6),
    ("ABCAPITAL", 8.8), ("VEDL", 8.5), ("MOTHERSON", 9.0), ("AFFLE", 5.4),
    ("CGCL", 8.6), ("PREMIERENE", 6.8),
]


def propose_rebalance(conn=None):
    entries = [{"symbol": sym, "slot_value": round(CAPITAL * pct / 100.0, 2)}
               for sym, pct in _HOLDINGS]
    return {
        "entries": entries,
        "selection_note": "FINZ Stable, fixed list per Finkhoz_Basket_Research_Protocol_v1.2, "
                           "seeded 31-Aug-2026, weights from the live portfolio (uneven, not equal)",
    }
