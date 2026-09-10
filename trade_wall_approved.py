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
# cc#1781: the ONE close-priority resolver (manual close > discretionary levels > engine default,
# engine exit mirrored once). trade_alerts_endpoints imports nothing from this module at load
# (its call-time import of _audit is the only edge back), so this is not a circular import;
# main.py imports trade_alerts_endpoints (L65) before this module (L135) either way.
from trade_alerts_endpoints import resolve_close_state

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


def approved_book(cur, conn):
    """cc#1957: THE approved-book row builder, importable. This is the body of the Approved tab's
    GET, moved out of the route function byte-for-byte (same SQL, same resolver calls, same P&L
    formula, same payload) so the Home APPROVED TRADES slider (v8_approved_trades.py) can read the
    SAME rows instead of carrying a second computation of entry / cmp / pnl / levels. Nothing in
    here changed; the route below just calls it. Caller owns the connection (psycopg v3, the
    mobile_endpoints._conn kind) and the guard."""
    _ensure(conn)
    if True:   # cc#1957: indentation kept so the block below is a pure move, diffable line-for-line
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
        # cc#1762 item 4 (PARITY, this is the side that CHANGED): a V8-engine alert is linked back to
        # its position through source_ref (= symbol@entry_ts, the key every approve surface writes)
        # so its rupee P&L is (mark - approved_price) x POSITION QTY, sign-aware, through the ONE
        # shared formula (v8_book_canon.unrealised_rupees) against the ONE V8 mark (open_marks) —
        # identical to the Wall of Trades "P&L (approved)" column to the paise. Before this card the
        # tab printed PER-SHARE rupees off the display resolver, which could never equal a
        # position-sized number. A closed V8 row keeps the qty too (realised = close_price basis).
        # Manual / non-V8 alerts have no position size and stay per share, labelled so.
        import v8_book_canon
        v8_link, v8_marks = {}, {}
        v8_refs = sorted({r["source_ref"] for r in rows if (r["source_engine"] or "") == "V8" and r["source_ref"]})
        if v8_refs:
            try:
                cur.execute("""SELECT ref, symbol, qty, book FROM (
                                   SELECT symbol || '@' || to_char(entry_ts, 'YYYY-MM-DD HH24:MI:SS') AS ref,
                                          symbol, qty, 'open' AS book
                                   FROM v8_paper_positions WHERE status = 'OPEN' AND entry_ts IS NOT NULL
                                   UNION ALL
                                   SELECT symbol || '@' || to_char(entry_ts, 'YYYY-MM-DD HH24:MI:SS'),
                                          symbol, qty, 'closed'
                                   FROM v8_paper_trades WHERE entry_ts IS NOT NULL
                               ) x WHERE ref = ANY(%s)""", (v8_refs,))
                for x in _rows(cur):
                    v8_link.setdefault(x["ref"], {"symbol": x["symbol"], "qty": _f(x["qty"]), "book": x["book"]})
                v8_marks = v8_book_canon.open_marks(cur, sorted({x["symbol"] for x in v8_link.values()}))
            except Exception as e:   # link unavailable -> rows fall back to per share, labelled
                log.warning("cc#1762 V8 link failed: %s", e)
                v8_link, v8_marks = {}, {}
        out, n_open, n_closed, mirrored = [], 0, 0, 0
        for r in rows:
            side = _side(r["direction"])
            sign = 1 if side == "LONG" else (-1 if side == "SHORT" else None)
            entry = _f(r["approved_price"])
            # cc#1781: open/closed and the levels on display come from the shared resolver, not the
            # raw LEFT JOIN alone — before this, a V8 position the engine had already exited stayed
            # OPEN on this tab forever with a ticking CMP against a flat position (gap_1).
            st = resolve_close_state(cur, {"id": r["id"], "symbol": r["symbol"],
                                           "source_engine": r["source_engine"], "source_ref": r["source_ref"]})
            if st["mirrored"]:
                mirrored += 1
            closed = st["closed"]
            lk = v8_link.get(r["source_ref"]) if (r["source_engine"] or "") == "V8" else None
            qty = lk["qty"] if lk else None
            cmp_px = cmp_label = cmp_date = None
            cmp_live = False
            if not closed:
                if lk:   # cc#1762: the V8 book mark — the same CMP the wall and the app home use
                    mk = v8_marks.get(r["symbol"]) or {}
                    cmp_px, cmp_label, cmp_date = mk.get("cmp"), "V8 book CMP", mk.get("cmp_ts")
                else:
                    try:
                        pr = price_resolver.resolve_price(cur, r["symbol"]) or {}
                        cmp_px = _f(pr.get("price"))
                        cmp_label, cmp_date, cmp_live = pr.get("label"), pr.get("date"), bool(pr.get("is_live"))
                    except Exception as e:   # no price path -> blank, never a carried-forward number
                        log.warning("cc#1735 resolver failed for %s: %s", r["symbol"], e)
                        cmp_px = None
            mark = st["close_price"] if closed else cmp_px
            pnl = pnl_pct = None
            if entry and mark is not None and sign is not None:
                pnl_pct = round((mark - entry) / entry * 100.0 * sign, 2)
                pnl = (v8_book_canon.unrealised_rupees(entry, mark, side, qty) if (lk and qty is not None)
                       else round((mark - entry) * sign, 2))
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
                # cc#1781: resolved levels — discretionary when set on this tab, else the origin
                # engine's, None once closed (the close record replaces them on display).
                "target_price": (None if closed else st["target"]), "stop_loss": (None if closed else st["stop"]),
                "levels_set": (st["level_source"] == "discretionary"), "level_source": st["level_source"],
                "closed": closed, "closed_at": _stamp(st["closed_at"]),
                "close_price": st["close_price"], "close_reason": st["close_reason"], "close_source": st["close_source"],
                "pnl": pnl, "pnl_pct": pnl_pct, "pnl_basis": "realised" if closed else ("mark" if pnl is not None else None),
                # cc#1762: what the rupee is multiplied by. V8-linked = the position's qty; else per share.
                "qty": qty,
                "qty_basis": ("position qty (v8_paper_positions)" if (lk and lk["book"] == "open")
                              else "position qty (v8_paper_trades)" if lk else "per share"),
                "levels_updated_at": _stamp(r["updated_ist"]), "notes": r["notes"],
            })
        if mirrored:
            conn.commit()                      # cc#1781: engine mirrors written by this read
    return _approved_book_payload(out, n_open, n_closed)


@router.get("/api/tradewall/approved")
@_json_safe
def tradewall_approved(request: Request):
    """The Approved tab: every trade_alerts row with status = approved, newest approval first,
    LEFT JOIN the sidecar. Open vs closed is `closed_at IS NULL` on the sidecar, never a state
    change on trade_alerts. cc#1957: the rows come from approved_book() above — one builder, also
    read by the Home slider."""
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        return approved_book(cur, conn)


def _approved_book_payload(out, n_open, n_closed):
    return {
        "rows": out, "count": len(out), "open_count": n_open, "closed_count": n_closed,
        "as_of": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_level_note": "Entry level = approved_price, the resolver price at the moment of approval — never a later price, never the trigger price.",
        "price_source": "price_resolver.resolve_price (cmp_resolver), per row; cmp_label/cmp_date say what each price is; blank when no path resolves the symbol",
        "pnl_rule": "sign-aware: LONG = mark - entry, SHORT = entry - mark; realised on closed rows uses the recorded close_price",
        "qty_rule": "cc#1762: a V8-engine alert is linked to its position by source_ref (symbol@entry_ts) and its rupee P&L is x position qty through v8_book_canon.unrealised_rupees against the V8 book CMP (v8_book_canon.open_marks) — identical to the Wall of Trades P&L (approved) column; manual / non-V8 alerts stay per share (qty_basis says which)",
        "storage": "trade_alert_levels sidecar (CREATE TABLE IF NOT EXISTS); trade_alerts is read only here",
        "close_rule": "cc#1781 TRADE_ALERT_CLOSE_PRIORITY_V1: manual close (closed_at on the sidecar) > discretionary target/stop on the sidecar > the origin engine's levels; an engine exit is mirrored into the sidecar once (close_reason 'engine: <reason>', logged as auto_close_engine_mirror) and never overwrites a manual close",
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
