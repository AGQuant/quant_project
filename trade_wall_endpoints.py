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

import json
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

  -- SCREENERS (cc#1733, founder 06-Sep "three source of equity signals. QB, Investment Scan and
  -- Screeners") — the THIRD equity bucket. v13_screen_results is the OPEN side (a symbol still in
  -- a screen tonight), v13_screen_exits the CLOSED side (it left), keyed on (screen_id, symbol,
  -- first_seen). DATE columns only -> day precision, never faked up to a clock time. No qty and no
  -- side column exist on these tables: LONG by construction, qty NULL. NO PRICES: these tables
  -- hold no entry_price / exit_price and none is invented or joined from a close as though it
  -- were an execution -- entry_price, exit_price and pnl stay NULL for this bucket (a pricing
  -- convention is a separate card). note = screen_name so a row says Momentum Kings vs Hidden
  -- Value. THIRD-PARTY SCREENS ARE EXCLUDED BY DERIVATION, NOT BY NAME: only a screen whose
  -- v13_presets.filters is a NON-EMPTY JSON object is a computed Scorr screen; the Finz lists
  -- carry an empty object because their membership is supplied, not screened ("third party
  -- screener which never run on quant"). A new Scorr screen appears here automatically; a new
  -- supplied list never does. Read-only on every v13 table -- the EOD v13 run owns them.
  UNION ALL
  SELECT 'screen', r.screen_id::text || ':' || r.symbol || ':' || r.first_seen::text,
         'open',
         r.symbol, 'LONG', 'Screeners', 'EQUITY',
         NULL::numeric,
         r.first_seen::timestamp, 'day', NULL::numeric,
         NULL::timestamp, NULL::text, NULL::numeric,
         NULL::numeric, NULL::numeric, NULL::text, r.screen_name,
         NULL::timestamp
  FROM v13_screen_results r
  JOIN v13_presets p ON p.id = r.screen_id
  WHERE r.first_seen IS NOT NULL
    AND jsonb_typeof(p.filters) = 'object' AND p.filters <> '{}'::jsonb

  UNION ALL
  SELECT 'screen', x.screen_id::text || ':' || x.symbol || ':' || COALESCE(x.first_seen, x.exited_on)::text || ':x' || x.id::text,
         'closed',
         x.symbol, 'LONG', 'Screeners', 'EQUITY',
         NULL::numeric,
         COALESCE(x.first_seen, x.exited_on)::timestamp, 'day', NULL::numeric,
         x.exited_on::timestamp, 'day', NULL::numeric,
         NULL::numeric, NULL::numeric, NULL::text, COALESCE(x.screen_name, p.name),
         NULL::timestamp
  FROM v13_screen_exits x
  JOIN v13_presets p ON p.id = x.screen_id
  WHERE x.exited_on IS NOT NULL
    AND jsonb_typeof(p.filters) = 'object' AND p.filters <> '{}'::jsonb

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

# ── cc#1587 WOT_APPROVED_ONLY_V1 (founder 02-Sep, session_log 36394) ─────────────────────────
# The wall shows ONLY the buckets named in app_config key `wot_buckets_enabled` (a JSON list).
# Default after this card: ["approved_alerts"] — the founder's approved trade_alerts and nothing
# else. The union above is UNCHANGED; a bucket not in the list is filtered out here, at the same
# composition point as WALL_EPOCH, so every consumer (page, chip counts, by_engine) inherits it.
# Reversible by editing the app_config row — no redeploy. Nothing is deleted anywhere: the raw
# engine signals that leave the wall are still in their own engine tables.
#
# Bucket name -> the union's `src` keys it covers. Names are what a founder edits, so they are
# spelled for a human; src keys are what the SQL matches. An unknown name in the config is
# IGNORED (logged), never a crash and never a silent "show everything".
WOT_BUCKETS = {
    "approved_alerts":    ("alert",),
    "v8":                 ("v8", "v8open"),
    "index_intel":        ("v10",),
    "tc_scanner":         ("tc",),
    "qb_basket":          ("quant",),
    "investment_scanner": ("invscan",),
    "screeners":          ("screen",),    # cc#1733: third equity source (Scorr screens only)
}
WOT_BUCKETS_KEY = "wot_buckets_enabled"
# cc#1609 WOT_APPROVAL_SURFACE_V1 (session_log 36394 correction_02sep_1612): the wall is the
# APPROVAL SURFACE — the engine signals with an Approve per row — and Alerts is the approved
# book. approved_alerts is therefore NOT a bucket any more (an approval is a STATE on an engine
# row, joined from trade_alerts below), and the default is the engine set. Reverses cc#1587.
# cc#1732 (founder 06-Sep "why Equity no signal in WOT"): the two EQUITY buckets join the default.
# They were fully wired in WOT_BUCKETS since cc#1295 but absent from both this default and the
# app_config row, so the Equity chip filtered a set that was never allowed in and every engine
# under it read 0 — config drift from the locked spec (36394 names Equity explicitly), not a bug.
# cc#1733: screeners joins as the THIRD equity source, same wot_equity_epoch gate as the other two.
WOT_BUCKETS_DEFAULT = ["v8", "index_intel", "tc_scanner", "qb_basket", "investment_scanner", "screeners"]

# ── cc#1732 EQUITY: fresh signals only + quant-run baskets only ──────────────────────────────
# WOT_EQUITY_EPOCH — a SECOND display epoch, applied to the qb_basket and investment_scanner
# branches ONLY, alongside WALL_EPOCH (which is untouched and still gates every bucket). The
# founder does not want the historical equity book landing in the pending queue on the day the
# buckets switch on: a row ENTERED before this date is not shown on the wall; rows from this
# date onward arrive as pending-approval normally. Read from app_config `wot_equity_epoch` (a
# YYYY-MM-DD date) per request; the constant below is the in-code fallback and is the date the
# card shipped. This is a DISPLAY filter — nothing is written to trade_alerts to achieve it
# (inserting synthetic approved/dismissed rows would put positions the founder never approved
# into the approved book that feeds Alerts; that is a data-honesty violation, not a shortcut).
WOT_EQUITY_EPOCH_KEY = "wot_equity_epoch"
WOT_EQUITY_EPOCH_DEFAULT = "2026-09-06"

# QB_DISCRETIONARY — the qb_basket bucket is narrowed to QUANT-RUN baskets. FINZ baskets and the
# Model Portfolio are discretionary, founder-picked books whose holdings were decided by hand, so
# they are not signals awaiting approval. The exclusion is DERIVED, never a name list in this
# file: the SQL in _wall_sql reads app_config `qb_discretionary_baskets` (today the six finz_*
# plus model_portfolio) and, if that row is ever absent, falls back to "has a quant_basket_config
# row" (the registry test — only quant-run baskets have one). A new discretionary basket added to
# that config drops off the wall automatically with no code change, which is exactly why the
# list is not inline here.
QB_DISCRETIONARY_KEY = "qb_discretionary_baskets"


def wot_equity_epoch(cur):
    """The equity display epoch from app_config as 'YYYY-MM-DD'. Returns (date_str, missing_flag).

    A value that is not a real ISO date is treated as MISSING (falls to the shipped default),
    never composed into SQL — the wall SQL runs with no parameters on the count path, so the only
    thing allowed into the text is a value this function has validated.
    """
    from datetime import date as _date
    cur.execute("SELECT value FROM app_config WHERE key=%s", (WOT_EQUITY_EPOCH_KEY,))
    row = cur.fetchone()
    raw = (str(row[0]).strip() if row and row[0] else "")
    try:
        return _date.fromisoformat(raw[:10]).isoformat(), False
    except Exception:
        if raw:
            log.warning("wot_equity_epoch: %r is not a YYYY-MM-DD date, using default %s", raw, WOT_EQUITY_EPOCH_DEFAULT)
        return WOT_EQUITY_EPOCH_DEFAULT, True


# The derived QB exclusion, as SQL, so the wall never carries a basket name of its own. Reads the
# app_config JSON list without a jsonb cast (a hand-edited value that is not valid JSON must not
# 500 the wall — stray tokens simply match no basket), and falls back to the quant_basket_config
# registry only when the app_config row is absent. w.note IS basket_name on the quant branch.
# No percent character anywhere in here — see PERCENT_SIGNS_IN_SQL above.
_QB_EXCLUDED_SQL = """(SELECT TRIM(BOTH ' "' FROM x) FROM app_config c,
        LATERAL REGEXP_SPLIT_TO_TABLE(REGEXP_REPLACE(c.value, '[\\[\\]]', '', 'g'), ',') AS x
        WHERE c.key = '""" + QB_DISCRETIONARY_KEY + """' AND TRIM(BOTH ' "' FROM x) <> '')"""
_QB_NARROW_SQL = (" AND NOT (w.src = 'quant' AND (w.note IN " + _QB_EXCLUDED_SQL + "\n"
                  "     OR (NOT EXISTS (SELECT 1 FROM app_config c2 WHERE c2.key = '" + QB_DISCRETIONARY_KEY + "')\n"
                  "         AND w.note NOT IN (SELECT basket_name FROM quant_basket_config))))\n")


def wot_buckets_enabled(cur):
    """The enabled bucket names from app_config. Returns (names, missing_flag).

    Same parsing doctrine as v8_book_canon.retired_baskets: a JSON array is the intended shape; a
    comma-separated string is accepted so a hand edit still works; a JSON array that does not
    parse is treated as MISSING (falls to the default) rather than being read as a bucket literally
    named "[oops" — that would match nothing and blank the wall while looking healthy.
    """
    cur.execute("SELECT value FROM app_config WHERE key=%s", (WOT_BUCKETS_KEY,))
    row = cur.fetchone()
    raw = row[0] if row and row[0] else None
    if not raw:
        return list(WOT_BUCKETS_DEFAULT), True
    raw = str(raw).strip()
    names = None
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                names = [str(x).strip().lower() for x in parsed if str(x).strip()]
        except Exception:
            names = None
        if names is None:
            return list(WOT_BUCKETS_DEFAULT), True
    else:
        names = [p.strip().lower() for p in raw.replace("\n", ",").split(",") if p.strip()]
    unknown = [n for n in names if n not in WOT_BUCKETS]
    if unknown:
        log.warning("wot_buckets_enabled: ignoring unknown bucket names %s", unknown)
    known = [n for n in names if n in WOT_BUCKETS]
    # Names given but NONE of them known = a typo, not a decision. A typo must not blank the wall;
    # fall to the default. An explicit empty list ([]) is a decision and stays empty.
    if names and not known:
        return list(WOT_BUCKETS_DEFAULT), True
    return known, False


def _wall_sql(names, equity_epoch=None):
    """_WALL_SQL narrowed to the enabled buckets.

    The src keys are literals from WOT_BUCKETS above — never config text — so quoting them into
    the SQL is safe, and it has to be literal: the chip-count queries in tradewall() call execute()
    with NO parameters, and psycopg leaves a bare placeholder verbatim on that path (see
    PERCENT_SIGNS_IN_SQL). An empty list yields a wall with no rows, stated as such, not the full
    union.

    cc#1732: `equity_epoch` ('YYYY-MM-DD', already validated by wot_equity_epoch()) gates the
    qb_basket + investment_scanner (+ cc#1733 screeners) branches to rows ENTERED on or after it; the QB narrowing to
    quant-run baskets (_QB_NARROW_SQL, derived from app_config / quant_basket_config) is always
    composed. Both sit here, at the same composition point as WALL_EPOCH, so every consumer —
    page, chip counts, by_engine, the pending count — inherits them from one place.
    """
    srcs = sorted({s for n in names for s in WOT_BUCKETS.get(n, ())})
    if not srcs:
        return _WALL_SQL + " AND false\n"
    # cc#1609 + V10_DISPLAY_OPTIONS_ONLY_V1 (36703): an Index Intel row on the wall is the OPTION
    # leg only. Futures legs stay in v10_trades (record) and never reach a display.
    sql = (_WALL_SQL + " AND w.src IN (" + ", ".join("'" + s + "'" for s in srcs) + ")\n"
           + " AND NOT (w.src = 'v10' AND w.instrument = 'FUTURES')\n")
    if equity_epoch:
        # cc#1733: the screeners bucket rides the same gate -- without it this bucket alone puts
        # ~247 historical rows into the pending queue on day one.
        sql += (" AND NOT (w.src IN ('quant', 'invscan', 'screen') AND w.entry_ts < '" + str(equity_epoch)[:10]
                + "'::timestamp)\n")
    sql += _QB_NARROW_SQL
    return sql


# The guard that makes PERCENT_SIGNS_IN_SQL enforceable instead of merely written down. This
# raises at IMPORT — so a bad edit fails the deploy loudly, at boot, instead of answering 500 to a
# reader who then has to report it. cc#991 shipped exactly that bug and only the founder caught it.
assert chr(37) not in _EVENTS_SQL, (
    "trade_wall_endpoints: a percent character reached _EVENTS_SQL. psycopg reads it as a "
    "placeholder and every parameterised call to this union will fail at the driver. Emit it with "
    "chr(37) instead, and keep it out of SQL comments too — the scanner does not skip them.")


def _fetch(cur, limit, cur_ts, cur_sk, instrument, status, wall_sql):
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
    sql = "SELECT * FROM (" + wall_sql + ") w"
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
        # cc#1587: where an approved alert came from. The union already files an approved ENGINE
        # signal under its engine label (cc#1524) and a hand-made one under "Manual Alert"; the
        # renderers show that as an Origin column, so it is named once here, not derived twice.
        "origin": (("manual" if r["engine"] == "Manual Alert" else r["engine"])
                   if r["src"] == "alert" else None),
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


def _decode_map():
    """cc#1734 scope 3: DETAIL decode sources, imported not copied. Either import failing leaves
    that half empty, and the page then prints the raw codes — never a made-up label."""
    out = {"exit_reasons": {}, "tags": {},
           "sources": {"exit_reasons": "trade_alerts_endpoints._CLOSE_WORDS", "tags": "mobile_endpoints.BASKET_LABELS"},
           "rule": "exact code -> engine word; 'CODE (suffix)' keeps the suffix; anything unmapped prints raw"}
    try:
        from trade_alerts_endpoints import _CLOSE_WORDS
        out["exit_reasons"] = dict(_CLOSE_WORDS)
    except Exception as e:
        out["sources"]["exit_reasons_error"] = str(e)[:120]
    try:
        from mobile_endpoints import BASKET_LABELS
        out["tags"] = dict(BASKET_LABELS)
    except Exception as e:
        out["sources"]["tags_error"] = str(e)[:120]
    return out


# ── cc#1736 SUPPRESS-IN-POSITION (founder 06-Sep: "if position already approved and open then no
# new signals for that position should display in WOT") ─────────────────────────────────────────
# approved_and_open = trade_alerts.status = 'approved' AND no trade_alert_levels.closed_at (the
# cc#1735 sidecar; LEFT JOIN so an alert with no levels row counts as open). Direction BUY/SELL on
# trade_alerts is mapped to the wall's LONG/SHORT. Position-level rule: both instruments, every
# engine bucket. Nothing is written here — the state is computed per request and the rows are
# still served (excluded from the default pending list, counted, revealable on the page).
_HELD_SQL = """SELECT a.id, a.symbol, UPPER(a.direction) AS direction,
       CASE WHEN UPPER(a.direction) = 'BUY' THEN 'LONG' WHEN UPPER(a.direction) = 'SELL' THEN 'SHORT' ELSE UPPER(a.direction) END AS side,
       a.source_engine, a.source_ref,
       a.approved_at AT TIME ZONE 'Asia/Kolkata' AS approved_ist, a.approved_price
  FROM trade_alerts a LEFT JOIN trade_alert_levels l ON l.alert_id = a.id
 WHERE a.status = 'approved' AND l.closed_at IS NULL"""
# Appended to the undecided-open-rows count in tradewall() (alias w). No percent character.
_SUPPRESSED_WHERE = ("AND EXISTS (SELECT 1 FROM trade_alerts a LEFT JOIN trade_alert_levels l ON l.alert_id = a.id "
                     "WHERE a.status = 'approved' AND l.closed_at IS NULL AND a.symbol = w.symbol "
                     "AND (CASE WHEN UPPER(a.direction) = 'BUY' THEN 'LONG' WHEN UPPER(a.direction) = 'SELL' THEN 'SHORT' END) = w.side "
                     "AND NOT (a.source_engine = w.engine AND a.source_ref = w.symbol || '@' || to_char(w.entry_ts, 'YYYY-MM-DD HH24:MI:SS')))")


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
        # cc#1587: the enabled-bucket list is read per request so an app_config edit takes effect
        # on the next load, no redeploy.
        buckets, buckets_missing = wot_buckets_enabled(cur)
        # cc#1732: equity epoch + QB narrowing ride the same composition point (see _wall_sql).
        equity_epoch, epoch_missing = wot_equity_epoch(cur)
        wall_sql = _wall_sql(buckets, equity_epoch=equity_epoch)
        rows = _fetch(cur, limit, cur_ts, cur_sk, inst, st, wall_sql)
        has_more = len(rows) > limit
        rows = rows[:limit]
        counts = {}
        status_counts = {}
        totals = {}
        approved_as_of = None
        if not cursor:
            # Head-of-feed only: the totals are what the chips need, and re-running them on every
            # page would cost a full union scan per scroll for a number that has not changed.
            # Instrument/status counts are over the WHOLE wall (both statuses / all instruments),
            # never just what happens to be loaded — by_engine is the one broken down BY status,
            # since that is what each bucket tab needs to show a live count under the active tab.
            cur.execute("SELECT instrument, COUNT(*) n FROM (" + wall_sql + ") w GROUP BY 1")
            counts = {r["instrument"]: r["n"] for r in _rows(cur)}
            cur.execute("SELECT status, COUNT(*) n FROM (" + wall_sql + ") w GROUP BY 1")
            status_counts = {r["status"]: r["n"] for r in _rows(cur)}
            cur.execute("SELECT engine, status, COUNT(*) n FROM (" + wall_sql + ") w GROUP BY 1,2")
            for r in _rows(cur):
                totals.setdefault(r["engine"], {})[r["status"]] = r["n"]
            # cc#1587 scope 5: the header says "as of <latest approved_at> IST" — the newest
            # approval, read from trade_alerts itself, never the page clock.
            cur.execute("SELECT MAX(approved_at AT TIME ZONE 'Asia/Kolkata') FROM trade_alerts "
                        "WHERE status = 'approved' AND approved_at IS NOT NULL")
            _m = cur.fetchone()
            approved_as_of = _m[0].strftime("%d %b %Y %H:%M") if (_m and _m[0]) else None
            # cc#1609 scope 6: the header line — pending = open engine rows with no approve /
            # dismiss decision yet; approved today; newest signal ts. All over the WHOLE wall.
            cur.execute("SELECT COUNT(*) n FROM (" + wall_sql + ") w WHERE w.status = 'open' AND w.src <> 'alert' "
                        "AND NOT EXISTS (SELECT 1 FROM trade_alerts a WHERE a.kind = 'entry' "
                        "AND a.source_engine = w.engine "
                        "AND a.source_ref = w.symbol || '@' || to_char(w.entry_ts, 'YYYY-MM-DD HH24:MI:SS') "
                        "AND a.status IN ('approved', 'dismissed'))")
            _p = cur.fetchone()
            # cc#1736: of those undecided rows, how many are SUPPRESSED — same symbol + same side as
            # an approved-and-open alert (see _SUPPRESSED_WHERE). Counted over the WHOLE wall, and
            # subtracted from `pending` below so the header number keeps meaning "awaiting a
            # decision". THE PENDING FIGURE DROPS BY THIS NUMBER FROM THIS CARD ON — that is the
            # rule working, not data going missing; `suppressed` is served beside it.
            cur.execute("SELECT COUNT(*) n FROM (" + wall_sql + ") w WHERE w.status = 'open' AND w.src <> 'alert' "
                        "AND NOT EXISTS (SELECT 1 FROM trade_alerts a WHERE a.kind = 'entry' "
                        "AND a.source_engine = w.engine "
                        "AND a.source_ref = w.symbol || '@' || to_char(w.entry_ts, 'YYYY-MM-DD HH24:MI:SS') "
                        "AND a.status IN ('approved', 'dismissed')) " + _SUPPRESSED_WHERE)
            _sup = cur.fetchone()
            cur.execute("SELECT COUNT(*) n FROM trade_alerts WHERE status = 'approved' AND source_engine IS NOT NULL "
                        "AND (approved_at AT TIME ZONE 'Asia/Kolkata')::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date")
            _a = cur.fetchone()
            cur.execute("SELECT MAX(w.entry_ts) FROM (" + wall_sql + ") w WHERE w.status = 'open' AND w.src <> 'alert'")
            _n = cur.fetchone()
            approval_counts = {"pending": int(_p[0] or 0) - int(_sup[0] or 0),   # cc#1736: excludes suppressed
                               "pending_including_suppressed": int(_p[0] or 0),
                               "suppressed": int(_sup[0] or 0),
                               "approved_today": int(_a[0] or 0),
                               "newest_signal_ts": _n[0].strftime("%Y-%m-%d %H:%M:%S") if (_n and _n[0]) else None}
            # cc#1732: the basket names the QB narrowing is excluding RIGHT NOW, read from the same
            # subselect the wall SQL uses, so the response states the live rule rather than a claim.
            cur.execute("SELECT DISTINCT x FROM " + _QB_EXCLUDED_SQL + " AS q(x) ORDER BY 1")
            qb_excluded = [r[0] for r in cur.fetchall()]
            # cc#1734 scope 4: the Closed book summary per instrument x engine over the WHOLE closed
            # wall (the page loads 100 rows at a time, so a client-side sum would describe the
            # loaded slice, not the book). Rupees are summed ONLY over rows that carry a rupee
            # value; a percent-only row (TC Scanner, Investment Scanner, Screeners) is counted
            # separately and NEVER converted into rupees. win/loss = the sign of whichever value
            # the engine produced (pnl first, else pnl_pct).
            cur.execute("SELECT instrument, engine, COUNT(*) n, "
                        "COUNT(*) FILTER (WHERE COALESCE(pnl, pnl_pct) > 0) wins, "
                        "COUNT(*) FILTER (WHERE COALESCE(pnl, pnl_pct) < 0) losses, "
                        "COUNT(*) FILTER (WHERE COALESCE(pnl, pnl_pct) = 0) flat, "
                        "COUNT(pnl) rs_rows, COALESCE(SUM(pnl), 0) rs_total, "
                        "COUNT(*) FILTER (WHERE pnl IS NULL AND pnl_pct IS NOT NULL) pct_only, "
                        "COUNT(*) FILTER (WHERE pnl IS NULL AND pnl_pct IS NULL) unpriced "
                        "FROM (" + wall_sql + ") w WHERE w.status = 'closed' GROUP BY 1, 2 ORDER BY 1, 2")
            closed_summary = [{"instrument": r["instrument"], "engine": r["engine"], "n": int(r["n"]),
                               "wins": int(r["wins"]), "losses": int(r["losses"]), "flat": int(r["flat"]),
                               "rs_rows": int(r["rs_rows"]), "rs_total": float(r["rs_total"] or 0),
                               "pct_only": int(r["pct_only"]), "unpriced": int(r["unpriced"])} for r in _rows(cur)]
        else:
            approval_counts = None
            qb_excluded = None
            closed_summary = None

        events = [_shape(r) for r in rows]
        # cc#1609 scope 2: STATE per engine row from trade_alerts — pending-approval | approved |
        # dismissed — joined on the same key every approve surface writes: (source_engine = the
        # engine label as served, source_ref = symbol@entry.ts, kind = entry). An 'alert' row is
        # the approval record itself. Done after the fetch so the union SQL is untouched.
        keys = [(e["engine"], e["symbol"] + "@" + (e["entry"]["ts"] or "")) for e in events if e["src"] != "alert"]
        amap = {}
        if keys:
            cur.execute("""SELECT source_engine, source_ref, status, id, approved_price,
                                  approved_at AT TIME ZONE 'Asia/Kolkata' AS approved_ist, approved_via, notes
                           FROM trade_alerts
                           WHERE kind = 'entry' AND source_engine IS NOT NULL
                             AND source_ref = ANY(%s) AND status IN ('approved', 'dismissed')""",
                        ([k[1] for k in keys],))
            for a in _rows(cur):
                amap[(a["source_engine"], a["source_ref"])] = a
        # cc#1736: the approved-and-OPEN book — every approved alert with no closed_at on the
        # cc#1735 sidecar (before cc#1735 there was no close concept, so this dependency is stated
        # here rather than assumed: the LEFT JOIN is what lets suppression LIFT after an exit).
        held_same, held_sym = {}, {}
        if events:
            from trade_wall_approved import _ensure as _ensure_levels   # CREATE TABLE IF NOT EXISTS only
            _ensure_levels(conn)
            cur.execute(_HELD_SQL)
            for h in _rows(cur):
                hd = {"id": h["id"], "symbol": h["symbol"], "side": h["side"], "direction": h["direction"],
                      "engine": h["source_engine"] or "Manual", "source_ref": h["source_ref"],
                      "approved_at": h["approved_ist"].strftime("%Y-%m-%d %H:%M") if h["approved_ist"] else None,
                      "approved_price": float(h["approved_price"]) if h["approved_price"] is not None else None}
                held_same.setdefault((h["symbol"], h["side"]), hd)
                held_sym.setdefault(h["symbol"], []).append(hd)
        for e in events:
            e["suppressed_by"] = None
            e["reversal_of"] = None
            if e["src"] == "alert":
                e["state"] = "approved"
                e["approval"] = None
                continue
            ref = e["symbol"] + "@" + (e["entry"]["ts"] or "")
            a = amap.get((e["engine"], ref))
            if a:
                e["state"] = a["status"]
                e["approval"] = {"id": a["id"],
                                 "approved_price": float(a["approved_price"]) if a["approved_price"] is not None else None,
                                 "approved_at": a["approved_ist"].strftime("%Y-%m-%d %H:%M:%S") if a["approved_ist"] else None,
                                 "approved_via": a["approved_via"], "notes": a["notes"]}
            else:
                e["state"] = "pending-approval"
                e["approval"] = None
                # cc#1736 MATCH KEY = SYMBOL + DIRECTION (Fable decision on the card): an undecided
                # row is SUPPRESSED when an approved-and-open alert holds the SAME symbol in the SAME
                # direction, whatever engine produced either — the founder cannot take the same
                # position twice. The row itself is never that alert (its own row carries a decision
                # above), the source_ref check just says so explicitly. An OPPOSITE-direction row on a
                # held symbol is NOT suppressed: it may be the signal to get out, so it stays pending
                # and carries a REVERSAL tag naming the position it contradicts. Full per-symbol
                # suppression would be: drop the side from the key (held_sym instead of held_same).
                hd = held_same.get((e["symbol"], e["side"]))
                if hd and not (hd["engine"] == e["engine"] and hd["source_ref"] == ref):
                    e["state"] = "suppressed-in-position"
                    e["suppressed_by"] = hd
                else:
                    opp = [x for x in held_sym.get(e["symbol"], []) if x["side"] != e["side"]]
                    if opp:
                        e["reversal_of"] = opp[0]
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
        # cc#1587: which buckets this response was narrowed to, and whether that came from the
        # app_config row or the in-code default (row absent/unparseable).
        "buckets_enabled": buckets,
        "buckets_known": list(WOT_BUCKETS),
        "buckets_source": "default" if buckets_missing else "app_config",
        "approved_as_of": approved_as_of,
        # cc#1732: the equity gates, stated. equity_epoch applies to qb_basket + investment_scanner
        # rows only (by ENTRY date); qb_excluded_baskets is the live discretionary list (app_config
        # qb_discretionary_baskets; registry fallback = baskets with no quant_basket_config row).
        "equity_epoch": equity_epoch,
        "equity_epoch_source": "default" if epoch_missing else "app_config",
        "qb_excluded_baskets": qb_excluded,
        "qb_exclusion_rule": "quant rows whose basket_name is in app_config." + QB_DISCRETIONARY_KEY
                             + " are not shown; if that row is absent, only baskets with a quant_basket_config row are shown",
        # cc#1609: the approval surface — header counts + how state was joined, so no surface guesses.
        "approval_counts": approval_counts,
        "state_join": "trade_alerts(kind=entry, source_engine=engine, source_ref=symbol@entry.ts) -> pending-approval | approved | dismissed | suppressed-in-position (cc#1736)",
        # cc#1734: the Closed book summary (see the SQL above) and the DECODE map the renderer
        # applies to DETAIL — read from the engines' own words, never typed into the page:
        # exit codes from trade_alerts_endpoints._CLOSE_WORDS (the alerts feed's own close words),
        # basket/strategy tags from mobile_endpoints.BASKET_LABELS (the one shared slug->name map).
        # A code absent from both prints raw on the page — that is the rule, not a gap.
        "closed_summary": closed_summary,
        "decode": _decode_map(),
        # cc#1736: the suppression rule, stated where the states are stated.
        "suppression_rule": "an undecided open row whose SYMBOL + SIDE matches an approved-and-open alert (trade_alerts status=approved with no trade_alert_levels.closed_at) is state suppressed-in-position: counted in approval_counts.suppressed, excluded from approval_counts.pending, served with suppressed_by; an opposite-side row on a held symbol stays pending with reversal_of set",
        "v10_display": "OPT legs only (V10_DISPLAY_OPTIONS_ONLY_V1 36703)",
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


# ── cc#1733 (i) SHEET: universe / entry / exit per engine, READ not typed ─────────────────────
# Founder 06-Sep: "add i button for proper explanation of universe, entry and exit rules for equity
# and futures both." The founder approves trades from this page, so a wrong rule here is worse
# than a gap: every cell below is read from the engine's own machine-readable source, and where a
# rule has none the cell says "not specified in config" and the `source` field says where the
# behaviour actually lives. Nothing on this sheet is typed into HTML.
#   QB Basket          quant_basket_config.stage1_sector / stage2_stock (the same rows the cc#1708
#                      QB (i) sheet reads) + quant_basket_registry capital/max_stocks/rebalance_freq
#   Screeners          v13_presets.filters rendered as readable gates, scope as the universe
#   Investment Scanner inv_scanner_rules ENTRY_*/EXIT_* constants + the live universe table
#   V8                 v8_signal_writer.BASKET_FILTERS (the registry /api/v8/filter_config serves)
#   TC Scanner         tc_scanner_config.TC_SCANNER_CONFIG (the dict /api/tc-scanner/config serves)
#   Index Intel        v10_st_ema.INDEX_CFG (per-index, founder-locked specs)
_NOT_IN_CONFIG = "not specified in config"


def _fmt_num(v):
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else ("{:g}".format(f))
    except (TypeError, ValueError):
        return str(v)


def _gates_from_filters(filters):
    """{'gvm_score': {'min': 7.5}, 'market_cap': {'max': 100000}} -> 'gvm_score >= 7.5; market_cap <= 100000'."""
    out = []
    for k in sorted(filters or {}):
        v = filters[k]
        if isinstance(v, dict):
            parts = []
            if v.get("min") is not None:
                parts.append(k + " >= " + _fmt_num(v["min"]))
            if v.get("max") is not None:
                parts.append(k + " <= " + _fmt_num(v["max"]))
            out.append(" and ".join(parts) if parts else k + " = " + str(v))
        else:
            out.append(k + " = " + _fmt_num(v))
    return "; ".join(out) if out else _NOT_IN_CONFIG


def _qb_gates(stage2):
    """quant_basket_config.stage2_stock keys that are gates (…_min / …_max / …_min_exclusive) -> prose.
    Non-gate keys (exit, spec, selection, sizing …) are reported by name+value so nothing is hidden."""
    if not isinstance(stage2, dict):
        return _NOT_IN_CONFIG, _NOT_IN_CONFIG
    gates, other = [], []
    for k in sorted(stage2):
        v = stage2[k]
        if k in ("exit", "spec", "amended_by"):
            continue
        if k.endswith("_min_exclusive"):
            gates.append(k[:-len("_min_exclusive")] + " > " + _fmt_num(v))
        elif k.endswith("_max_exclusive"):
            gates.append(k[:-len("_max_exclusive")] + " < " + _fmt_num(v))
        elif k.endswith("_min"):
            gates.append(k[:-4] + " >= " + _fmt_num(v))
        elif k.endswith("_max"):
            gates.append(k[:-4] + " <= " + _fmt_num(v))
        else:
            other.append(k + " = " + (_fmt_num(v) if isinstance(v, (int, float)) else str(v)))
    entry = ("; ".join(gates) if gates else _NOT_IN_CONFIG) + ((" | " + "; ".join(other)) if other else "")
    exit_rule = stage2.get("exit") or _NOT_IN_CONFIG
    return entry, str(exit_rule)


def _v8_rule_text(filters):
    parts = []
    for f in filters:
        lo, hi = f.get("cond_min") or "", f.get("cond_max") or ""
        cond = (lo + " " + hi).strip() if (lo or hi) else ("(" + str(f.get("type")) + ")")
        parts.append(str(f.get("label") or f.get("key")) + " " + cond)
    return "; ".join(parts) if parts else _NOT_IN_CONFIG


def wall_engine_rules(cur):
    """The (i) sheet payload. One row per engine (per basket / screen / index where the config is
    per-unit) for every bucket currently enabled. Each block is guarded on its own so one engine
    whose source fails to import reports the failure in its row instead of blanking the sheet."""
    buckets, _ = wot_buckets_enabled(cur)
    epoch, epoch_missing = wot_equity_epoch(cur)
    rows = []

    def row(engine, sub, instrument, universe, entry, exit_rule, source):
        rows.append({"engine": engine, "sub": sub, "instrument": instrument, "universe": universe,
                     "entry": entry, "exit": exit_rule, "source": source})

    # ── FUTURES ────────────────────────────────────────────────────────────────────────────────
    if "v8" in buckets:
        try:
            cur.execute("SELECT COUNT(*) FROM futures_universe WHERE is_active")
            n_fut = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT value FROM app_config WHERE key='v8_retired_baskets'")
            _r = cur.fetchone()
            retired = set()
            try:
                retired = {str(x).strip().lower() for x in json.loads(_r[0])} if (_r and _r[0]) else set()
            except Exception:
                retired = set()
            cur.execute("SELECT DISTINCT basket FROM v8_paper_trades WHERE entry_ts >= '" + WALL_EPOCH + "'::timestamp "
                        "UNION SELECT DISTINCT basket FROM v8_paper_positions WHERE status = 'OPEN'")
            live_baskets = sorted({(r[0] or "") for r in cur.fetchall()} - retired - {""})
            from v8_signal_writer import BASKET_FILTERS as _BF   # the registry /api/v8/filter_config serves
            for b in live_baskets:
                row("V8", b, "FUTURES",
                    "futures_universe WHERE is_active (" + str(n_fut) + " symbols); 5-min live engine",
                    _v8_rule_text(_BF.get(b, [])) if b in _BF else _NOT_IN_CONFIG,
                    _NOT_IN_CONFIG + " -- v8_paper.py computes per signal: target = the signal's pivot level (R1 long / S1 short), stop = 1:1 mirror off the live entry",
                    "v8_signal_writer.BASKET_FILTERS + futures_universe + app_config.v8_retired_baskets")
        except Exception as e:
            row("V8", None, "FUTURES", _NOT_IN_CONFIG, _NOT_IN_CONFIG, _NOT_IN_CONFIG, "error reading config: " + str(e)[:160])

    if "tc_scanner" in buckets:
        try:
            from tc_scanner_config import TC_SCANNER_CONFIG as _TC
            cur.execute("SELECT COUNT(DISTINCT symbol), COUNT(DISTINCT symbol) FILTER (WHERE symbol IN "
                        "(SELECT symbol FROM futures_universe WHERE is_active)) FROM tc_scanner_holds")
            _n, _nf = cur.fetchone()
            th = _TC.get("score_thresholds") or {}
            gates = _TC.get("gates") or {}
            def _g(side):
                return "; ".join(str(g.get("label")) + " " + str(g.get("op")) + " " + _fmt_num(g.get("bound")) + str(g.get("unit") or "")
                                 for g in gates.get(side, []))
            # cc#1746 / 39467: the BOOK enters on the score bar alone; the gates are marks on the scanner page.
            entry = ("book enters on score100 >= " + ", ".join(k + " " + _fmt_num(v) for k, v in th.items())
                     + " (no gate, no cap on the book) | scanner-page marks only -- BUY gates: " + (_g("BUY") or _NOT_IN_CONFIG)
                     + " | SELL gates: " + (_g("SELL") or _NOT_IN_CONFIG))
            ex = _TC.get("exit") or {}
            exit_rule = ("target " + _fmt_num(ex.get("target_pct")) + " pct, stop " + _fmt_num(ex.get("stop_pct")) + " pct, time stop "
                         + str(ex.get("time_exit"))) if ex else _NOT_IN_CONFIG
            caps = _TC.get("caps") or {}
            row("TC Scanner", _TC.get("version"), "FUTURES",
                _NOT_IN_CONFIG + " -- book so far: " + str(_n) + " symbols, " + str(_nf) + " of them in the active futures universe"
                + ("; caps: " + str(caps.get("per_bucket_per_day")) + "/bucket/day, " + str(caps.get("book_total")) + " total" if caps else ""),
                entry, exit_rule, "tc_scanner_config.TC_SCANNER_CONFIG (" + str(_TC.get("source")) + ") + tc_scanner_holds")
        except Exception as e:
            row("TC Scanner", None, "FUTURES", _NOT_IN_CONFIG, _NOT_IN_CONFIG, _NOT_IN_CONFIG, "error reading config: " + str(e)[:160])

    if "index_intel" in buckets:
        try:
            from v10_st_ema import INDEX_CFG as _IX
            for name in sorted(_IX):
                c = _IX[name]
                row("Index Intel", name, "OPTIONS (wall shows the option leg only, 36703)",
                    name + " index, lot " + _fmt_num(c.get("lot")) + ", bars " + str(c.get("table")),
                    "SuperTrend " + _fmt_num(c.get("st_period")) + "/" + _fmt_num(c.get("st_mult")) + " on " + str(c.get("tf_main"))
                    + " + EMA " + _fmt_num(c.get("ema_fast")) + "/" + _fmt_num(c.get("ema_slow")) + " gate on " + str(c.get("tf_gate")),
                    "stop " + _fmt_num(c.get("sl_pts")) + " pts / target " + _fmt_num(c.get("tgt_pts")) + " pts (close-based)",
                    "v10_st_ema.INDEX_CFG")
        except Exception as e:
            row("Index Intel", None, "OPTIONS", _NOT_IN_CONFIG, _NOT_IN_CONFIG, _NOT_IN_CONFIG, "error reading config: " + str(e)[:160])

    # ── EQUITY ─────────────────────────────────────────────────────────────────────────────────
    if "qb_basket" in buckets:
        try:
            # the same quant-run set the wall shows: every basket with a config row that is NOT in
            # the discretionary list (the cc#1732 derivation, reused not re-stated)
            cur.execute("SELECT c.basket_name, c.cap_type, c.stage1_sector, c.stage2_stock, "
                        "       r.capital, r.max_stocks, r.rebalance_freq, r.weight_band, r.next_rebalance "
                        "FROM quant_basket_config c LEFT JOIN quant_basket_registry r ON r.basket_name = c.basket_name "
                        "WHERE c.basket_name NOT IN " + _QB_EXCLUDED_SQL + " ORDER BY c.basket_name")
            for bn, cap_type, s1, s2, capital, mx, freq, band, nxt in cur.fetchall():
                if isinstance(s1, str):
                    try: s1 = json.loads(s1)
                    except Exception: pass
                if isinstance(s2, str):
                    try: s2 = json.loads(s2)
                    except Exception: pass
                uni = (str(cap_type) if cap_type else "") + (" | " + "; ".join(k + " = " + str(v) for k, v in sorted(s1.items())) if isinstance(s1, dict) and s1 else "")
                entry, exit_rule = _qb_gates(s2)
                sizing = "capital Rs " + _fmt_num(capital) + ", max " + _fmt_num(mx) + " stocks, " + str(freq or _NOT_IN_CONFIG)                          + (", band " + str(band) if band else "") + (", next " + str(nxt) if nxt else "")
                row("QB Basket", bn, "EQUITY", (uni or _NOT_IN_CONFIG) + " | " + sizing, entry, exit_rule,
                    "quant_basket_config.stage1_sector/stage2_stock + quant_basket_registry")
        except Exception as e:
            row("QB Basket", None, "EQUITY", _NOT_IN_CONFIG, _NOT_IN_CONFIG, _NOT_IN_CONFIG, "error reading config: " + str(e)[:160])

    if "investment_scanner" in buckets:
        try:
            import inv_scanner_rules as _IR
            cur.execute("SELECT MAX(run_date) FROM investment_scanner_universe")
            _d = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*), string_agg(DISTINCT split_part(t, ':', 1), ', ') FROM investment_scanner_universe u, "
                        "unnest(u.tags) t WHERE u.run_date = (SELECT MAX(run_date) FROM investment_scanner_universe)")
            _n, _kinds = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM investment_scanner_universe WHERE run_date = (SELECT MAX(run_date) FROM investment_scanner_universe)")
            _nsym = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT string_agg(k, ', ') FROM (SELECT DISTINCT jsonb_object_keys(gates) k FROM investment_scanner_signals "
                        "WHERE gates IS NOT NULL AND event = 'BUY' ORDER BY 1) g")
            _gk = cur.fetchone()[0]
            row("Investment Scanner", "V1", "EQUITY",
                "investment_scanner_universe on " + str(_d) + ": " + str(_nsym) + " symbols, tag kinds " + str(_kinds),
                "mom_score > " + _fmt_num(_IR.ENTRY_MOM) + " or rev_score > " + _fmt_num(_IR.ENTRY_REV)
                + " | price gates logged per signal (" + str(_gk or _NOT_IN_CONFIG) + "); gate bounds " + _NOT_IN_CONFIG + " -- coded in inv_scanner_rules.run",
                "momentum track: mom_score < " + _fmt_num(_IR.EXIT_MOM) + "; reversal track: rev_score < " + _fmt_num(_IR.EXIT_REV)
                + "; dual-track exits only when both fail; no SL/target legs in V1",
                "inv_scanner_rules.ENTRY_MOM/ENTRY_REV/EXIT_MOM/EXIT_REV + investment_scanner_universe + investment_scanner_signals.gates")
        except Exception as e:
            row("Investment Scanner", None, "EQUITY", _NOT_IN_CONFIG, _NOT_IN_CONFIG, _NOT_IN_CONFIG, "error reading config: " + str(e)[:160])

    if "screeners" in buckets:
        try:
            cur.execute("SELECT id, name, COALESCE(scope, 'global'), filters, sort_key, sort_dir FROM v13_presets "
                        "WHERE jsonb_typeof(filters) = 'object' AND filters <> '{}'::jsonb ORDER BY id")
            for pid, name, scope, filters, sk, sd in cur.fetchall():
                if isinstance(filters, str):
                    try: filters = json.loads(filters)
                    except Exception: filters = {}
                row("Screeners", name, "EQUITY",
                    "scope " + str(scope) + " (GVM-scored universe, nightly v13 run)" + (", ranked by " + str(sk) + (" desc" if (sd or 0) < 0 else " asc") if sk else ""),
                    _gates_from_filters(filters),
                    "leaves the screen at the nightly v13 rerun (v13_screen_exits); no separate exit threshold in config",
                    "v13_presets(id=" + str(pid) + ").filters/scope/sort_key")
        except Exception as e:
            row("Screeners", None, "EQUITY", _NOT_IN_CONFIG, _NOT_IN_CONFIG, _NOT_IN_CONFIG, "error reading config: " + str(e)[:160])

    return {"wall_epoch": WALL_EPOCH, "equity_epoch": epoch,
            "equity_epoch_source": "default" if epoch_missing else "app_config",
            "buckets_enabled": buckets, "engines": rows,
            "not_in_config_marker": _NOT_IN_CONFIG,
            "note": "Every cell is read from the engine's own config source named in `source`; a cell reading '"
                    + _NOT_IN_CONFIG + "' has no machine-readable rule and the source names where the behaviour lives."}


@router.get("/api/tradewall/engine-rules")
@_json_safe
def tradewall_engine_rules(request: Request):
    """cc#1733: the (i) sheet -- universe / entry / exit rule per engine on the wall, read not typed."""
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        return wall_engine_rules(cur)


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
