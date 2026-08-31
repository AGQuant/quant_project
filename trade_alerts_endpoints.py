"""cc#1503 — MANUAL TRADE ALERTS: schema + create/list/dismiss API (session_log 34521).

The founder sets a price trigger on a symbol ("alert me when RELIANCE crosses 2300 from
below"); a scheduled check (cc#1504, separate task) later flips pending -> triggered; an
approve endpoint (cc#1505) and the app's Alerts feed (cc#1506-1508) build on top. THIS file is
only the raw data API — table + create + list + dismiss. No trigger-checking, no approval, no
frontend, per the sprint's task split.

DATA HONESTY RULES BUILT IN:
  * A symbol must RESOLVE through the shared cmp_resolver before an alert row exists — an alert
    on a symbol no price path can see would sit pending forever and read as a working alert.
    Rejected honestly with the reason, never silently inserted.
  * Status transitions are enforced IN THE UPDATE's WHERE clause, not read-then-write: dismiss
    is valid only from pending or triggered. An approved alert is a decision already taken —
    it cannot be re-written into dismissed after the fact.
  * list is newest-first by created_at (the raw API order). The chat-feed's newest-at-bottom
    is a FRONTEND display choice layered on top — the backend does not pre-bake it.

Lifecycle (owned across the sprint): pending -> triggered (cc#1504 price check)
                                     triggered -> approved (cc#1505) | dismissed
                                     pending -> dismissed
"""

import os
import logging

import psycopg
from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger("scorr.trade_alerts")
router = APIRouter()

DIRECTIONS = ("BUY", "SELL")
CONDITIONS = ("ABOVE", "BELOW")
STATUSES = ("pending", "triggered", "approved", "dismissed")

_COLS = ("id, symbol, direction, trigger_price, trigger_condition, status, notes, "
         "created_at, triggered_at, approved_at, approved_price")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _row(r):
    return {
        "id": r[0], "symbol": r[1], "direction": r[2],
        "trigger_price": float(r[3]) if r[3] is not None else None,
        "trigger_condition": r[4], "status": r[5], "notes": r[6],
        "created_at": str(r[7]) if r[7] else None,
        "triggered_at": str(r[8]) if r[8] else None,
        "approved_at": str(r[9]) if r[9] else None,
        "approved_price": float(r[10]) if r[10] is not None else None,
    }


@router.post("/api/alerts/create")
async def create_alert(req: Request):
    body = await req.json()
    sym = str(body.get("symbol") or "").strip().upper()
    direction = str(body.get("direction") or "").strip().upper()
    condition = str(body.get("trigger_condition") or "").strip().upper()
    notes = body.get("notes")
    if not sym:
        raise HTTPException(400, "symbol required")
    if direction not in DIRECTIONS:
        raise HTTPException(400, f"direction must be one of {DIRECTIONS}")
    if condition not in CONDITIONS:
        raise HTTPException(400, f"trigger_condition must be one of {CONDITIONS}")
    try:
        trigger_price = float(body.get("trigger_price"))
    except (TypeError, ValueError):
        raise HTTPException(400, "trigger_price must be a number")
    if not trigger_price > 0:
        raise HTTPException(400, "trigger_price must be positive")

    with _conn() as conn, conn.cursor() as cur:
        # The shared resolver is the gate: if no price path can see this symbol, the trigger
        # check (cc#1504) could never fire and the row would be a lie. Reject with the reason.
        import cmp_resolver
        res = cmp_resolver.resolve_cmp(cur, sym)
        if not res or res.get("cmp") is None:
            raise HTTPException(422, f"{sym} does not resolve to any price "
                                     "(unknown or unpriced symbol) — alert not created")
        cur.execute(
            f"""INSERT INTO trade_alerts (symbol, direction, trigger_price, trigger_condition, notes)
                VALUES (%s, %s, %s, %s, %s) RETURNING {_COLS}""",
            (sym, direction, trigger_price, condition, notes))
        row = _row(cur.fetchone())
        conn.commit()
    # cmp at creation returned for context only — nothing stores it; approved_price is cc#1505's.
    return {"status": "ok", "alert": row, "cmp_at_create": res.get("cmp")}


@router.get("/api/alerts/list")
def list_alerts(status: str = "all", limit: int = 200):
    status = (status or "all").strip().lower()
    if status not in STATUSES + ("all",):
        raise HTTPException(400, f"status must be one of {STATUSES + ('all',)}")
    limit = max(1, min(int(limit), 1000))
    with _conn() as conn, conn.cursor() as cur:
        if status == "all":
            cur.execute(f"""SELECT {_COLS} FROM trade_alerts
                            ORDER BY created_at DESC, id DESC LIMIT %s""", (limit,))
        else:
            cur.execute(f"""SELECT {_COLS} FROM trade_alerts WHERE status = %s
                            ORDER BY created_at DESC, id DESC LIMIT %s""", (status, limit))
        rows = [_row(r) for r in cur.fetchall()]
    return {"status_filter": status, "count": len(rows), "alerts": rows}


@router.post("/api/alerts/dismiss")
async def dismiss_alert(req: Request):
    body = await req.json()
    try:
        alert_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "id required")
    with _conn() as conn, conn.cursor() as cur:
        # Transition enforced in the WHERE — no read-then-write race, no re-dismissing a
        # decision already taken (approved) or repeating one (dismissed).
        cur.execute(
            f"""UPDATE trade_alerts SET status = 'dismissed'
                WHERE id = %s AND status IN ('pending', 'triggered')
                RETURNING {_COLS}""", (alert_id,))
        r = cur.fetchone()
        if not r:
            cur.execute("SELECT status FROM trade_alerts WHERE id = %s", (alert_id,))
            cur_status = cur.fetchone()
            conn.commit()
            if not cur_status:
                raise HTTPException(404, f"alert {alert_id} not found")
            raise HTTPException(409, f"alert {alert_id} is '{cur_status[0]}' — "
                                     "only pending or triggered alerts can be dismissed")
        row = _row(r)
        conn.commit()
    return {"status": "ok", "alert": row}
