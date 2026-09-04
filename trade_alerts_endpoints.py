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
from fastapi.responses import HTMLResponse

log = logging.getLogger("scorr.trade_alerts")
router = APIRouter()


# cc#1506: the Alerts feed screen — mobile/alerts.html via the shared promoted-template reader.
@router.get("/m/alerts", response_class=HTMLResponse)
def m_alerts():
    from mobile_endpoints import _page
    return _page("alerts")


# cc#1536: the WEB renderer — same relationship to /m/alerts that /trades has to /m/trades.
@router.get("/alerts", response_class=HTMLResponse)
def web_alerts():
    """Served with its own reader rather than mobile_endpoints._page(): that helper is rooted at
    the mobile template directory, and reaching a repo-root file through it would mean passing
    "../", which is a path-traversal shape that stays out of route handlers even when the
    argument is a constant (the web_trades() precedent, verbatim reasoning)."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "trade_alerts_web.html"), "r", encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return HTMLResponse("Alerts (web) is not wired yet.", status_code=404,
                            headers={"Cache-Control": "no-store"})

DIRECTIONS = ("BUY", "SELL")
CONDITIONS = ("ABOVE", "BELOW")
STATUSES = ("pending", "triggered", "approved", "dismissed")
KINDS = ("entry", "exit")   # cc#1524 (TRADE_CONTROL_V1, session_log 35003)

_COLS = ("id, symbol, direction, trigger_price, trigger_condition, status, notes, "
         "created_at, triggered_at, approved_at, approved_price, "
         "source_engine, source_ref, kind, approved_via")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


# cc#1524 · additive in-code migration, the codebase's own pattern (main.py, v8_engine.py and a
# dozen other modules already run ADD COLUMN IF NOT EXISTS at their own call sites — the run_sql
# MCP path hard-blocks ALTER TABLE per MAINTENANCE_LOCK_RULE, and session_log 35003 locks this
# schema change as plain additive/inline). Idempotent, lazy-once per process: nullable/defaulted
# ADD COLUMNs and one partial unique index on a table of founder-clicked rows (tiny by nature).
_SCHEMA_READY = False


def _ensure_schema(conn):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE trade_alerts ADD COLUMN IF NOT EXISTS source_engine TEXT")
        cur.execute("ALTER TABLE trade_alerts ADD COLUMN IF NOT EXISTS source_ref TEXT")
        cur.execute("ALTER TABLE trade_alerts ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'entry'")
        cur.execute("ALTER TABLE trade_alerts ADD COLUMN IF NOT EXISTS approved_via TEXT")
        # idempotency key for one-click approve: one approved row per (engine, ref, kind)
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS trade_alerts_source_uniq
                       ON trade_alerts (source_engine, source_ref, kind)
                       WHERE source_engine IS NOT NULL""")
    conn.commit()
    _SCHEMA_READY = True


# Run once at import (main.py imports this at boot) so the columns exist BEFORE the trade wall's
# _EVENTS_SQL — which reads a.source_engine as of cc#1524 — serves its first request. Guarded:
# a DB hiccup at boot must never kill the app; every endpoint and the 5-min trigger sweep call
# _ensure_schema again, so a deferred migration heals on the next touch.
try:
    with _conn() as _mig_conn:
        _ensure_schema(_mig_conn)
except Exception as _mig_e:
    log.warning("cc#1524 trade_alerts schema migration deferred to first use: %s", _mig_e)


def _row(r):
    return {
        "id": r[0], "symbol": r[1], "direction": r[2],
        "trigger_price": float(r[3]) if r[3] is not None else None,
        "trigger_condition": r[4], "status": r[5], "notes": r[6],
        "created_at": str(r[7]) if r[7] else None,
        "triggered_at": str(r[8]) if r[8] else None,
        "approved_at": str(r[9]) if r[9] else None,
        "approved_price": float(r[10]) if r[10] is not None else None,
        # cc#1524: the link back to the originating signal (NULL on manual alerts)
        "source_engine": r[11], "source_ref": r[12], "kind": r[13], "approved_via": r[14],
    }


def insert_qb_rebalance_alert(conn, basket_name, rebalance_date, candidates, portfolio_value=None):
    """cc#1704 (QB Rebalance Gate Surfacing, session_log 38966): one Alerts-book row per gated
    rebalance, so the founder sees "N candidates are waiting" without opening the QB page.

    This is deliberately NOT run through create_alert()'s cmp_resolver gate above — that gate
    exists because a price-cross alert on an unresolvable symbol would sit pending forever and
    read as a working alert, but this isn't a price-cross alert at all: there is no single
    trigger price for "a basket rebalance is due." symbol carries the top candidate (a real,
    resolvable stock, so any generic symbol-aware rendering downstream still works); the honest
    description lives in trigger_condition; kind='rebalance_due' is how a reader tells it apart
    from a real price alert; notes carries the basket_name plainly.

    Idempotent via trade_alerts_source_uniq (source_engine, source_ref, kind WHERE source_engine
    IS NOT NULL) — calling this twice for the same basket+date is a no-op, never a duplicate
    row, so a re-run of the nightly rebalance job can't spam the Alerts book."""
    if not candidates:
        return None
    symbols = [c.get("symbol") for c in candidates if c.get("symbol")]
    if not symbols:
        return None
    shown = ", ".join(symbols[:8]) + (" +{} more".format(len(symbols) - 8) if len(symbols) > 8 else "")
    condition = "QB REBALANCE DUE — {} candidate(s): {}".format(len(symbols), shown)
    source_ref = "{}:{}".format(basket_name, rebalance_date)
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO trade_alerts (symbol, direction, trigger_price, trigger_condition,
                                              notes, source_engine, source_ref, kind)
                   VALUES (%s, 'BUY', %s, %s, %s, 'qb', %s, 'rebalance_due')
                   ON CONFLICT (source_engine, source_ref, kind) WHERE source_engine IS NOT NULL
                   DO NOTHING RETURNING id""",
                (symbols[0], float(portfolio_value or 0), condition, basket_name, source_ref))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    except Exception:
        log.exception("insert_qb_rebalance_alert failed for %s %s", basket_name, rebalance_date)
        return None


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
    try:
        _ensure_schema(conn)   # cc#1524: the scheduled sweep doubles as the migration healer
    except Exception as e:
        log.warning("cc#1524 ensure_schema in sweep failed (sweep continues): %s", e)
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
    # cc#1524: optional source link so a price alert set FROM a signal row files back to it.
    # Existing callers send none of these and behave exactly as before (kind defaults 'entry').
    source_engine = (str(body.get("source_engine")).strip() or None) if body.get("source_engine") else None
    source_ref = (str(body.get("source_ref")).strip() or None) if body.get("source_ref") else None
    approved_via = (str(body.get("approved_via")).strip() or None) if body.get("approved_via") else None
    kind = str(body.get("kind") or "entry").strip().lower()
    if kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")

    with _conn() as conn, conn.cursor() as cur:
        _ensure_schema(conn)
        # The shared resolver is the gate: if no price path can see this symbol, the trigger
        # check (cc#1504) could never fire and the row would be a lie. Reject with the reason.
        import cmp_resolver
        res = cmp_resolver.resolve_cmp(cur, sym)
        if not res or res.get("cmp") is None:
            raise HTTPException(422, f"{sym} does not resolve to any price "
                                     "(unknown or unpriced symbol) — alert not created")
        try:
            cur.execute(
                f"""INSERT INTO trade_alerts (symbol, direction, trigger_price, trigger_condition, notes,
                                              source_engine, source_ref, kind, approved_via)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}""",
                (sym, direction, trigger_price, condition, notes,
                 source_engine, source_ref, kind, approved_via))
        except psycopg.errors.UniqueViolation:
            # cc#1524: the (source_engine, source_ref, kind) key already has a row — the 35003
            # one-per-signal-per-kind rule, stated honestly instead of a raw 500. A different
            # price for the same signal+kind means dismissing the old alert first.
            raise HTTPException(409, f"an alert for {source_engine}/{source_ref} kind={kind} "
                                     "already exists — dismiss it before setting a new one")
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
        _ensure_schema(conn)   # cc#1524: rows now carry the source columns
        if status == "all":
            cur.execute(f"""SELECT {_COLS} FROM trade_alerts
                            ORDER BY created_at DESC, id DESC LIMIT %s""", (limit,))
        else:
            cur.execute(f"""SELECT {_COLS} FROM trade_alerts WHERE status = %s
                            ORDER BY created_at DESC, id DESC LIMIT %s""", (status, limit))
        rows = [_row(r) for r in cur.fetchall()]
        # cc#1586 scope 2: pending / triggered rows carry the SAME resolver price the trigger sweep
        # uses (cmp_resolver.resolve_cmp_many, see check_triggers), so the page can print
        # "waits for cross <= 1,150 (live 1,306)" beside a disabled Approve instead of an empty
        # cell. Read-only, batched, never a fabricated number: a symbol the resolver cannot see
        # gets cmp None and the page says so.
        open_syms = sorted({r["symbol"] for r in rows if r["status"] in ("pending", "triggered")})
        if open_syms:
            try:
                import cmp_resolver
                live = cmp_resolver.resolve_cmp_many(cur, open_syms) or {}
            except Exception as e:                     # a price outage must not blank the list
                live = {}
                log.warning("list_alerts: resolve_cmp_many failed (%s) — rows ship without cmp", e)
            for r in rows:
                if r["status"] in ("pending", "triggered"):
                    hit = live.get(r["symbol"]) or {}
                    r["cmp"] = hit.get("cmp")
                    r["cmp_live"] = bool(hit.get("live")) if hit else None
                    r["cmp_source"] = hit.get("source")
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
    # cc#1586 scope 5 — FOUNDER OVERRIDE: a PENDING alert may be approved before its trigger
    # crosses when the caller says so explicitly (override=true, behind a confirm on the page).
    # The record stays honest: approved_price is still the live price at the click, and the
    # notes gain "override before trigger". Nothing else changes; a plain call keeps the
    # triggered-only rule.
    override = bool(body.get("override"))
    with _conn() as conn, conn.cursor() as cur:
        _ensure_schema(conn)   # cc#1524: RETURNING reads the source columns
        cur.execute("SELECT symbol, status FROM trade_alerts WHERE id = %s", (alert_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, f"alert {alert_id} not found")
        sym, cur_status = r
        allowed = ("triggered", "pending") if override else ("triggered",)
        if cur_status not in allowed:
            raise HTTPException(409, f"alert {alert_id} is '{cur_status}' — "
                                     + ("only a pending or triggered alert can be approved with override"
                                        if override else "only a triggered alert can be approved"))
        import cmp_resolver
        res = cmp_resolver.resolve_cmp(cur, sym)
        price = (res or {}).get("cmp")
        if price is None:
            raise HTTPException(422, f"{sym} has no resolvable price right now — "
                                     "approval refused rather than recording a fabricated approved_price")
        if override and cur_status == "pending":
            cur.execute(
                f"""UPDATE trade_alerts
                    SET status = 'approved', approved_at = NOW(), approved_price = %s,
                        notes = CASE WHEN notes IS NULL OR notes = '' THEN 'override before trigger'
                                     ELSE notes || ' · override before trigger' END
                    WHERE id = %s AND status = 'pending'
                    RETURNING {_COLS}""", (price, alert_id))
        else:
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




@router.post("/api/alerts/approve_signal")
async def approve_signal(req: Request):
    """cc#1524 (TRADE_CONTROL_V1, session_log 35003 lock 2) — ONE-CLICK APPROVE of an engine
    signal. Creates a trade_alerts row straight into status=approved: approved_price is the LIVE
    resolver price at the moment of the click (never the signal's entry price). trigger_price and
    trigger_condition are NOT NULL columns on this table, so nothing to watch is recorded as an
    honest sentinel rather than NULL — see the INSERT below (cc#1531 fix: the original NULL,NULL
    insert violated the NOT NULL constraint and 500'd every call; never shipped-verified against
    the live schema). Idempotent on (source_engine, source_ref, kind): a second click returns the
    EXISTING row with already_approved=true and HTTP 200 — a double-tap is not an error. This
    endpoint sits BESIDE cc#1505's /api/alerts/approve (triggered -> approved), which is
    untouched."""
    body = await req.json()
    source_engine = str(body.get("source_engine") or "").strip()
    source_ref = str(body.get("source_ref") or "").strip()
    sym = str(body.get("symbol") or "").strip().upper()
    direction = str(body.get("direction") or "").strip().upper()
    kind = str(body.get("kind") or "entry").strip().lower()
    approved_via = (str(body.get("approved_via")).strip() or None) if body.get("approved_via") else None
    if not source_engine or not source_ref:
        raise HTTPException(400, "source_engine and source_ref required — they are the "
                                 "idempotency key that stops a double-tap duplicating the approval")
    if not sym:
        raise HTTPException(400, "symbol required")
    if direction not in DIRECTIONS:
        raise HTTPException(400, f"direction must be one of {DIRECTIONS}")
    if kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    with _conn() as conn, conn.cursor() as cur:
        _ensure_schema(conn)
        import cmp_resolver
        res = cmp_resolver.resolve_cmp(cur, sym)
        price = (res or {}).get("cmp")
        if price is None:
            raise HTTPException(422, f"{sym} has no resolvable price right now — approval "
                                     "refused rather than recording a fabricated approved_price")
        live = bool((res or {}).get("live"))
        # the partial unique index arbitrates the race: first INSERT wins, the second returns
        # nothing here and falls through to the existing-row read.
        # cc#1531: trigger_price/trigger_condition are NOT NULL on trade_alerts — NULL,NULL 500'd
        # every call. Sentinel here, not fabricated: trigger_price is the SAME live price already
        # resolved for approved_price (not a made-up watch level); trigger_condition is 'N/A'
        # (not one of CONDITIONS) so it can never be misread as a real ABOVE/BELOW threshold.
        # check_triggers() only ever reads status='pending' rows, so this row is never watched —
        # the UI (mobile/alerts.html card()) suppresses the Trigger line for these rows instead.
        cur.execute(
            f"""INSERT INTO trade_alerts
                    (symbol, direction, trigger_price, trigger_condition, status,
                     approved_at, approved_price, source_engine, source_ref, kind, approved_via)
                VALUES (%s, %s, %s, 'N/A', 'approved', NOW(), %s, %s, %s, %s, %s)
                ON CONFLICT (source_engine, source_ref, kind) WHERE source_engine IS NOT NULL
                DO NOTHING
                RETURNING {_COLS}""",
            (sym, direction, price, price, source_engine, source_ref, kind, approved_via))
        r = cur.fetchone()
        if r:
            out, already = _row(r), False
        else:
            cur.execute(f"""SELECT {_COLS} FROM trade_alerts
                            WHERE source_engine = %s AND source_ref = %s AND kind = %s""",
                        (source_engine, source_ref, kind))
            r2 = cur.fetchone()
            if not r2:   # conflict row vanished mid-flight — report the truth, no retry loop
                conn.commit()
                raise HTTPException(409, "approval raced a concurrent change — re-read the row")
            out, already = _row(r2), True
        conn.commit()
    return {"status": "ok", "alert": out, "already_approved": already,
            "live": live, "price_source": (res or {}).get("source")}


@router.post("/api/alerts/dismiss_signal")
async def dismiss_signal(req: Request):
    """cc#1609 (WOT_APPROVAL_SURFACE_V1, session_log 36394): DISMISS an engine signal from the wall.
    Mirror of approve_signal on the same idempotency key (source_engine, source_ref, kind): writes
    a trade_alerts row straight into status=dismissed so the wall shows the decision and the row
    can never be approved by a later tap without a deliberate change of the record. No price is
    recorded — a dismissal is not a trade. A second tap returns the existing row (already=true).
    If the key already carries an APPROVED row the call refuses (409): a taken position is not
    dismissed from a wall button. /api/alerts/dismiss (pending/triggered manual alerts) untouched."""
    body = await req.json()
    source_engine = str(body.get("source_engine") or "").strip()
    source_ref = str(body.get("source_ref") or "").strip()
    sym = str(body.get("symbol") or "").strip().upper()
    direction = str(body.get("direction") or "").strip().upper()
    kind = str(body.get("kind") or "entry").strip().lower()
    via = (str(body.get("dismissed_via")).strip() or None) if body.get("dismissed_via") else None
    if not source_engine or not source_ref:
        raise HTTPException(400, "source_engine and source_ref required")
    if not sym:
        raise HTTPException(400, "symbol required")
    if direction not in DIRECTIONS:
        raise HTTPException(400, f"direction must be one of {DIRECTIONS}")
    if kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    note = "dismissed via " + (via or "wall")
    with _conn() as conn, conn.cursor() as cur:
        _ensure_schema(conn)
        cur.execute(
            f"""INSERT INTO trade_alerts
                    (symbol, direction, trigger_price, trigger_condition, status,
                     source_engine, source_ref, kind, approved_via, notes)
                VALUES (%s, %s, 0, 'N/A', 'dismissed', %s, %s, %s, %s, %s)
                ON CONFLICT (source_engine, source_ref, kind) WHERE source_engine IS NOT NULL
                DO NOTHING
                RETURNING {_COLS}""",
            (sym, direction, source_engine, source_ref, kind, via, note))
        r = cur.fetchone()
        if r:
            out, already = _row(r), False
        else:
            cur.execute(f"""SELECT {_COLS} FROM trade_alerts
                            WHERE source_engine = %s AND source_ref = %s AND kind = %s""",
                        (source_engine, source_ref, kind))
            r2 = cur.fetchone()
            if not r2:
                conn.commit()
                raise HTTPException(409, "dismissal raced a concurrent change — re-read the row")
            out, already = _row(r2), True
            if out.get("status") == "approved":
                conn.commit()
                raise HTTPException(409, "this signal is already APPROVED — a taken position is not dismissed from the wall")
        conn.commit()
    return {"status": "ok", "alert": out, "already": already}


@router.get("/api/alerts/approved_map")
def approved_map(engine: str):
    """cc#1524 (lock 5's cheap paint call) — {source_ref: {id, approved_at, approved_price}}
    for status=approved rows of ONE engine, so a wall page paints APPROVED on load with one
    request instead of one per row."""
    engine = (engine or "").strip()
    if not engine:
        raise HTTPException(400, "engine required")
    with _conn() as conn, conn.cursor() as cur:
        _ensure_schema(conn)
        cur.execute("""SELECT source_ref, id, approved_at, approved_price, kind
                       FROM trade_alerts
                       WHERE status = 'approved' AND source_engine = %s AND source_ref IS NOT NULL""",
                    (engine,))
        out = {}
        for ref, aid, at, px, kind in cur.fetchall():
            out[ref] = {"id": aid, "approved_at": str(at) if at else None,
                        "approved_price": float(px) if px is not None else None, "kind": kind}
    return {"engine": engine, "count": len(out), "map": out}


@router.post("/api/alerts/dismiss")
async def dismiss_alert(req: Request):
    body = await req.json()
    try:
        alert_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "id required")
    with _conn() as conn, conn.cursor() as cur:
        _ensure_schema(conn)   # cc#1524: RETURNING reads the source columns
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


# ══════════════════════════════════════════════════════════════════════════════════════════════
# cc#1620 · APP_ALERTS_IDEAS_V1 (session_log 37072) — the approved book as CURATED IDEA CARDS.
# READ-ONLY. One call, everything the app Ideas screen prints: live price and since-approval P&L,
# the plan (target / stop / to-target / R:R) from the ORIGIN signal the approval points to, a
# plain-words why line, the sparkline since approval, held / expiry, and header stats.
#
# Origin link (P1 discovery): source_ref = symbol@to_char(entry_ts,'YYYY-MM-DD HH24:MI:SS') of
# the wall row (trade_wall_endpoints _EVENTS_SQL, cc#1524) with source_engine = the wall's engine
# label. V8 rows resolve to v8_paper_positions (OPEN) first, then v8_paper_trades; TC Scanner to
# tc_scanner_holds; Index Intel to v10_trades (OPTION legs only, V10_DISPLAY_OPTIONS_ONLY_V1 36703:
# a futures leg is never rendered, so such an approval is left out and counted). Manual rows have
# no origin by design — the trigger IS the story.
#
# DISPLAY RULE (founder 02-Sep 20:50, 37072 display_rule_v2): no engine or basket code reaches the
# card. origin_tag is always "EXPERT HANDPICKED"; `style` is plain words; the engine name stays in
# source_engine / source_ref for the founder-only web tabs, which this payload does not repeat.
# ══════════════════════════════════════════════════════════════════════════════════════════════
from datetime import datetime, timedelta, date as _date
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")
_ORIGIN_TAG = "EXPERT HANDPICKED"
_STYLE_WORDS = {"buy_momentum": "Momentum long", "sell_momentum": "Momentum short",
                "buy_reversal": "Reversal long", "sell_reversal": "Reversal short"}
_CLOSE_WORDS = {"TARGET": "target hit", "SL": "stop hit", "GAP_TARGET_EXIT": "gap through target",
                "GAP_SL_EXIT": "gap through stop", "TIME": "time out", "GATE_EXIT": "setup faded",
                "SUITE_REBUILD": "book rebuilt", "MANUAL": "closed by hand"}
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fnum(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _ist_naive(ts):
    """Any timestamp -> naive IST. A tz-aware value is converted; a naive one is ALREADY IST
    (v8 / tc / intraday tables store naive IST, the wall doctrine)."""
    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is not None:
        return ts.astimezone(_IST).replace(tzinfo=None)
    return ts


def _fmt_ist(dt):
    dt = _ist_naive(dt)
    return None if dt is None else "%d %s %02d:%02d" % (dt.day, _MONTHS[dt.month - 1], dt.hour, dt.minute)


def _held_words(seconds):
    if seconds is None:
        return None
    s = max(0, int(seconds))
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm" % m


def _last_tuesday(year, month):
    """NSE monthly expiry = last Tuesday (tc_v4_endpoints rule, weekday()==1)."""
    nxt = _date(year + 1, 1, 1) if month == 12 else _date(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


def _fut_expiry(today):
    exp = _last_tuesday(today.year, today.month)
    if today > exp:
        ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        exp = _last_tuesday(ny, nm)
    return exp


def _pct_since(price, entry, direction):
    """Direction-aware move from entry to price, in %. SELL: a fall is positive."""
    if price is None or entry in (None, 0):
        return None
    v = (price / entry - 1.0) * 100.0
    return round(v if direction == "BUY" else -v, 2)


def _to_target_pct(price, target, direction):
    if price in (None, 0) or target is None:
        return None
    v = (target - price) / price * 100.0
    return round(v if direction == "BUY" else -v, 2)


def _track(entry, stop, target, price):
    """Positions on the stop -> target axis, 0..100. Works for both sides because the axis is
    signed by (target - stop)."""
    if stop is None or target is None or target == stop:
        return None
    def pos(x):
        if x is None:
            return None
        return round(max(0.0, min(100.0, (x - stop) / (target - stop) * 100.0)), 1)
    return {"entry_pos": pos(entry), "price_pos": pos(price)}


def _n(v, d=2):
    """Number for a sentence: no trailing zeros, Indian grouping is the page's job."""
    if v is None:
        return None
    s = ("%." + str(d) + "f") % float(v)
    return s.rstrip("0").rstrip(".") if "." in s else s


def _origin_v8(cur, sym, ts):
    cur.execute("""SELECT id, basket, entry_price, entry_ts, target, stop_loss, pp
                   FROM v8_paper_positions
                   WHERE symbol = %s AND status = 'OPEN'
                     AND to_char(entry_ts, 'YYYY-MM-DD HH24:MI:SS') = %s
                   ORDER BY id DESC LIMIT 1""", (sym, ts))
    r = cur.fetchone()
    if r:
        return {"table": "v8_paper_positions", "id": r[0], "basket": r[1], "entry_price": _fnum(r[2]),
                "entry_ts": r[3], "target": _fnum(r[4]), "stop": _fnum(r[5]), "pivot": _fnum(r[6]),
                "exit_price": None, "exit_ts": None, "exit_reason": None}
    cur.execute("""SELECT id, basket, entry_price, entry_ts, target, stop_loss, exit_price,
                          COALESCE(exit_ts, closed_at), result
                   FROM v8_paper_trades
                   WHERE symbol = %s AND to_char(entry_ts, 'YYYY-MM-DD HH24:MI:SS') = %s
                   ORDER BY id DESC LIMIT 1""", (sym, ts))
    r = cur.fetchone()
    if not r:
        return None
    return {"table": "v8_paper_trades", "id": r[0], "basket": r[1], "entry_price": _fnum(r[2]),
            "entry_ts": r[3], "target": _fnum(r[4]), "stop": _fnum(r[5]), "pivot": None,
            "exit_price": _fnum(r[6]), "exit_ts": r[7], "exit_reason": r[8]}


def _evidence_v8(cur, sym, basket, entry_ts):
    """The qualified row the entry came from: same symbol + basket, nearest signal_ts inside
    fifteen minutes of the entry. Its metrics feed the why line; nothing is recomputed."""
    if entry_ts is None:
        return {}
    lo, hi = entry_ts - timedelta(minutes=15), entry_ts + timedelta(minutes=15)
    cur.execute("""SELECT metrics, sector_week, sector_month, month_return, week_return, mom_2d,
                          week_index_52, gvm_score
                   FROM v8_qualified
                   WHERE symbol = %s AND basket = %s AND signal_ts BETWEEN %s AND %s
                   ORDER BY abs(extract(epoch from (signal_ts - %s))) LIMIT 1""",
                (sym, basket, lo, hi, entry_ts))
    r = cur.fetchone()
    if not r:
        return {}
    m = dict(r[0] or {}) if isinstance(r[0], dict) else {}
    for k, v in zip(("sector_week", "sector_month", "month_return", "week_return", "mom_2d",
                     "week_index_52", "gvm_score"), r[1:]):
        if m.get(k) is None and v is not None:
            m[k] = _fnum(v)
    return m


def _origin_tc(cur, sym, ts):
    cur.execute("""SELECT id, side, style, entry_price, entry_ts, target, sl, score,
                          exit_price, exit_ts, exit_reason
                   FROM tc_scanner_holds
                   WHERE symbol = %s AND to_char(entry_ts, 'YYYY-MM-DD HH24:MI:SS') = %s
                   ORDER BY id DESC LIMIT 1""", (sym, ts))
    r = cur.fetchone()
    if not r:
        return None
    is_open = (r[10] or "OPEN") == "OPEN"
    style = "Momentum" if str(r[2] or "").upper().startswith("MOM") else "Reversal"
    side = "long" if str(r[1] or "").upper() == "BUY" else "short"
    return {"table": "tc_scanner_holds", "id": r[0], "basket": None, "style_word": style + " " + side,
            "entry_price": _fnum(r[3]), "entry_ts": r[4], "target": _fnum(r[5]), "stop": _fnum(r[6]),
            "pivot": None, "score": r[7],
            "exit_price": None if is_open else _fnum(r[8]), "exit_ts": None if is_open else r[9],
            "exit_reason": None if is_open else r[10]}


def _origin_v10(cur, sym, ts):
    cur.execute("""SELECT id, leg, opt_type, opt_strike, entry_price, exit_price, exit_ts, exit_reason,
                          (entry_ts AT TIME ZONE 'Asia/Kolkata')
                   FROM v10_trades
                   WHERE symbol = %s
                     AND to_char(entry_ts AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS') = %s
                   ORDER BY id DESC LIMIT 1""", (sym, ts))
    r = cur.fetchone()
    if not r:
        return None
    if str(r[1] or "FUT").upper() != "OPT":
        return {"hidden": True, "table": "v10_trades", "id": r[0]}     # 36703: never rendered
    return {"table": "v10_trades", "id": r[0], "basket": None, "style_word": "Option write",
            "opt_type": r[2], "opt_strike": _fnum(r[3]), "entry_price": _fnum(r[4]),
            "entry_ts": r[8], "target": None, "stop": None, "pivot": None,
            "exit_price": _fnum(r[5]), "exit_ts": _ist_naive(r[6]), "exit_reason": r[7]}


def _resolve_origin(cur, row):
    eng = (row.get("source_engine") or "").strip()
    ref = (row.get("source_ref") or "").strip()
    if not eng or "@" not in ref:
        return None
    sym, ts = ref.split("@", 1)
    try:
        if eng == "V8":
            o = _origin_v8(cur, sym, ts)
            if o:
                o["instrument"] = "FUT"
                o["style_word"] = _STYLE_WORDS.get(o["basket"] or "", str(o["basket"] or "").replace("_", " ").capitalize())
                o["evidence"] = _evidence_v8(cur, sym, o["basket"], o["entry_ts"])
            return o
        if eng == "TC Scanner":
            o = _origin_tc(cur, sym, ts)
            if o:
                o["instrument"] = "FUT"
                o["evidence"] = {}
            return o
        if eng == "Index Intel":
            o = _origin_v10(cur, sym, ts)
            if o and not o.get("hidden"):
                o["instrument"] = "OPT"
                o["evidence"] = {}
            return o
    except Exception as e:                       # an origin table hiccup must not blank the book
        log.warning("ideas: origin lookup failed for %s/%s: %s", eng, ref, e)
    return None


def _why_line(kind, direction, o, ev, row, extra):
    """ONE plain-words line: setup + evidence, never the engine (37072; 36283 plain_words_v2).
    Templates filed for Fable OK in cc_task_logs (cc#1620 P1). A missing field drops its
    sentence rather than printing a blank."""
    parts = []
    def add(s):
        if s:
            parts.append(s)
    if kind == "waiting":
        cond = "above" if row.get("trigger_condition") == "ABOVE" else "below"
        add("Becomes an idea only if price crosses %s %s." % (cond, _n(row.get("trigger_price"))))
        dist = extra.get("distance_pct")
        if dist is not None:
            add("%s%% away today, will not fire early." % _n(abs(dist), 1))
        return " ".join(parts)
    if kind == "manual":
        cond = "above" if row.get("trigger_condition") == "ABOVE" else "below"
        add("Price level. Set to %s on a cross %s %s." % ("buy" if direction == "BUY" else "sell", cond, _n(row.get("trigger_price"))))
        if extra.get("triggered_day"):
            add("Triggered %s and approved at %s." % (extra["triggered_day"], _n(row.get("approved_price"))))
        add("No set target, managed by hand.")
        return " ".join(parts)
    basket = (o or {}).get("basket") or ""
    ev = ev or {}
    if basket == "sell_momentum":
        add("Momentum short.")
        if ev.get("mom_2d") is not None:
            add("Down %s%% in two days." % _n(abs(ev["mom_2d"]), 1))
        if ev.get("sector_week") is not None and abs(ev["sector_week"]) >= 0.05:
            add("Sector %s%s%% on the week." % ("+" if ev["sector_week"] > 0 else "", _n(ev["sector_week"], 1)))
        add("Target is the next support.")
    elif basket == "buy_momentum":
        add("Momentum long.")
        if ev.get("month_return") is not None:
            add("Up %s%% on the month." % _n(ev["month_return"], 1))
        if ev.get("week_index_52") is not None:
            add("Sits at %s%% of its 52-week range." % _n(ev["week_index_52"], 0))
        if ev.get("sector_week") is not None and abs(ev["sector_week"]) >= 0.05:
            add("Sector %s%s%% on the week." % ("+" if ev["sector_week"] > 0 else "", _n(ev["sector_week"], 1)))
    elif basket == "buy_reversal":
        add("Reversal long.")
        add("Bounced off support S1 today." if ev.get("s1_touch") else "Bought the dip at support.")
        if ev.get("month_return") is not None and ev.get("gvm_score") is not None:
            add("Down %s%% on the month, quality %s." % (_n(abs(ev["month_return"]), 1), _n(ev["gvm_score"], 1)))
        if (o or {}).get("pivot"):
            add("Needs the pivot %s to run." % _n(o["pivot"], 2))
    elif basket == "sell_reversal":
        add("Reversal short.")
        add("Touched resistance R1 and turned." if ev.get("r1_touch") else "Sold the bounce at resistance.")
        if ev.get("fall_from_r1") is not None and ev.get("room_pct") is not None:
            add("%s%% off R1, %s%% room to target." % (_n(ev["fall_from_r1"], 1), _n(ev["room_pct"], 1)))
    elif (o or {}).get("table") == "tc_scanner_holds":
        add("%s." % o.get("style_word", "Scan idea").capitalize())
        if o.get("score") is not None:
            add("Scored %s of 100 on the scan." % _n(o["score"], 0))
    elif (o or {}).get("table") == "v10_trades":
        add("Option write.")
        if o.get("opt_type") and o.get("opt_strike"):
            add("%s %s, sold at %s." % (o["opt_type"], _n(o["opt_strike"], 0), _n(o.get("entry_price"))))
    else:
        add("Handpicked idea.")
    return " ".join(parts)


def _spark(cur, sym, start_ist, end_ist=None, cap=80):
    """5-minute spot closes since approval (intraday_prices fyers_eq, naive IST), thinned to at
    most `cap` points; daily raw_prices when the 5m window has fewer than two bars."""
    if start_ist is None:
        return None
    pts, tf = [], "5m"
    try:
        if end_ist is None:
            cur.execute("""SELECT ts, close FROM intraday_prices
                           WHERE symbol = %s AND timeframe = '5m' AND source = 'fyers_eq'
                             AND ts >= %s AND close IS NOT NULL ORDER BY ts""", (sym, start_ist))
        else:
            cur.execute("""SELECT ts, close FROM intraday_prices
                           WHERE symbol = %s AND timeframe = '5m' AND source = 'fyers_eq'
                             AND ts >= %s AND ts <= %s AND close IS NOT NULL ORDER BY ts""",
                        (sym, start_ist, end_ist))
        pts = [(r[0], _fnum(r[1])) for r in cur.fetchall()]
        if len(pts) < 2:
            tf = "1d"
            cur.execute("""SELECT price_date, close FROM raw_prices
                           WHERE symbol = %s AND price_date >= %s AND close IS NOT NULL
                           ORDER BY price_date""", (sym, start_ist.date()))
            pts = [(r[0], _fnum(r[1])) for r in cur.fetchall()]
            if end_ist is not None:
                pts = [p for p in pts if p[0] <= end_ist.date()]
    except Exception as e:
        log.warning("ideas: spark failed for %s: %s", sym, e)
        return None
    if not pts:
        return {"tf": tf, "n": 0, "pts": []}
    n = len(pts)
    if n > cap:
        step = -(-n // cap)
        thinned = pts[::step]
        if thinned[-1] is not pts[-1]:
            thinned.append(pts[-1])
        pts = thinned
    return {"tf": tf, "n": n, "pts": [p[1] for p in pts],
            "from": str(pts[0][0]), "to": str(pts[-1][0])}


@router.get("/api/alerts/ideas")
def alerts_ideas(limit: int = 100):
    limit = max(1, min(int(limit), 500))
    now_ist = datetime.now(_IST).replace(tzinfo=None)
    today = now_ist.date()
    with _conn() as conn, conn.cursor() as cur:
        _ensure_schema(conn)
        cur.execute(f"""SELECT {_COLS} FROM trade_alerts
                        WHERE status IN ('approved', 'pending', 'triggered')
                        ORDER BY COALESCE(approved_at, created_at) DESC, id DESC LIMIT %s""", (limit,))
        raws = cur.fetchall()
        rows = [_row(r) for r in raws]
        syms = sorted({r["symbol"] for r in rows})
        live = {}
        if syms:
            try:
                import cmp_resolver
                live = cmp_resolver.resolve_cmp_many(cur, syms) or {}
            except Exception as e:
                log.warning("ideas: resolve_cmp_many failed (%s) — cards ship without cmp", e)
        ideas, hidden = [], 0
        for raw, row in zip(raws, rows):
            sym, direction = row["symbol"], row["direction"]
            created_at, triggered_at, approved_at = _ist_naive(raw[7]), _ist_naive(raw[8]), _ist_naive(raw[9])
            hit = live.get(sym) or {}
            cmp_v = _fnum(hit.get("cmp"))
            card = {
                "id": row["id"], "symbol": sym, "direction": direction,
                "side": "LONG" if direction == "BUY" else "SHORT",
                "origin_tag": _ORIGIN_TAG, "style": "Price level", "instrument": "EQ",
                "cmp": cmp_v, "cmp_live": bool(hit.get("live")) if hit else None,
                "cmp_source": hit.get("source"), "cmp_ts": str(hit.get("ts")) if hit.get("ts") else None,
                "notes": row.get("notes"),
                "via": ("app" if "app" in str(row.get("approved_via") or "") else
                        "web" if "web" in str(row.get("approved_via") or "") else None),
            }
            if row["status"] in ("pending", "triggered"):
                tp = row["trigger_price"]
                dist = (round((tp - cmp_v) / cmp_v * 100.0, 2) if (cmp_v and tp is not None) else None)
                card.update({
                    "status": "waiting", "triggered": row["status"] == "triggered",
                    "trigger_price": tp, "trigger_condition": row["trigger_condition"],
                    "set_at_ist": _fmt_ist(created_at), "triggered_at_ist": _fmt_ist(triggered_at),
                    "distance_pct": dist,
                    "why": _why_line("waiting", direction, None, None, row, {"distance_pct": dist}),
                })
                ideas.append(card)
                continue
            # approved ────────────────────────────────────────────────────────────────────────
            entry = row["approved_price"]
            o = _resolve_origin(cur, row)
            if o and o.get("hidden"):
                hidden += 1                     # a V10 futures leg: stored, never shown (36703)
                continue
            ev = (o or {}).get("evidence") or {}
            closed = bool(o and o.get("exit_ts"))
            final_price = o["exit_price"] if closed else None
            exit_ts = _ist_naive(o["exit_ts"]) if closed else None
            ref_price = final_price if closed else cmp_v
            card.update({
                "status": "closed" if closed else "live",
                "approved_at_ist": _fmt_ist(approved_at), "approved_price": entry,
                "since_pct": _pct_since(ref_price, entry, direction),
                "held": _held_words(((exit_ts or now_ist) - approved_at).total_seconds() if approved_at else None),
                "held_seconds": int(((exit_ts or now_ist) - approved_at).total_seconds()) if approved_at else None,
                "spark": _spark(cur, sym, approved_at, exit_ts),
            })
            if closed:
                card["closed"] = {"at_ist": _fmt_ist(exit_ts), "price": final_price,
                                  "final_pct": card["since_pct"],
                                  "reason": _CLOSE_WORDS.get(str(o.get("exit_reason") or "").upper(),
                                                             str(o.get("exit_reason") or "closed").replace("_", " ").lower())}
            if o:
                target, stop = o.get("target"), o.get("stop")
                rr = (round(abs(target - entry) / abs(entry - stop), 2)
                      if (target is not None and stop is not None and entry not in (None, stop)) else None)
                card.update({
                    "style": o.get("style_word") or "Handpicked idea", "instrument": o.get("instrument", "FUT"),
                    "target": target, "stop": stop, "pivot": o.get("pivot"),
                    "to_target_pct": _to_target_pct(ref_price, target, direction),
                    "rr_at_entry": rr, "track": _track(entry, stop, target, ref_price),
                    "why": _why_line("engine", direction, o, ev, row, {}),
                })
                if card["instrument"] == "FUT":
                    exp = _fut_expiry(exit_ts.date() if closed else today)
                    card["expiry"] = {"date": str(exp), "label": _MONTHS[exp.month - 1] + " expiry",
                                      "days": (exp - today).days if not closed else None}
                if card["instrument"] == "OPT":
                    card["opt"] = {"type": o.get("opt_type"), "strike": o.get("opt_strike")}
                    card["since_pct"] = None       # option price is not what the resolver tracks
                    card["since_note"] = "option leg — P&L tracked on the Index page"
            else:
                # manual: the trigger IS the plan
                tp = row["trigger_price"]
                is_manual = not row.get("source_engine")
                card.update({
                    "style": "Price level" if is_manual else "Handpicked idea",
                    "instrument": "EQ",
                    "trigger_price": tp if is_manual else None,
                    "trigger_condition": row["trigger_condition"] if is_manual else None,
                    "from_trigger_pct": _pct_since(ref_price, tp, direction) if is_manual else None,
                    "why": _why_line("manual" if is_manual else "engine", direction, None, {}, row,
                                     {"triggered_day": (" ".join(_fmt_ist(triggered_at).split(" ")[:2]) if triggered_at else None)}),
                    "origin_unresolved": (not is_manual),
                })
            ideas.append(card)
    live_cards = [c for c in ideas if c["status"] == "live" and c.get("since_pct") is not None]
    best = max(live_cards, key=lambda c: c["since_pct"]) if live_cards else None
    stats = {
        "live": sum(1 for c in ideas if c["status"] == "live"),
        "winning": sum(1 for c in live_cards if c["since_pct"] > 0),
        "avg_since_pct": (round(sum(c["since_pct"] for c in live_cards) / len(live_cards), 2) if live_cards else None),
        "best_pct": best["since_pct"] if best else None,
        "best_symbol": best["symbol"] if best else None,
    }
    counts = {
        "all": len(ideas),
        "live": stats["live"],
        "waiting": sum(1 for c in ideas if c["status"] == "waiting"),
        "closed": sum(1 for c in ideas if c["status"] == "closed"),
        "long": sum(1 for c in ideas if c["direction"] == "BUY"),
        "short": sum(1 for c in ideas if c["direction"] == "SELL"),
    }
    any_live = any(c.get("cmp_live") for c in ideas)
    return {
        "as_of_ist": "%02d:%02d IST" % (now_ist.hour, now_ist.minute),
        "price_basis": "LIVE" if any_live else "CLOSE",
        "counts": counts, "stats": stats,
        "honesty": "Every idea here was handpicked and approved by hand. Prices and P&L are live since approval.",
        "ideas": ideas, "hidden_option_context": hidden,
        "spec_ref": "APP_ALERTS_IDEAS_V1 session_log 37072 · cc#1620",
    }


@router.get("/api/alerts/pending_manual")
def alerts_pending_manual():
    """cc#1634: the bell. MANUAL price alerts still waiting: status pending with NO engine origin
    (source_engine IS NULL; engine rows from the wall carry their engine label and are not the
    founder's own alerts). Read-only. Distance is trigger vs the same CMP resolver the ideas feed
    uses, so the bell and the Alerts page never disagree about how far a trigger is."""
    now = datetime.now(_IST)
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT id, symbol, direction, trigger_condition, trigger_price, created_at, notes, kind
                           FROM trade_alerts
                           WHERE status = 'pending' AND source_engine IS NULL
                           ORDER BY created_at DESC, id DESC""")
            rows = cur.fetchall()
            live = {}
            syms = sorted({r[1] for r in rows if r[1]})
            if syms:
                try:
                    import cmp_resolver
                    live = cmp_resolver.resolve_cmp_many(cur, syms) or {}
                except Exception as e:
                    log.warning("pending_manual: resolve_cmp_many failed (%s) - rows ship without cmp", e)
        out = []
        for aid, sym, direction, cond, tp, created, notes, kind in rows:
            hit = live.get(sym) or {}
            cmp_v, tp_f = _fnum(hit.get("cmp")), _fnum(tp)
            dist = round((tp_f - cmp_v) / cmp_v * 100.0, 2) if (cmp_v and tp_f is not None) else None
            out.append({
                "id": aid, "symbol": sym, "direction": direction, "trigger_condition": cond,
                "trigger_price": tp_f, "kind": kind, "notes": notes,
                "created_at_ist": created.astimezone(_IST).strftime("%d %b %H:%M") if created else None,
                "cmp": cmp_v, "cmp_live": bool(hit.get("live")) if hit else None,
                "distance_pct": dist,
            })
        return {"count": len(out), "alerts": out, "as_of_ist": now.strftime("%H:%M"),
                "basis": "trade_alerts status=pending AND source_engine IS NULL"}
    except Exception as e:
        log.warning("pending_manual failed: %s", e)
        return {"count": 0, "alerts": [], "error": str(e)[:200], "as_of_ist": now.strftime("%H:%M")}
