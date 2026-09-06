"""trade_wall_approved.py — cc#1735 · the APPROVED tab on the Wall of Trades (management view).

Founder 06-Sep: "create a tab Approved where approved position display with that time entry
level and timestamp and set target, SL and Close Option." Sits on WOT_APPROVAL_SURFACE_V1
(session_log 36394): the wall is the approval surface, Alerts is the approved book. This file
gives the approved book a working management view ON the wall — read, set levels, close.

STORAGE IS A SIDECAR, NEVER AN ALTER. trade_alerts carries no target, no stop loss, no close
price and no close time, and MAINTENANCE_LOCK_RULE (cc#351) makes ALTER TABLE Railway-console-
only. So the levels live in their own table keyed by alert id (CREATE TABLE IF NOT EXISTS,
which is permitted). A trade_alert_levels row exists only once the founder sets something —
absence means "not set", never zero — and trade_alerts itself is READ here, never written.

DATA HONESTY RULES BUILT IN:
  * Entry level IS approved_price — the resolver price at the moment of approval (cc#1505) —
    never a later price and never the trigger_price. The tab says so in its header line.
  * CMP comes from the ONE canonical price path the rest of the app reads
    (price_resolver.resolve_price -> cmp_resolver). When no path can see a symbol, CMP and P&L
    are blank: a stale price is never carried forward and the entry price is never substituted
    to make P&L read zero.
  * P&L is sign-aware: direction BUY = LONG (cmp - entry), SELL = SHORT (entry - cmp).
  * A close is recorded ONCE. The UPDATE's own WHERE clause (closed_at IS NULL) is the guard,
    so an accidental double click cannot rewrite a recorded exit — it gets a 409 that quotes
    the close already on record. The close price is whatever the founder typed: the page
    defaults it to the current CMP but the number stored is the one entered, because the real
    exit may not be the price on screen.
  * Every write appends a trade_alert_level_log row (action, payload, via, client, at) and a
    server log line — who/when, not just the latest state.
Auth: the same session guard the wall's own reads use (_guard -> 401 JSON), like every other
XHR route behind a rendered page.
"""
import json
import logging
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mobile_endpoints import _conn, _rows, _ist_now, _guard, _json_safe

log = logging.getLogger("scorr.trade_wall_approved")
router = APIRouter()

# CREATE TABLE only. No ALTER is issued against trade_alerts anywhere in this file.
_DDL = """
CREATE TABLE IF NOT EXISTS trade_alert_levels (
    alert_id     BIGINT PRIMARY KEY REFERENCES trade_alerts(id),
    target_price NUMERIC,
    stop_loss    NUMERIC,
    closed_at    TIMESTAMPTZ,
    close_price  NUMERIC,
    close_reason TEXT,
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS trade_alert_level_log (
    id        BIGSERIAL PRIMARY KEY,
    alert_id  BIGINT NOT NULL,
    action    TEXT   NOT NULL,
    payload   JSONB,
    via       TEXT,
    client    TEXT,
    at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
_READY = False


def _ensure(conn):
    global _READY
    if _READY:
        return
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()
    _READY = True


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num_or_none(v, field):
    """Body value -> float or None. '' / None clear the level; anything else must be a positive number."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None, field + " must be a number"
    if f <= 0:
        return None, field + " must be a positive number"
    return f, None


def _side(direction):
    d = (direction or "").upper()
    return "LONG" if d == "BUY" else ("SHORT" if d == "SELL" else d or "?")


def _stamp(ts):
    return ts.strftime("%Y-%m-%d %H:%M") if ts else None


def _audit(cur, alert_id, action, payload, via, client):
    cur.execute("INSERT INTO trade_alert_level_log (alert_id, action, payload, via, client) VALUES (%s, %s, %s::jsonb, %s, %s)",
                (alert_id, action, json.dumps(payload, default=str), via, client))
    log.info("cc#1735 %s alert=%s via=%s client=%s payload=%s", action, alert_id, via, client, json.dumps(payload, default=str))


def _err(status, msg, **extra):
    body = {"error": msg}
    body.update(extra)
    return JSONResponse(body, status_code=status)


@router.get("/api/tradewall/approved")
@_json_safe
def tradewall_approved(request: Request):
    """The Approved tab: every trade_alerts row with status = approved, newest approval first,
    LEFT JOIN the sidecar. Open vs closed is `closed_at IS NULL` on the sidecar, never a state
    change on trade_alerts."""
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        _ensure(conn)
        cur.execute("""SELECT a.id, a.symbol, a.direction, a.source_engine, a.source_ref, a.notes, a.approved_via,
                              a.approved_price,
                              a.approved_at AT TIME ZONE 'Asia/Kolkata' AS approved_ist,
                              l.target_price, l.stop_loss,
                              l.closed_at AT TIME ZONE 'Asia/Kolkata' AS closed_ist,
                              l.close_price, l.close_reason,
                              l.updated_at AT TIME ZONE 'Asia/Kolkata' AS updated_ist
                       FROM trade_alerts a
                       LEFT JOIN trade_alert_levels l ON l.alert_id = a.id
                       WHERE a.status = 'approved'
                       ORDER BY a.approved_at DESC NULLS LAST, a.id DESC""")
        rows = _rows(cur)
        import price_resolver
        out, n_open, n_closed = [], 0, 0
        for r in rows:
            side = _side(r["direction"])
            sign = 1 if side == "LONG" else (-1 if side == "SHORT" else None)
            entry = _f(r["approved_price"])
            closed = r["closed_ist"] is not None
            cmp_px = cmp_label = cmp_date = None
            cmp_live = False
            if not closed:
                try:
                    pr = price_resolver.resolve_price(cur, r["symbol"]) or {}
                    cmp_px = _f(pr.get("price"))
                    cmp_label, cmp_date, cmp_live = pr.get("label"), pr.get("date"), bool(pr.get("is_live"))
                except Exception as e:   # no price path -> blank, never a carried-forward number
                    log.warning("cc#1735 resolver failed for %s: %s", r["symbol"], e)
                    cmp_px = None
            mark = _f(r["close_price"]) if closed else cmp_px
            pnl = pnl_pct = None
            if entry and mark is not None and sign is not None:
                pnl = round((mark - entry) * sign, 2)
                pnl_pct = round((mark - entry) / entry * 100.0 * sign, 2)
            if closed:
                n_closed += 1
            else:
                n_open += 1
            out.append({
                "id": r["id"], "symbol": r["symbol"], "side": side, "direction": r["direction"],
                "engine": r["source_engine"] or "Manual", "source_ref": r["source_ref"],
                "approved_at": _stamp(r["approved_ist"]), "approved_via": r["approved_via"],
                "entry_level": entry,                       # = approved_price, the price AT approval
                "cmp": cmp_px, "cmp_label": cmp_label, "cmp_date": cmp_date, "cmp_live": cmp_live,
                "target_price": _f(r["target_price"]), "stop_loss": _f(r["stop_loss"]),
                "levels_set": (r["target_price"] is not None or r["stop_loss"] is not None),
                "closed": closed, "closed_at": _stamp(r["closed_ist"]),
                "close_price": _f(r["close_price"]), "close_reason": r["close_reason"],
                "pnl": pnl, "pnl_pct": pnl_pct, "pnl_basis": "realised" if closed else ("mark" if pnl is not None else None),
                "levels_updated_at": _stamp(r["updated_ist"]), "notes": r["notes"],
            })
    return {
        "rows": out, "count": len(out), "open_count": n_open, "closed_count": n_closed,
        "as_of": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_level_note": "Entry level = approved_price, the resolver price at the moment of approval — never a later price, never the trigger price.",
        "price_source": "price_resolver.resolve_price (cmp_resolver), per row; cmp_label/cmp_date say what each price is; blank when no path resolves the symbol",
        "pnl_rule": "sign-aware: LONG = mark - entry, SHORT = entry - mark; realised on closed rows uses the recorded close_price",
        "storage": "trade_alert_levels sidecar (CREATE TABLE IF NOT EXISTS); trade_alerts is read only here",
    }


@router.post("/api/tradewall/approved/levels")
async def tradewall_approved_levels(request: Request):
    """Upsert target_price / stop_loss for one approved alert. A key PRESENT in the body is set
    (null or '' clears it); a key ABSENT is left alone. Positive numbers only. A stop loss on the
    wrong side of entry is WARNED, not blocked — the founder may be recording a real hedge."""
    g = _guard(request)
    if g:
        return g
    try:
        body = await request.json()
    except Exception:
        return _err(400, "JSON body required")
    try:
        alert_id = int(body.get("alert_id"))
    except (TypeError, ValueError):
        return _err(400, "alert_id required")
    has_t, has_s = ("target_price" in body), ("stop_loss" in body)
    if not (has_t or has_s):
        return _err(400, "target_price and/or stop_loss required")
    target, e1 = _num_or_none(body.get("target_price"), "target_price") if has_t else (None, None)
    stop, e2 = _num_or_none(body.get("stop_loss"), "stop_loss") if has_s else (None, None)
    if e1 or e2:
        return _err(400, e1 or e2)
    via = str(body.get("via") or "web-wall")[:40]
    client = request.client.host if request.client else None
    with _conn() as conn, conn.cursor() as cur:
        _ensure(conn)
        cur.execute("SELECT symbol, direction, status, approved_price FROM trade_alerts WHERE id = %s", (alert_id,))
        r = cur.fetchone()
        if not r:
            return _err(404, "alert " + str(alert_id) + " not found")
        sym, direction, status, entry = r[0], r[1], r[2], _f(r[3])
        if status != "approved":
            return _err(409, "alert " + str(alert_id) + " is '" + str(status) + "' — levels are set on approved alerts only")
        cur.execute("SELECT closed_at FROM trade_alert_levels WHERE alert_id = %s", (alert_id,))
        lr = cur.fetchone()
        if lr and lr[0] is not None:
            return _err(409, "alert " + str(alert_id) + " is already closed — levels on a closed position are not edited")
        cur.execute("""INSERT INTO trade_alert_levels (alert_id, target_price, stop_loss, updated_at)
                       VALUES (%s, %s, %s, NOW())
                       ON CONFLICT (alert_id) DO UPDATE SET
                           target_price = CASE WHEN %s THEN EXCLUDED.target_price ELSE trade_alert_levels.target_price END,
                           stop_loss    = CASE WHEN %s THEN EXCLUDED.stop_loss    ELSE trade_alert_levels.stop_loss    END,
                           updated_at   = NOW()
                       RETURNING target_price, stop_loss, updated_at AT TIME ZONE 'Asia/Kolkata'""",
                    (alert_id, target, stop, has_t, has_s))
        t_now, s_now, upd = cur.fetchone()
        _audit(cur, alert_id, "levels", {"target_price": target if has_t else "(unchanged)",
                                         "stop_loss": stop if has_s else "(unchanged)", "symbol": sym}, via, client)
        conn.commit()
    warnings = []
    side = _side(direction)
    s_now_f = _f(s_now)
    if s_now_f is not None and entry:
        if side == "LONG" and s_now_f > entry:
            warnings.append("stop loss " + str(s_now_f) + " sits ABOVE entry " + str(entry) + " on a LONG — recorded as given (a hedge?)")
        if side == "SHORT" and s_now_f < entry:
            warnings.append("stop loss " + str(s_now_f) + " sits BELOW entry " + str(entry) + " on a SHORT — recorded as given (a hedge?)")
    t_now_f = _f(t_now)
    if t_now_f is not None and entry:
        if side == "LONG" and t_now_f < entry:
            warnings.append("target " + str(t_now_f) + " sits BELOW entry " + str(entry) + " on a LONG — recorded as given")
        if side == "SHORT" and t_now_f > entry:
            warnings.append("target " + str(t_now_f) + " sits ABOVE entry " + str(entry) + " on a SHORT — recorded as given")
    return {"status": "ok", "alert_id": alert_id, "symbol": sym, "side": side, "entry_level": entry,
            "levels": {"target_price": t_now_f, "stop_loss": s_now_f, "updated_at": _stamp(upd)},
            "warnings": warnings}


@router.post("/api/tradewall/approved/close")
async def tradewall_approved_close(request: Request):
    """Record the exit ONCE: close_price (positive, required — what actually happened), optional
    close_reason. The WHERE closed_at IS NULL inside the upsert is the double-click guard."""
    g = _guard(request)
    if g:
        return g
    try:
        body = await request.json()
    except Exception:
        return _err(400, "JSON body required")
    try:
        alert_id = int(body.get("alert_id"))
    except (TypeError, ValueError):
        return _err(400, "alert_id required")
    price, e = _num_or_none(body.get("close_price"), "close_price")
    if e or price is None:
        return _err(400, e or "close_price required (a positive number — the price actually exited at)")
    reason = (str(body.get("close_reason") or "").strip() or None)
    if reason:
        reason = reason[:200]
    via = str(body.get("via") or "web-wall")[:40]
    client = request.client.host if request.client else None
    with _conn() as conn, conn.cursor() as cur:
        _ensure(conn)
        cur.execute("SELECT symbol, direction, status, approved_price FROM trade_alerts WHERE id = %s", (alert_id,))
        r = cur.fetchone()
        if not r:
            return _err(404, "alert " + str(alert_id) + " not found")
        sym, direction, status, entry = r[0], r[1], r[2], _f(r[3])
        if status != "approved":
            return _err(409, "alert " + str(alert_id) + " is '" + str(status) + "' — only an approved alert can be closed")
        cur.execute("""INSERT INTO trade_alert_levels (alert_id, closed_at, close_price, close_reason, updated_at)
                       VALUES (%s, NOW(), %s, %s, NOW())
                       ON CONFLICT (alert_id) DO UPDATE SET
                           closed_at = NOW(), close_price = EXCLUDED.close_price, close_reason = EXCLUDED.close_reason,
                           updated_at = NOW()
                       WHERE trade_alert_levels.closed_at IS NULL
                       RETURNING closed_at AT TIME ZONE 'Asia/Kolkata', close_price""",
                    (alert_id, price, reason))
        got = cur.fetchone()
        if not got:
            cur.execute("SELECT closed_at AT TIME ZONE 'Asia/Kolkata', close_price, close_reason FROM trade_alert_levels WHERE alert_id = %s", (alert_id,))
            ex = cur.fetchone()
            _audit(cur, alert_id, "close_rejected_already_closed", {"attempted_close_price": price, "attempted_reason": reason, "symbol": sym}, via, client)
            conn.commit()
            return _err(409, "alert " + str(alert_id) + " (" + str(sym) + ") is already closed at " + _stamp(ex[0]) + " IST @ "
                        + str(_f(ex[1])) + (" (" + str(ex[2]) + ")" if ex[2] else "") + " — not overwritten",
                        closed_at=_stamp(ex[0]), close_price=_f(ex[1]), close_reason=ex[2])
        closed_at, cp = got
        _audit(cur, alert_id, "close", {"close_price": price, "close_reason": reason, "symbol": sym}, via, client)
        conn.commit()
    side = _side(direction)
    sign = 1 if side == "LONG" else (-1 if side == "SHORT" else None)
    pnl = round((price - entry) * sign, 2) if (entry and sign is not None) else None
    return {"status": "ok", "alert_id": alert_id, "symbol": sym, "side": side, "entry_level": entry,
            "closed_at": _stamp(closed_at), "close_price": _f(cp), "close_reason": reason, "realised_pnl": pnl}
