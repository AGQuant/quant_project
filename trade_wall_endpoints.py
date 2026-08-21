"""
trade_wall_endpoints.py — cc#991 WALL OF TRADES.

ONE chronological feed of every entry and every exit across every engine that keeps a trade log.
Newest first, keyset-paged, styled like the news feed. Read-only: this file SELECTs and nothing
else. Own file with its own router, per rule 5 — main.py stays wiring.

ROUTE NAMING. The card asked for /api/mobile/tradewall; the amendment then asked for a WEB page
built on the same computation. A mobile-prefixed route serving the desktop dashboard would be a
lie about who owns it, so the canonical route is **/api/tradewall** and /api/mobile/tradewall is
kept as an alias calling the same function. One computation, two renderers, and no existing caller
is broken.

═══ WHAT I VERIFIED IN THE SCHEMA BEFORE WRITING ONE LINE OF SQL ════════════════════════════════
The card warned that column names differ per table and told me to check. They differ more than it
said, and two of its own statements turned out not to match the data:

  1. V10 IS NOT ALL FUTURES. v10_trades carries a `leg` column: 45 rows FUT and 36 rows OPT (the
     option legs are hedges, cc#889's note that "OPT legs are hedges, never the state"). Mapping
     every V10 row to FUTURES would have filed 36 real option trades under the wrong instrument
     and made the Options filter under-report by a factor of four. The card said to follow the
     data where it disagrees, so instrument is read PER ROW from `leg`.

  2. TIMESTAMP TYPES ARE MIXED, and the codebase doctrine differs by type:
       naive IST, read RAW      -> v8_paper_trades, v8_paper_positions, tc_intraday_trades
       timestamptz, CONVERT     -> v10_trades, v14_trades, options_trades, v9_paper_trades
     Every timestamptz column below is converted with AT TIME ZONE 'Asia/Kolkata' IN SQL. That is
     the cc#887 rule: converting in Python is how a tz-aware value reaches code that assumes naive
     and takes a screen down. Getting this wrong would not crash — it would silently shift 168
     V10/V14/options events by 5h30m and scramble the chronology, which is the whole point of the
     page.

  3. DAY-PRECISION EVENTS ARE REAL EVENTS AND ARE NOT FAKED UP TO A TIME.
     quant_paper_positions has entry_date / exit_date (DATE, no time) and options_trades has
     exit_date with exit_time NULL on every row. Casting a DATE to midnight would invent a time
     the database does not hold — an event stamped 00:00 on a market that opens at 09:15 is a
     fabricated number. Excluding them instead would drop 112 of 569 events, a fifth of the wall.
     So they are INCLUDED and carry `prec: 'day'`; the renderer prints the date with no time, and
     the API says which precision each event has. Ordering uses the date at 00:00 because
     something has to sort — that is stated here rather than hidden.

  4. v9_paper_trades (pairs) holds ZERO rows today. It is still in the union, expanded properly
     into its long leg and short leg, so the wall lights up by itself the day the engine writes.
     A hardcoded "skip v9" would have been a rule-9 violation waiting to happen.

  5. quant_paper_positions has NO side column. Those baskets are long-only by construction, so
     side is stated as LONG rather than left blank. Its statuses are 'open' (62) and 'exited_stop'
     (22) — lowercase, unlike v8's uppercase 'OPEN'.

ENGINE / INSTRUMENT MAP — cc#992 (founder 10-Aug), which SUPERSEDES cc#991's:
  v8_paper_trades + v8_paper_positions -> V8 Swing        FUTURES  <- was EQUITY. The V8 universe
                                                                      IS the F&O futures list; the
                                                                      cmp being pinned to spot
                                                                      (cc#367) is a pricing detail,
                                                                      not the instrument traded.
  v10_trades leg='FUT'                 -> V10 Index       FUTURES
  v10_trades leg='OPT'                 -> V10 Index       OPTIONS  (per row, from `leg`)
  v14_trades                           -> V14 Intraday    EQUITY
  tc_intraday_trades                   -> Intraday        EQUITY
  options_trades                       -> Options         OPTIONS
  quant_paper_positions                -> Quant Basket    EQUITY
  v9_paper_trades                      -> V9 Pairs        EQUITY   (empty today)

PER-CLASS EVENT COUNTS under the cc#992 map (10-Aug 15:05 IST — this is a LIVE wall, so the
numbers move as engines trade; they are stated with their timestamp for that reason):
  EQUITY   180  = V14 Intraday 50 + Intraday 24 + Quant Basket 106
  FUTURES  306  = V8 Swing 216 (118 entries + 98 exits) + V10 FUT legs 90
  OPTIONS   84  = V10 OPT legs 72 + Options 12
                 -----
  TOTAL    570
cc#991 documented 569 with V8 under EQUITY. The single-event difference is not a discrepancy in
the union: one more V8 trade closed between the two counts, which is exactly what a live feed
should do. The reclassification moved 216 events from EQUITY to FUTURES and changed no total.
"""

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mobile_endpoints import _conn, _rows, _ist_now, _guard, _json_safe, _page

log = logging.getLogger("scorr.tradewall")
router = APIRouter()

# The instrument classes the filter chips offer. Derived from the union below, not typed twice.
INSTRUMENTS = ("EQUITY", "FUTURES", "OPTIONS")

# cc#1000 (founder 10-Aug): the wall shows TODAY-ONWARDS only. WALL_EPOCH is a DISPLAY filter —
# every event with ts >= this date is shown, everything older is hidden. NOTHING is deleted from any
# source table (all read-only; the full trade history stays intact in the DB). Both the web page and
# the mobile page inherit this through the single union below. Stated in the header as "since 10 Aug".
WALL_EPOCH = "2026-08-10"

# cc#1000: the wall is scoped to EXACTLY FOUR engine groups (founder list):
#   V8 Futures      <- v8_paper_trades + v8_paper_positions   (FUTURES)
#   Index           <- v10_trades, split FUTURES/OPTIONS by leg (cc#992)
#   Quant Basket    <- quant_paper_positions                  (EQUITY)
#   Equity Screener <- tc_intraday_trades + v14_trades        (EQUITY, the two intraday scanners merged)
# DELIBERATELY EXCLUDED, recorded so it is a decision not an accident:
#   options_trades  (the stock-options engine) — not on the founder's four-group list.
#   v9_paper_trades (V9 Pairs) — not on the list either; it would be a fifth group. Empty today; when
#                   the pairs engine goes live it needs a NEW card to re-enter the wall, by design.

# ── PERCENT_SIGNS_IN_SQL (cc#992, my own P0) ─────────────────────────────────────────────────
# NOT ONE percent character may appear anywhere inside _EVENTS_SQL — not in a string literal, and
# NOT IN A COMMENT EITHER.
#
# cc#991 shipped `|| '<pct> net'` to label V14's percentage P&L. psycopg scans the query text for
# placeholders on the CLIENT, before Postgres ever sees it, and read that as a broken parameter:
# "incomplete placeholder". Every parameterised call to this union died at the driver, so
# /m/trades answered 500 and the page rendered no chips at all. The whole feature was dark.
#
# Two things I got wrong, recorded so the next person does not repeat either:
#   * DOUBLING IT UP IS NOT THE RIGHT FIX HERE. Doubling only unescapes when parameters ARE
#     passed. The count queries in tradewall() deliberately call execute() with none, and psycopg
#     skips placeholder parsing on that path — so a doubled sign would survive verbatim into the
#     text and read "1.13<pct><pct> net". chr(37) emits the character from Postgres instead, so
#     it is correct on every path, parameterised or not.
#   * psycopg's SCANNER DOES NOT RESPECT SQL COMMENTS. My first attempt at this very fix put the
#     explanation in a `--` comment next to the line, spelled the sign out, and reproduced the
#     identical error. That is why this note lives in a PYTHON comment outside the string and
#     writes "<pct>" wherever it means the character.
#
# ── HOW TO VERIFY AN EDIT TO _EVENTS_SQL (cc#992, learned twice) ─────────────────────────────
# EXECUTE IT. Extract this string from the file and run it against the database. Nothing else
# catches a SQL mistake:
#   * ast.parse validates PYTHON. A broken query is a perfectly valid Python string.
#   * the psycopg placeholder scan validates PLACEHOLDERS. It does not parse SQL.
#   * a FastAPI TestClient over a stubbed cursor validates the HANDLER. The stub never sends the
#     query anywhere.
# All three passed on a version of this file whose V14 branch read
# "CONCAT_WS(sep, tag, THEN ... END)" — the CASE WHEN had been dropped by a bad edit, and the
# endpoint answered 500 on every request while three green checks said it was fine. The founder
# found it, twice in a row, because the only test that could have found it was the one not run.
#
# ── THE UNION ────────────────────────────────────────────────────────────────────────────────
# Every branch emits the SAME column list so the outer query can sort and page one flat stream.
# `sk` is the tie-break sort key: source + id + event, which is unique across the whole union, so
# the keyset cursor has a total order and can never repeat or skip a row.
_EVENTS_SQL = """
WITH ev AS (
  -- V8 SWING · closed: entry + exit. entry_ts/exit_ts are NAIVE IST -> read raw.
  SELECT 'v8'::text src, id, 'ENTRY'::text event, entry_ts::timestamp ts, 'min'::text prec,
         symbol, UPPER(side) side, 'V8 Futures'::text engine, 'FUTURES'::text instrument,
         qty::numeric qty, entry_price::numeric price, NULL::numeric pnl, NULL::text result,
         basket::text note
  FROM v8_paper_trades WHERE entry_ts IS NOT NULL
  UNION ALL
  SELECT 'v8', id, 'EXIT', COALESCE(exit_ts, closed_at)::timestamp, 'min',
         symbol, UPPER(side), 'V8 Futures', 'FUTURES',
         qty::numeric, exit_price::numeric, pnl::numeric, result, basket
  FROM v8_paper_trades WHERE COALESCE(exit_ts, closed_at) IS NOT NULL

  -- V8 SWING · still open: ONE event, the entry. No exit is invented for a live position.
  UNION ALL
  SELECT 'v8open', id, 'ENTRY', entry_ts::timestamp, 'min',
         symbol, UPPER(side), 'V8 Futures', 'FUTURES',
         qty::numeric, entry_price::numeric, NULL, NULL, basket
  FROM v8_paper_positions WHERE status = 'OPEN' AND entry_ts IS NOT NULL

  -- V10 INDEX · timestamptz -> converted in SQL. Instrument comes from `leg`, per row.
  UNION ALL
  SELECT 'v10', id, 'ENTRY', (entry_ts AT TIME ZONE 'Asia/Kolkata'), 'min',
         symbol, UPPER(side), 'Index',
         CASE WHEN UPPER(COALESCE(leg,'FUT')) = 'OPT' THEN 'OPTIONS' ELSE 'FUTURES' END,
         lot_size::numeric, entry_price::numeric, NULL, NULL,
         NULLIF(CONCAT_WS(' ', leg, opt_type, opt_strike::text), '')
  FROM v10_trades WHERE entry_ts IS NOT NULL
  UNION ALL
  SELECT 'v10', id, 'EXIT', (exit_ts AT TIME ZONE 'Asia/Kolkata'), 'min',
         symbol, UPPER(side), 'Index',
         CASE WHEN UPPER(COALESCE(leg,'FUT')) = 'OPT' THEN 'OPTIONS' ELSE 'FUTURES' END,
         lot_size::numeric, exit_price::numeric, pnl::numeric, exit_reason,
         NULLIF(CONCAT_WS(' ', leg, opt_type, opt_strike::text), '')
  FROM v10_trades WHERE exit_ts IS NOT NULL

  -- V14 INTRADAY · timestamptz -> converted. No qty column; pnl is in PERCENT, not rupees, so it
  -- is NOT shipped in the `pnl` field (that field means rupees everywhere else on this wall).
  -- net_pnl_pct rides in the note instead, labelled, so nothing reads a percent as money.
  UNION ALL
  SELECT 'v14', id, 'ENTRY', (entry_ts AT TIME ZONE 'Asia/Kolkata'), 'min',
         symbol, UPPER(side), 'Equity Screener', 'EQUITY',
         NULL, entry_px::numeric, NULL, NULL, tag
  FROM v14_trades WHERE entry_ts IS NOT NULL
  UNION ALL
  SELECT 'v14', id, 'EXIT', (exit_ts AT TIME ZONE 'Asia/Kolkata'), 'min',
         symbol, UPPER(side), 'Equity Screener', 'EQUITY',
         NULL, exit_px::numeric, NULL, exit_reason,
         NULLIF(CONCAT_WS(' · ', tag,
                -- chr(37) is the percent sign; see PERCENT_SIGNS_IN_SQL above for why the
                -- character itself must never appear in this string, comments included.
                CASE WHEN net_pnl_pct IS NOT NULL
                     THEN ROUND(net_pnl_pct::numeric, 2)::text || chr(37) || ' net' END), '')
  FROM v14_trades WHERE exit_ts IS NOT NULL

  -- INTRADAY (tc) · entry_ts/exit_ts are NAIVE IST -> read raw. Context isolation (rule 7) is a
  -- WRITE rule; this is a read-only display and each event states its own engine, so a tc row can
  -- never be mistaken for a v8 row here.
  UNION ALL
  SELECT 'tc', id, 'ENTRY', entry_ts::timestamp, 'min',
         symbol, UPPER(side), 'Equity Screener', 'EQUITY',
         qty::numeric, entry_price::numeric, NULL, NULL, NULL
  FROM tc_intraday_trades WHERE entry_ts IS NOT NULL
  UNION ALL
  SELECT 'tc', id, 'EXIT', exit_ts::timestamp, 'min',
         symbol, UPPER(side), 'Equity Screener', 'EQUITY',
         qty::numeric, exit_price::numeric, pnl::numeric, result, NULL
  FROM tc_intraday_trades WHERE exit_ts IS NOT NULL

  -- EQUITY SWING (QSR) · cc#1175, session_log 27980. The equity swing slot, with a live source.
  -- entry_ts and exit_ts are NAIVE IST already (qsr_engine writes them that way, the cc#844
  -- convention) so they are read RAW — no AT TIME ZONE here. Converting them would shift every
  -- QSR event by 5h30m and scramble it against the v8 rows beside it, which is the exact bug the
  -- header of this file warns about.
  -- pnl is in RUPEES on this engine, so it ships in the `pnl` field like v8 and quant do, and the
  -- PERCENT rides in the note instead — labelled, and emitted with chr(37) because a literal
  -- percent character anywhere in this string breaks psycopg's placeholder scan (see
  -- PERCENT_SIGNS_IN_SQL above; that outage took the whole wall dark once).
  UNION ALL
  SELECT 'qsr', id, 'ENTRY', entry_ts::timestamp, 'min',
         symbol, 'LONG', 'Equity Swing', 'EQUITY',
         qty::numeric, entry_price::numeric, NULL, NULL, segment
  FROM qsr_trades WHERE entry_ts IS NOT NULL
  UNION ALL
  SELECT 'qsr', id, 'EXIT', exit_ts::timestamp, 'min',
         symbol, 'LONG', 'Equity Swing', 'EQUITY',
         qty::numeric, exit_price::numeric, pnl::numeric, exit_reason,
         NULLIF(CONCAT_WS(' · ', segment,
                CASE WHEN pnl_pct IS NOT NULL
                     THEN ROUND(pnl_pct::numeric, 2)::text || chr(37) END), '')
  FROM qsr_trades WHERE exit_ts IS NOT NULL

  -- cc#1000: OPTIONS (options_trades, the stock-options engine) is EXCLUDED from the wall — it is
  -- NOT on the founder's four-group list. Read-only exclusion; the table is untouched. The OPTIONS
  -- instrument CLASS still exists on the wall via the V10 Index option legs above.

  -- QUANT BASKETS · DATE columns only -> day precision, never faked up to a clock time.
  -- No side column: these baskets are long-only by construction, so LONG is stated, not blank.
  UNION ALL
  SELECT 'quant', id, 'ENTRY', entry_date::timestamp, 'day',
         symbol, 'LONG', 'Quant Basket', 'EQUITY',
         qty::numeric, entry_price::numeric, NULL, NULL, basket_name
  FROM quant_paper_positions WHERE entry_date IS NOT NULL
  UNION ALL
  SELECT 'quant', id, 'EXIT', exit_date::timestamp, 'day',
         symbol, 'LONG', 'Quant Basket', 'EQUITY',
         qty::numeric, exit_price::numeric, pnl::numeric, status, basket_name
  FROM quant_paper_positions WHERE exit_date IS NOT NULL

  -- cc#1000: V9 PAIRS (v9_paper_trades) is EXCLUDED — not on the founder's four-group list, and it
  -- would be a fifth group. Empty today; when the pairs engine goes live it re-enters via a NEW card.
)
-- cc#1000: TODAY-ONWARDS display filter (WALL_EPOCH). ev.ts is naive IST on every branch (the
-- timestamptz sources are already AT TIME ZONE-converted above), so a bare ::timestamp compare is
-- correct. This is the ONLY place the epoch is applied; every consumer (events, chip counts,
-- by_engine) wraps this union, so all of them inherit the filter from here.
SELECT ev.*, (src || ':' || id::text || ':' || event) AS sk FROM ev
WHERE ev.ts >= '""" + WALL_EPOCH + """'::timestamp
"""


# The guard that makes PERCENT_SIGNS_IN_SQL enforceable instead of merely written down. This
# raises at IMPORT — so a bad edit fails the deploy loudly, at boot, instead of answering 500 to a
# reader who then has to report it. cc#991 shipped exactly that bug and only the founder caught it.
assert chr(37) not in _EVENTS_SQL, (
    "trade_wall_endpoints: a percent character reached _EVENTS_SQL. psycopg reads it as a "
    "placeholder and every parameterised call to this union will fail at the driver. Emit it with "
    "chr(37) instead, and keep it out of SQL comments too — the scanner does not skip them.")


def _fetch(cur, limit, cur_ts, cur_sk, instrument):
    """One page of the wall, newest first, keyset-paged. Returns limit+1 rows when more exist."""
    where, args = [], []
    if instrument:
        where.append("instrument = %s")
        args.append(instrument)
    if cur_ts is not None:
        # Written as two comparisons rather than a row constructor so it cannot trip on the
        # column's exact timestamp type — the same shape cc#983 used on the intel feed.
        where.append("(ts < %s::timestamp OR (ts = %s::timestamp AND sk < %s))")
        args += [cur_ts, cur_ts, cur_sk]
    sql = "SELECT * FROM (" + _EVENTS_SQL + ") w"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # +1 row answers has_more without a second COUNT.
    sql += " ORDER BY ts DESC, sk DESC LIMIT %s"
    args.append(limit + 1)
    cur.execute(sql, args)
    return _rows(cur)


def _shape(r):
    def f(v):
        return float(v) if v is not None else None
    ts = r["ts"]
    day = (r["prec"] == "day")
    return {
        "id": r["sk"],
        "src": r["src"],
        "event": r["event"],
        "symbol": r["symbol"],
        "side": r["side"],
        "engine": r["engine"],
        "instrument": r["instrument"],
        "qty": f(r["qty"]),
        "price": f(r["price"]),
        "pnl": f(r["pnl"]),
        "result": r["result"],
        "note": r["note"],
        # `prec` is shipped so the renderer never prints a clock time the DB does not hold.
        "prec": r["prec"],
        "ts": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None,
        "when": (ts.strftime("%d %b") if day else ts.strftime("%H:%M")) if ts else None,
        "when_full": (ts.strftime("%d %b %Y") if day else ts.strftime("%d %b %Y %H:%M IST")) if ts else None,
        "day": ts.strftime("%d %b %Y") if ts else None,
    }


@router.get("/api/tradewall")
@_json_safe
def tradewall(request: Request, limit: int = 40, cursor: str = "", instrument: str = ""):
    """Every entry and every exit, newest first. Keyset-paged (cc#983 pattern, reused not rebuilt).

    cursor is "<iso ts>|<sort key>" and is opaque to the client — it only ever echoes back what
    the previous response handed it. A malformed cursor is an ERROR, never a silent page 1: that
    would be an infinite scroll that never advances and looks like duplicate content.
    """
    g = _guard(request)
    if g:
        return g
    limit = max(1, min(limit, 100))
    inst = (instrument or "").strip().upper()
    if inst in ("", "ALL"):
        inst = None
    elif inst not in INSTRUMENTS:
        return {"error": "unknown instrument", "known": list(INSTRUMENTS), "events": [], "has_more": False}

    cur_ts = cur_sk = None
    if cursor:
        try:
            _t, _s = str(cursor).rsplit("|", 1)
            cur_ts, cur_sk = _t, _s
            if not cur_ts or not cur_sk:
                raise ValueError("empty half")
        except Exception:
            return {"error": "bad cursor", "events": [], "has_more": False}

    with _conn() as conn, conn.cursor() as cur:
        rows = _fetch(cur, limit, cur_ts, cur_sk, inst)
        has_more = len(rows) > limit
        rows = rows[:limit]
        counts = {}
        totals = {}
        if not cursor:
            # Head-of-feed only: the totals are what the chips need, and re-running them on every
            # page would cost a full union scan per scroll for a number that has not changed.
            cur.execute("SELECT instrument, COUNT(*) n FROM (" + _EVENTS_SQL + ") w GROUP BY 1")
            counts = {r["instrument"]: r["n"] for r in _rows(cur)}
            cur.execute("SELECT engine, event, COUNT(*) n FROM (" + _EVENTS_SQL + ") w GROUP BY 1,2")
            for r in _rows(cur):
                totals.setdefault(r["engine"], {})[r["event"]] = r["n"]

    events = [_shape(r) for r in rows]
    last = rows[-1] if rows else None
    return {
        "events": events,
        "count": len(events),
        "has_more": has_more,
        "next_cursor": (last["ts"].strftime("%Y-%m-%d %H:%M:%S") + "|" + last["sk"]) if (last and has_more) else None,
        "instrument": inst or "ALL",
        # Chip counts over the WHOLE wall, not the loaded page — a chip that counts only what has
        # scrolled into view tells the reader the wall is smaller than it is.
        "counts": counts,
        "total": sum(counts.values()) if counts else None,
        "by_engine": totals,
        "instruments": list(INSTRUMENTS),
        "as_of": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        # cc#1000: the wall is today-onwards only — both renderers state the scope in the header so
        # the count is never mistaken for the all-time book. `count`/`counts` already reflect the epoch.
        "epoch": WALL_EPOCH,
        "scope": "since 10 Aug 2026",
    }


@router.get("/api/mobile/tradewall")
@_json_safe
def tradewall_mobile_alias(request: Request, limit: int = 40, cursor: str = "", instrument: str = ""):
    """Alias kept because the card named this path. Same function, one computation."""
    return tradewall(request, limit=limit, cursor=cursor, instrument=instrument)


@router.get("/m/trades", response_class=HTMLResponse)
def m_trades():
    return _page("trade_wall")


@router.get("/trades", response_class=HTMLResponse)
def web_trades():
    """The web renderer. Same endpoint, desktop layout.

    Served with its own reader rather than mobile_endpoints._page(): that helper is rooted at the
    mobile template directory, and reaching a repo-root file through it would mean passing "../",
    which is a path-traversal shape I am not putting in a route handler even when the argument is
    a constant.
    """
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "trade_wall_web.html"), "r", encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return HTMLResponse("Wall of Trades is not wired yet.", status_code=404,
                            headers={"Cache-Control": "no-store"})
