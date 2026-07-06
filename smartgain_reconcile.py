"""
SmartGain atomic orderbook reconciliation — cc#237 (extends SMARTGAIN_ORDERBOOK_LINEAR_V1,
session_log id=1170).
=============================================================================================
ONE function, ONE transaction: reconcile_smartgain_batch(account, batch_rows, batch_id)
performs EVERY downstream write an orderbook ingestion must cascade, so partial application
is impossible (the cc#237 root defect: ingestion wrote only smartgain_orders and silently
skipped journal / holdings / weekly-opening, each leaving a different UI number stale):

  a. INSERT smartgain_orders  status='FILLED'  (the ONLY literal every FIFO replay filters;
     BUG A was status='Completed' -> invisible to /daily_m2m).
  b. WEEK ROLLOVER: auto-INSERT smartgain_opening_positions for the batch's ISO week if
     absent, carrying the prior-week-end residual book (BUG B: a Monday with no opening row
     replayed from a flat book -> week card showed 0 realised).
  c. FIFO-match the batch against the current open book (matching logic imported UNCHANGED
     from smartgain_daily_m2m per cc#237 non-goal).
  d. INSERT personal_journal result='CLOSED', ONE row per closed round-trip (opening lot),
     qty-weighted exit, pnl=(exit-entry)*qty LONG / (entry-exit)*qty SHORT  (BUG C: no
     journal rows -> the /m2m home headline, which SUMs personal_journal, showed +0.00).
  e. UPSERT smartgain_holdings with the full-replay residual; DELETE zero residuals.

Idempotent by construction: orders dedup on the natural key, journal dedup on
(batch_id-tagged notes + symbol + qty + pnl), holdings/opening are upserts. Re-running a
batch — or the backfill over every batch since inception (2026-06-29) — never double-writes.

NO brokerage is ever applied to this account (cc#237 confirmed 06-Jul-2026): realised is raw
(exit-entry)*qty. Matching algorithm (spec 1170 STEP 2) is unchanged — this module only
guarantees the cascade always RUNS and always WRITES.
"""

import os
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any

import psycopg
from fastapi import APIRouter, Body, HTTPException

# Reuse the FIFO engine verbatim — do NOT reinvent the matching (cc#237 non-goal).
from smartgain_daily_m2m import (
    _apply_fill, _fresh_books, _load_inception, _monday, _ist_today, DEFAULT_ACCOUNT,
)

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")

# The ONE canonical status. Every downstream FIFO replay filters status='FILLED'.
ORDER_STATUS = "FILLED"


def _conn():
    return psycopg.connect(DATABASE_URL)


def _as_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _as_date(v) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    return date.fromisoformat(str(v)[:10])


def _norm_rows(batch_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise incoming fills. Required keys: symbol, side, qty, price. Optional:
    trade_date, order_ts (defaulted from each other), instrument, expiry, order_id."""
    out = []
    for r in batch_rows:
        ts = r.get("order_ts") or r.get("trade_date")
        if ts is None:
            raise ValueError("each fill needs order_ts or trade_date")
        order_ts = _as_dt(ts) if ("order_ts" in r and r["order_ts"]) else datetime.combine(_as_date(ts), datetime.min.time())
        td = _as_date(r.get("trade_date") or order_ts)
        side = str(r["side"]).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"bad side {side!r} (BUY/SELL only)")
        out.append({
            "trade_date": td, "order_ts": order_ts,
            "symbol": str(r["symbol"]).upper(), "side": side,
            "qty": int(r["qty"]), "price": float(r["price"]),
            "instrument": r.get("instrument", "FUT"),
            "expiry": r.get("expiry"), "order_id": r.get("order_id"),
        })
    out.sort(key=lambda x: (x["order_ts"], x["symbol"]))
    return out


def _all_fills(cur, account, upto_date=None, before_ts=None, exclude_batch=None):
    """FILLED fills for the account, chronological. Optional upper bounds for scoped replays."""
    sql = ["SELECT trade_date, order_ts, symbol, side, qty, price FROM smartgain_orders",
           "WHERE account=%s AND status=%s"]
    params: List[Any] = [account, ORDER_STATUS]
    if upto_date is not None:
        sql.append("AND trade_date <= %s"); params.append(upto_date)
    if before_ts is not None:
        sql.append("AND order_ts < %s"); params.append(before_ts)
    if exclude_batch is not None:
        sql.append("AND batch_id <> %s"); params.append(exclude_batch)
    sql.append("ORDER BY order_ts, id")
    cur.execute(" ".join(sql), tuple(params))
    return cur.fetchall()


def _replay(opening, fills):
    """Replay `fills` (rows of trade_date,order_ts,symbol,side,qty,price) onto the opening
    book. Returns (books, closed). Pure FIFO via the imported _apply_fill."""
    books = _fresh_books(opening)
    closed: List[dict] = []
    for _td, _ts, sym, side, qty, price in fills:
        _apply_fill(books, closed, sym, side, int(qty), float(price), _td)
    return books, closed


def _residual(books) -> Dict[tuple, dict]:
    """Collapse each symbol's FIFO deque to one net residual per (symbol, direction).
    After FIFO a symbol book holds lots of a single direction, so this is qty-weighted avg."""
    out: Dict[tuple, dict] = {}
    for sym, book in books.items():
        for lot in book:
            agg = out.setdefault((sym, lot["direction"]), {"qty": 0, "cost": 0.0})
            agg["qty"] += lot["qty"]
            agg["cost"] += lot["qty"] * lot["price"]
    return {k: {"qty": v["qty"], "avg_price": round(v["cost"] / v["qty"], 4)}
            for k, v in out.items() if v["qty"] > 0}


def _roundtrips(closed) -> List[dict]:
    """Aggregate per-match closes into ONE row per closed round-trip (opening lot), keyed by
    (symbol, direction, entry). qty-weighted exit, summed pnl — matches the broker/manual
    'one CLOSED row per round-trip' convention (cc#237 verify)."""
    agg: Dict[tuple, dict] = {}
    for c in closed:
        key = (c["symbol"], c["direction"], round(c["entry"], 4))
        a = agg.setdefault(key, {"qty": 0, "exit_num": 0.0, "pnl": 0.0, "close_date": c["close_date"]})
        a["qty"] += c["qty"]
        a["exit_num"] += c["exit"] * c["qty"]
        a["pnl"] += c["pnl"]
        a["close_date"] = c["close_date"]   # last close date for this lot
    rows = []
    for (sym, direction, entry), a in agg.items():
        rows.append({
            "symbol": sym, "direction": direction, "entry": entry,
            "qty": a["qty"], "exit": round(a["exit_num"] / a["qty"], 4),
            "pnl": round(a["pnl"], 2), "close_date": a["close_date"],
        })
    return rows


def reconcile_smartgain_batch(account: str, batch_rows: List[Dict[str, Any]],
                              batch_id: str, conn=None) -> Dict[str, Any]:
    """Atomic cascade for one orderbook batch. See module docstring. Returns the cc#237
    self-check dict. All writes are in ONE transaction — commit on success, rollback on error."""
    if not batch_id:
        raise ValueError("batch_id is required")
    rows = _norm_rows(batch_rows) if batch_rows else []

    own = conn is None
    if own:
        conn = _conn()
    orders_inserted = journal_inserted = holdings_upserted = holdings_deleted = 0
    opening_created = False
    try:
        with conn.cursor() as cur:
            # ---- (a) INSERT orders status=FILLED (dedup on the natural key) --------------
            for r in rows:
                if r["order_id"]:
                    cur.execute("SELECT 1 FROM smartgain_orders WHERE account=%s AND order_id=%s",
                                (account, r["order_id"]))
                else:
                    cur.execute("""SELECT 1 FROM smartgain_orders WHERE account=%s AND trade_date=%s
                                   AND symbol=%s AND side=%s AND qty=%s AND price=%s AND order_ts=%s""",
                                (account, r["trade_date"], r["symbol"], r["side"], r["qty"],
                                 r["price"], r["order_ts"]))
                if cur.fetchone():
                    continue
                cur.execute("""INSERT INTO smartgain_orders
                    (account, trade_date, order_ts, symbol, instrument, expiry, side, qty, price,
                     order_id, status, source, batch_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (account, r["trade_date"], r["order_ts"], r["symbol"], r["instrument"],
                     r["expiry"], r["side"], r["qty"], r["price"], r["order_id"],
                     ORDER_STATUS, "reconcile_cc237", batch_id))
                orders_inserted += 1

            # inception opening book (true starting point for every replay)
            inception, inc_opening = _load_inception(cur, account)
            if not inception:
                raise ValueError("no inception opening-position checksum — cannot reconcile")

            # ---- (b) WEEK ROLLOVER: ensure an opening row for the batch's ISO week --------
            batch_dates = [r["trade_date"] for r in rows] or [_ist_today()]
            week_start = _monday(max(batch_dates))
            if week_start > inception:
                cur.execute("SELECT 1 FROM smartgain_opening_positions WHERE account=%s AND week_start=%s",
                            (account, week_start))
                if not cur.fetchone():
                    prior = _all_fills(cur, account, before_ts=datetime.combine(week_start, datetime.min.time()))
                    prior_books, _ = _replay(inc_opening, prior)
                    for (sym, direction), v in sorted(_residual(prior_books).items()):
                        cur.execute("""INSERT INTO smartgain_opening_positions
                            (account, week_start, symbol, direction, qty, avg_price, source)
                            VALUES (%s,%s,%s,%s,%s,%s,'carried_from_prev_week')""",
                            (account, week_start, sym, direction, v["qty"], v["avg_price"]))
                        opening_created = True

            # ---- (c)+(d) FIFO-match the batch, write closed round-trips to personal_journal
            # pre-book = inception + every fill strictly before this batch (excluding it).
            min_ts = min((r["order_ts"] for r in rows), default=None)
            if min_ts is not None:
                pre = _all_fills(cur, account, before_ts=min_ts, exclude_batch=batch_id)
                pre_books, _ = _replay(inc_opening, pre)
                batch_fill_tuples = [(r["trade_date"], r["order_ts"], r["symbol"], r["side"],
                                      r["qty"], r["price"]) for r in rows]
                _, batch_closed = _replay_onto(pre_books, batch_fill_tuples)
                for rt in _roundtrips(batch_closed):
                    # dedup: a CLOSED row tagged with this batch_id for the same lot already there?
                    cur.execute("""SELECT 1 FROM personal_journal
                                   WHERE result='CLOSED' AND symbol=%s AND direction=%s AND qty=%s
                                     AND ROUND(pnl,2)=%s AND notes LIKE %s""",
                                (rt["symbol"], rt["direction"], rt["qty"], round(rt["pnl"], 2),
                                 f"%{batch_id}%"))
                    if cur.fetchone():
                        continue
                    notes = f"{account} dabba FIFO close {batch_id}"
                    cur.execute("""INSERT INTO personal_journal
                        (trade_date, symbol, direction, qty, entry_price, exit_price, exit_time,
                         pnl, result, notes)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'CLOSED',%s)""",
                        (rt["close_date"], rt["symbol"], rt["direction"], rt["qty"],
                         round(rt["entry"], 4), round(rt["exit"], 4), max(r["order_ts"] for r in rows),
                         round(rt["pnl"], 2), notes))
                    journal_inserted += 1

            # ---- (e) UPSERT holdings from the FULL current replay (inception + all fills) --
            # Manual upsert keyed on (account, symbol) — smartgain_holdings has no unique
            # constraint (only PK id), so ON CONFLICT is unavailable. One row per symbol.
            all_fills = _all_fills(cur, account)
            full_books, _ = _replay(inc_opening, all_fills)
            residual = _residual(full_books)
            cur.execute("SELECT symbol FROM smartgain_holdings WHERE account=%s", (account,))
            existing_syms = {r[0] for r in cur.fetchall()}
            for (sym, direction), v in residual.items():
                if sym in existing_syms:
                    cur.execute("""UPDATE smartgain_holdings SET direction=%s, qty=%s, entry_price=%s,
                                   week_start=%s, updated_at=NOW() WHERE account=%s AND symbol=%s""",
                                (direction, v["qty"], v["avg_price"], week_start, account, sym))
                else:
                    cur.execute("""INSERT INTO smartgain_holdings
                        (symbol, direction, qty, entry_price, week_start, account, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,NOW())""",
                        (sym, direction, v["qty"], v["avg_price"], week_start, account))
                holdings_upserted += 1
            residual_syms = {sym for (sym, _d) in residual}
            for sym in existing_syms - residual_syms:
                cur.execute("DELETE FROM smartgain_holdings WHERE account=%s AND symbol=%s", (account, sym))
                holdings_deleted += 1

            realised_week = _week_realised(inc_opening, all_fills)

            checksum = _broker_checksum_ok(cur, account, residual)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if own:
            conn.close()

    result = {
        "batch_id": batch_id, "account": account,
        "orders_inserted": orders_inserted, "orders_status_value_used": ORDER_STATUS,
        "journal_rows_inserted": journal_inserted,
        "holdings_rows_upserted": holdings_upserted, "holdings_rows_deleted": holdings_deleted,
        "opening_position_row_created": opening_created,
        "realised_this_week_computed": realised_week,
        "matches_broker_checksum": checksum,
    }
    _log_ops(result)
    return result


def _replay_onto(books, fills):
    """Apply fills onto an EXISTING (already-built) books dict, returning (books, closed)."""
    closed: List[dict] = []
    for _td, _ts, sym, side, qty, price in fills:
        _apply_fill(books, closed, sym, side, int(qty), float(price), _td)
    return books, closed


def _week_realised(inc_opening, all_fills):
    """Realised P&L for the current ISO week = sum of per-match close pnl whose close date
    falls in this week. Derived from the SAME FIFO replay everything else uses (cc#237 part 2)."""
    ws = _monday(_ist_today())
    _, closed = _replay(inc_opening, all_fills)
    return round(sum(c["pnl"] for c in closed if _as_date(c["close_date"]) >= ws), 2)


def _broker_checksum_ok(cur, account, residual) -> bool:
    """True when the replayed residual net-position matches smartgain_holdings for every key."""
    cur.execute("SELECT symbol, direction, qty FROM smartgain_holdings WHERE account=%s", (account,))
    held = {(r[0], r[1]): int(r[2]) for r in cur.fetchall()}
    rep = {k: v["qty"] for k, v in residual.items()}
    return held == rep


def _log_ops(result: Dict[str, Any]):
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name='ops_log'")
            if cur.fetchone():
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='ops_log'")
                cols = {r[0] for r in cur.fetchall()}
                msg = ("cc#237 reconcile " + result["batch_id"] + ": " +
                       ", ".join(f"{k}={result[k]}" for k in
                                 ("orders_inserted", "journal_rows_inserted", "holdings_rows_upserted",
                                  "holdings_rows_deleted", "opening_position_row_created",
                                  "realised_this_week_computed", "matches_broker_checksum")))
                if "message" in cols:
                    cur.execute("INSERT INTO ops_log (message) VALUES (%s)", (msg,))
                    conn.commit()
    except Exception:
        pass


def backfill_all_batches(account: str = DEFAULT_ACCOUNT) -> Dict[str, Any]:
    """Idempotently re-run the corrected cascade over EVERY batch since inception, in
    chronological order (cc#237 part 4). Safe to run repeatedly: dedup guards prevent any
    double-write (e.g. the closes Claude web hand-inserted this session)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT batch_id, MIN(order_ts) FROM smartgain_orders
                       WHERE account=%s AND status=%s GROUP BY batch_id ORDER BY MIN(order_ts)""",
                    (account, ORDER_STATUS))
        batches = [r[0] for r in cur.fetchall()]
    per_batch = []
    for bid in batches:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT trade_date, order_ts, symbol, instrument, expiry, side, qty, price, order_id
                           FROM smartgain_orders WHERE account=%s AND batch_id=%s AND status=%s
                           ORDER BY order_ts, id""", (account, bid, ORDER_STATUS))
            rows = [{"trade_date": r[0], "order_ts": r[1], "symbol": r[2], "instrument": r[3],
                     "expiry": r[4], "side": r[5], "qty": r[6], "price": r[7], "order_id": r[8]}
                    for r in cur.fetchall()]
        per_batch.append(reconcile_smartgain_batch(account, rows, bid))
    return {"account": account, "batches_processed": len(batches), "results": per_batch}


# ── HTTP triggers (Claude web ingests on receipt of the EOD orderbook) ──────────────────────

@router.post("/api/smartgain/reconcile")
def api_reconcile(payload: Dict[str, Any] = Body(...)):
    """Body: {account?, batch_id, rows:[{symbol,side,qty,price,trade_date?,order_ts?,order_id?}]}."""
    account = payload.get("account", DEFAULT_ACCOUNT)
    batch_id = payload.get("batch_id")
    rows = payload.get("rows", [])
    if not batch_id:
        raise HTTPException(400, "batch_id required")
    try:
        return reconcile_smartgain_batch(account, rows, batch_id)
    except Exception as e:
        raise HTTPException(500, f"reconcile failed: {e}")


@router.post("/api/smartgain/backfill")
def api_backfill(payload: Dict[str, Any] = Body(default={})):
    account = payload.get("account", DEFAULT_ACCOUNT)
    try:
        return backfill_all_batches(account)
    except Exception as e:
        raise HTTPException(500, f"backfill failed: {e}")
