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

import calendar
import json
import logging
import time
import urllib.parse
from datetime import datetime, timezone, timedelta, date
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


def _next_rebalance_date(cur_date: date, freq: str) -> date:
    """cc#439: advance a rebalance date by its cadence (monthly=+1mo, quarterly=+3mo), clamping the
    day to the target month's length."""
    months = 3 if (freq or "").lower().startswith("quarter") else 1
    y, m = cur_date.year, cur_date.month + months
    while m > 12:
        m -= 12; y += 1
    day = min(cur_date.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def run_scheduled_rebalance(conn, basket_name: str) -> Dict:
    """cc#439 fix_1: scheduled rebalance runner for one basket. SAFE by design — it processes the
    EXIT + cadence + bookkeeping side of a rebalance and never auto-buys new stocks on a guessed
    screen (the founder's GVM/ret_1y entry methodology is not encoded here, so auto-entering picks
    into the ₹5L paper book would be unsafe). Steps:
      1. run the EOD checker (mark-to-market + HS1/HS2 hard-stop exits),
      2. re-fix allocation + park the NIFTYBEES cash residual (established convention),
      3. write a quant_rebalance_log row (exits, held count, portfolio value, entry-review note),
      4. advance next_rebalance by the basket's rebalance_freq (kept in the future).
    New-entry selection stays a founder-confirmed step; the log row surfaces the cap + ret_1y rule
    so the qualifying picks can be reviewed at the surfaced rebalance."""
    import qb_eod_checker
    today = datetime.now(IST).date()

    with conn.cursor() as cur:
        cur.execute("SELECT capital, max_stocks, rebalance_freq, next_rebalance "
                    "FROM quant_basket_registry WHERE basket_name=%s", (basket_name,))
        row = cur.fetchone()
    if not row:
        return {"error": f"{basket_name} not in registry"}
    capital, max_stocks, freq, next_reb = float(row[0]), int(row[1]), row[2], row[3]

    # 1) exits (marks + HS1/HS2)
    eod = qb_eod_checker.run_eod_checker(conn, basket_name=basket_name)
    hs1 = eod.get("hard_stop_1_exits", []) or []
    hs2 = eod.get("hard_stop_2_exits", []) or []
    exits = len(hs1) + len(hs2)

    # 2) allocation fix + NIFTYBEES residual (established convention)
    alloc = None
    try:
        alloc = fix_basket_overdeployment(conn, basket_name)
    except Exception as e:
        log.warning(f"run_scheduled_rebalance {basket_name}: alloc fix failed: {e}")

    # 3) held count + portfolio value after exits
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COALESCE(SUM(current_value),0) "
                    "FROM quant_paper_positions WHERE basket_name=%s AND status='open'", (basket_name,))
        hc = cur.fetchone()
    held = int(hc[0] or 0); pv = float(hc[1] or 0)

    # 4) advance next_rebalance, keeping it strictly in the future even if long overdue
    base = next_reb or today
    new_next = _next_rebalance_date(base, freq)
    while new_next <= today:
        new_next = _next_rebalance_date(new_next, freq)

    actions = {
        "exits_hs1": hs1, "exits_hs2": hs2, "held_after_exits": held,
        "cap_max_stocks": max_stocks, "entry_priority": "highest ret_1y (1-year return)",
        "entry_note": (f"Exits + cadence processed. New-entry selection (top {max_stocks} by GVM, "
                       f">{max_stocks} candidates broken by highest ret_1y) is a founder-confirmed step "
                       f"— not auto-bought into the ₹5L book here."),
        "was_due": str(base), "advanced_to": str(new_next),
        "alloc_residual": (alloc or {}).get("residual"),
    }
    with conn.cursor() as cur:
        cur.execute("INSERT INTO quant_rebalance_log "
                    "(basket_name, rebalance_date, stocks_in, stocks_out, stocks_held, "
                    " total_portfolio_value, actions) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (basket_name, today, 0, exits, held, pv, json.dumps(actions)))
        cur.execute("UPDATE quant_basket_registry SET next_rebalance=%s, updated_at=NOW() "
                    "WHERE basket_name=%s", (new_next, basket_name))
    conn.commit()

    return {"status": "ok", "basket": basket_name, "exits": exits, "held": held,
            "portfolio_value": pv, "was_due": str(base), "advanced_to": str(new_next),
            "cap_max_stocks": max_stocks}
