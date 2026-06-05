"""
Quant Basket — Rebalance Engine
=================================
Handles position sizing with strict ₹5L basket cap and NIFTYBEES cash parking.

Rules:
  1. Basket capital = exactly ₹5,00,000 (from quant_basket_registry.capital)
  2. Per-stock allocation = capital / max_stocks (equal weight)
  3. Actual qty = floor(allocation / cmp) — lot rounding down
  4. Actual cost = qty * cmp
  5. Residual = capital - SUM(actual_cost) → park in NIFTYBEES
  6. NIFTYBEES capped at 5% of capital (₹25,000)
  7. If residual > 5% → flag as sizing error, do not exceed cap

NIFTYBEES symbol: NSE:NIFTYBEES-EQ (Yahoo: NIFTYBEES.NS)
1 unit ≈ 1/10th of Nifty 50 value.
"""

import logging
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

log = logging.getLogger("scorr.qb_rebalance")

IST              = timezone(timedelta(hours=5, minutes=30))
NIFTYBEES_SYMBOL = "NIFTYBEES"
NIFTYBEES_YAHOO  = "NIFTYBEES.NS"
CASH_CAP_PCT     = 0.05   # max 5% of capital in NIFTYBEES
_YAHOO_UA        = "Mozilla/5.0"
_YAHOO_CHART     = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fetch_ltp(symbol: str) -> Optional[float]:
    """Fetch live price from Yahoo chart API."""
    ticker = NIFTYBEES_YAHOO if symbol == NIFTYBEES_SYMBOL else urllib.parse.quote(symbol, safe="") + ".NS"
    url = _YAHOO_CHART.format(sym=ticker)
    for attempt in range(2):
        try:
            r = requests.get(url, params={"interval": "1m", "range": "1d"},
                             headers={"User-Agent": _YAHOO_UA}, timeout=10)
            r.raise_for_status()
            data = r.json()
            chart = (data.get("chart") or {}).get("result") or []
            if not chart:
                if attempt == 0:
                    time.sleep(0.4); continue
                return None
            meta = chart[0].get("meta") or {}
            price = meta.get("regularMarketPrice")
            if price:
                return float(price)
            closes = ((chart[0].get("indicators") or {})
                      .get("quote", [{}])[0].get("close") or [])
            closes = [c for c in closes if c is not None]
            return float(closes[-1]) if closes else None
        except Exception as e:
            if attempt == 0:
                time.sleep(0.4); continue
            log.warning(f"_fetch_ltp {symbol}: {e}")
            return None
    return None


def compute_position_sizing(
    capital: float,
    max_stocks: int,
    symbols: List[str],
    prices: Dict[str, float]
) -> Dict:
    """
    Compute position sizes for a basket strictly within capital.
    Returns sizing dict with per-stock qty + NIFTYBEES residual.
    """
    per_stock_alloc = capital / max_stocks
    positions = []
    total_deployed = 0.0

    for sym in symbols:
        cmp = prices.get(sym)
        if not cmp or cmp <= 0:
            log.warning(f"compute_sizing: no price for {sym}, skipping")
            continue
        qty = int(per_stock_alloc / cmp)   # floor — never exceed alloc
        if qty <= 0:
            log.warning(f"compute_sizing: {sym} CMP={cmp} too high for alloc={per_stock_alloc:.0f}")
            continue
        cost = round(qty * cmp, 2)
        total_deployed += cost
        positions.append({
            "symbol":     sym,
            "cmp":        round(cmp, 2),
            "qty":        qty,
            "allocation": round(per_stock_alloc, 2),
            "cost":       cost,
        })

    residual = round(capital - total_deployed, 2)
    cash_cap = round(capital * CASH_CAP_PCT, 2)
    niftybees = None
    warning   = None

    if residual > 0:
        if residual > cash_cap:
            warning = (
                f"Residual ₹{residual:,.0f} exceeds 5% cap ₹{cash_cap:,.0f}. "
                f"Position sizing error — check per-stock alloc or stock count."
            )
            niftybees_invest = cash_cap
        else:
            niftybees_invest = residual

        nb_price = prices.get(NIFTYBEES_SYMBOL) or _fetch_ltp(NIFTYBEES_SYMBOL)
        if nb_price and nb_price > 0:
            nb_qty = int(niftybees_invest / nb_price)
            if nb_qty > 0:
                nb_cost = round(nb_qty * nb_price, 2)
                niftybees = {
                    "symbol":  NIFTYBEES_SYMBOL,
                    "cmp":     round(nb_price, 2),
                    "qty":     nb_qty,
                    "cost":    nb_cost,
                    "purpose": "cash_residual",
                }
                total_deployed += nb_cost
        else:
            warning = (warning or "") + " Could not fetch NIFTYBEES price."

    return {
        "capital":         capital,
        "max_stocks":      max_stocks,
        "stocks_sized":    len(positions),
        "total_deployed":  round(total_deployed, 2),
        "residual":        residual,
        "cash_cap":        cash_cap,
        "utilisation_pct": round(total_deployed / capital * 100, 2),
        "positions":       positions,
        "niftybees":       niftybees,
        "warning":         warning,
    }


def fix_basket_overdeployment(conn, basket_name: str) -> Dict:
    """
    Fix existing basket:
    1. Corrects allocation column to actual cost (entry_price * qty)
    2. Computes residual vs ₹5L capital
    3. Adds NIFTYBEES position for residual (capped at 5%)
    Does NOT change qty or entry prices.
    """
    now = datetime.now(IST)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT capital, max_stocks FROM quant_basket_registry WHERE basket_name=%s",
            (basket_name,)
        )
        row = cur.fetchone()
    if not row:
        return {"error": f"basket {basket_name} not in registry"}

    capital, max_stocks = float(row[0]), int(row[1])

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, symbol, entry_price, qty
            FROM quant_paper_positions
            WHERE basket_name=%s AND status='open' AND symbol != %s
            ORDER BY symbol
        """, (basket_name, NIFTYBEES_SYMBOL))
        positions = [{"id": r[0], "symbol": r[1],
                      "entry_price": float(r[2]), "qty": float(r[3])}
                     for r in cur.fetchall()]

    if not positions:
        return {"status": "no_positions", "basket": basket_name}

    # Fix allocation = actual cost
    total_cost = sum(p["entry_price"] * p["qty"] for p in positions)
    residual   = round(capital - total_cost, 2)
    cash_cap   = round(capital * CASH_CAP_PCT, 2)
    fixed = 0

    for p in positions:
        actual_cost = round(p["entry_price"] * p["qty"], 2)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE quant_paper_positions SET allocation=%s, updated_at=NOW() WHERE id=%s",
                (actual_cost, p["id"])
            )
        conn.commit()
        fixed += 1

    # Check NIFTYBEES
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM quant_paper_positions WHERE basket_name=%s AND symbol=%s AND status='open'",
            (basket_name, NIFTYBEES_SYMBOL)
        )
        nb_exists = cur.fetchone()

    niftybees_added = None
    warning = None

    if residual > 0 and not nb_exists:
        invest = residual
        if residual > cash_cap:
            warning = f"Residual ₹{residual:,.0f} > 5% cap ₹{cash_cap:,.0f} — capping at ₹{cash_cap:,.0f}"
            invest = cash_cap

        nb_price = _fetch_ltp(NIFTYBEES_SYMBOL)
        if nb_price and nb_price > 0:
            nb_qty = int(invest / nb_price)
            if nb_qty > 0:
                nb_cost = round(nb_qty * nb_price, 2)
                entry_date = datetime.now(IST).date()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO quant_paper_positions
                        (basket_name, symbol, entry_price, entry_date, qty, allocation,
                         current_price, current_value, pnl, pnl_pct, status, notes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,'open',%s)
                        ON CONFLICT (basket_name, symbol, entry_date) DO NOTHING
                    """, (
                        basket_name, NIFTYBEES_SYMBOL, nb_price, entry_date,
                        nb_qty, nb_cost, nb_price, nb_cost,
                        f"Cash residual. Capital={capital:.0f} deployed={total_cost:.0f} residual={residual:.0f}"
                    ))
                conn.commit()
                niftybees_added = {"symbol": NIFTYBEES_SYMBOL, "price": nb_price,
                                   "qty": nb_qty, "cost": nb_cost, "residual": residual}
        else:
            warning = (warning or "") + " Could not fetch NIFTYBEES price."

    elif residual < 0:
        warning = f"OVERDEPLOYED by ₹{abs(residual):,.0f} — review position sizing at next rebalance"

    return {
        "status":            "ok",
        "basket":            basket_name,
        "capital":           capital,
        "total_stock_cost":  round(total_cost, 2),
        "residual":          residual,
        "cash_cap_5pct":     cash_cap,
        "positions_fixed":   fixed,
        "niftybees_added":   niftybees_added,
        "warning":           warning,
        "run_at":            now.strftime("%Y-%m-%d %H:%M IST"),
    }
