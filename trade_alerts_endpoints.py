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


def check_triggers(conn):
    """cc#1504 — the scheduled price-trigger pass. Called by scheduler.py's
    _bg_trade_alerts_check every 5 minutes during market hours; kept here so ONE file owns
    trade_alerts logic (the v10_st_ema.tick pattern — scheduler stays a dispatcher).

    Prices come from cmp_resolver.resolve_cmp_many — the SAME batch path every list surface
    uses, deliberately: it omits the one-at-a-time Yahoo network tier, which has no place in a
    scheduled sweep. LIVE-ONLY FIRING: an alert fires only on a price marked live (a tick from
    the current session, or a fresh cache entry). A symbol sitting on a stale EOD close is
    SKIPPED and counted, never fired — triggering an intraday price alert off yesterday's close
    would be a false trigger, the badge running ahead of the data.

    Condition semantics (spec): ABOVE fires when live price >= trigger_price; BELOW fires when
    live price <= trigger_price. Independent of direction. This pass ONLY flips
    pending -> triggered (+ triggered_at); approval is a human-clicked action (cc#1505),
    nothing is auto-placed."""
    import cmp_resolver
    with conn.cursor() as cur:
        cur.execute("""SELECT id, symbol, trigger_price, trigger_condition
                       FROM trade_alerts WHERE status = 'pending' ORDER BY id""")
        rows = cur.fetchall()
        if not rows:
            return {"pending": 0, "triggered": [], "skipped_not_live": 0}
        live = cmp_resolver.resolve_cmp_many(cur, sorted({r[1] for r in rows}))
        fired, not_live = [], 0
        for aid, sym, tp, cond in rows:
            lv = live.get(sym) or {}
            cmp_v = lv.get("cmp")
            if cmp_v is None or not lv.get("live"):
                not_live += 1
                continue
            tp_f = float(tp)
            hit = (cmp_v >= tp_f) if cond == "ABOVE" else (cmp_v <= tp_f) if cond == "BELOW" else False
            if not hit:
                continue
            # status guarded in the WHERE — a dismiss racing this sweep wins, the flip loses.
            cur.execute("""UPDATE trade_alerts SET status = 'triggered', triggered_at = NOW()
                           WHERE id = %s AND status = 'pending' RETURNING id""", (aid,))
            if cur.fetchone():
                fired.append({"id": aid, "symbol": sym, "cmp": cmp_v,
                              "trigger_price": tp_f, "condition": cond})
    conn.commit()
    return {"pending": len(rows), "triggered": fired, "skipped_not_live": not_live}


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


@router.post("/api/alerts/approve")
async def approve_alert(req: Request):
    """cc#1505 — the human click. Only a TRIGGERED alert can be approved (a pending one has not
    crossed yet; a dismissed/approved one is a decision already taken). approved_price is the
    CURRENT resolver price at the moment of approval — an honest record of what it actually was
    when the founder clicked, NEVER the trigger_price copied over. If no price path can see the
    symbol right now the approval is refused rather than a number fabricated."""
    body = await req.json()
    try:
        alert_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "id required")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol, status FROM trade_alerts WHERE id = %s", (alert_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, f"alert {alert_id} not found")
        sym, cur_status = r
        if cur_status != "triggered":
            raise HTTPException(409, f"alert {alert_id} is '{cur_status}' — "
                                     "only a triggered alert can be approved")
        import cmp_resolver
        res = cmp_resolver.resolve_cmp(cur, sym)
        price = (res or {}).get("cmp")
        if price is None:
            raise HTTPException(422, f"{sym} has no resolvable price right now — "
                                     "approval refused rather than recording a fabricated approved_price")
        cur.execute(
            f"""UPDATE trade_alerts
                SET status = 'approved', approved_at = NOW(), approved_price = %s
                WHERE id = %s AND status = 'triggered'
                RETURNING {_COLS}""", (price, alert_id))
        row = cur.fetchone()
        if not row:   # racing dismiss won between the SELECT and here — report the truth
            conn.commit()
            raise HTTPException(409, f"alert {alert_id} changed state mid-approval — re-read it")
        out = _row(row)
        conn.commit()
    return {"status": "ok", "alert": out, "price_source": (res or {}).get("source"),
            "price_live": bool((res or {}).get("live"))}


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
