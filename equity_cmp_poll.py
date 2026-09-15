"""
equity_cmp_poll.py -- cc#2094: EQUITY LIVE PRICE VIA YAHOO (5-min poll).

Founder-dictated design (voice session, 15-Sep-2026): "Equity trades, equity alerts, and
equity/quant baskets should get their live price from Yahoo instead of building a new Fyers
subscription mechanism -- polled every 5 minutes while the position/alert is open." This
REPLACES the Fyers ad-hoc-subscribe plan in cc#2041/cc#2042 (both now status='superseded') --
no subscribe/unsubscribe state at all. The equity universe is recomputed fresh every cycle from
real open positions/alerts, so there is nothing to leak, reconcile, or tear down on close.

REUSE, not a second implementation:
  - yahoo_live_quote.fetch_live_quotes() (cc#1417) is the actual batched Yahoo v7/finance/quote
    HTTP call -- that module's own docstring scopes ITS CALLERS to index rows + ADR breadth
    (a Fyers-outage fallback); this file is a second, legitimate caller for a different purpose
    (always-Yahoo equity position pricing, never a fallback). The underlying fetch function is
    generic (any symbol list) and this reuse is stated here so a future reader is not confused
    by cc#2094 importing from a module titled "cc#1417 LIVE Yahoo fallback".
  - qb_eod_checker._pct_change() is the exact pnl%% formula already used by the (basket-only,
    unscoped) 15-min qb_intraday_mark job -- reused verbatim so this job's numbers and that job's
    numbers agree by construction, not by coincidence.

Persists with source='yahoo' (this card's own spec), never yahoo_live_quote's own
'yahoo_live_fallback' tag -- two different sourcing semantics (always-Yahoo-for-equity vs
Yahoo-only-when-Fyers-is-down) that must not be conflated in cmp_prices.source.

Investigated, not assumed: the existing _bg_qb_intraday_mark job (scheduler.py, wired every 15 min
market-hours) queries ALL open quant_paper_positions with NO futures_universe exclusion, and never
writes cmp_prices -- it is not what this card needs even where it overlaps, which is why this card
does not touch it and builds a separate path instead. An initial read of scheduler_master suspected
a mid-day stall (last_run_at looked like "10:01" with market open to 15:30); re-checked before
writing that up anywhere permanent and it does NOT hold -- that timestamp is UTC (+00:00), i.e.
15:31:28 IST, the last market-hours slot of the day, not a stall. last_status='ok', last_error is
NULL, last_duration_ms=29045 -- a clean 29-second run touching all 134 rows, matching
quant_paper_positions' own updated_at spread (10:00:59.531-10:01:28.504 UTC) exactly. No open gap
here; retracted rather than carried forward.
"""
import os
from datetime import datetime, timezone, timedelta

import psycopg
from fastapi import APIRouter

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

FLAG_KEY = "equity_cmp_poll_run"
_running = False


def _conn():
    return psycopg.connect(DATABASE_URL)


def equity_universe(cur) -> list:
    """cc#2094 scope item 1 -- the union of three real, currently-open sources, minus anything
    already in futures_universe(is_active=true) (that leg's price source is untouched, per
    do_not_touch). LEFT JOIN on trade_alert_levels (not INNER): an approved alert with no levels
    row yet (target/SL never set) is still open -- NULL.closed_at IS NULL is TRUE, same
    "approved and open" definition trade_wall_endpoints.py's own _SUPPRESSED_WHERE already uses,
    reused here rather than re-derived."""
    cur.execute("""
        WITH fu AS (SELECT symbol FROM futures_universe WHERE is_active = true),
        u AS (
            SELECT DISTINCT a.symbol FROM trade_alerts a
            LEFT JOIN trade_alert_levels l ON l.alert_id = a.id
            WHERE a.status = 'approved' AND l.closed_at IS NULL
            UNION
            SELECT DISTINCT a.symbol FROM trade_alerts a
            WHERE a.kind = 'entry' AND a.status = 'pending' AND a.triggered_at IS NULL
            UNION
            SELECT DISTINCT p.symbol FROM quant_paper_positions p
            JOIN quant_basket_registry r ON r.basket_name = p.basket_name AND r.is_active = true
            WHERE p.status = 'open'
        )
        SELECT u.symbol FROM u WHERE u.symbol NOT IN (SELECT symbol FROM fu) ORDER BY u.symbol
    """)
    return [r[0] for r in cur.fetchall()]


def run_equity_cmp_poll() -> dict:
    """One full cycle: compute the universe, fetch Yahoo quotes once for all of it, UPSERT every
    quote into cmp_prices (source='yahoo'), then refresh quant_paper_positions for the
    equity-only QB subset using the exact same pnl/current_value/pnl_pct formula
    qb_eod_checker.qb_intraday_mark already uses. Never writes a fabricated price: a symbol
    Yahoo did not return this cycle is simply skipped (both the cmp_prices write and the QB
    position update), left for next cycle, never zero-filled or carried forward as fresh."""
    from yahoo_live_quote import fetch_live_quotes
    from qb_eod_checker import _pct_change

    with _conn() as conn, conn.cursor() as cur:
        universe = equity_universe(cur)
    if not universe:
        return {"universe_size": 0, "quoted": 0, "cmp_written": 0, "qb_marked": 0}

    quotes = fetch_live_quotes(universe)   # {symbol: {price, ..., asof, source}} -- absent = no quote this cycle

    cmp_written = qb_marked = 0
    with _conn() as conn, conn.cursor() as cur:
        rows = [(sym, q["price"]) for sym, q in quotes.items() if q.get("price") is not None]
        if rows:
            cur.executemany(
                "INSERT INTO cmp_prices (symbol, cmp, updated_at, source) VALUES (%s,%s,NOW(),'yahoo') "
                "ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, updated_at=EXCLUDED.updated_at, source='yahoo'",
                rows)
            cmp_written = len(rows)

        if quotes:
            cur.execute("""
                SELECT p.id, p.symbol, p.entry_price, p.qty FROM quant_paper_positions p
                JOIN quant_basket_registry r ON r.basket_name = p.basket_name AND r.is_active = true
                WHERE p.status = 'open' AND p.symbol = ANY(%s)
            """, (list(quotes.keys()),))
            for pid, sym, entry_price, qty in cur.fetchall():
                ltp = quotes.get(sym, {}).get("price")
                if ltp is None:
                    continue
                ep = float(entry_price) if entry_price is not None else None
                q = float(qty) if qty is not None else None
                pnl = (ltp - ep) * q if ep and q else None
                curr_val = ltp * q if q else None
                pnl_pct = _pct_change(ltp, ep)
                cur.execute("""
                    UPDATE quant_paper_positions SET
                        current_price = %s, current_value = %s, pnl = %s, pnl_pct = %s, updated_at = NOW()
                    WHERE id = %s
                """, (round(ltp, 2),
                      round(curr_val, 2) if curr_val is not None else None,
                      round(pnl, 2) if pnl is not None else None,
                      round(pnl_pct, 4) if pnl_pct is not None else None,
                      pid))
                qb_marked += 1
        conn.commit()

    return {"universe_size": len(universe), "quoted": len(quotes),
            "cmp_written": cmp_written, "qb_marked": qb_marked,
            "unquoted_symbols": sorted(set(universe) - set(quotes.keys()))}


def _run_bg():
    global _running
    if _running:
        return
    _running = True
    try:
        import json
        res = run_equity_cmp_poll()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_config(key,value,updated_at) VALUES(%s,%s,NOW()) "
                        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                        (FLAG_KEY + "_result", json.dumps(res, default=str)))
            cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s", (FLAG_KEY,))
            conn.commit()
    except Exception as e:
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config(key,value,updated_at) VALUES(%s,%s,NOW()) "
                            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                            (FLAG_KEY + "_result", str(e)[:2000]))
                cur.execute("UPDATE app_config SET value='error', updated_at=NOW() WHERE key=%s", (FLAG_KEY,))
                conn.commit()
        except Exception:
            pass
    finally:
        _running = False


@router.on_event("startup")
def _startup_trigger():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key=%s", (FLAG_KEY,))
            row = cur.fetchone()
        if row and str(row[0]).strip() == "run":
            import threading
            threading.Thread(target=_run_bg, name="equity-cmp-poll-selftest", daemon=True).start()
    except Exception:
        pass


@router.get("/api/admin/equity_cmp_poll/run")
def admin_run():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_config(key,value,updated_at) VALUES(%s,'run',NOW()) "
                    "ON CONFLICT(key) DO UPDATE SET value='run', updated_at=NOW()", (FLAG_KEY,))
        conn.commit()
    import threading
    threading.Thread(target=_run_bg, name="equity-cmp-poll-manual", daemon=True).start()
    return {"status": "started"}


@router.get("/api/admin/equity_cmp_poll/status")
def admin_status():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM app_config WHERE key=%s", (FLAG_KEY,))
        flag = cur.fetchone()
        cur.execute("SELECT value FROM app_config WHERE key=%s", (FLAG_KEY + "_result",))
        result = cur.fetchone()
    return {"flag": flag[0] if flag else None, "result": result[0] if result else None}
