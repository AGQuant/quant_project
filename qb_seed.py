"""
qb_seed.py — cc#838 P1: INITIAL SEED for Quant Baskets, registry-derived.

WHY THIS FILE EXISTS (the cc#838 diagnosis, stated in code because it is the reason for the design):

The founder's report was "contra_value + breakout_52w built 19-Jul, never kicked off". Both of the
spec's suspects were wrong, and the real cause is worse than either:

  (a) The scheduler is NOT hardcoded. scheduler._bg_qb_eod already enumerates
      `quant_basket_registry WHERE is_active=TRUE`, so both baskets HAVE been visited nightly since
      19-Jul. (qb_endpoints.BASKETS is a hardcoded 4-name list, but it only serves
      /fix_all_allocations — it is not on the seed or rebalance path.)

  (b) There is NO initial-seed path anywhere. And qb_rebalance.run_scheduled_rebalance — the thing
      that fires on the due date — DELIBERATELY NEVER BUYS. Its own docstring says so: it processes
      "the EXIT + cadence + bookkeeping side" and leaves new-entry selection as a founder-confirmed
      step. It writes stocks_in=0 unconditionally.

So the 06-Aug rebalance would have run, logged a row, advanced the cadence to the next quarter and
still left both baskets at zero positions. Waiting for 06-Aug was never going to work.

WHAT THIS FIXES STRUCTURALLY. A basket is seeded from its OWN selector, discovered BY CONVENTION
from the registry row: basket_name `foo` -> module `qb_foo_select` exposing propose_rebalance().
Nothing here enumerates basket names. Adding a 7th basket = one registry row + one selector file
named by the convention; no code in this module, the scheduler, or qb_endpoints changes. That is the
step_3 requirement ("a new registry row must never again require a code change to participate").

WHAT THIS DELIBERATELY DOES NOT CHANGE. The four legacy baskets keep their founder-confirmed entry
doctrine exactly as it is. Seeding fires ONLY for a basket that has never held a position AND ships
its own encoded selector — i.e. the selector IS the founder's confirmation, written down as a locked
spec (contra id=6104, breakout id=6103) rather than typed at a prompt. A basket with no selector
module is skipped and says so.
"""

import os
import json
import math
import logging
from datetime import datetime
from typing import Optional, Dict, Any

import psycopg2
import psycopg2.extras
from zoneinfo import ZoneInfo

log = logging.getLogger("scorr.qb_seed")
IST = ZoneInfo("Asia/Kolkata")

HS1_STOP_PCT = 0.20    # HS1 hard stop, the established -20% convention on every existing position


def _conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def selector_for(basket_name: str):
    """Convention-over-configuration: basket `foo` -> module `qb_foo_select`.

    Registry has no selector column and adding one would be a lock-taking ALTER on a live table
    (MAINTENANCE_LOCK_RULE, cc#351). The convention gives the same zero-code-change property for a
    new basket without touching the schema at all."""
    mod_name = f"qb_{basket_name}_select"
    try:
        mod = __import__(mod_name)
    except Exception:
        return None, mod_name
    return (mod if hasattr(mod, "propose_rebalance") else None), mod_name


def _entry_price(cur, symbol: str, price_date) -> Optional[tuple]:
    """Last completed daily close on or before price_date. Returns (price, actual_date).

    EOD close, never a live tick: a seed is a bookkeeping event dated to a session, and a paper book
    that marks its inception at an intraday price can never be reconciled against that session."""
    cur.execute("""SELECT close, price_date FROM raw_prices
                   WHERE symbol=%s AND price_date <= %s AND close > 0
                   ORDER BY price_date DESC LIMIT 1""", (symbol, price_date))
    r = cur.fetchone()
    if not r or r[0] is None:
        return None
    return float(r[0]), r[1]


def seed_basket(conn, basket_name: str, price_date=None, dry_run: bool = False) -> Dict[str, Any]:
    """Seed ONE basket from its own selector. Idempotent and refuses to double-seed."""
    today = datetime.now(IST).date()

    with conn.cursor() as cur:
        cur.execute("""SELECT capital, max_stocks, is_active, notes
                       FROM quant_basket_registry WHERE basket_name=%s""", (basket_name,))
        reg = cur.fetchone()
        if not reg:
            return {"basket": basket_name, "status": "skipped", "reason": "not in registry"}
        if not reg[2]:
            return {"basket": basket_name, "status": "skipped", "reason": "registry is_active=false"}

        # HARD IDEMPOTENCE GUARD. Any position ever — open OR exited — means this basket has a
        # history and a "seed" would be a second inception on top of it.
        cur.execute("SELECT COUNT(*) FROM quant_paper_positions WHERE basket_name=%s", (basket_name,))
        if cur.fetchone()[0] > 0:
            return {"basket": basket_name, "status": "skipped", "reason": "already has position history"}

        if price_date is None:
            cur.execute("SELECT MAX(price_date) FROM raw_prices")
            price_date = cur.fetchone()[0] or today

    mod, mod_name = selector_for(basket_name)
    if mod is None:
        return {"basket": basket_name, "status": "skipped",
                "reason": f"no selector module {mod_name}.propose_rebalance()"}

    try:
        prop = mod.propose_rebalance(conn=conn)
    except Exception as e:
        log.exception("qb_seed: selector %s failed", mod_name)
        return {"basket": basket_name, "status": "error", "reason": f"{mod_name}: {str(e)[:200]}"}

    entries = prop.get("entries") or []
    note = prop.get("selection_note")

    # A screen whose own rules say "hold cash" is a RESULT, not a failure — the breakout basket's
    # N<5 branch exists precisely to stay out of a dead tape. Log the inception either way so the
    # cc#839 NAV series has a clean t0 rather than an absent one.
    if not entries:
        if dry_run:
            return {"basket": basket_name, "status": "would_seed_cash", "entries": 0,
                    "selection_note": note, "price_date": str(price_date)}
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO quant_rebalance_log
                             (basket_name, rebalance_date, stocks_in, stocks_out, stocks_held,
                              total_portfolio_value, actions)
                           VALUES (%s,%s,0,0,0,%s,%s)""",
                        (basket_name, today, float(reg[0] or 0),
                         json.dumps({"event": "inception", "cc": 838, "entries": 0,
                                     "selection_note": note, "price_basis": str(price_date),
                                     "reason": "screen resolved to full cash under its own rules"})))
        conn.commit()
        return {"basket": basket_name, "status": "seeded_cash", "entries": 0,
                "selection_note": note, "price_date": str(price_date)}

    placed, unpriced, deployed = [], [], 0.0
    with conn.cursor() as cur:
        for e in entries:
            sym = e["symbol"]
            slot = float(e.get("slot_value") or 0)
            px = _entry_price(cur, sym, price_date)
            if px is None or slot <= 0:
                # Never invent a price. A symbol with no close in raw_prices is reported, not
                # guessed at — the cc#828 lesson, applied here before it can become a P&L number.
                unpriced.append(sym)
                continue
            price, actual_date = px
            qty = int(math.floor(slot / price))
            if qty < 1:
                unpriced.append(f"{sym} (slot {int(slot)} < 1 share at {price})")
                continue
            alloc = round(qty * price, 2)
            deployed += alloc
            if dry_run:
                placed.append({"symbol": sym, "price": price, "qty": qty, "allocation": alloc})
                continue
            cur.execute("""INSERT INTO quant_paper_positions
                             (basket_name, symbol, entry_price, entry_date, qty, allocation,
                              current_price, current_value, pnl, pnl_pct, stop_loss_price,
                              status, gvm_at_entry, g_at_entry, v_at_entry, m_at_entry, notes)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,'open',%s,%s,%s,%s,%s)""",
                        (basket_name, sym, price, actual_date, qty, alloc, price, alloc,
                         round(price * (1 - HS1_STOP_PCT), 2),
                         e.get("gvm"), e.get("g"), e.get("v"), e.get("m"),
                         f"cc#838 inception seed | selector={mod_name} | basis=EOD {actual_date}"))
            placed.append({"symbol": sym, "price": price, "qty": qty, "allocation": alloc})

    capital = float(reg[0] or 0)
    cash = round(capital - deployed, 2)
    if dry_run:
        return {"basket": basket_name, "status": "would_seed", "entries": len(placed),
                "unpriced": unpriced, "deployed": round(deployed, 2), "cash": cash,
                "selection_note": note, "price_date": str(price_date), "positions": placed}

    with conn.cursor() as cur:
        cur.execute("""INSERT INTO quant_rebalance_log
                         (basket_name, rebalance_date, stocks_in, stocks_out, stocks_held,
                          total_portfolio_value, actions)
                       VALUES (%s,%s,%s,0,%s,%s,%s)""",
                    (basket_name, today, len(placed), len(placed), round(deployed + cash, 2),
                     json.dumps({"event": "inception", "cc": 838, "selector": mod_name,
                                 "selection_note": note, "price_basis": str(price_date),
                                 "deployed": round(deployed, 2), "cash": cash,
                                 "unpriced": unpriced,
                                 "symbols": [p["symbol"] for p in placed]})))
        # step_4: inception date lands in the registry notes so cc#839's NAV series has a clean t0
        # for these two baskets — P&L and alpha start from the SEED date, not the 19-Jul build date.
        cur.execute("""UPDATE quant_basket_registry
                          SET notes = COALESCE(notes,'') ||
                              CASE WHEN COALESCE(notes,'')='' THEN '' ELSE ' | ' END ||
                              %s,
                              updated_at = NOW()
                        WHERE basket_name=%s""",
                    (f"inception={price_date} (cc#838 seed, {len(placed)} positions)", basket_name))
    conn.commit()
    log.info("qb_seed %s: %d positions, deployed %.0f, cash %.0f", basket_name, len(placed), deployed, cash)
    return {"basket": basket_name, "status": "seeded", "entries": len(placed),
            "unpriced": unpriced, "deployed": round(deployed, 2), "cash": cash,
            "selection_note": note, "price_date": str(price_date), "positions": placed}


def seed_pending(conn, dry_run: bool = False) -> Dict[str, Any]:
    """Seed every ACTIVE registry basket that has never held a position and ships a selector.

    This is what the scheduler calls. It enumerates from the registry, so it can never fall behind
    the registry the way the old arrangement did."""
    with conn.cursor() as cur:
        cur.execute("""SELECT r.basket_name FROM quant_basket_registry r
                       WHERE r.is_active
                         AND NOT EXISTS (SELECT 1 FROM quant_paper_positions p
                                         WHERE p.basket_name = r.basket_name)
                       ORDER BY r.basket_name""")
        pending = [r[0] for r in cur.fetchall()]
    results = []
    for b in pending:
        try:
            results.append(seed_basket(conn, b, dry_run=dry_run))
        except Exception as e:
            log.exception("qb_seed %s failed", b)
            results.append({"basket": b, "status": "error", "reason": str(e)[:200]})
    return {"pending": len(pending), "seeded": sum(1 for r in results if r.get("status") == "seeded"),
            "results": results}
