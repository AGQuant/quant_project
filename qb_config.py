"""
qb_config.py — cc#1710 QB_CAP_AMENDMENT_V1 (session_log 39173, founder 05-Sep-2026).

ONE read path for a basket's stock cap and capital. Before this card every selection engine
carried its own literals (TOP_N = 12, CAPITAL = 500000.0, "top_n": 12 ...) and the rebalance
runner read quant_basket_registry — so Fable's config change (large_cap + alpha_multicap
max_stocks 12 -> 15) could never reach the engines. Now:

    basket_params(conn, basket_name) -> {"max_stocks": int, "capital": float, "source": {...}}

Source order, per field:
  max_stocks : quant_basket_config.stage2_stock.max_stocks
               -> stage2_stock.selection "topNN..." (mid_cap / breakout_52w / contra_value carry
                  their cap only in that string)
               -> quant_basket_registry.max_stocks
  capital    : quant_basket_config.stage2_stock.capital_rs -> quant_basket_registry.capital

No basket-name literal anywhere here; a basket with no cap or capital in either table raises,
which is the honest outcome (a silent default would size a book on an invented number).

    size_slots(capital, max_stocks, n) -> (slot, cash, mode)

is the amendment's fill_rule: fill to the cap in rank order; slot = capital / max_stocks at a
full book (cash for empty slots); N < 10 -> capital / 10 per name + cash (concentration brake,
unchanged). alpha_multicap and the composite engines (large_cap / mid_cap) use it; small_cap
keeps its own equal-capital/N sizing and breakout/contra their fixed capital/cap slot — their
caps are unchanged by the amendment, only the read path moved here.
"""
import json
import re
from typing import Dict, Tuple

BRAKE_N = 10   # concentration brake — below this many names, size at capital/10 and hold cash


def basket_params(conn, basket_name: str) -> Dict:
    with conn.cursor() as cur:
        cur.execute("SELECT stage2_stock FROM quant_basket_config WHERE basket_name=%s", (basket_name,))
        row = cur.fetchone()
        s2 = (row[0] if row and row[0] else {}) or {}
        if isinstance(s2, str):
            try:
                s2 = json.loads(s2)
            except ValueError:
                s2 = {}
        cur.execute("SELECT max_stocks, capital FROM quant_basket_registry WHERE basket_name=%s", (basket_name,))
        reg = cur.fetchone() or (None, None)

    max_stocks, ms_src = None, None
    if s2.get("max_stocks") is not None:
        max_stocks, ms_src = int(s2["max_stocks"]), "quant_basket_config.stage2_stock.max_stocks"
    else:
        m = re.search(r"top\s*_?(\d+)", str(s2.get("selection") or ""), re.I)
        if m:
            max_stocks, ms_src = int(m.group(1)), "quant_basket_config.stage2_stock.selection"
        elif reg[0] is not None:
            max_stocks, ms_src = int(reg[0]), "quant_basket_registry.max_stocks"

    capital, cap_src = None, None
    if s2.get("capital_rs") is not None:
        capital, cap_src = float(s2["capital_rs"]), "quant_basket_config.stage2_stock.capital_rs"
    elif reg[1] is not None:
        capital, cap_src = float(reg[1]), "quant_basket_registry.capital"

    if max_stocks is None or capital is None:
        raise LookupError(f"{basket_name}: no max_stocks/capital in quant_basket_config or quant_basket_registry")
    return {"max_stocks": max_stocks, "capital": capital,
            "source": {"max_stocks": ms_src, "capital": cap_src}}


def size_slots(capital: float, max_stocks: int, n: int, brake_n: int = BRAKE_N) -> Tuple[float, float, str]:
    """(slot per name, cash left, mode) for n names entering under the amendment fill_rule."""
    capital = float(capital)
    if n <= 0:
        return 0.0, round(capital, 2), "empty"
    if n < brake_n:
        slot = round(capital / brake_n, 2)
        return slot, round(capital - slot * n, 2), f"brake_capital_div_{brake_n}"
    slot = round(capital / int(max_stocks), 2)
    return slot, round(capital - slot * n, 2), f"equal_capital_div_{int(max_stocks)}"
