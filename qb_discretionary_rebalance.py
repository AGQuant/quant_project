"""
qb_discretionary_rebalance.py — cc#1715 scope 3: NEED-BASIS rebalance for a DISCRETIONARY basket.

    POST /api/qb/discretionary/rebalance      (X-Admin-Token)
    body {basket_name, as_of_date, sells:[{symbol, reason}], buys:[{symbol, slot_rs}], dry_run, note}

A discretionary basket (app_config qb_discretionary_baskets — model_portfolio, finz_*) has no
selection engine and is never rescreened (qb_rebalance._CANDIDATE_ENGINES has no entry for it).
Its holdings change only on founder instruction. This endpoint is that instruction, written down:
in ONE transaction it
  * closes every SELL at the as_of_date raw_prices close — status exited_rank, exit_price,
    exit_date, reason kept in notes and in the log row so the cc#1709 block shows it as a SELL;
  * opens every BUY at that close with qty = nearest whole share of slot_rs / close (founder rule
    05-Sep), allocation = qty * close, stop_loss_price = 0.8 * close (HS1 -20%, same as the seed);
  * writes ONE quant_rebalance_log row (actions.type = 'rebalance', was_due = as_of_date, the
    sells/buys arrays with qty/price/amount, cash before/after, held_after) so the QB page and the
    V8 Model Portfolio tab render it as a block with state DONE;
and REFUSES — writing nothing, HTTP 409 with the shortfall — when the buys would push holdings
above quant_basket_registry.max_stocks or cash below zero, when a sell is not currently held, a
buy is already held, or a symbol has no close on as_of_date. dry_run=true returns the same plan
and writes nothing. Only baskets in qb_discretionary_baskets are accepted.

Cash basis = the qb_nav convention: capital - SUM(allocation of every position ever entered)
+ SUM(qty * exit_price of exited positions). Read-only helpers imported from qb_endpoints
(_conn, _check_admin, _discretionary_baskets); nothing under worker/** and no engine file touched.
"""
import json
import logging
import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from qb_endpoints import _conn, _check_admin, _discretionary_baskets

log = logging.getLogger("qb_discretionary")
router = APIRouter(prefix="/api/qb/discretionary", tags=["qb-discretionary"])

CARD = "cc#1715"
HS1_STOP_MULT = 0.80          # stop_loss_price = 0.8 * entry close (HS1 -20%), same as the seed rows


class SellLeg(BaseModel):
    symbol: str
    reason: str = "founder instruction"


class BuyLeg(BaseModel):
    symbol: str
    slot_rs: float = Field(gt=0)


class RebalanceBody(BaseModel):
    basket_name: str
    as_of_date: date
    sells: List[SellLeg] = []
    buys: List[BuyLeg] = []
    dry_run: bool = False
    note: Optional[str] = None


def _r2(x) -> float:
    return round(float(x), 2)


def _whole_shares(slot_rs: float, close: float) -> int:
    """Nearest whole share (founder rule 05-Sep): 15.3 -> 15, 15.5 -> 16. Never banker's rounding."""
    return int(math.floor(slot_rs / close + 0.5))


def plan_rebalance(reg: Dict[str, Any], open_positions: List[Dict[str, Any]], cash_before: float,
                   closes: Dict[str, float], sells: List[Dict[str, Any]], buys: List[Dict[str, Any]],
                   as_of: date) -> Dict[str, Any]:
    """Pure: no DB. Returns {"ok": bool, "refusals": [...], "sells": [...], "buys": [...],
    "cash_before", "cash_after", "shortfall_rs", "held_before", "held_after", "max_stocks"}.
    open_positions rows: {id, symbol, qty, entry_price, allocation, close}. closes: symbol -> close
    ON as_of (exact date) for every sell/buy symbol."""
    refusals: List[str] = []
    max_stocks = int(reg.get("max_stocks") or 0)
    open_by_sym = {p["symbol"]: p for p in open_positions}

    seen = set()
    for leg in list(sells) + list(buys):
        s = leg["symbol"]
        if s in seen:
            refusals.append(f"{s}: listed twice")
        seen.add(s)

    sell_legs = []
    for leg in sells:
        s = leg["symbol"]
        p = open_by_sym.get(s)
        if not p:
            refusals.append(f"{s}: not an open holding of this basket")
            continue
        px = closes.get(s)
        if px is None:
            refusals.append(f"{s}: no raw_prices close on {as_of}")
            continue
        qty = int(p["qty"])
        amt = _r2(qty * px)
        entry = float(p["entry_price"])
        sell_legs.append({"id": p["id"], "symbol": s, "qty": qty, "price": _r2(px), "amount": amt,
                          "entry_price": _r2(entry), "pnl": _r2(qty * (px - entry)),
                          "pnl_pct": _r2((px / entry - 1) * 100) if entry else None,
                          "reason": leg.get("reason") or "founder instruction"})

    buy_legs = []
    for leg in buys:
        s = leg["symbol"]
        if s in open_by_sym and s not in {x["symbol"] for x in sell_legs}:
            refusals.append(f"{s}: already held — sell it first or skip")
            continue
        px = closes.get(s)
        if px is None:
            refusals.append(f"{s}: no raw_prices close on {as_of}")
            continue
        qty = _whole_shares(float(leg["slot_rs"]), px)
        if qty < 1:
            refusals.append(f"{s}: slot Rs {leg['slot_rs']} buys 0 whole shares at {px}")
            continue
        buy_legs.append({"symbol": s, "qty": qty, "price": _r2(px), "amount": _r2(qty * px),
                         "slot_rs": _r2(leg["slot_rs"]), "stop_loss_price": _r2(px * HS1_STOP_MULT)})

    held_before = len(open_positions)
    held_after = held_before - len(sell_legs) + len(buy_legs)
    cash_after = _r2(cash_before + sum(x["amount"] for x in sell_legs) - sum(x["amount"] for x in buy_legs))
    shortfall = _r2(-cash_after) if cash_after < 0 else 0.0
    if max_stocks and held_after > max_stocks:
        refusals.append(f"holdings after = {held_after} > registry max_stocks {max_stocks}")
    if cash_after < 0:
        refusals.append(f"cash after = Rs {cash_after:.2f} < 0 (shortfall Rs {shortfall:.2f})")
    return {"ok": not refusals, "refusals": refusals, "sells": sell_legs, "buys": buy_legs,
            "cash_before": _r2(cash_before), "cash_after": cash_after, "shortfall_rs": shortfall,
            "held_before": held_before, "held_after": held_after, "max_stocks": max_stocks}


def _load_closes(cur, symbols: List[str], as_of: date) -> Dict[str, float]:
    if not symbols:
        return {}
    cur.execute("SELECT symbol, close FROM raw_prices WHERE symbol = ANY(%s) AND price_date = %s AND close > 0",
                (list(symbols), as_of))
    return {r[0]: float(r[1]) for r in cur.fetchall()}


def _latest_closes(cur, symbols: List[str], as_of: date) -> Dict[str, float]:
    if not symbols:
        return {}
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices
                   WHERE symbol = ANY(%s) AND price_date <= %s AND close > 0
                   ORDER BY symbol, price_date DESC""", (list(symbols), as_of))
    return {r[0]: float(r[1]) for r in cur.fetchall()}


@router.post("/rebalance")
def discretionary_rebalance(body: RebalanceBody, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    basket = body.basket_name.strip()
    as_of = body.as_of_date
    sells = [{"symbol": s.symbol.strip().upper(), "reason": s.reason} for s in body.sells]
    buys = [{"symbol": b.symbol.strip().upper(), "slot_rs": b.slot_rs} for b in body.buys]
    if not sells and not buys:
        return JSONResponse(status_code=400, content={"ok": False, "error": "nothing to do: sells and buys both empty"})

    with _conn() as conn, conn.cursor() as cur:
        if basket not in _discretionary_baskets(cur):
            return JSONResponse(status_code=400, content={
                "ok": False, "error": f"{basket} is not in app_config qb_discretionary_baskets — "
                                      "quant baskets rebalance through their own engine, not here"})
        cur.execute("SELECT basket_name, capital, max_stocks, next_rebalance FROM quant_basket_registry "
                    "WHERE basket_name=%s", (basket,))
        r = cur.fetchone()
        if not r:
            return JSONResponse(status_code=404, content={"ok": False, "error": f"{basket} not in quant_basket_registry"})
        reg = {"basket_name": r[0], "capital": float(r[1] or 0), "max_stocks": int(r[2] or 0),
               "next_rebalance": str(r[3]) if r[3] else None}

        cur.execute("""SELECT id, symbol, qty, entry_price, allocation FROM quant_paper_positions
                       WHERE basket_name=%s AND status='open' ORDER BY symbol""", (basket,))
        open_positions = [{"id": a, "symbol": b, "qty": c, "entry_price": d, "allocation": e}
                          for a, b, c, d, e in cur.fetchall()]
        cur.execute("""SELECT COALESCE(SUM(allocation),0),
                              COALESCE(SUM(CASE WHEN exit_date IS NOT NULL THEN qty*exit_price END),0)
                       FROM quant_paper_positions WHERE basket_name=%s""", (basket,))
        entered, realised = cur.fetchone()
        cash_before = reg["capital"] - float(entered or 0) + float(realised or 0)

        legs_syms = sorted({x["symbol"] for x in sells} | {x["symbol"] for x in buys})
        closes = _load_closes(cur, legs_syms, as_of)
        plan = plan_rebalance(reg, open_positions, cash_before, closes, sells, buys, as_of)

        # book value after = remaining holdings at their latest close on/before as_of + buys + cash
        sold = {x["symbol"] for x in plan["sells"]}
        remaining = [p for p in open_positions if p["symbol"] not in sold]
        rem_close = _latest_closes(cur, [p["symbol"] for p in remaining], as_of)
        holdings_after = sum(float(p["qty"]) * rem_close.get(p["symbol"], float(p["entry_price"])) for p in remaining) \
            + sum(x["amount"] for x in plan["buys"])
        tpv_after = _r2(holdings_after + plan["cash_after"])
        held_syms_after = sorted([p["symbol"] for p in remaining] + [x["symbol"] for x in plan["buys"]])

        note = body.note or "founder instruction (need basis)"
        actions = {
            "type": "rebalance", "card": CARD, "was_due": str(as_of), "source": note,
            "entry_status": "executed", "discretionary": True,
            "sells": [{k: v for k, v in x.items() if k != "id"} for x in plan["sells"]],
            "buys": plan["buys"],
            "entries": [x["symbol"] for x in plan["buys"]],
            "exits_rank": [x["symbol"] for x in plan["sells"]],
            "exits_hs1": [], "exits_hs2": [],
            "cash_before": plan["cash_before"], "cash_after": plan["cash_after"],
            "alloc_residual": plan["cash_after"],
            "held_before": plan["held_before"], "held_after": plan["held_after"],
            "held_symbols_after": held_syms_after,
            "sizing": "nearest whole share of slot_rs / close (founder rule 05-Sep); stop = 0.8 x close",
            # cc#1709 block renderer reads exit reasons off actions.positions[] of the row dated exit_date
            "positions": [{"symbol": x["symbol"], "status": "exited_rank", "eod_close": x["price"],
                           "stock_ret_pct": x["pnl_pct"], "pnl": x["pnl"],
                           "exit_reason": "FOUNDER: " + str(x["reason"])} for x in plan["sells"]],
        }
        preview = {"ok": plan["ok"], "dry_run": body.dry_run, "basket": basket, "as_of_date": str(as_of),
                   "sells": actions["sells"], "buys": plan["buys"], "refusals": plan["refusals"],
                   "cash_before": plan["cash_before"], "cash_after": plan["cash_after"],
                   "shortfall_rs": plan["shortfall_rs"], "held_before": plan["held_before"],
                   "held_after": plan["held_after"], "max_stocks": plan["max_stocks"],
                   "total_portfolio_value_after": tpv_after, "next_review": reg["next_rebalance"],
                   "log_preview": {"basket_name": basket, "rebalance_date": str(as_of),
                                   "stocks_in": len(plan["buys"]), "stocks_out": len(plan["sells"]),
                                   "stocks_held": plan["held_after"], "total_portfolio_value": tpv_after,
                                   "actions": actions}}
        if not plan["ok"]:
            preview["error"] = "refused — nothing written"
            return JSONResponse(status_code=409, content=preview)
        if body.dry_run:
            preview["written"] = False
            return preview

        # ---- ONE transaction ------------------------------------------------------------------
        gvm = {}
        if plan["buys"]:
            cur.execute("""SELECT symbol, gvm_score, g_score, v_score, m_score FROM gvm_scores
                           WHERE symbol = ANY(%s) AND score_date = (SELECT MAX(score_date) FROM gvm_scores)""",
                        ([x["symbol"] for x in plan["buys"]],))
            gvm = {r[0]: r[1:] for r in cur.fetchall()}
        tag = f"discretionary rebalance {as_of} ({CARD}) | {note}"
        for x in plan["sells"]:
            cur.execute("""UPDATE quant_paper_positions SET status='exited_rank', exit_price=%s, exit_date=%s,
                               current_price=%s, current_value=%s, pnl=%s, pnl_pct=%s,
                               notes = COALESCE(notes,'') || %s, updated_at=NOW()
                           WHERE id=%s AND status='open'""",
                        (x["price"], as_of, x["price"], x["amount"], x["pnl"], x["pnl_pct"],
                         f" | {tag} | sold: {x['reason']}", x["id"]))
        for x in plan["buys"]:
            g = gvm.get(x["symbol"], (None, None, None, None))
            cur.execute("""INSERT INTO quant_paper_positions
                               (basket_name, symbol, entry_price, entry_date, qty, allocation, current_price,
                                current_value, pnl, pnl_pct, stop_loss_price, status,
                                gvm_at_entry, g_at_entry, v_at_entry, m_at_entry, notes, created_at, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,'open',%s,%s,%s,%s,%s,NOW(),NOW())""",
                        (basket, x["symbol"], x["price"], as_of, x["qty"], x["amount"], x["price"], x["amount"],
                         x["stop_loss_price"], g[0], g[1], g[2], g[3],
                         f"{tag} | slot Rs {x['slot_rs']:.2f} -> {x['qty']} whole shares at {x['price']}"))
        cur.execute("""INSERT INTO quant_rebalance_log
                           (basket_name, rebalance_date, stocks_in, stocks_out, stocks_held, total_portfolio_value,
                            actions, computed_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,NOW()) RETURNING id""",
                    (basket, as_of, len(plan["buys"]), len(plan["sells"]), plan["held_after"], tpv_after,
                     json.dumps(actions, default=str)))
        log_id = cur.fetchone()[0]
        conn.commit()
    preview["written"] = True
    preview["rebalance_log_id"] = log_id
    log.info("%s discretionary rebalance %s %s: -%d +%d cash %.2f -> %.2f (log %s)", CARD, basket, as_of,
             len(plan["sells"]), len(plan["buys"]), plan["cash_before"], plan["cash_after"], log_id)
    return preview
