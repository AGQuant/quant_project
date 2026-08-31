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

# cc#1295 (founder 24-Aug, session_log 30420 WALL_OF_TRADES_GROUPS_V2 — supersedes cc#1000/cc#1175's
# four-group lock). STATUSES the wall's level-1 split offers, default OPEN.
STATUSES = ("open", "closed")

# cc#1000 (founder 10-Aug): the wall shows TODAY-ONWARDS only. WALL_EPOCH is a DISPLAY filter —
# every event with ts >= this date is shown, everything older is hidden. NOTHING is deleted from any
# source table (all read-only; the full trade history stays intact in the DB). Both the web page and
# the mobile page inherit this through the single union below. Stated in the header as "since 10 Aug".
WALL_EPOCH = "2026-08-10"

# cc#1295 (founder 24-Aug, session_log 30420 WALL_OF_TRADES_GROUPS_V2) SUPERSEDES cc#1000's and
# cc#1175's four-group, event-chronology design. The prior scope note (four flat groups, an
# ENTRY/EXIT event toggle) lived here; both are gone from this file, not archived in it — the git
# history at commit-before-cc#1295 is where that shape is preserved if it is ever needed again.
#
# NEW SHAPE, two levels: STATUS first (open / closed, default open), then INSTRUMENT (Futures /
# Equity), each holding TAGGED ENGINE BUCKETS:
#   FUTURES: V8 (v8_paper_trades + v8_paper_positions, unchanged source)
#            TC Scanner — NEW, source tc_scanner_holds (cc#464/1288/1289/1292)
#            Index Intel — renamed from plain "Index", source v10_trades leg='FUT' (unchanged)
#   EQUITY:  QB Basket (quant_paper_positions, unchanged source) — renamed from "Quant Basket"
#            Investment Scanner — NEW, source investment_scanner_state (cc#1283-1285/1297)
# OPTIONS instrument still exists (V10 leg='OPT') but carries no tagged bucket of its own on this
# wall, same as before.
#
# THE EVENT->POSITION SHIFT. Every source table already carries entry AND exit columns on the SAME
# row (verified live before writing this: v8_paper_trades, v10_trades, quant_paper_positions,
# tc_scanner_holds, investment_scanner_state all do — none of them needed a join to get there). The
# old union emitted each trade TWICE, once as an ENTRY event and once as an EXIT event, to build a
# flat chronological feed. That shape cannot satisfy "Closed tab shows realised P&L on every row"
# — an entry-half row has no pnl. So this union now emits ONE ROW PER POSITION, carrying both
# entry_ts/entry_price and exit_ts/exit_price/pnl together, with a `status` column ('open'/'closed')
# derived from whether an exit exists. `ts` (used for sort/paging/epoch) is COALESCE(exit_ts,
# entry_ts) — an open row sorts on when it was entered, a closed row on when it was closed, which
# is what "newest first" should mean once status is its own filter dimension.
#
# EQUITY SWING (QSR) — REMOVED from this union entirely, per the founder's explicit 24-Aug
# clarifying answer (session_log 30420): "drop it and just add in i button other scanners." qsr_trades
# is UNTOUCHED in the database and is still queryable — see other_wall_engines() below, which the
# i-button reads. Bringing QSR back onto the primary wall is a one-push revert of removing that
# function call from the button and re-adding a branch here, not a data change.
#
# ALSO STILL EXCLUDED, same reasoning as before cc#1295, unaffected by this card:
#   tc_intraday_trades + v14_trades — the old Equity Screener group, removed at cc#1175. Tables
#                   untouched; a one-push revert of that removal (not touched by this card).
#   options_trades  (the stock-options engine) — never on the founder's list.
#   v9_paper_trades (V9 Pairs) — empty today; needs its own future card to re-enter the wall.

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
# Every branch emits the SAME column list so the outer query can sort and page one flat stream —
# ONE ROW PER POSITION now (see the cc#1295 note above for why). `ts` = COALESCE(exit_ts,
# entry_ts): an open row sorts/pages on its entry, a closed row on its exit. `sk` is the tie-break
# sort key (source + id), unique across the whole union, so the keyset cursor has a total order.
_EVENTS_SQL = """
WITH ev AS (
  -- V8 · entry_ts/exit_ts are NAIVE IST -> read raw. One row per trade already.
  SELECT 'v8'::text src, id::text id,
         CASE WHEN COALESCE(exit_ts, closed_at) IS NOT NULL THEN 'closed' ELSE 'open' END::text status,
         symbol, UPPER(side) side, 'V8'::text engine, 'FUTURES'::text instrument,
         qty::numeric qty,
         entry_ts::timestamp entry_ts, 'min'::text entry_prec, entry_price::numeric entry_price,
         COALESCE(exit_ts, closed_at)::timestamp exit_ts, 'min'::text exit_prec, exit_price::numeric exit_price,
         pnl::numeric pnl, NULL::numeric pnl_pct, result, basket::text note,
         NULL::timestamp computed_ts   -- cc#1532: QB Basket only, see the quant branch below
  FROM v8_paper_trades WHERE entry_ts IS NOT NULL

  -- V8 · still-open positions live in a SEPARATE table with no exit columns at all.
  UNION ALL
  SELECT 'v8open', id::text, 'open',
         symbol, UPPER(side), 'V8', 'FUTURES',
         qty::numeric,
         entry_ts::timestamp, 'min', entry_price::numeric,
         NULL::timestamp, NULL::text, NULL::numeric,
         NULL::numeric, NULL::numeric, NULL::text, basket,
         NULL::timestamp
  FROM v8_paper_positions WHERE status = 'OPEN' AND entry_ts IS NOT NULL

  -- INDEX INTEL (v10_trades, renamed from "Index") · timestamptz -> converted in SQL. Instrument
  -- per row from `leg`. side is BUY/SELL here (verified live — NOT already Long/Short like v8),
  -- mapped the same way TC Scanner needs below.
  UNION ALL
  SELECT 'v10', id::text,
         CASE WHEN exit_ts IS NOT NULL THEN 'closed' ELSE 'open' END,
         symbol,
         CASE WHEN UPPER(side)='BUY' THEN 'LONG' WHEN UPPER(side)='SELL' THEN 'SHORT' ELSE UPPER(side) END,
         'Index Intel', CASE WHEN UPPER(COALESCE(leg,'FUT')) = 'OPT' THEN 'OPTIONS' ELSE 'FUTURES' END,
         lot_size::numeric,
         (entry_ts AT TIME ZONE 'Asia/Kolkata')::timestamp, 'min', entry_price::numeric,
         (exit_ts AT TIME ZONE 'Asia/Kolkata')::timestamp, 'min', exit_price::numeric,
         pnl::numeric, NULL::numeric, exit_reason,
         NULLIF(CONCAT_WS(' ', leg, opt_type, opt_strike::text), ''),
         NULL::timestamp
  FROM v10_trades WHERE entry_ts IS NOT NULL

  -- TC SCANNER (tc_scanner_holds) — NEW, cc#1295. entry_ts/exit_ts NAIVE IST -> read raw, same
  -- doctrine as v8. side is BUY/SELL -> mapped to Long/Short. pnl_pct computed the same way
  -- tc_scanner_endpoints.py's own get_repair_sheet does: (exit-entry)/entry*100, sign flipped for
  -- SHORT. No rupee pnl on this engine (no position sizing), pnl stays NULL.
  UNION ALL
  SELECT 'tc', id::text,
         CASE WHEN exit_reason = 'OPEN' THEN 'open' ELSE 'closed' END,
         symbol,
         CASE WHEN UPPER(side)='BUY' THEN 'LONG' WHEN UPPER(side)='SELL' THEN 'SHORT' ELSE UPPER(side) END,
         'TC Scanner', 'FUTURES',
         NULL::numeric,
         entry_ts::timestamp, 'min', entry_price::numeric,
         exit_ts::timestamp, 'min', exit_price::numeric,
         NULL::numeric,
         CASE WHEN exit_reason <> 'OPEN' AND entry_price IS NOT NULL AND entry_price <> 0
                   AND exit_price IS NOT NULL
              THEN ROUND(((exit_price - entry_price) / entry_price * 100
                          * CASE WHEN UPPER(side)='BUY' THEN 1 ELSE -1 END)::numeric, 2)
         END,
         exit_reason, style::text,
         NULL::timestamp
  FROM tc_scanner_holds WHERE entry_ts IS NOT NULL

  -- cc#1000: OPTIONS (options_trades, the stock-options engine) is EXCLUDED from the wall — never
  -- on the founder's list. Read-only exclusion; the table is untouched. The OPTIONS instrument
  -- CLASS still exists on the wall via the V10 Index option legs above.

  -- QB BASKET (quant_paper_positions, renamed from "Quant Basket") · DATE columns only -> day
  -- precision, never faked up to a clock time. Long-only by construction, no side column.
  UNION ALL
  SELECT 'quant', id::text,
         CASE WHEN status = 'open' THEN 'open' ELSE 'closed' END,
         symbol, 'LONG', 'QB Basket', 'EQUITY',
         qty::numeric,
         entry_date::timestamp, 'day', entry_price::numeric,
         exit_date::timestamp, 'day', exit_price::numeric,
         pnl::numeric, pnl_pct::numeric, status, basket_name,
         -- cc#1532: created_at is naive UTC (a third convention this file's own header does not
         -- document — confirmed live, not assumed). Double AT TIME ZONE, NOT the single-conversion
         -- naive-IST-table pattern used elsewhere in this union — a single conversion silently
         -- misreads this column. Honest batch-write timestamp, never a market entry time.
         (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata')::timestamp computed_ts
  FROM quant_paper_positions WHERE entry_date IS NOT NULL

  -- INVESTMENT SCANNER (investment_scanner_state) — NEW, cc#1295/1297. No qty (signal-only
  -- engine, V1 has no position sizing), no side column (V1 writes BUY/Long only per cc#1283-1285's
  -- own spec). entered_at/exited_at are DATE only. symbol is this table's PK, used as `id` since
  -- there is no separate id column. entry_price/exit_price were added by cc#1297 tonight — the one
  -- legacy row entered before that card has entry_price NULL and its pct is correctly NULL below,
  -- not fabricated.
  UNION ALL
  SELECT 'invscan', symbol,
         CASE WHEN status = 'open' THEN 'open' ELSE 'closed' END,
         symbol, 'LONG', 'Investment Scanner', 'EQUITY',
         NULL::numeric,
         entered_at::timestamp, 'day', entry_price::numeric,
         exited_at::timestamp, 'day', exit_price::numeric,
         NULL::numeric,
         CASE WHEN status <> 'open' AND entry_price IS NOT NULL AND entry_price <> 0
                   AND exit_price IS NOT NULL
              THEN ROUND(((exit_price - entry_price) / entry_price * 100)::numeric, 2)
         END,
         exit_reason, entry_track,
         NULL::timestamp
  FROM investment_scanner_state WHERE entered_at IS NOT NULL

  -- MANUAL ALERT (trade_alerts) — NEW bucket, cc#1505 (MANUAL_TRADE_ALERTS_V1, 34521). Only
  -- APPROVED alerts reach the wall: pending/triggered are intent, approved is the founder's
  -- click, and the wall shows positions taken, not positions considered. approved_at is
  -- timestamptz -> converted in SQL (the cc#887 doctrine above). entry_price is approved_price
  -- (the resolver price AT approval, never the trigger). Instrument reuses the app's ONE
  -- bare-symbol classifier — futures_universe WHERE is_active (the same source the trade card's
  -- lot_size, the strip's D-button and cc#1500's caret already read) — not a new rule. No exit
  -- concept on a manual alert yet, so every row is 'open' with no exit columns; no qty (no
  -- position sizing in V1).
  UNION ALL
  SELECT 'alert', id::text, 'open',
         symbol,
         CASE WHEN UPPER(direction)='BUY' THEN 'LONG' WHEN UPPER(direction)='SELL' THEN 'SHORT' ELSE UPPER(direction) END,
         -- cc#1524 scope 6: an approved ENGINE signal files under its own engine (V8 etc), so
         -- the wall shows it where the signal lives; only a truly manual alert (no source link)
         -- stays in the Manual Alert bucket. COALESCE, no new branch, no new bucket.
         COALESCE(a.source_engine, 'Manual Alert'),
         CASE WHEN EXISTS (SELECT 1 FROM futures_universe f
                           WHERE f.symbol = a.symbol AND f.is_active) THEN 'FUTURES' ELSE 'EQUITY' END,
         NULL::numeric,
         (approved_at AT TIME ZONE 'Asia/Kolkata')::timestamp, 'min', approved_price::numeric,
         NULL::timestamp, NULL::text, NULL::numeric,
         NULL::numeric, NULL::numeric, NULL::text, notes,
         NULL::timestamp
  FROM trade_alerts a WHERE status = 'approved' AND approved_at IS NOT NULL

  -- cc#1000: V9 PAIRS (v9_paper_trades) is EXCLUDED — never on the founder's list, would be a
  -- sixth bucket. Empty today; when the pairs engine goes live it re-enters via a NEW card.

  -- EQUITY SWING (QSR) — REMOVED from this union at cc#1295 (session_log 30420, founder's
  -- explicit "drop it and just add in i button other scanners"). qsr_trades is UNTOUCHED in the
  -- database and stays queryable via other_wall_engines() below, which the i-button reads.
)
SELECT ev.*, COALESCE(exit_ts, entry_ts) AS ts, (src || ':' || id) AS sk FROM ev
"""

# cc#1000: TODAY-ONWARDS display filter (WALL_EPOCH), applied HERE rather than inside _EVENTS_SQL
# so it can be composed once around the whole union — every consumer (events, chip counts,
# by_engine) wraps this, so all of them inherit the filter from the same place.
_WALL_SQL = "SELECT * FROM (" + _EVENTS_SQL + """) w
WHERE w.ts >= '""" + WALL_EPOCH + """'::timestamp
"""


# The guard that makes PERCENT_SIGNS_IN_SQL enforceable instead of merely written down. This
# raises at IMPORT — so a bad edit fails the deploy loudly, at boot, instead of answering 500 to a
# reader who then has to report it. cc#991 shipped exactly that bug and only the founder caught it.
assert chr(37) not in _EVENTS_SQL, (
    "trade_wall_endpoints: a percent character reached _EVENTS_SQL. psycopg reads it as a "
    "placeholder and every parameterised call to this union will fail at the driver. Emit it with "
    "chr(37) instead, and keep it out of SQL comments too — the scanner does not skip them.")


def _fetch(cur, limit, cur_ts, cur_sk, instrument, status):
    """One page of the wall, newest first, keyset-paged. Returns limit+1 rows when more exist."""
    where, args = [], []
    if instrument:
        where.append("instrument = %s")
        args.append(instrument)
    if status:
        where.append("status = %s")
        args.append(status)
    if cur_ts is not None:
        # Written as two comparisons rather than a row constructor so it cannot trip on the
        # column's exact timestamp type — the same shape cc#983 used on the intel feed.
        where.append("(ts < %s::timestamp OR (ts = %s::timestamp AND sk < %s))")
        args += [cur_ts, cur_ts, cur_sk]
    sql = "SELECT * FROM (" + _WALL_SQL + ") w"
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

    def stamp(ts, prec):
        if not ts:
            return {"ts": None, "when": None, "when_full": None, "day": None}
        day = (prec == "day")
        return {
            "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "when": ts.strftime("%d %b") if day else ts.strftime("%H:%M"),
            "when_full": ts.strftime("%d %b %Y") if day else ts.strftime("%d %b %Y %H:%M IST"),
            "day": ts.strftime("%d %b %Y"),
        }

    entry = stamp(r["entry_ts"], r["entry_prec"])
    exitd = stamp(r["exit_ts"], r["exit_prec"])
    ts = r["ts"]
    # cc#1532: QB Basket only (every other branch emits NULL for computed_ts in the union above).
    # entry_date/exit_date stay day-precision, untouched — this is a SEPARATE field: the real
    # batch-write clock time, honestly labelled "computed" so it is never mistaken for a market
    # entry/execution time. entry.when/when_full/day above are unchanged.
    computed_raw = r["computed_ts"]
    entry["computed"] = ({"ts": computed_raw.strftime("%Y-%m-%d %H:%M:%S"),
                           "when": computed_raw.strftime("%H:%M")}
                          if computed_raw else None)
    return {
        "id": r["sk"],
        "src": r["src"],
        "status": r["status"],
        "symbol": r["symbol"],
        "side": r["side"],
        "engine": r["engine"],
        "instrument": r["instrument"],
        "qty": f(r["qty"]),
        "entry_price": f(r["entry_price"]),
        "exit_price": f(r["exit_price"]),
        "pnl": f(r["pnl"]),
        "pnl_pct": f(r["pnl_pct"]),
        "result": r["result"],
        "note": r["note"],
        "entry": entry,
        "exit": exitd,
        # `ts`/`when*` kept at top level too, mirroring the entry/exit block for whichever anchor
        # this row is currently sorted on (entry for open, exit for closed) — same convenience the
        # old flat-event shape gave callers, so a renderer that just wants "when did this happen"
        # does not have to branch on status.
        "ts": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None,
        "when_full": (exitd["when_full"] if r["status"] == "closed" else entry["when_full"]),
        "day": (exitd["day"] if r["status"] == "closed" else entry["day"]),
    }


@router.get("/api/tradewall")
@_json_safe
def tradewall(request: Request, limit: int = 40, cursor: str = "", instrument: str = "", status: str = "open"):
    """Every position, newest first, keyset-paged (cc#983 pattern, reused not rebuilt).

    cc#1295: `status` (open/closed, default open) is now the primary split — see the union's own
    header note for why this replaced the old ENTRY/EXIT event toggle.

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

    st = (status or "open").strip().lower()
    if st in ("", "all"):
        st = None
    elif st not in STATUSES:
        return {"error": "unknown status", "known": list(STATUSES), "events": [], "has_more": False}

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
        rows = _fetch(cur, limit, cur_ts, cur_sk, inst, st)
        has_more = len(rows) > limit
        rows = rows[:limit]
        counts = {}
        status_counts = {}
        totals = {}
        if not cursor:
            # Head-of-feed only: the totals are what the chips need, and re-running them on every
            # page would cost a full union scan per scroll for a number that has not changed.
            # Instrument/status counts are over the WHOLE wall (both statuses / all instruments),
            # never just what happens to be loaded — by_engine is the one broken down BY status,
            # since that is what each bucket tab needs to show a live count under the active tab.
            cur.execute("SELECT instrument, COUNT(*) n FROM (" + _WALL_SQL + ") w GROUP BY 1")
            counts = {r["instrument"]: r["n"] for r in _rows(cur)}
            cur.execute("SELECT status, COUNT(*) n FROM (" + _WALL_SQL + ") w GROUP BY 1")
            status_counts = {r["status"]: r["n"] for r in _rows(cur)}
            cur.execute("SELECT engine, status, COUNT(*) n FROM (" + _WALL_SQL + ") w GROUP BY 1,2")
            for r in _rows(cur):
                totals.setdefault(r["engine"], {})[r["status"]] = r["n"]

    events = [_shape(r) for r in rows]
    last = rows[-1] if rows else None
    return {
        "events": events,
        "count": len(events),
        "has_more": has_more,
        "next_cursor": (last["ts"].strftime("%Y-%m-%d %H:%M:%S") + "|" + last["sk"]) if (last and has_more) else None,
        "instrument": inst or "ALL",
        "status": st or "ALL",
        # Chip counts over the WHOLE wall, not the loaded page — a chip that counts only what has
        # scrolled into view tells the reader the wall is smaller than it is.
        "counts": counts,
        "status_counts": status_counts,
        "total": sum(status_counts.values()) if status_counts else None,
        "by_engine": totals,
        "instruments": list(INSTRUMENTS),
        "statuses": list(STATUSES),
        "as_of": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        # cc#1000: the wall is today-onwards only — both renderers state the scope in the header so
        # the count is never mistaken for the all-time book. `count`/`counts` already reflect the epoch.
        "epoch": WALL_EPOCH,
        "scope": "since 10 Aug 2026",
    }


@router.get("/api/mobile/tradewall")
@_json_safe
def tradewall_mobile_alias(request: Request, limit: int = 40, cursor: str = "", instrument: str = "", status: str = "open"):
    """Alias kept because the card named this path. Same function, one computation."""
    return tradewall(request, limit=limit, cursor=cursor, instrument=instrument, status=status)


# ── OTHER WALL ENGINES (i-button) ────────────────────────────────────────────────────────────
# cc#1295 (founder 24-Aug, session_log 30420): QSR is off the primary wall but "just add in i
# button other scanners" — this is that button's data source. Durable home for any FUTURE minor
# engine too (the role V9 Pairs was meant to fill per this file's own header, cc#1000): add a
# branch here and it is visible without a nav redesign, never a new tagged bucket.
_OTHER_ENGINES_SQL = """
SELECT 'Equity Swing (QSR)'::text name, 'EQUITY'::text instrument, 'qsr_trades'::text table_name,
       COUNT(*) FILTER (WHERE exit_ts IS NULL) AS open_n,
       COUNT(*) FILTER (WHERE exit_ts IS NOT NULL) AS closed_n
FROM qsr_trades
WHERE COALESCE(exit_ts, entry_ts) >= '""" + WALL_EPOCH + """'::timestamp
"""

assert chr(37) not in _OTHER_ENGINES_SQL, (
    "trade_wall_endpoints: a percent character reached _OTHER_ENGINES_SQL — same driver-level "
    "failure PERCENT_SIGNS_IN_SQL describes for _EVENTS_SQL. Emit it with chr(37) instead.")


@router.get("/api/tradewall/other-engines")
@_json_safe
def tradewall_other_engines(request: Request):
    """Engines with a real trade book that are NOT one of the wall's 5 tagged buckets — the
    i-button's list. QSR today; any future minor engine gets a branch here, not a wall redesign."""
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(_OTHER_ENGINES_SQL)
        rows = _rows(cur)
    return {
        "engines": [
            {"name": r["name"], "instrument": r["instrument"],
             "open": r["open_n"], "closed": r["closed_n"],
             "total": r["open_n"] + r["closed_n"]}
            for r in rows
        ],
        "as_of": _ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": WALL_EPOCH,
    }


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
