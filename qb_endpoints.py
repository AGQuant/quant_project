"""
Quant Basket (QB) endpoints — EQUITY buy-and-hold baskets.

Extracted from main.py (refactor file 1/5, 04-Jun-2026) to keep pushes small.
Self-contained: own _conn, own api_query, own _check_admin. Imports nothing
from main.py to avoid circular imports.

Endpoints (all /api/qb/*):
  POST /api/qb/eod_check            — run EOD stop-loss + P&L mark for one basket
  POST /api/qb/eod_check_all        — run EOD check for every basket with open positions
  POST /api/qb/mark_intraday        — intraday P&L mark
  POST /api/qb/fix_allocations      — fix allocation column + add NIFTYBEES residual for one basket
  POST /api/qb/fix_all_allocations  — fix all 4 baskets at once
  GET  /api/qb/positions            — open/closed positions with P&L
  GET  /api/qb/summary              — basket summary (market value, unreal/real P&L)
  GET  /api/qb/rebalance_log        — raw rebalance + EOD check history (one row per night)
  GET  /api/qb/rebalance_history    — cc#1703: same log, classified (rebalance/stop exit/cash
                                       move shown by default, nightly checks behind a count)
  GET  /api/qb/registry             — basket registry
  GET  /api/qb/gated_rebalances     — cc#1704: {basket: {due, n_candidates}} for every basket
                                       still awaiting a founder confirm — powers the card pill
  POST /api/qb/rebalance/confirm    — cc#1704: buy a gated rebalance's stored candidates
  POST /api/qb/rebalance/skip       — cc#1704: dismiss a gated rebalance's candidates, no buy
"""

from fastapi import APIRouter, HTTPException, Header
from typing import Optional
from datetime import datetime
import psycopg
import json
import os

import qb_eod_checker
import qb_rebalance
import qb_alpha_select   # cc#553: Alpha Multicap V2 FINAL selection/proposal engine (spec id=6086)
import qb_smallcap_select # cc#554: Small Cap V2 selection/proposal engine (spec id=6094)
import qb_composite_select # cc#555+556: parameterized Large Cap V2 (id=6097) + Mid Cap V2 (id=6098)
import qb_breakout_select  # cc#559: 52-Week Breakout basket (5th QB) selection/proposal (spec id=6103)
import qb_contra_select    # cc#560: Contra Value basket (6th QB) selection/proposal (spec id=6104)

router = APIRouter(prefix="/api/qb", tags=["quant_basket"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
BASKETS     = ["large_cap", "mid_cap", "small_cap", "alpha_multicap"]


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


def api_query(sql, params=None, single=False):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d[0] for d in cur.description] if cur.description else []
            if single:
                r = cur.fetchone()
                return dict(zip(cols, r)) if r else None
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        return {"error": str(e)}


@router.post("/eod_check")
def qb_eod_check_now(basket_name: str = "large_cap", x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with _conn() as conn:
        return qb_eod_checker.run_eod_checker(conn, basket_name=basket_name)


@router.post("/eod_check_all")
def qb_eod_check_all(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    out = []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT basket_name FROM quant_paper_positions WHERE status='open'")
            baskets = [r[0] for r in cur.fetchall()]
        for b in baskets:
            out.append(qb_eod_checker.run_eod_checker(conn, basket_name=b))
    return {"baskets_run": len(out), "results": out}


@router.post("/mark_intraday")
async def qb_mark_intraday_now(x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    with _conn() as conn:
        return qb_eod_checker.qb_intraday_mark(conn)


@router.post("/rebalance_now")
def qb_rebalance_now(basket_name: str = "large_cap", x_admin_token: Optional[str] = Header(None)):
    """cc#439: run the scheduled rebalance for ONE basket (exits + NIFTYBEES residual + advance
    next_rebalance + log). New-stock entries stay a founder-confirmed step (see run_scheduled_rebalance)."""
    _check_admin(x_admin_token)
    with _conn() as conn:
        return qb_rebalance.run_scheduled_rebalance(conn, basket_name=basket_name)


@router.post("/rebalance_due")
def qb_rebalance_due(x_admin_token: Optional[str] = Header(None)):
    """cc#439: run the scheduled rebalance for every ACTIVE basket whose next_rebalance is due —
    the founder-approved overdue 06-Jul large_cap + mid_cap catch-up runs here (also runs nightly
    via scheduler._bg_qb_eod on trading days)."""
    _check_admin(x_admin_token)
    out = []
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT basket_name FROM quant_basket_registry "
                        "WHERE is_active=TRUE AND next_rebalance IS NOT NULL "
                        "AND next_rebalance <= CURRENT_DATE ORDER BY basket_name")
            due = [r[0] for r in cur.fetchall()]
        for b in due:
            out.append(qb_rebalance.run_scheduled_rebalance(conn, basket_name=b))
    return {"due": len(out), "results": out}


def _gated_row(cur, basket_name, rebalance_date):
    """cc#1704: the ONE row a confirm/skip call acts on — the specific was_due row for this
    basket+date still sitting entry_status='awaiting_founder'. Returns (actions_dict, id) or
    (None, None). Shared by confirm + skip so both enforce the exact same "which row" and
    "still actually gated" rule."""
    cur.execute("""SELECT id, actions FROM quant_rebalance_log
                   WHERE basket_name=%s AND rebalance_date=%s AND actions ? 'was_due'
                   ORDER BY computed_at DESC LIMIT 1""", (basket_name, rebalance_date))
    row = cur.fetchone()
    if not row:
        return None, None
    return (row[1] or {}), row[0]


@router.post("/rebalance/confirm")
def qb_rebalance_confirm(basket_name: str, rebalance_date: str,
                          x_admin_token: Optional[str] = Header(None)):
    """cc#1704 P5 (QB_REBALANCE_GATE_SURFACING_V1, session_log 38966): execute a founder-
    confirmed rebalance's stored candidates as real ₹5L paper positions.

    GATE, stated on the card itself: CC implements this endpoint but does NOT call it against any
    basket — only a founder action may. Admin-token gated like every other QB write endpoint in
    this file (_check_admin), same convention as /eod_check, /rebalance_now etc.

    Buys via compute_position_sizing (qb_rebalance.py) — the SAME sizing function the original
    engine already trusts, not new sizing logic — priced off cmp_resolver.resolve_cmp_many (the
    same batch price path list surfaces already use). A candidate already held (open position,
    same symbol) is skipped rather than double-bought. After the new positions land,
    fix_basket_overdeployment (also existing, already-used code) re-derives the allocation column
    and tops up/adds the NIFTYBEES cash residual against the NEW deployed capital — the confirm
    step does not hand-roll its own residual math."""
    _check_admin(x_admin_token)
    with _conn() as conn:
        with conn.cursor() as cur:
            actions, row_id = _gated_row(cur, basket_name, rebalance_date)
        if row_id is None:
            raise HTTPException(404, f"no rebalance row for {basket_name} on {rebalance_date}")
        if actions.get("entry_status") != "awaiting_founder":
            raise HTTPException(409, f"entry_status is {actions.get('entry_status')!r} — "
                                      "already confirmed/skipped, or this row never gated")
        candidates = actions.get("entry_candidates") or []
        symbols = [c.get("symbol") for c in candidates if c.get("symbol")]
        if not symbols:
            raise HTTPException(422, "no candidates on this row to buy")

        with conn.cursor() as cur:
            cur.execute("SELECT capital, max_stocks FROM quant_basket_registry WHERE basket_name=%s",
                        (basket_name,))
            reg = cur.fetchone()
            cur.execute("SELECT symbol FROM quant_paper_positions "
                        "WHERE basket_name=%s AND status='open'", (basket_name,))
            already_held = {r[0] for r in cur.fetchall()}
        if not reg:
            raise HTTPException(404, f"{basket_name} not in registry")
        capital, max_stocks = float(reg[0]), int(reg[1])

        to_buy = [s for s in symbols if s not in already_held]
        already_skipped = [s for s in symbols if s in already_held]
        if not to_buy:
            raise HTTPException(409, f"every candidate ({', '.join(symbols)}) is already an open "
                                      "position in this basket — nothing to buy")

        import cmp_resolver
        with conn.cursor() as cur:
            price_map = cmp_resolver.resolve_cmp_many(cur, to_buy)
        prices = {s: (price_map.get(s) or {}).get("cmp") for s in to_buy}

        sizing = qb_rebalance.compute_position_sizing(capital, max_stocks, to_buy, prices)
        today = datetime.now(qb_rebalance.IST).date()
        bought = []
        with conn.cursor() as cur:
            for p in sizing["positions"]:
                cur.execute("""INSERT INTO quant_paper_positions
                    (basket_name, symbol, entry_price, entry_date, qty, allocation,
                     current_price, current_value, pnl, pnl_pct, status, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,'open',%s)
                    ON CONFLICT (basket_name, symbol, entry_date) DO NOTHING""",
                    (basket_name, p["symbol"], p["cmp"], today, p["qty"], p["cost"],
                     p["cmp"], p["cost"],
                     f"cc#1704 rebalance confirm — candidates due {rebalance_date}"))
                bought.append(p["symbol"])
        conn.commit()

        no_price = [s for s in to_buy if not prices.get(s)]
        alloc = None
        try:
            alloc = qb_rebalance.fix_basket_overdeployment(conn, basket_name)
        except Exception as e:
            qb_rebalance.log.warning(f"qb_rebalance_confirm {basket_name}: allocation/residual fix failed: {e}")

        actions["entry_status"] = "confirmed"
        actions["confirmed_at"] = datetime.now(qb_rebalance.IST).isoformat()
        actions["confirmed_symbols"] = bought
        actions["confirmed_already_held_skipped"] = already_skipped
        actions["confirmed_no_price_skipped"] = no_price
        with conn.cursor() as cur:
            cur.execute("UPDATE quant_rebalance_log SET actions=%s WHERE id=%s",
                        (json.dumps(actions), row_id))
        conn.commit()

    return {"status": "ok", "basket": basket_name, "rebalance_date": rebalance_date,
            "bought": bought, "already_held_skipped": already_skipped,
            "no_price_skipped": no_price, "allocation_fix": alloc}


@router.post("/rebalance/skip")
def qb_rebalance_skip(basket_name: str, rebalance_date: str,
                       x_admin_token: Optional[str] = Header(None)):
    """cc#1704 P5: the Dismiss path — marks a gated rebalance's candidates skipped without buying
    anything. Same GATE and admin-token convention as confirm above."""
    _check_admin(x_admin_token)
    with _conn() as conn:
        with conn.cursor() as cur:
            actions, row_id = _gated_row(cur, basket_name, rebalance_date)
            if row_id is None:
                raise HTTPException(404, f"no rebalance row for {basket_name} on {rebalance_date}")
            if actions.get("entry_status") != "awaiting_founder":
                raise HTTPException(409, f"entry_status is {actions.get('entry_status')!r} — "
                                          "already confirmed/skipped, or this row never gated")
            actions["entry_status"] = "skipped_by_founder"
            actions["skipped_at"] = datetime.now(qb_rebalance.IST).isoformat()
            cur.execute("UPDATE quant_rebalance_log SET actions=%s WHERE id=%s",
                        (json.dumps(actions), row_id))
        conn.commit()
    return {"status": "ok", "basket": basket_name, "rebalance_date": rebalance_date,
            "entry_status": "skipped_by_founder"}


@router.get("/nav")
def qb_nav_series(basket_name: str = "large_cap", window: str = "MAX"):
    """cc#839: stored NAV vs benchmark for the basket card's performance chart.
    window = 1M | 3M | 6M | MAX; both legs re-based to 100 at the start of the window."""
    import qb_nav
    with _conn() as conn:
        return qb_nav.get_series(conn, basket_name, window)


@router.post("/nav/rebuild")
def qb_nav_rebuild(basket_name: Optional[str] = None, x_admin_token: Optional[str] = Header(None)):
    """cc#839: (re)build the NAV series from position history + raw_prices closes. Full recompute,
    idempotent — a backdated exit corrects the history it belongs to instead of leaving a wrong tail."""
    _check_admin(x_admin_token)
    import qb_nav
    with _conn() as conn:
        if basket_name:
            return qb_nav.persist_series(conn, basket_name)
        return qb_nav.persist_all(conn)


@router.get("/seed/preview")
def qb_seed_preview():
    """cc#838: dry-run of the initial seed for every never-started active basket. READ-ONLY —
    shows exactly what the seed pass would write, priced off the latest EOD close."""
    import qb_seed
    with _conn() as conn:
        return qb_seed.seed_pending(conn, dry_run=True)


@router.post("/seed/run")
def qb_seed_run(basket_name: Optional[str] = None, x_admin_token: Optional[str] = Header(None)):
    """cc#838: run the initial seed. Idempotent — a basket with ANY position history (open or
    exited) is refused, so this can never double-seed. Omit basket_name to seed all pending."""
    _check_admin(x_admin_token)
    import qb_seed
    with _conn() as conn:
        if basket_name:
            return qb_seed.seed_basket(conn, basket_name)
        return qb_seed.seed_pending(conn)


@router.post("/fix_allocations")
def qb_fix_allocations(basket_name: str = "large_cap", x_admin_token: Optional[str] = Header(None)):
    """Fix allocation column + add NIFTYBEES residual for one basket."""
    _check_admin(x_admin_token)
    with _conn() as conn:
        return qb_rebalance.fix_basket_overdeployment(conn, basket_name=basket_name)


@router.post("/fix_all_allocations")
def qb_fix_all_allocations(x_admin_token: Optional[str] = Header(None)):
    """Fix all 4 baskets — allocation column + NIFTYBEES residual."""
    _check_admin(x_admin_token)
    results = []
    with _conn() as conn:
        for b in BASKETS:
            results.append(qb_rebalance.fix_basket_overdeployment(conn, basket_name=b))
    return {"baskets_fixed": len(results), "results": results}


@router.get("/positions")
def qb_positions(basket_name: str = "large_cap", status: str = "open"):
    return api_query("""
        SELECT symbol, entry_price, entry_date, qty,
               ROUND(qty*entry_price,2) AS cost_basis,
               current_price, current_value,
               ROUND(pnl,2) AS pnl, ROUND(pnl_pct,2) AS pnl_pct,
               stop_loss_price, gvm_at_entry AS gvm,
               g_at_entry AS g, v_at_entry AS v, m_at_entry AS m,
               status, exit_price, exit_date, notes, updated_at
        FROM quant_paper_positions
        WHERE basket_name=%s AND status=%s
        ORDER BY pnl_pct DESC NULLS LAST
    """, (basket_name, status))


@router.get("/ledger")
def qb_ledger(basket_name: str = "large_cap"):
    """cc#1298: the full per-symbol Buy/Sell ledger for a basket — BOTH open and every exited-
    status row (status='open' OR status LIKE 'exited%', not just 'exited_stop', matching the
    pattern /summary already uses for future exit-reason variants), newest entry first.
    Read-only, no new engine logic, no schema change — brings together data that already exists
    across quant_paper_positions and quant_rebalance_log.
    quant_paper_positions.notes is the inception-seed note (unchanged since entry), NOT a human
    exit reason — the real one lives in quant_rebalance_log.actions.positions[].exit_reason,
    logged on the rebalance_date that matches the exit. Resolved here per exited row so the page
    renders it directly rather than guessing at a column that does not carry it."""
    rows = api_query("""
        SELECT symbol, entry_price, entry_date, qty, status, exit_price, exit_date, notes, pnl, pnl_pct
        FROM quant_paper_positions
        WHERE basket_name=%s AND (status='open' OR status LIKE 'exited%%')
        ORDER BY entry_date DESC
    """, (basket_name,))
    if isinstance(rows, dict) and rows.get("error"):
        return rows
    exit_dates = sorted({r["exit_date"] for r in rows if r.get("exit_date")})
    reason_map = {}
    if exit_dates:
        log_rows = api_query(
            "SELECT rebalance_date, actions FROM quant_rebalance_log "
            "WHERE basket_name=%s AND rebalance_date = ANY(%s)",
            (basket_name, exit_dates))
        if isinstance(log_rows, list):
            for lr in log_rows:
                actions = lr.get("actions") or {}
                for p in (actions.get("positions") or []):
                    if p.get("exit_reason"):
                        reason_map[(str(lr["rebalance_date"]), p.get("symbol"))] = p["exit_reason"]
    for r in rows:
        # NOT falling back to r["notes"] here — verified live that notes on every row in this
        # basket is the inception-seed line ("cc#838 inception seed | ..."), unchanged since
        # entry, not an exit reason. Rendering that as "why it exited" would be worse than an
        # honest blank. reason_map (quant_rebalance_log) is the only real source.
        r["exit_reason"] = reason_map.get((str(r["exit_date"]), r["symbol"])) if r.get("exit_date") else None
        r.pop("notes", None)
    return rows


@router.get("/summary")
def qb_summary(basket_name: str = "large_cap"):
    open_pos   = api_query(
        "SELECT COUNT(*) AS cnt, ROUND(SUM(current_value),2) AS mkt_value, ROUND(SUM(pnl),2) AS unreal_pnl "
        "FROM quant_paper_positions WHERE basket_name=%s AND status='open'",
        (basket_name,), single=True)
    closed_pos = api_query(
        "SELECT COUNT(*) AS cnt, ROUND(SUM(pnl),2) AS real_pnl "
        "FROM quant_paper_positions WHERE basket_name=%s AND status LIKE 'exited%%'",
        (basket_name,), single=True)
    return {
        "basket":           basket_name,
        "open_positions":   open_pos.get("cnt", 0),
        "market_value":     open_pos.get("mkt_value", 0),
        "unrealised_pnl":   open_pos.get("unreal_pnl", 0),
        "closed_positions": closed_pos.get("cnt", 0),
        "realised_pnl":     closed_pos.get("real_pnl", 0),
        "total_pnl":        round((open_pos.get("unreal_pnl") or 0) + (closed_pos.get("real_pnl") or 0), 2),
    }


@router.get("/rebalance_log")
def qb_rebalance_log(basket_name: str = "large_cap", limit: int = 30):
    return api_query(
        "SELECT rebalance_date, stocks_in, stocks_out, stocks_held, total_portfolio_value, actions, computed_at "
        "FROM quant_rebalance_log WHERE basket_name=%s ORDER BY computed_at DESC LIMIT %s",
        (basket_name, limit))


_BEES = ("NIFTYBEES", "LIQUIDBEES")


# ── cc#1709: Rebalance History as BLOCKS + HSL History ─────────────────────────────────────────
# Founder 05-Sep (two screenshots): "Hide Buy Sell Ledger, replace by HSL history, and rebalance
# tab display information month (date) wise as per image 1 format, each rebalance is a block."
# Read-only over quant_rebalance_log + quant_paper_positions. There is NO separate eod_stop_check
# table: the nightly checks are quant_rebalance_log rows with actions.type='eod_stop_check', and
# each carries actions.positions[] with exit_reason / vs_nifty_pct / stock_ret_pct for the names
# that exited that night — that is the join the HSL tab needs (exit_date = rebalance_date).
_HSL_RULES_V2_FROM = "2026-07-19"   # scope 5: exits dated before this carry the V1 tag, after -> V2


def _exit_reason_map(log_rows):
    """(date 'YYYY-MM-DD', symbol) -> the actions.positions[] entry of the nightly row where that
    symbol shows status exited*. One source for exit_reason and the measured vs_nifty / abs value."""
    m = {}
    for r in log_rows:
        d = str(r["rebalance_date"])
        for p in (r.get("actions") or {}).get("positions") or []:
            if str(p.get("status", "")).startswith("exited") and p.get("symbol"):
                m.setdefault((d, p["symbol"]), p)
    return m


def _rule_text(p):
    """Classify one logged exit. kind: 'hard_stop' (HS1/HS2 — an EVENT, HSL History tab),
    'quality' (GVM quality exit / M_RECOVERED profit-take / rank exit — a DECISION, shown in the
    SELL table of its date's block, scope 4 + A3). An exit with no logged reason is treated as a
    hard stop (its status is exited_stop) and says so, rather than inventing a reason."""
    reason = (p or {}).get("exit_reason") or ""
    vs, ab = (p or {}).get("vs_nifty_pct"), (p or {}).get("stock_ret_pct")
    def _f(v, lbl):
        try:
            return "{} {:+.2f}%".format(lbl, float(v))
        except (TypeError, ValueError):
            return None
    if reason.startswith("HARD_STOP_2"):
        return {"rule": "HS2", "text": "HS2 -10% vs Nifty", "measured": _f(vs, "vs Nifty"), "kind": "hard_stop"}
    if reason.startswith("HARD_STOP_1"):
        return {"rule": "HS1", "text": "HS1 -20% abs", "measured": _f(ab, "abs"), "kind": "hard_stop"}
    if reason.startswith("GVM_EXIT"):
        return {"rule": "Quality", "text": reason.replace("GVM_EXIT:", "Quality exit:", 1), "measured": None, "kind": "quality"}
    if reason.startswith("M_RECOVERED"):
        return {"rule": "Profit-take", "text": reason.replace("M_RECOVERED:", "Profit-take:", 1), "measured": None, "kind": "quality"}
    if reason.startswith("RANK") or "rank" in reason.lower() or "re-screen" in reason.lower():
        return {"rule": "Rank", "text": reason, "measured": None, "kind": "quality"}
    if reason:
        return {"rule": reason.split(":")[0][:24], "text": reason, "measured": None, "kind": "quality"}
    return {"rule": "Stop", "text": "stop exit (reason not logged)", "measured": None, "kind": "hard_stop"}


def _first_num(*vals):
    """cc#1715: first value that parses as a number, else None (block footer cash)."""
    for v in vals:
        try:
            if v is not None:
                return round(float(v), 2)
        except (TypeError, ValueError):
            continue
    return None


def _money(q, px):
    try:
        return round(float(q) * float(px), 2)
    except (TypeError, ValueError):
        return None


def _rebalance_blocks(log_rows, positions, held_asof, value_by_date, reasons):
    """scope 2/3/4/6: one block per rebalance date (actions has was_due) plus one per date that
    carried a quality/rank exit. SELL rows = stock positions whose exit_date is that day and whose
    logged reason is NOT a hard stop; BUY rows = stock positions entered that day. Price is the
    position's own entry/exit price, Amount = qty * price — never a close. The NIFTYBEES cash
    residual folds into the footer of the block dated the same day (A2), never its own block.
    state: DONE when the block has sells or buys; AWAITING CONFIRMATION only when the log row says
    entry_status=awaiting_founder AND a candidate list exists; NO ACTION when nothing moved and
    there were no candidates (Alpha 06-Aug) — the standing 'awaiting confirmation' chip is dropped
    there because there was nothing to confirm."""
    stocks = [p for p in positions if p["symbol"] not in _BEES]
    bees = [p for p in positions if p["symbol"] in _BEES]
    row_by_date, due = {}, {}
    for r in log_rows:                      # log_rows are newest-first; keep the first seen per date
        d = str(r["rebalance_date"])
        row_by_date.setdefault(d, r)
        if "was_due" in (r.get("actions") or {}):
            due.setdefault(d, r)
    quality_dates = set()
    for (d, _sym), p in reasons.items():
        if _rule_text(p)["kind"] == "quality":
            quality_dates.add(d)
    blocks = []
    for d in sorted(set(due) | quality_dates, reverse=True):
        r = due.get(d) or row_by_date.get(d)
        a = (r.get("actions") or {}) if r else {}
        sells, buys = [], []
        for p in stocks:
            if str(p.get("exit_date")) == d:
                rt = _rule_text(reasons.get((d, p["symbol"])))
                if rt["kind"] == "hard_stop":
                    continue                # scope 4: hard stops are events -> HSL History tab
                sells.append({"symbol": p["symbol"], "price": p.get("exit_price"), "qty": p.get("qty"),
                              "amount": _money(p.get("qty"), p.get("exit_price")), "action": "Full Sell",
                              "rule": rt["rule"], "reason": rt["text"]})
            if str(p.get("entry_date")) == d:
                buys.append({"symbol": p["symbol"], "price": p.get("entry_price"), "qty": p.get("qty"),
                             "amount": _money(p.get("qty"), p.get("entry_price")), "action": "Buy"})
        # scope 2: exits_hs1/exits_hs2 on the was_due row are the same-night hard stops the
        # rebalance run processed — they are hard stops, so they stay on the HSL tab too.
        cash = None
        for b in bees:
            if str(b.get("entry_date")) == d:
                cash = {"symbol": b["symbol"], "direction": "in", "qty": b.get("qty"), "price": b.get("entry_price"),
                        "amount": _money(b.get("qty"), b.get("entry_price")), "residual": a.get("alloc_residual")}
            elif str(b.get("exit_date")) == d:
                cash = {"symbol": b["symbol"], "direction": "out", "qty": b.get("qty"), "price": b.get("exit_price"),
                        "amount": _money(b.get("qty"), b.get("exit_price")), "residual": None}
        cands = a.get("entry_candidates") or []
        if sells or buys:
            state, chip = "DONE", "Done"
        elif a.get("entry_status") == "awaiting_founder" and cands:
            state, chip = "AWAITING CONFIRMATION", "Awaiting confirmation"
        else:
            state, chip = "NO ACTION", "No action needed"
        dobj = r["rebalance_date"] if r else None
        held = held_asof(dobj) if dobj else []
        bees_held = any(b.get("entry_date") and dobj and b["entry_date"] <= dobj
                        and (b.get("exit_date") is None or b["exit_date"] > dobj) for b in bees)
        blocks.append({
            "date": d, "kind": "rebalance" if d in due else "quality_exit",
            "state": state, "chip": chip, "sells": sells, "buys": buys,
            "footer": {"held_after": len(held), "held_symbols": held, "bees_held": bees_held,
                       "book_value_after": (value_by_date.get(dobj) if dobj else None), "cash_move": cash,
                       # cc#1715: cash left after the rebalance when the row logged it (discretionary
                       # rows write cash_after; the seed row wrote cash; QB rows carry alloc_residual).
                       "cash_after": _first_num(a.get("cash_after"), a.get("cash"), a.get("alloc_residual"))},
            "next_due": a.get("advanced_to"), "n_candidates": len(cands),
        })
    return blocks


def _hsl_rows(positions, reasons):
    """scope 5: every quant_paper_positions row with status='exited_stop' (V2's count check is on
    exactly that set), newest first, joined to its nightly row for the rule + measured value.
    P&L Rs = qty * (exit - entry) from the position's own prices (equals the stored pnl column;
    ADANIPOWER 148 * (218.45 - 235.93) = -2587.04). Version tag V1 before 2026-07-19, else V2."""
    rows = []
    for p in positions:
        if p.get("status") != "exited_stop" or p["symbol"] in _BEES:
            continue
        d = str(p.get("exit_date"))
        rt = _rule_text(reasons.get((d, p["symbol"])))
        ai, ao = _money(p.get("qty"), p.get("entry_price")), _money(p.get("qty"), p.get("exit_price"))
        pnl = round(ao - ai, 2) if (ai is not None and ao is not None) else None
        try:
            pnl_pct = round((float(p["exit_price"]) / float(p["entry_price"]) - 1) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pnl_pct = None
        rows.append({"date": d, "symbol": p["symbol"], "entry_price": p.get("entry_price"),
                     "exit_price": p.get("exit_price"), "qty": p.get("qty"), "pnl": pnl, "pnl_pct": pnl_pct,
                     "rule": rt["rule"], "rule_text": rt["text"], "measured": rt["measured"], "kind": rt["kind"],
                     "version": "V1" if d < _HSL_RULES_V2_FROM else "V2"})
    rows.sort(key=lambda x: (x["date"], x["symbol"]), reverse=True)
    total = round(sum(x["pnl"] for x in rows if x["pnl"] is not None), 2)
    return {"rows": rows, "count": len(rows), "total_pnl": total}


@router.get("/rebalance_history")
def qb_rebalance_history(basket_name: str = "large_cap", limit: int = 300):
    """cc#1703 (session_log 38966, founder 04-Sep screenshot "every row +0/-0"): the raw
    /rebalance_log feed is one row PER NIGHT (an EOD stop-check runs every trading close, whether
    or not anything happened) — that is the wall of zeros. This endpoint classifies each row
    (REBALANCE / STOP EXIT / CASH MOVE / NIGHTLY CHECK), hides nightly-check noise behind a count
    the page can toggle open, and reads real symbols + a stock-only HELD count instead of the raw
    stocks_held column (which counts the NIFTYBEES cash-residual slot as a "stock").

    do_not_touch respected: reads quant_rebalance_log + quant_paper_positions, writes nothing —
    the engine/table this reads is untouched, this is a presentation layer over existing rows.
    Additive: the raw /rebalance_log endpoint above is unchanged (mcp_dispatch.py's MCP tool reads
    its raw shape) — this is a NEW endpoint, not a rewrite of that one."""
    log_rows = api_query(
        "SELECT rebalance_date, stocks_in, stocks_out, stocks_held, total_portfolio_value, "
        "actions, computed_at FROM quant_rebalance_log WHERE basket_name=%s "
        "ORDER BY rebalance_date DESC, computed_at DESC LIMIT %s",
        (basket_name, limit))
    if isinstance(log_rows, dict) and log_rows.get("error"):
        return log_rows
    positions = api_query(
        "SELECT symbol, qty, entry_price, entry_date, exit_date, exit_price, status, notes "
        "FROM quant_paper_positions WHERE basket_name=%s ORDER BY entry_date",
        (basket_name,))
    if isinstance(positions, dict) and positions.get("error"):
        return positions
    reg = api_query(
        "SELECT rebalance_freq, next_rebalance FROM quant_basket_registry WHERE basket_name=%s",
        (basket_name,), single=True) or {}
    freq = reg.get("rebalance_freq") or "scheduled"

    def dfmt(d):
        return d.strftime("%d-%b") if d else "—"

    def dfmt_json(v):
        """actions is JSONB -> a date value inside it comes back as a plain "YYYY-MM-DD..."
        string, never a python date. Parse just the date portion before formatting."""
        if not v:
            return "—"
        try:
            from datetime import datetime as _dt
            return _dt.strptime(str(v)[:10], "%Y-%m-%d").strftime("%d-%b")
        except Exception:
            return str(v)[:10]

    def held_asof(d):
        """Stock-only (bees excluded) open count as of date d, from the position ledger — the
        one honest source, independent of what a given log row's own stocks_held happened to
        count that night."""
        syms = [p["symbol"] for p in positions
                if p["symbol"] not in _BEES and p["entry_date"] and p["entry_date"] <= d
                and (p["exit_date"] is None or p["exit_date"] > d)]
        return syms

    stock_entries_after = lambda d: any(
        p["symbol"] not in _BEES and p["entry_date"] and p["entry_date"] > d for p in positions)

    def entries_on(d):
        """New stock buys dated exactly d — real confirmed entries, not the gated proposal list.
        Today every rebalance is still awaiting confirmation so this is always [], but a future
        confirmed rebalance dated the same day as its was_due row must show up as IN, not blank
        forever."""
        return [p["symbol"] for p in positions
                if p["symbol"] not in _BEES and p["entry_date"] == d]

    value_by_date = {}
    for r in log_rows:
        d = r["rebalance_date"]
        v = r.get("total_portfolio_value")
        if v is not None and (d not in value_by_date or value_by_date[d] is None):
            value_by_date[d] = v

    rows = []
    for r in log_rows:
        d = r["rebalance_date"]
        actions = r.get("actions") or {}
        held_syms = held_asof(d)
        base = {"date": str(d), "held": len(held_syms), "held_symbols": held_syms,
                "value_after": r.get("total_portfolio_value")}
        if "was_due" in actions:
            exits = list(actions.get("exits_hs1") or []) + list(actions.get("exits_hs2") or [])
            entries = list(actions.get("entries") or []) or entries_on(d)
            entry_note = actions.get("entry_note") or ""
            gated = "founder-confirmed" in entry_note.lower() and not entries and not stock_entries_after(d)
            note = "{} due {}; exits {}; next {}".format(
                freq, dfmt(d), (", ".join(exits) if exits else "none"),
                dfmt_json(actions.get("advanced_to")))
            if gated:
                note += "; NEW ENTRIES AWAITING FOUNDER CONFIRMATION"
            base.update(row_type="rebalance", action="Rebalance",
                        **{"in": entries, "out": exits}, note=note, gated=gated)
            rows.append(base)
        else:
            stocks_out = r.get("stocks_out") or 0
            gvm_exits = actions.get("gvm_exits") or []
            exited = [p for p in actions.get("positions") or []
                      if str(p.get("status", "")).startswith("exited")]
            if stocks_out > 0 or gvm_exits or exited:
                out_syms = [p.get("symbol") for p in exited] or list(gvm_exits)
                reasons = {p.get("symbol"): (p.get("exit_reason") or "GVM exit") for p in exited}
                note = "stop hit: " + ", ".join(
                    "{} ({})".format(s, reasons.get(s, "GVM exit")) for s in out_syms)
                base.update(row_type="stop_exit", action="Stop exit",
                            **{"in": [], "out": out_syms}, note=note, gated=False)
            else:
                base.update(row_type="nightly_check", action="Nightly check",
                            **{"in": [], "out": []}, note="no changes", gated=False)
            rows.append(base)

    for p in positions:
        if p["symbol"] not in _BEES:
            continue
        if p.get("entry_date"):
            d = p["entry_date"]
            note = (p.get("notes") or "").strip() or "{} units of {} — cash slot".format(p.get("qty"), p["symbol"])
            rows.append({"date": str(d), "row_type": "cash_move", "action": "Cash move",
                        "in": [p["symbol"]], "out": [], "held": len(held_asof(d)),
                        "held_symbols": held_asof(d), "value_after": value_by_date.get(d),
                        "note": note, "gated": False})
        if p.get("exit_date"):
            d = p["exit_date"]
            rows.append({"date": str(d), "row_type": "cash_move", "action": "Cash move",
                        "in": [], "out": [p["symbol"]], "held": len(held_asof(d)),
                        "held_symbols": held_asof(d), "value_after": value_by_date.get(d),
                        "note": "{} liquidated back to cash".format(p["symbol"]), "gated": False})

    rows.sort(key=lambda r: r["date"], reverse=True)
    nightly = [r for r in rows if r["row_type"] == "nightly_check"]
    visible = [r for r in rows if r["row_type"] != "nightly_check"]
    # cc#1709: same round trip also carries the block view + the HSL History rows. The flat
    # rows/nightly_rows keys stay for any reader still on the cc#1703 shape.
    reasons = _exit_reason_map(log_rows)
    blocks = _rebalance_blocks(log_rows, positions, held_asof, value_by_date, reasons)
    hsl = _hsl_rows(positions, reasons)
    return {"basket": basket_name, "rows": visible, "nightly_rows": nightly,
            "nightly_count": len(nightly), "next_rebalance": reg.get("next_rebalance"),
            "empty": len(rows) == 0,
            "blocks": blocks, "hsl": hsl}


@router.get("/gated_rebalances")
def qb_gated_rebalances():
    """cc#1704 scope 3a: one batch call for the card-face pill — every basket's LATEST real
    rebalance row (actions has was_due) and whether it is still entry_status='awaiting_founder'.
    One query for all baskets (quant_basket.html's init() already batches its other 3 fetches
    the same way) rather than a per-card round trip."""
    rows = api_query("""
        SELECT DISTINCT ON (basket_name) basket_name, rebalance_date,
               actions->>'entry_status' AS entry_status,
               actions->>'was_due' AS was_due,
               actions->'entry_candidates' AS entry_candidates
        FROM quant_rebalance_log WHERE actions ? 'was_due'
        ORDER BY basket_name, rebalance_date DESC, computed_at DESC
    """)
    if isinstance(rows, dict) and rows.get("error"):
        return rows
    out = {}
    for r in rows:
        if r.get("entry_status") != "awaiting_founder":
            continue
        cands = r.get("entry_candidates") or []
        out[r["basket_name"]] = {"due": r.get("was_due") or str(r.get("rebalance_date")),
                                  "n_candidates": len(cands)}
    return out


def _discretionary_baskets(cur):
    """cc#1677: the Quant/Discretionary split for the Model Portfolio pane's Type column, from
    app_config key qb_discretionary_baskets — registry-driven (rule 9 corollary), never a
    hardcoded list here. Accepts a JSON array or comma-separated string, same tolerance
    v8_book_canon.retired_baskets() gives a hand-edited config row. Missing/unparsable = empty
    set (every basket reads Quant) rather than guessing."""
    import json as _json
    cur.execute("SELECT value FROM app_config WHERE key='qb_discretionary_baskets'")
    row = cur.fetchone()
    raw = row[0] if row and row[0] else None
    if not raw:
        return set()
    raw = str(raw).strip()
    try:
        if raw.startswith("["):
            parsed = _json.loads(raw)
            return set(str(x).strip() for x in parsed if str(x).strip()) if isinstance(parsed, list) else set()
        return set(p.strip() for p in raw.replace("\n", ",").split(",") if p.strip())
    except Exception:
        return set()


@router.get("/registry")
def qb_registry(basket_name: Optional[str] = None):
    if basket_name:
        return api_query("SELECT * FROM quant_basket_registry WHERE basket_name=%s", (basket_name,), single=True)
    with _conn() as conn, conn.cursor() as cur:
        discretionary = _discretionary_baskets(cur)
        cur.execute(
            "SELECT basket_name, cap_type, capital, max_stocks, rebalance_freq, weight_band, "
            "next_rebalance, is_active, notes FROM quant_basket_registry ORDER BY basket_name")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # cc#1677: Type is SERVED, not derived client-side — one source for the Model Portfolio
    # pane's Type column and any future reader of this endpoint.
    for r in rows:
        r["type"] = "Discretionary" if r.get("basket_name") in discretionary else "Quant"
    return rows


@router.get("/alpha/propose")
def qb_alpha_propose(as_of: Optional[str] = None):
    """cc#553 (spec id=6086): DRY-RUN Alpha Multicap V2 FINAL rebalance proposal — top-N entries
    (N = quant_basket_config max_stocks, 15 after cc#1710 QB_CAP_AMENDMENT_V1; 0.5*GVM+0.5*M, gates
    GVM>=7.5/V>=7.5/M>7/dGVM_180d>+0.5, Nifty500), cash slots when fewer than N pass,
    plus the monthly max-3 exit (held names ranked outside composite top-25, worst first) and
    gate-passing refills. READ-ONLY — execution stays founder-confirmed. `as_of` defaults to today.
    Reproduces the manual SQL replication exactly (acceptance)."""
    try:
        return qb_alpha_select.propose_rebalance(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/smallcap/propose")
def qb_smallcap_propose(as_of: Optional[str] = None):
    """cc#554 (spec id=6094): DRY-RUN Small Cap V2 ENTRY proposal — qualifiers with mcap rank>250,
    gates GVM>=8/V>=7.5/dGVM_180d>+0.5/segment-avg-GVM>=6.0, mapped to one of the 8 themes; N-based
    equal sizing (5L/N, N<10 -> 5L/10 per name + cash brake). ENTRY-ONLY — current holdings are
    never flagged for exit here (exits stay HS1/HS2/quarterly). READ-ONLY, founder-confirmed to
    execute. `as_of` defaults to today."""
    try:
        return qb_smallcap_select.propose_rebalance(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/largecap/propose")
def qb_largecap_propose(as_of: Optional[str] = None):
    """cc#555 (spec id=6097): DRY-RUN Large Cap V2 proposal — universe mcap rank<=100, score
    0.5*GVM+0.5*M, gates GVM>=7.0 AND dGVM_180d>+0.5 (10-filter gauntlet retired); top-12 equal
    weight 5L/12, <10 -> 5L/10 + cash; monthly max-3 exit outside composite top-20. READ-ONLY,
    founder-confirmed. `as_of` defaults to today."""
    try:
        return qb_composite_select.propose_largecap(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/midcap/propose")
def qb_midcap_propose(as_of: Optional[str] = None):
    """cc#556 (spec id=6098): DRY-RUN Mid Cap V2 proposal — universe mcap rank 101-250, gates
    GVM>=7.5 AND G>=7.0 (V gate dropped, no dGVM), ranked by M SCORE desc; top-20 equal weight
    5L/20, <10 -> 5L/10 + cash. ENTRY-ONLY (exits UNCHANGED, HS2 kept). READ-ONLY, founder-
    confirmed. `as_of` defaults to today."""
    try:
        return qb_composite_select.propose_midcap(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/breakout/propose")
def qb_breakout_propose(as_of: Optional[str] = None):
    """cc#559 (spec id=6103): DRY-RUN 52-Week Breakout proposal — screen GVM>=7.5 AND week_index_52
    >=90 AND month_index>=90 AND mcap>1000Cr AND vol_ratio_21>=1.0 (universe_technicals x gvm_scores,
    vol computed inline from raw_prices); N>10 -> top 10 by 1y return, 5<=N<=10 -> all, N<5 -> cash;
    Rs 50k/slot. READ-ONLY, founder-confirmed. `as_of` defaults to today."""
    try:
        return qb_breakout_select.propose_rebalance(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/contra/propose")
def qb_contra_propose(as_of: Optional[str] = None):
    """cc#560 (spec id=6104): DRY-RUN Contra Value proposal — screen GVM>7 AND G>=7 AND V>=7.5 AND
    M<=6.5 AND sector-avg-GVM>6 AND above-20DMA AND mcap>1000Cr (universe_technicals x gvm_scores,
    20-DMA inline from raw_prices); max 10, top 10 by V desc; Rs 50k/slot. Exits: HS1 -20% + GVM<6.8
    + M>=8 profit-take. READ-ONLY, founder-confirmed. `as_of` defaults to today."""
    try:
        return qb_contra_select.propose_rebalance(as_of=as_of)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
