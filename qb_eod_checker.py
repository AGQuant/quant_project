"""
Quant Basket — EOD Stop-Loss Checker & Daily P&L Marker
=========================================================
Runs ONCE at EOD (after raw_prices is updated, ~21:00 IST).

Covers ALL quant baskets (large_cap, mid_cap, small_cap, alpha_multicap) — basket-agnostic.
The scheduler in main.py loops every basket with open positions and calls
this once per basket.

What it does:
  1. Marks all open positions to today's EOD close (unrealised P&L)
  2. Checks Hard Stop 1: stock down >= 20% from entry price
  3. Checks Hard Stop 2: (stock return since entry) - (Nifty return since entry) <= -10%
  4. Exits breached positions: status=exited_stop, realised P&L locked at EOD close
  5. Logs every run to quant_rebalance_log (actions JSONB)

Exit rules (spec locked 05-Jun-2026):
  Hard Stop 1: stock down >= 20% from entry → immediate exit
  Hard Stop 2: stock vs Nifty50 <= -10% from entry date → immediate exit (ALL baskets)
  Filter Exit: stock not in top-20 qualified at monthly rebalance (4th of month) → exit

What it does NOT do:
  - Does NOT re-run universe filters (that is monthly rebalance, separate engine)
  - Does NOT touch NIFTYBEES position stops (cash parking, no stop)
  - Does NOT change weights or quantities

Tables touched:
  quant_paper_positions  — updates current_price, current_value, pnl, pnl_pct, status
  quant_rebalance_log    — appends one row per run

Also provides:
  qb_intraday_mark(conn) — intraday price-only mark via Yahoo chart API.
    Runs every 15 min during market hours. Updates current_price/value/pnl/pnl_pct
    for ALL open QB positions across ALL baskets. No stop exits — EOD only.

Timezone: always IST (Asia/Kolkata, UTC+5:30). Never UTC.
"""

import logging
import json
import re
import time
import urllib.parse
from datetime import datetime, date, timezone, timedelta
from typing import Dict, List, Optional

import requests

log = logging.getLogger("scorr.qb_eod")

IST = timezone(timedelta(hours=5, minutes=30))
NIFTY_SYMBOL   = "NIFTY50"
NIFTYBEES_SYMBOL = "NIFTYBEES"
HARD_STOP_PCT  = -20.0   # stock down 20% from entry
REL_STOP_PCT   = -10.0   # stock underperforms Nifty by 10% from entry date (all baskets)

# Robust: pull the first decimal number after "Nifty entry=" regardless of
# surrounding separators (comma, pipe, trailing period, spaces).
_NIFTY_ENTRY_RE = re.compile(r"Nifty\s*entry\s*=\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)

_YAHOO_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"


def _safe_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct_change(current, base) -> Optional[float]:
    """Returns (current/base - 1) * 100. None if base is zero/None."""
    if not base or base == 0:
        return None
    return (float(current) / float(base) - 1) * 100


def _parse_nifty_entry(notes: str) -> Optional[float]:
    """Extract Nifty entry benchmark from a position's notes field.
    Robust to any separator format: 'Nifty entry=23382.60',
    'Nifty entry=23382.60 | theme=X', '...GOLDBEES=102. Nifty entry=23382.60. Exit:...'
    """
    if not notes:
        return None
    m = _NIFTY_ENTRY_RE.search(notes)
    if m:
        try:
            return float(m.group(1))
        except (ValueError, IndexError):
            return None
    return None


def _yahoo_ticker_eq(symbol: str) -> str:
    """NSE equity ticker for Yahoo. Handles special chars (& -> %26)."""
    indices = {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK"}
    if symbol in indices:
        return indices[symbol]
    return urllib.parse.quote(symbol, safe="") + ".NS"


def _fetch_yahoo_ltp(symbol: str) -> Optional[float]:
    """Fetch latest traded price for one equity symbol from Yahoo chart API.
    Returns float or None on any failure. One retry on empty result."""
    ticker = _yahoo_ticker_eq(symbol)
    url = _YAHOO_CHART.format(sym=ticker)
    params = {"interval": "1m", "range": "1d"}
    for attempt in range(2):
        try:
            r = requests.get(url, params=params,
                             headers={"User-Agent": _YAHOO_UA}, timeout=10)
            r.raise_for_status()
            data = r.json()
            chart = (data.get("chart") or {}).get("result") or []
            if not chart:
                if attempt == 0:
                    time.sleep(0.4)
                    continue
                return None
            res = chart[0]
            meta = res.get("meta") or {}
            price = meta.get("regularMarketPrice")
            if price:
                return float(price)
            closes = ((res.get("indicators") or {})
                      .get("quote", [{}])[0]
                      .get("close") or [])
            closes = [c for c in closes if c is not None]
            return float(closes[-1]) if closes else None
        except Exception as e:
            if attempt == 0:
                time.sleep(0.4)
                continue
            log.warning(f"qb_intraday: yahoo LTP failed {symbol}: {e}")
            return None
    return None


def qb_intraday_mark(conn) -> Dict:
    """
    Intraday price-only mark for ALL open QB positions across ALL baskets.
    Fetches latest price from Yahoo chart API (1-min, 1d range).
    Updates current_price, current_value, pnl, pnl_pct, updated_at.
    NO stop-loss exits — those are EOD-only.
    Runs every 15 min during market hours from main.py _live_loop.
    NIFTYBEES positions are marked but never stopped.
    """
    now = datetime.now(IST)

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, entry_price, qty
                FROM quant_paper_positions
                WHERE status = 'open'
                ORDER BY symbol
            """)
            cols = [d[0] for d in cur.description]
            positions = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"qb_intraday: positions fetch failed: {e}")
        return {"status": "error", "error": str(e)}

    if not positions:
        return {"status": "ok", "marked": 0, "msg": "no open positions"}

    symbols = list(dict.fromkeys(p["symbol"] for p in positions))

    prices: Dict[str, Optional[float]] = {}
    for sym in symbols:
        prices[sym] = _fetch_yahoo_ltp(sym)
        time.sleep(0.3)

    marked, skipped, errors = 0, 0, []
    for pos in positions:
        sym         = pos["symbol"]
        entry_price = _safe_float(pos["entry_price"])
        qty         = _safe_float(pos["qty"])
        ltp         = prices.get(sym)

        if ltp is None:
            skipped += 1
            continue

        pnl      = (ltp - entry_price) * qty if entry_price and qty else None
        curr_val = ltp * qty if qty else None
        pnl_pct  = _pct_change(ltp, entry_price)

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE quant_paper_positions SET
                        current_price = %s,
                        current_value = %s,
                        pnl           = %s,
                        pnl_pct       = %s,
                        updated_at    = NOW()
                    WHERE id = %s
                """, (
                    round(ltp, 2),
                    round(curr_val, 2) if curr_val is not None else None,
                    round(pnl, 2)     if pnl      is not None else None,
                    round(pnl_pct, 4) if pnl_pct  is not None else None,
                    pos["id"]
                ))
            conn.commit()
            marked += 1
        except Exception as e:
            conn.rollback()
            errors.append(f"{sym}: {str(e)[:60]}")
            log.warning(f"qb_intraday: update failed {sym}: {e}")

    result = {
        "status":  "ok",
        "run_at":  now.strftime("%H:%M IST"),
        "symbols": len(symbols),
        "marked":  marked,
        "skipped": skipped,
        "errors":  errors[:10],
    }
    log.info(f"qb_intraday_mark: {marked}/{len(positions)} marked, "
             f"{skipped} skipped, {len(errors)} errors")
    return result


def run_eod_checker(conn, basket_name: str = "large_cap") -> Dict:
    """
    Main entry point. Run once at EOD after raw_prices updated.
    Returns summary dict.
    """
    today = datetime.now(IST).date()
    summary = {
        "basket":              basket_name,
        "run_date":            str(today),
        "positions_marked":    0,
        "hard_stop_1_exits":   [],   # down 20%
        "hard_stop_2_exits":   [],   # vs Nifty <= -10% (all baskets)
        "total_unrealised_pnl": 0.0,
        "total_realised_pnl":  0.0,
        "errors":              []
    }

    # ── Step 1: Get today's Nifty close ──────────────────────────────────────
    nifty_today = None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT close FROM raw_prices
                WHERE symbol=%s AND price_date=(SELECT MAX(price_date) FROM raw_prices WHERE symbol=%s)
            """, (NIFTY_SYMBOL, NIFTY_SYMBOL))
            row = cur.fetchone()
            nifty_today = _safe_float(row[0]) if row else None
    except Exception as e:
        summary["errors"].append(f"nifty_fetch: {e}")
        log.error(f"qb_eod: nifty fetch failed: {e}")

    if nifty_today is None:
        log.warning(f"qb_eod: no Nifty close — Hard Stop 2 will be skipped this run")

    # ── Step 2: Load all open positions ──────────────────────────────────────
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, entry_price, entry_date, qty,
                       stop_loss_price, notes
                FROM quant_paper_positions
                WHERE basket_name=%s AND status='open'
                ORDER BY symbol
            """, (basket_name,))
            cols = [d[0] for d in cur.description]
            positions = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        summary["errors"].append(f"positions_fetch: {e}")
        log.error(f"qb_eod: positions fetch failed: {e}")
        return summary

    if not positions:
        log.info(f"qb_eod: no open positions for {basket_name}")
        return summary

    # ── Step 3: Process each position ────────────────────────────────────────
    actions = []
    for pos in positions:
        sym         = pos["symbol"]
        entry_price = _safe_float(pos["entry_price"])
        entry_date  = pos["entry_date"]
        qty         = _safe_float(pos["qty"])
        nifty_entry = _parse_nifty_entry(pos.get("notes", ""))

        # NIFTYBEES = cash parking, never stopped
        if sym == NIFTYBEES_SYMBOL:
            eod_close = _fetch_yahoo_ltp(NIFTYBEES_SYMBOL)
            if eod_close and qty and entry_price:
                pnl       = round((eod_close - entry_price) * qty, 2)
                curr_val  = round(eod_close * qty, 2)
                pnl_pct   = _pct_change(eod_close, entry_price)
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE quant_paper_positions SET
                            current_price=%s, current_value=%s,
                            pnl=%s, pnl_pct=%s, updated_at=NOW()
                        WHERE id=%s
                    """, (eod_close, curr_val, pnl,
                          round(pnl_pct, 4) if pnl_pct else None, pos["id"]))
                conn.commit()
                summary["positions_marked"] += 1
                summary["total_unrealised_pnl"] += pnl
            continue

        # Get today's EOD close
        eod_close = None
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT close FROM raw_prices
                    WHERE symbol=%s
                    AND price_date=(SELECT MAX(price_date) FROM raw_prices WHERE symbol=%s)
                """, (sym, sym))
                row = cur.fetchone()
                eod_close = _safe_float(row[0]) if row else None
        except Exception as e:
            summary["errors"].append(f"{sym}_price: {e}")
            continue

        if not eod_close:
            log.warning(f"qb_eod: no EOD price for {sym}, skipping")
            continue

        # Compute returns
        stock_ret  = _pct_change(eod_close, entry_price)
        nifty_ret  = _pct_change(nifty_today, nifty_entry) if nifty_today and nifty_entry else None
        vs_nifty   = (stock_ret - nifty_ret) if stock_ret is not None and nifty_ret is not None else None
        pnl        = (eod_close - entry_price) * qty if qty else None
        curr_value = eod_close * qty if qty else None

        # ── Hard Stop checks ──────────────────────────────────────────────────
        hs1_breach = stock_ret is not None and stock_ret <= HARD_STOP_PCT
        hs2_breach = vs_nifty  is not None and vs_nifty  <= REL_STOP_PCT

        exit_reason = None
        if hs1_breach:
            exit_reason = f"HARD_STOP_1: stock {stock_ret:.2f}% from entry (<= -20%)"
            summary["hard_stop_1_exits"].append(sym)
        elif hs2_breach:
            exit_reason = f"HARD_STOP_2: vs_nifty {vs_nifty:.2f}% (<= -10%)"
            summary["hard_stop_2_exits"].append(sym)

        new_status = "exited_stop" if exit_reason else "open"

        # ── Update position ───────────────────────────────────────────────────
        try:
            with conn.cursor() as cur:
                # cc#439: cast the nullable params (curr_value/pnl/pnl_pct numeric, exit_reason text)
                # so psycopg3 can infer their type when they are NULL. A bare None in `CASE WHEN %s IS
                # NOT NULL` raised "could not determine data type of parameter $6" -> the whole EOD
                # transaction aborted -> positions never marked and HS1/HS2 exits never recorded.
                cur.execute("""
                    UPDATE quant_paper_positions SET
                        current_price  = %s,
                        current_value  = %s::numeric,
                        pnl            = %s::numeric,
                        pnl_pct        = %s::numeric,
                        status         = %s,
                        exit_price     = CASE WHEN %s::text IS NOT NULL THEN %s ELSE exit_price END,
                        exit_date      = CASE WHEN %s::text IS NOT NULL THEN %s ELSE exit_date END,
                        updated_at     = NOW()
                    WHERE id = %s
                """, (
                    eod_close, curr_value, pnl,
                    round(stock_ret, 4) if stock_ret is not None else None,
                    new_status,
                    exit_reason, eod_close,
                    exit_reason, today,
                    pos["id"]
                ))
            conn.commit()
        except Exception as e:
            summary["errors"].append(f"{sym}_update: {e}")
            log.error(f"qb_eod: update failed for {sym}: {e}")
            continue

        summary["positions_marked"] += 1
        if new_status == "open":
            summary["total_unrealised_pnl"] += pnl or 0
        else:
            summary["total_realised_pnl"] += pnl or 0

        actions.append({
            "symbol":        sym,
            "eod_close":     eod_close,
            "stock_ret_pct": round(stock_ret, 4) if stock_ret is not None else None,
            "vs_nifty_pct":  round(vs_nifty, 4)  if vs_nifty  is not None else None,
            "pnl":           round(pnl, 2)        if pnl       is not None else None,
            "status":        new_status,
            "exit_reason":   exit_reason
        })

        if exit_reason:
            log.warning(f"qb_eod: EXIT {sym} | {exit_reason} | EOD close={eod_close}")

    summary["total_unrealised_pnl"] = round(summary["total_unrealised_pnl"], 2)
    summary["total_realised_pnl"]   = round(summary["total_realised_pnl"], 2)

    # ── Step 4: Log to quant_rebalance_log ───────────────────────────────────
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO quant_rebalance_log
                (basket_name, rebalance_date, stocks_in, stocks_out,
                 stocks_held, total_portfolio_value, actions, computed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            """, (
                basket_name, today, 0,
                len(summary["hard_stop_1_exits"]) + len(summary["hard_stop_2_exits"]),
                summary["positions_marked"] - len(summary["hard_stop_1_exits"]) - len(summary["hard_stop_2_exits"]),
                round(summary["total_unrealised_pnl"] + summary["total_realised_pnl"], 2),
                json.dumps({
                    "type":           "eod_stop_check",
                    "nifty_today":    nifty_today,
                    "unrealised_pnl": summary["total_unrealised_pnl"],
                    "realised_pnl":   summary["total_realised_pnl"],
                    "hard_stop_1":    summary["hard_stop_1_exits"],
                    "hard_stop_2":    summary["hard_stop_2_exits"],
                    "positions":      actions
                })
            ))
        conn.commit()
    except Exception as e:
        summary["errors"].append(f"rebalance_log: {e}")
        log.error(f"qb_eod: rebalance_log write failed: {e}")

    log.info(
        f"qb_eod done | basket={basket_name} | marked={summary['positions_marked']} | "
        f"HS1_exits={summary['hard_stop_1_exits']} | HS2_exits={summary['hard_stop_2_exits']} | "
        f"unrealised={summary['total_unrealised_pnl']} | realised={summary['total_realised_pnl']}"
    )
    return summary
