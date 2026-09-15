"""
wot_option_approve.py -- cc#2081: WALL OF TRADES "Approve Option".

Founder ask (14-Sep-2026, verbatim in the cc#2081 spec): "In WOT for future trade can we add one
more button Approve Option, on click subscribe ATM strike on web socket and show option trade in
alerts." Five founder decisions closed 14-Sep (quoted below) settle direction mapping, expiry, lot
sizing and teardown. This file builds scope items 3-4 of that spec (storage + surface) -- the
gate (scope 1 discovery, scope 2 founder decisions) was already closed before this file existed.

FOUNDER ANSWERS, 14-Sep-2026 (quoted, not paraphrased, so a later reader does not have to trust a
summary):
  Q1 expiry: "SAME EXPIRY AS THE FUTURES CONTRACT... Indian single-stock options are MONTHLY only."
  Q2 direction: "LONG futures signal -> BUY ATM CALL. SHORT futures signal -> BUY ATM PUT. Long
    premium only... Do NOT implement option selling/writing on this path."
  Q3 relationship to futures approve: "FULLY INDEPENDENT BUTTONS... A row may end up with futures
    approved only, option approved only, both, or neither... do not model this as one status field."
  Q4 lot sizing: "ALWAYS 1 LOT."
  Q5 subscription lifecycle: "TEAR DOWN WHEN THE OPTION TRADE IS CLOSED... a position still open
    at contract expiry is an edge case this answer does not cover -- flag it."

NOT BUILT HERE, stated plainly (see reports/CC2081_wot_approve_option.md): a live options
WebSocket subscription, and automatic target/SL-hit detection (which needs that subscription to
watch a live price). The card's own discovery (already in its spec, "own_preliminary_findings")
found no options WebSocket exists anywhere in this codebase -- fyers_options_feed.py is REST
polling, index-only, and no other file carries one. Building a live per-stock-option WS from
scratch is new live-trading-path infrastructure; it needs its own unambiguous founder go-ahead
(rule 12) and its own card, not a fold-in here. approved_price / close_price below are a live,
ONE-SHOT Fyers REST quote at the moment of the click -- the exact "resolver price at the moment
of approval" convention every other approve endpoint in this codebase already uses
(trade_alerts_endpoints.py's approve_alert / approve_signal) -- never a subscription, never
fabricated (a quote miss is stored as NULL, not a stale or invented number).

FULLY INDEPENDENT of the existing futures Approve/Dismiss flow (Q3): this table and these
endpoints never read or write trade_alerts. do_not_touch per the card's own spec: the existing
APPROVE / DISMISS buttons and trade_wall_approved.py's flow -- untouched, not imported here except
read-only (_ensure_table mirrors, does not call, trade_wall_approved._ensure).
"""
import os
import time
from datetime import date
from typing import Optional

import psycopg
from fastapi import APIRouter, HTTPException, Request

from trade_alerts_endpoints import DIRECTIONS, _approval_gate  # reused, not re-typed

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


# Own symbol-master cache, same 6h TTL convention as deriv_metrics.py's _SYM_MASTER_CACHE (cc#2031)
# -- a module-private cache is not reached into across files; this file gets its own copy of the
# same cheap pattern rather than coupling to another module's internal state.
_SYM_MASTER_CACHE = {"t": 0.0, "text": None}

_COLS = ["id", "symbol", "direction", "option_type", "strike", "expiry_code", "expiry_date",
         "qty_lots", "approved_price", "approved_at", "target_price", "stop_loss",
         "closed_at", "close_price", "close_reason"]


def _row(r) -> dict:
    d = dict(zip(_COLS, r))
    for k in ("strike", "approved_price", "target_price", "stop_loss", "close_price"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    for k in ("expiry_date", "approved_at", "closed_at"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wot_option_trades (
            id             BIGSERIAL PRIMARY KEY,
            source_engine  TEXT NOT NULL,
            source_ref     TEXT NOT NULL,
            symbol         TEXT NOT NULL,
            direction      TEXT NOT NULL,     -- BUY/SELL, the FUTURES signal's own direction
            option_type    TEXT NOT NULL,     -- CE/PE, derived from direction per Q2
            strike         NUMERIC NOT NULL,
            expiry_code    TEXT NOT NULL,
            expiry_date    DATE NOT NULL,
            qty_lots       INTEGER NOT NULL DEFAULT 1,   -- Q4: always 1
            approved_price NUMERIC,           -- live premium at approval; NULL if no quote (never fabricated)
            approved_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            approved_via   TEXT,
            target_price   NUMERIC,
            stop_loss      NUMERIC,
            closed_at      TIMESTAMPTZ,
            close_price    NUMERIC,
            close_reason   TEXT,
            notes          TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # partial unique: idempotent while OPEN, re-approvable after a close -- same style as
    # trade_alerts' own ON CONFLICT (source_engine, source_ref, kind) WHERE source_engine IS NOT NULL
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS wot_option_trades_open_uq
            ON wot_option_trades (source_engine, source_ref) WHERE closed_at IS NULL
    """)


@router.post("/api/tradewall/approve_option")
async def approve_option(req: Request):
    """The button's click target. Body: {source_engine, source_ref, symbol, direction} -- same
    shape as /api/alerts/approve_signal, so the wall's existing twRef()/e.engine identification
    is reused verbatim, not re-derived. Idempotent while an option-trade for this signal is open
    (a second click returns the existing row, already_approved=true, HTTP 200)."""
    body = await req.json()
    source_engine = str(body.get("source_engine") or "").strip()
    source_ref = str(body.get("source_ref") or "").strip()
    sym = str(body.get("symbol") or "").strip().upper()
    direction = str(body.get("direction") or "").strip().upper()
    if not source_engine or not source_ref:
        raise HTTPException(400, "source_engine and source_ref required — the same idempotency key approve_signal uses")
    if not sym:
        raise HTTPException(400, "symbol required")
    if direction not in DIRECTIONS:
        raise HTTPException(400, f"direction must be one of {DIRECTIONS}")
    _approval_gate()  # same 09:15-15:15 IST trading-day window as the futures approve — this
    # action also resolves a live quote at click time, so the same reasoning applies; there is no
    # founder ruling for a separate window, and a separate one would be an unstated invention.

    option_type = "CE" if direction == "BUY" else "PE"   # Q2, exactly as ruled

    with _conn() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(f"""SELECT {",".join(_COLS)} FROM wot_option_trades
                        WHERE source_engine=%s AND source_ref=%s AND closed_at IS NULL""",
                    (source_engine, source_ref))
        existing = cur.fetchone()
        if existing:
            conn.commit()
            return {"status": "ok", "already_approved": True, "option_trade": _row(existing)}

        import cmp_resolver
        res = cmp_resolver.resolve_cmp(cur, sym)
        spot = (res or {}).get("cmp")
        if spot is None:
            raise HTTPException(422, f"{sym} has no resolvable spot price right now — option "
                                     "approval refused rather than resolving a strike off a fabricated spot")

        import stock_options_backfill as sob
        token = sob._load_token(conn)
        now_t = time.time()
        if not _SYM_MASTER_CACHE["text"] or now_t - _SYM_MASTER_CACHE["t"] > 21600:
            _SYM_MASTER_CACHE["text"] = sob._load_symbol_master()
            _SYM_MASTER_CACHE["t"] = now_t
        today = date.today()
        # each_side=0 -> exactly one strike, the nearest to spot = ATM. Q1: this IS "same expiry
        # as the futures contract" -- Indian single-stock F&O has one active monthly contract for
        # both legs, no weeklies, so _resolve_strikes' own "nearest expiry >= today" already
        # resolves the same contract month the futures signal is on. Stated explicitly, not
        # silently assumed: this file does not do a second, separate futures-expiry lookup.
        code, exp, strikes = sob._resolve_strikes(_SYM_MASTER_CACHE["text"], sym, spot, today, each_side=0)
        if not strikes:
            raise HTTPException(422, f"no listed strikes for {sym} in the Fyers symbol master — "
                                     "likely not F&O-optioned, or the master hasn't loaded this name")
        strike = strikes[0]

    # Fyers REST round-trip stays OUTSIDE any held DB connection -- same invariant strike_chain()
    # (deriv_metrics.py, cc#2031) established: a network round-trip must never hold Postgres open.
    ticker = sob.strike_ticker(sym, code, strike, option_type)
    from deriv_metrics import _batch_quotes
    premium = _batch_quotes([ticker], token).get(ticker)   # None if no quote — never fabricated

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"""INSERT INTO wot_option_trades
                (source_engine, source_ref, symbol, direction, option_type, strike, expiry_code,
                 expiry_date, qty_lots, approved_price, approved_via)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s, 1, %s, %s)
            ON CONFLICT (source_engine, source_ref) WHERE closed_at IS NULL DO NOTHING
            RETURNING {",".join(_COLS)}""",
            (source_engine, source_ref, sym, direction, option_type, strike, code, exp,
             premium, body.get("approved_via")))
        row = cur.fetchone()
        if not row:   # raced -- report the truth, same pattern as approve_signal
            cur.execute(f"""SELECT {",".join(_COLS)} FROM wot_option_trades
                            WHERE source_engine=%s AND source_ref=%s AND closed_at IS NULL""",
                        (source_engine, source_ref))
            row = cur.fetchone()
            conn.commit()
            return {"status": "ok", "already_approved": True, "option_trade": _row(row)}
        conn.commit()
    return {"status": "ok", "already_approved": False, "option_trade": _row(row),
            "premium_live": premium is not None}


@router.get("/api/tradewall/approve_option/map")
def approve_option_map(engine: str):
    """Mirror of /api/alerts/approved_map's shape — {source_ref: {...}} for the wall's paint
    call. OPEN option-trades only for this engine."""
    with _conn() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(f"""SELECT source_ref, {",".join(_COLS)} FROM wot_option_trades
                       WHERE source_engine=%s AND closed_at IS NULL""", (engine,))
        out = {}
        for r in cur.fetchall():
            out[r[0]] = _row(r[1:])
    return {"map": out}


@router.get("/api/tradewall/approve_option/list")
def approve_option_list(status: str = "open", limit: int = 200):
    """All option-trades (open or closed), newest first — the read-only listing this task's
    "visible on the Alerts page" scope item reads from (see the report for what that page build
    covers vs. defers)."""
    limit = min(max(int(limit), 1), 500)
    where = "closed_at IS NULL" if status == "open" else "closed_at IS NOT NULL" if status == "closed" else "TRUE"
    with _conn() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(f"""SELECT source_engine, source_ref, {",".join(_COLS)}
                        FROM wot_option_trades WHERE {where}
                        ORDER BY approved_at DESC LIMIT %s""", (limit,))
        cols = ["source_engine", "source_ref"] + _COLS
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            for k in ("strike", "approved_price", "target_price", "stop_loss", "close_price"):
                if r.get(k) is not None:
                    r[k] = float(r[k])
            for k in ("expiry_date", "approved_at", "closed_at"):
                if r.get(k) is not None:
                    r[k] = str(r[k])
    return {"count": len(rows), "trades": rows}


@router.post("/api/tradewall/approve_option/close")
async def approve_option_close(req: Request):
    """Manual close only. Automatic target/SL-hit close is NOT built here — it needs the live
    price subscription this file explicitly does not build (see module docstring); Q5's teardown
    ruling applies once that subscription exists. This endpoint exists so a row is never a
    one-way roach motel in the meantime."""
    body = await req.json()
    try:
        oid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "id required")
    reason = str(body.get("close_reason") or "manual").strip()

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT symbol, option_type, strike, expiry_code FROM wot_option_trades
                       WHERE id=%s AND closed_at IS NULL""", (oid,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "no open option trade with that id")
        sym, otype, strike, code = r
        import stock_options_backfill as sob
        token = sob._load_token(conn)

    ticker = sob.strike_ticker(sym, code, strike, otype)
    from deriv_metrics import _batch_quotes
    close_px = _batch_quotes([ticker], token).get(ticker)

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE wot_option_trades SET closed_at=NOW(), close_price=%s, close_reason=%s
                       WHERE id=%s AND closed_at IS NULL RETURNING id""", (close_px, reason, oid))
        row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(409, "already closed")
    return {"status": "ok", "id": oid, "close_price": close_px}
