"""
mobile_home2.py — cc#889 HOME v2 aggregate endpoint (MOBILE_REBUILD_IN_PLACE_V1, session_log 17782).

ONE call serves the whole home_v2 screen (previews/home_v2.html, founder-directed 07-Aug:
market-first scroll, My Portfolio demoted mid-scroll, tools grid). The template makes exactly one
fetch; every section below is one query (mobile_endpoints born-clean rule).

OWN FILE, OWN ROUTER — pushed by Claude (Fable) under CHARTER_OVERRIDE_08AUG2026 (session_log
17783). Helpers are IMPORTED from mobile_endpoints, never duplicated: rail_state, basket_label,
_conn/_rows/_ist_now/_guard/_json_safe are the one implementation both files share. No circular
import: mobile_endpoints never imports this module; the wiring shim (preview_endpoints.py) does.

FOUNDER COMMENTS 08-Aug (batch 2, this commit):
  * TICKER: NIFTY/BANKNIFTY + the Daily Digest global set move into a top ticker strip
    (payload key `ticker`); the old 2-tile `indices` grid is retired from the template but the
    key is kept for one deploy so a cached template never crashes.
  * V10 LINE inside the market-mood hero: "Nifty Long · Bank Nifty No Trade" — read from
    v10_positions OPEN FUT legs (the directional leg; OPT legs are hedges, never the state).
    No open FUT leg = "No Trade". Never derived from a stale signal.
  * V8 OPEN BOOK: unrealised split LONG/SHORT with capital deployed per side
    (deployed = entry_price * qty notional). pct = unrealised/deployed. A side with zero
    positions returns nulls — the template renders --, never a fabricated 0%.
  * PCR + VIX chip values are served HERE (hero.pcr / hero.vix) from pcr_daily and
    global_indices — the cc#894 frontend guessed response keys of other endpoints and
    rendered --. Server-side values end the guessing.
  * Live News: cc#899 — up to the LAST 100 stories (2 editorials pinned + 98 newest shorts).
  * NEW /api/mobile/trends?kind=adr|pcr|vix|nifty|banknifty — uniform {series:[{d,v}]} for the chip chart
    popups. adr_daily / pcr_daily(NIFTY) / global_indices(name='India VIX', quote_date is the
    daily history axis, 1,231 rows verified 08-Aug).

DATA DOCTRINE, inherited:
  * smartgain_holdings.updated_at is TIMESTAMPTZ -> converted in SQL (cc#887 class).
  * v8_qualified.signal_ts and intraday_prices.ts are NAIVE IST -> read raw, never converted.
  * market_mood() real keys: mood / fails / checks / checked_at / adr_detail (cc#888 finding) —
    hero chips are built from checks[] as returned, never from invented key names.
  * since-% on a signal uses v8_qualified.cmp (price AT signal) vs the live LATERAL close, with
    the sign flipped for sell_* baskets. Both prices real; nothing derived from a close alone.
  * A section whose source returns nothing states so (empty:true / message) — never a fake zero.
  * BOOK = WEB MASTER DASHBOARD FORMULA, exactly (founder caught the drift 08-Aug: app showed
    all-time 6.26L where web showed 3.49L). The web book is the FRESH ERA only —
    entry_ts >= app_config.v8_paper_rebuild_cutover_ts (cc#504 cutover, era doctrine cc#510),
    basket 's1_reclaim_obs' excluded — with REALISED shown NET of Rs.500/closed-trade brokerage
    and W/L counted as clean result='TARGET' vs result='SL' (gap/gate/conflict exits count in the
    money, not in W/L). Open/unrealised use the same era scope.
"""

import logging
from datetime import datetime, time as dt_time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mobile_endpoints import (
    rail_state, feed_rail, feed_auction_ok, basket_label,
    _conn, _rows, _ist_now, _guard, _json_safe, _page,
    MARKET_OPEN, SESSION_END, FEED_SOURCES, FEED_AUCTION_SOURCES,
)

log = logging.getLogger("scorr.mobile.home2")
router = APIRouter()

_SELL_PREFIX = "sell_"          # sign convention for since-%: a short gains when price falls
from v8_book_canon import book_canon   # cc#970: the ONE book formula (rule 13)
from price_sources import NOT_FUT_SQL   # cc#1056 / cc#1053 source registry — one list, never retyped
# cc#1123: the tape's colour band comes from the SAME function the Digest tile's does. Imported
# rather than reimplemented so the VIX thresholds live in exactly one place (card task 4).
from global_heatstrip import _band as _hs_band, INVERTED as _HS_INVERTED
# cc#1390: the SAME single-source BUILD_ID main.py's _MOBILE_HEAD already stamps onto every
# /m/* page as window.__SCORR_BUILD — imported here (not recomputed) so the client can compare
# "what this page was served as" against "what the server answering THIS live, no-store call is
# actually running" and self-heal if a layer this app does not control served a stale document.
from pwa_endpoints import BUILD_ID

BROKERAGE_PER_TRADE = 500       # web daylog doctrine: Rs.500 per closed trade

# Ticker order (founder 08-Aug batch 4): EXPLICIT Indian-investor relevance, not family order.
# NIFTY/BANKNIFTY lead (added first, live), then: India VIX (fear gauge) -> US overnight cue
# (Dow, Nasdaq, S&P) -> Asia same-session (Nikkei, Hang Seng) -> Europe (FTSE, DAX) -> currency
# pressure (USDINR, DXY) -> commodities (Gold, Silver, Brent, WTI, Nat Gas) -> Bitcoin last.
# A name not on this list still renders (after the listed ones) — new feed additions are never
# silently dropped from the tape.
# cc#904 (founder 08-Aug): names excluded from the tape. WTI goes because Brent already covers
# oil; the entry stays in _TICKER_NAME_ORDER below so the ordering list keeps matching the source
# table, and one set decides what ships.
_TICKER_SKIP = {"WTI"}

_TICKER_NAME_ORDER = ["India VIX", "Dow", "Nasdaq", "S&P 500", "Nikkei", "Hang Seng",
                      "FTSE", "DAX", "USDINR", "DXY", "Gold", "Silver", "Brent", "WTI",
                      "Natural Gas", "Bitcoin"]

# cc#910 — the tape row name and the intraday row name are NOT the same string. global_indices
# carries display names ("S&P 500", "Natural Gas", "Hang Seng"); global_intraday carries feed
# keys (SP500, NATURAL_GAS, HANGSENG). /api/global/intraday/{name} matches on UPPER(name), so
# those three would silently return an empty series — a 1D pill that draws nothing. Only the
# three that genuinely differ are listed; every other name matches on upper-case alone.
# cc#949 — HARD FLOOR for the 5-minute pan-back, founder-set 09-Aug. Below this date the
# *_5m_test_data time base changes (true UTC vs IST wall-clock tagged +00), and one intraday axis
# must never cross it. Enforced in the SQL WHERE clause, not in the client.
_V10_5M_FLOOR = "2026-07-01"

_INTRADAY_ALIAS = {"S&P 500": "SP500", "Natural Gas": "NATURAL_GAS", "Hang Seng": "HANGSENG"}


def _intraday_name(display_name, available):
    """Feed key for a tape name, or None when that name has no 5-min series.

    `available` is the live set of names in global_intraday, so this answers from data. India VIX
    resolves to None — it is genuinely not in the intraday feed (the same fact cc#908 states on
    the VIX chip), so its row gets no 1D pill rather than a fabricated intraday line."""
    key = _INTRADAY_ALIAS.get(display_name, (display_name or "").upper())
    return key if key in available else None


def _v10_state(cur):
    """Nifty/Bank Nifty V10 state from v10_positions OPEN FUT legs (the directional leg).
    OPT legs are hedges and never define the state. No open FUT leg = No Trade."""
    cur.execute("""
        SELECT symbol, side FROM v10_positions
        WHERE status = 'OPEN' AND leg = 'FUT'
    """)
    open_fut = {r["symbol"]: (r["side"] or "").upper() for r in _rows(cur)}
    def word(sym):
        s = open_fut.get(sym)
        if s == "BUY":
            return "Long"
        if s == "SELL":
            return "Short"
        return "No Trade"
    # cc#906: the symbol travels with the state so the template never has to map a display name
    # back to a feed symbol to open that index's chart.
    return [
        {"name": "Nifty", "symbol": "NIFTY50", "state": word("NIFTY50")},
        {"name": "Bank Nifty", "symbol": "BANKNIFTY", "state": word("BANKNIFTY")},
    ]


# cc#908 — LIVE-DATA-EVERYWHERE. India VIX has no intraday source: global_intraday carries 15
# names and India VIX is not one of them (data audit 08-Aug). So VIX 1D says so rather than
# drawing a shape it does not have. Never a fabricated intraday line.
_VIX_NO_INTRADAY = ("India VIX has no intraday feed — it is not one of the 15 names in "
                    "global_intraday. This is the daily close line.")


# ── cc#1231 · THE INTRADAY COVERAGE SET, NAMED ONCE ────────────────────────────────────────────
# This tuple was written inline in mobile_trends as `kind in ("adr", "pcr")` and it is the ONLY
# place the platform records which chip series have a 5-minute source. India VIX was not in it
# because there was no intraday India VIX anywhere — global_intraday carries 15 names and VIX is not
# one of them — and nifty/banknifty are daily close series by the same reasoning (cc#998).
#
# It is hoisted and EXPORTED because the client needs the same answer. The Home chip popout was
# offering a 1D tab for every kind, so tapping 1D on VIX drew a straight diagonal between two daily
# closes with an area fill under it — a full day of intraday movement that never happened, directly
# contradicting the honest caption printed beneath it. The fix is not a second list in JavaScript:
# the page reads THIS set, so there is exactly one definition and the chart cannot disagree with the
# caption again.
#
# cc#1398 · VIX ADDED, on a source cc#1231 never checked. cc#1231's own finding above is UNCHANGED
# and still true of global_intraday — VIX genuinely is not among its 15 names. What changed is a
# DIFFERENT table: intraday_prices carries a real 5-min INDIAVIX feed (symbol='INDIAVIX',
# timeframe='5m') that this session found and verified directly — 76 bars/session, full
# 09:15-15:30 coverage, checked across 10+ trading days with zero gaps. This is not "the old
# source finally got populated"; it is a second source cc#1231 had no reason to look for at the
# time. The constraint the comment above describes still holds for global_intraday; it no longer
# gates VIX's own 1D tab, because VIX's 1D tab reads intraday_prices now, not global_intraday.
#
# cc#1409 · NIFTY/BANKNIFTY ADDED, same shape of gap. cc#998/cc#1159's own comment above was
# right that raw_prices is DAILY-only — that constraint is unchanged and still governs 7D/15D/30D.
# What was never checked is intraday_prices, which carries real 5-min NIFTY50 and BANKNIFTY feeds
# (verified this session: 75-76 bars/session for NIFTY50, 09:15-15:30, no gaps in 5 sessions
# checked) — the SAME table already driving Card 4's live futures CMP. BANKNIFTY specifically also
# needed the NOT_FUT_SQL cash-leg filter (see the query itself) that VIX's own branch did not,
# because BANKNIFTY (unlike VIX or NIFTY50) stores its futures leg under this same symbol.
_INTRADAY_KINDS = ("adr", "pcr", "vix", "nifty", "banknifty")


@router.get("/api/mobile/trends")
@_json_safe
def mobile_trends(request: Request, kind: str = "adr", days: int = 30):
    """Uniform series for the Home chip chart popups. ONE shape, two resolutions.

    DAILY (days >= 2), unchanged:
      adr -> adr_daily.adr                  (price_date)
      pcr -> pcr_daily.pcr, NIFTY           (price_date)
      vix -> global_indices.price, name='India VIX' (quote_date IS the daily history axis)

    INTRADAY (days == 1) — cc#908, founder 08-Aug "1D should mean intraday":
      adr -> adr_intraday.adr,      universe_count >= 50   (the SAME gate /api/v8/adr_intraday
             and the mood-gate use — an ungated pre-open tick on a thin universe is not breadth)
      pcr -> pcr_intraday.pcr_total, underlying = 'NIFTY'  (the total-PCR series the web
             /api/pcr/intraday serves; ATM+-5 is a different question and stays on the web)
      vix -> intraday_prices.close, symbol='INDIAVIX', timeframe='5m' (cc#1398 — a real 5-min
             feed, verified this session: 76 bars/session, 09:15-15:30, no gaps in 10+ days.
             Different table from adr/pcr's own intraday sources, and different from
             global_indices' daily line above; the daily VIX view is unaffected.)

    SESSION ANCHOR: the intraday day is MAX(ts)::date of the source table, NOT CURRENT_DATE.
    Anchoring on today would render an empty chart every weekend and every holiday; rolling
    forward to the last session with data is the rule-9 corollary applied to a read path. The
    session actually shown is returned in `as_of` and stated in `note`, so a Monday-morning
    reader is never shown Friday's curve believing it is today's.

    adr_intraday.ts and pcr_intraday.ts are BOTH naive IST — read raw, never converted
    (the cc#844 phantom-330-minute class).

    Returns {kind, series:[{d,v}], latest, intraday, as_of, note} oldest-first, where `d` is
    'YYYY-MM-DD' on a daily series and 'HH:MM' on an intraday one. Empty source returns
    series:[] — the popup states 'no data', never draws a fake line."""
    g = _guard(request)
    if g:
        return g
    kind = (kind or "adr").lower()
    if kind not in ("adr", "pcr", "vix", "nifty", "banknifty"):
        kind = "adr"
    want = max(1, min(int(days or 30), 120))
    # cc#998: nifty is a DAILY-only line — the Home Nifty Day/Week/Month chips are three deltas of the
    # SAME NIFTY50 close series, so there is no per-chip intraday variant (unlike adr/pcr's 1D view).
    intraday = (want == 1 and kind in _INTRADAY_KINDS)
    note = None

    with _conn() as conn, conn.cursor() as cur:
        if intraday and kind == "adr":
            cur.execute("""
                SELECT to_char(ts, 'HH24:MI') AS d, adr AS v, ts::date AS sd
                FROM adr_intraday
                WHERE adr IS NOT NULL AND universe_count >= 50
                  AND ts::date = (SELECT MAX(ts)::date FROM adr_intraday
                                  WHERE adr IS NOT NULL AND universe_count >= 50)
                ORDER BY ts ASC
            """)
            rows = _rows(cur)
        elif intraday and kind == "vix":
            # cc#1398: intraday_prices, NOT global_intraday — a different table from the one
            # cc#1231 checked (see the _INTRADAY_KINDS comment above). symbol/timeframe verified
            # this session: 'INDIAVIX'/'5m' is real, 76 bars/session, 09:15-15:30, no gaps.
            cur.execute("""
                SELECT to_char(ts, 'HH24:MI') AS d, close AS v, ts::date AS sd
                FROM intraday_prices
                WHERE symbol = 'INDIAVIX' AND timeframe = '5m' AND close IS NOT NULL
                  AND ts::date = (SELECT MAX(ts)::date FROM intraday_prices
                                  WHERE symbol = 'INDIAVIX' AND timeframe = '5m' AND close IS NOT NULL)
                ORDER BY ts ASC
            """)
            rows = _rows(cur)
        elif intraday and kind in ("nifty", "banknifty"):
            # cc#1409: same real 5-min intraday_prices feed that already drives Card 4's live
            # futures CMP — same class of fix as cc#1398 (VIX): a wiring gap, not a data gap.
            #
            # NOT_FUT_SQL IS NOT OPTIONAL HERE, and this is the whole reason this is its own
            # branch rather than a copy of VIX's. Checked before writing this query, not assumed:
            # BANKNIFTY writes BOTH its cash leg (fyers_eq, 72-73 bars/session) AND its futures
            # leg (fyers_fut/fyers_fut_rest, 77-78 bars/session) under the SAME symbol in
            # intraday_prices — price_sources.py's own documented rule ("Bank Nifty stores both
            # legs under BANKNIFTY, like every stock"). Without this filter the query would
            # silently interleave two different instruments' closes into one line — exactly the
            # bug domestic_live() (Card 1's own NIFTY/BankNifty tile, v8_endpoints.py) already had
            # to fix once for this exact table (cc#1053). NIFTY50 has no futures rows under this
            # symbol (its futures leg sits under the separate symbol 'NIFTY' — price_sources.py's
            # own documented exception), so the filter is a no-op for it, not a behaviour change —
            # applied to both anyway so one query cannot silently regress if that ever changes.
            sym = {"nifty": "NIFTY50", "banknifty": "BANKNIFTY"}[kind]
            cur.execute("""
                SELECT to_char(ts, 'HH24:MI') AS d, close AS v, ts::date AS sd
                FROM intraday_prices
                WHERE symbol = %s AND timeframe = '5m' AND close IS NOT NULL AND """ + NOT_FUT_SQL + """
                  AND ts::date = (SELECT MAX(ts)::date FROM intraday_prices
                                  WHERE symbol = %s AND timeframe = '5m' AND close IS NOT NULL
                                    AND """ + NOT_FUT_SQL + """)
                ORDER BY ts ASC
            """, (sym, sym))
            rows = _rows(cur)
        elif intraday:
            cur.execute("""
                SELECT to_char(ts, 'HH24:MI') AS d, pcr_total AS v, ts::date AS sd
                FROM pcr_intraday
                WHERE underlying = 'NIFTY' AND pcr_total IS NOT NULL
                  AND ts::date = (SELECT MAX(ts)::date FROM pcr_intraday
                                  WHERE underlying = 'NIFTY' AND pcr_total IS NOT NULL)
                ORDER BY ts ASC
            """)
            rows = _rows(cur)
        else:
            # a one-point line is a dot, so a daily window never asks for fewer than 2 prints
            dd = max(2, want)
            if kind == "pcr":
                cur.execute("""
                    SELECT price_date AS d, pcr AS v FROM pcr_daily
                    WHERE underlying = 'NIFTY' AND pcr IS NOT NULL
                    ORDER BY price_date DESC LIMIT %s
                """, (dd,))
            elif kind == "vix":
                cur.execute("""
                    SELECT quote_date AS d, price AS v FROM global_indices
                    WHERE name = 'India VIX' AND price IS NOT NULL
                    ORDER BY quote_date DESC LIMIT %s
                """, (dd,))
            elif kind in ("nifty", "banknifty"):
                # cc#998: NIFTY50 daily close series (raw_prices, price_date-keyed, same EOD source the
                # digest reads). One line; the three Home chips (Day/Week/Month) all open it.
                # cc#1159 adds banknifty off the SAME table and the SAME shape, so the Home footer
                # index cells open a chart through the existing opener rather than a second one.
                # Verified before wiring: raw_prices holds 1,296 BANKNIFTY closes against NIFTY50's
                # 1,297, both 2021-05-24 to 2026-08-19 — the series exists and is as deep.
                # The symbol comes from a LOOKUP, never from string-building on the query param,
                # so the parameter cannot reach the SQL text (the _V10_TABLES rule, applied here).
                cur.execute("""
                    SELECT price_date AS d, close AS v FROM raw_prices
                    WHERE symbol = %s AND close IS NOT NULL
                    ORDER BY price_date DESC LIMIT %s
                """, ({"nifty": "NIFTY50", "banknifty": "BANKNIFTY"}[kind], dd))
            else:
                cur.execute("""
                    SELECT price_date AS d, adr AS v FROM adr_daily
                    WHERE adr IS NOT NULL
                    ORDER BY price_date DESC LIMIT %s
                """, (dd,))
            rows = list(reversed(_rows(cur)))
            # cc#1398: `if want == 1 and kind == "vix": note = _VIX_NO_INTRADAY` REMOVED — it is
            # unreachable now. `intraday` is True whenever want==1 and kind=='vix' (vix is in
            # _INTRADAY_KINDS above), so that combination takes the elif branch above and never
            # reaches this else at all. _VIX_NO_INTRADAY itself and its own comment are left in
            # place, untouched, as the historical record (do-not-touch) — only this now-dead call
            # site is removed.

    series = [{"d": str(r["d"]), "v": float(r["v"])} for r in rows if r["v"] is not None]
    as_of = None
    if intraday and rows:
        as_of = str(rows[-1]["sd"])
        note = "%s intraday · 5-min ticks" % as_of
    elif series:
        as_of = series[-1]["d"]
    return {"kind": kind, "series": series, "intraday": intraday,
            # cc#1231: whether this kind HAS a 5-min source at all, as distinct from `intraday`
            # above which says whether THIS request returned one. A surface needs the first to
            # decide if a 1D tab should exist; the second only tells it what it just got.
            "has_intraday": kind in _INTRADAY_KINDS,
            "as_of": as_of, "note": note,
            "latest": series[-1]["v"] if series else None}


# ── cc#1410 · ADVANCE/DECLINE FULL-SCREEN TABLE, behind Card 1's adrBar() ──────────────────────
@router.get("/api/mobile/breadth")
@_json_safe
def mobile_breadth(request: Request):
    """Per-symbol day-change + sector breakdown behind Card 1's ADVANCE/DECLINE bar (adrBar()).

    SAME UNIVERSE AS THE CARD'S OWN 85/123 COUNT, RECONCILED NOT ASSUMED. adrBar()'s advances/
    declines is market_mood()'s adr_detail, which reads the LATEST adr_intraday row
    (v8_endpoints._read_adr — WHERE universe_count >= 50 ORDER BY ts DESC LIMIT 1). That row was
    written by _write_adr_intraday (v8_signal_writer.py) from a point-in-time JOIN between
    intraday_prices (today's last CASH tick per symbol, futures/auction excluded) and raw_prices
    (each symbol's own prior close) — a query that has NO reference to futures_universe at all.

    Checked directly against the live DB before writing this (not assumed): futures_universe.
    is_active also happens to hold 208 rows today, the SAME count as the latest adr_intraday row
    — but the two SETS are not identical. NIFTY500 (a broad index, not an individual F&O stock)
    is IN the adr_intraday universe (it has both an intraday tick and a raw_prices row) and is
    NOT in futures_universe. NIFTY (the futures root symbol) is the reverse — it IS in
    futures_universe but has no raw_prices row of its own (its cash leg is filed under NIFTY50,
    price_sources.py's own documented Nifty exception), so the adr_intraday join never picks it
    up. The counts match today by coincidence, not because the sets agree.

    RESOLUTION, per this task's own instruction to reconcile rather than ship two numbers that
    can disagree: the adr_intraday/(intraday_prices JOIN raw_prices) universe is AUTHORITATIVE
    for this table's rows and its own advances/declines/total, because that is the number already
    on screen. Anchored to the SAME adr_intraday row market_mood() is currently serving (not a
    fresh independent computation that could tick over mid-request and disagree with the card by
    a few seconds) — re-running the identical li/pc join bounded to that row's own `ts` reproduces
    that exact snapshot, verified this session to return the identical 85/123/0/208.
    futures_universe is then LEFT JOINed in ONLY to attach the theme (sector) label — a symbol
    with no match (NIFTY500) gets 'Unclassified' rather than being dropped, which would break the
    count-match guarantee above.

    SECTOR AGGREGATE: a SIMPLE AVERAGE of the theme's member symbols' own day_chg_pct — a new
    derived figure, so stated explicitly per this task's own instruction. Matches this codebase's
    own precedent for a per-sector average (gvm_market_endpoints.get_sectors() /api/sectors
    already exposes sector_ratings.simple_avg_gvm alongside its cap-weighted figure). NOT
    cap-weighted: that would need a verified per-symbol market-cap join this task's own scope
    does not ask for and this session has not checked for this specific use — not used
    speculatively."""
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT ts FROM adr_intraday WHERE universe_count >= 50 ORDER BY ts DESC LIMIT 1")
        anchor = cur.fetchone()
        if not anchor:
            return {"rows": [], "advances": None, "declines": None, "unchanged": None,
                    "as_of": None, "note": "breadth pending"}
        anchor_ts = anchor[0]
        cur.execute("""
            WITH li AS (
                SELECT DISTINCT ON (symbol) symbol, close AS cmp
                FROM intraday_prices WHERE ts::date = %(d)s AND ts <= %(cut)s
                  AND COALESCE(source,'') NOT IN ('fyers_eq_auction','auction')
                  AND COALESCE(source,'') NOT IN ('fyers_fut', 'fyers_fut_rest')
                ORDER BY symbol, ts DESC
            ),
            pc AS (
                SELECT DISTINCT ON (symbol) symbol, close AS pclose
                FROM raw_prices WHERE price_date < %(d)s
                ORDER BY symbol, price_date DESC
            ),
            base AS (
                SELECT li.symbol,
                       ROUND(((li.cmp - pc.pclose) / NULLIF(pc.pclose, 0) * 100)::numeric, 2) AS day_chg_pct
                FROM li JOIN pc ON pc.symbol = li.symbol
            ),
            themed AS (
                SELECT b.symbol, b.day_chg_pct, COALESCE(fu.theme, 'Unclassified') AS theme
                FROM base b LEFT JOIN futures_universe fu ON fu.symbol = b.symbol
            )
            SELECT symbol, day_chg_pct, theme,
                   ROUND(AVG(day_chg_pct) OVER (PARTITION BY theme)::numeric, 2) AS sector_day_chg_pct
            FROM themed ORDER BY symbol
        """, {"d": anchor_ts.date(), "cut": anchor_ts})
        rows = _rows(cur)
    advances = sum(1 for r in rows if r["day_chg_pct"] is not None and r["day_chg_pct"] > 0)
    declines = sum(1 for r in rows if r["day_chg_pct"] is not None and r["day_chg_pct"] < 0)
    unchanged = sum(1 for r in rows if r["day_chg_pct"] is not None and r["day_chg_pct"] == 0)
    return {
        "rows": [{"symbol": r["symbol"],
                  "day_chg_pct": float(r["day_chg_pct"]) if r["day_chg_pct"] is not None else None,
                  "theme": r["theme"],
                  "sector_day_chg_pct": float(r["sector_day_chg_pct"]) if r["sector_day_chg_pct"] is not None else None}
                 for r in rows],
        "advances": advances, "declines": declines, "unchanged": unchanged,
        "as_of": str(anchor_ts),
    }


# ── cc#906: V10 trade-log chart ───────────────────────────────────────────────────────────────
# Table chosen by LOOKUP, never by string-building from the query param — a user-supplied symbol
# never reaches the SQL text.
_V10_TABLES = {"NIFTY50": "nifty_5m_test_data", "BANKNIFTY": "banknifty_5m_test_data"}
_V10_ALIAS = {"NIFTY": "NIFTY50", "NIFTY 50": "NIFTY50", "BANKNIFTY50": "BANKNIFTY",
              "NIFTYBANK": "BANKNIFTY", "BNF": "BANKNIFTY"}


@router.get("/api/mobile/v10chart")
@_json_safe
def mobile_v10chart(request: Request, symbol: str = "NIFTY50", days: int = 92, bars: int = 90,
                    before: str = ""):
    """cc#906 — 3 months of index price with the V10 paper trades pinned on it.

    WHY DAILY AND NOT THE WEB'S 5-MIN (the finding that decided this build, verified in the DB
    before a line of chart code was written):

      1. TIME BASE. /api/v10/candles emits the raw epoch of *_5m_test_data.ts and the web chart
         applies no shift, which is correct for ITS 30-day window. Over 3 months it is not: the
         bar time base CHANGED inside this window. Bars up to Jun-2026 are true UTC (a session
         runs 03:45 -> 09:55); from Jul-2026 they are IST wall-clock tagged +00 (09:15 -> 15:25).
         A single 90-day epoch axis would therefore shift the older third of the window by 5.5h
         against the newer two thirds. Aggregating to the CALENDAR DATE is invariant to that
         switch — both regimes put a session's bars on the session's own date — and no date in
         the window mixes the two regimes (checked: zero dates carry both). So a daily series is
         not merely lighter here, it is the only one that is correct across the whole window.
      2. PAYLOAD AND LEGIBILITY. 3 months of 5-min bars is ~4,600 candles. On a ~360px phone
         that is more than 12 bars per pixel — unreadable, and a ~300KB payload on mobile data.
         64 daily candles read cleanly at ~5px each.

    PINS. v10_trades / v10_positions timestamps are genuine TIMESTAMPTZ, so they are converted
    with AT TIME ZONE 'Asia/Kolkata' IN SQL (never in Python — cc#844 class) and matched to a
    session by date. FUT leg only: the FUT leg is the directional trade, OPT legs are hedges and
    would double-count every pin (the same rule _v10_state already applies to the hero line).
    Proof the conversion lands right — for the 3 most recent NIFTY FUT trades the entry price
    falls inside its entry-day bar range: 24614.9 in [24431.4, 24645.3], 24530.8 in [24522.3,
    24774.3], 24187.0 in [24149.6, 24253.2]. A wrong conversion would move a trade to the
    neighbouring session and break that containment.

    A trade older than the window keeps its place in `trades` and is COUNTED in `outside_window`;
    only its PIN is withheld, never clamped onto the first candle (which would put a marker on a
    price that never happened). cc#973 corrected this: the row used to be dropped from `trades`
    altogether, which quietly turned the OPTIONS tab — the only consumer built from `trades` —
    into a view of the chart window instead of the log.

    TARGET/SL: v10_trades has no stop/target columns, so a CLOSED trade carries its exit_reason
    ('TARGET' / 'SL') — which is that information — and never an invented level. OPEN positions
    do carry stop/target, so those levels ship for them. Read-only; no writes anywhere."""
    g = _guard(request)
    if g:
        return g
    sym = (symbol or "NIFTY50").upper().strip()
    sym = _V10_ALIAS.get(sym, sym)
    if sym not in _V10_TABLES:
        sym = "NIFTY50"
    table = _V10_TABLES[sym]
    days = max(5, min(int(days or 92), 400))
    # cc#941 task 5, founder 09-Aug: the 3-month daily window was "clumsy". The chart is now a
    # 90-BAR ROLLING 5-MIN window — roughly all of the latest session plus the tail of the one
    # before, which is the horizon a 5-min intraday strategy is actually judged on.
    #
    # THE cc#906 TIME-BASE OBJECTION DOES NOT APPLY AT THIS LENGTH, and that is why this is safe
    # rather than a reversal. cc#906 refused 5-min bars because the bar time base CHANGED inside
    # its 3-month window (true UTC up to Jun-2026, IST-tagged-+00 from Jul-2026), so one epoch
    # axis would have shifted the older third by 5.5h. 90 bars is ~1.2 sessions and sits entirely
    # inside the post-Jul regime, so there is exactly one time base in the window. The daily
    # aggregation is KEPT and still served on the same endpoint for any longer window.
    # Full history stays in the DB; only the response is windowed (the cc#940 principle).
    # cc#949 — DAILY MODE WAS UNREACHABLE, a defect I shipped in cc#941 and found here because
    # task 3's floor handoff depends on it. The clamp read `max(20, min(int(bars or 90), 400))`
    # and `intraday = bars > 0`, so `bars` could never be 0 and `intraday` was ALWAYS true: the
    # daily aggregation cc#941's own comment promised was "kept for longer windows" could not be
    # reached by any request. bars=0 is now an explicit sentinel for daily, resolved BEFORE the
    # `or 90` default that was swallowing it.
    _want_daily = (str(bars).strip() == "0")
    intraday = not _want_daily
    bars = max(20, min(int(bars or 90), 400))

    # ── cc#949 — PAN-BACK PAGING, and the floor that makes it safe ────────────────────────────
    # `before` is a cursor, not an offset: the page is the `bars` newest rows STRICTLY OLDER than
    # it. A cursor cannot drift when rows are appended between requests, which an OFFSET would.
    #
    # THE FLOOR IS THE WHOLE REASON THIS IS SAFE, so it is enforced in SQL and not left to the
    # client. *_5m_test_data carries TWO TIME BASES: bars written as true UTC (a session runs
    # 03:45-09:55) and bars written as IST wall-clock tagged +00 (09:15-15:25). Splicing them onto
    # one continuous 5-minute axis shifts half the window by 5h30m — the cc#906 finding, and the
    # reason cc#941 kept the long view on DAILY aggregation, which is invariant to the switch.
    #
    # MEASURED 09-Aug rather than taken on trust, because the card states the boundary as "pre-Jul
    # is UTC" and the data is untidier than that. Per trading day, first bar before 08:00 = UTC
    # regime: 246 UTC days spanning 2025-06-06..2026-06-05, and 45 IST days spanning
    # 2025-10-21..2026-08-07. The regimes are NOT cleanly split by July — but exactly ONE IST day
    # (2025-10-21) sits inside the UTC era, so the real switch is 2026-06-08 and everything from
    # there on is IST. The founder-set floor of 2026-07-01 is therefore comfortably inside the
    # single-regime zone; it is honoured as written and gives 1,288 bars = 15 pages per index.
    # Lowering it to 2026-06-08 would add ~16 sessions and is a founder call, not mine.
    floor_ts = _V10_5M_FLOOR
    cursor = (before or "").strip() or None
    older = 0                      # bars behind this page, above the floor; 0 => at the floor

    with _conn() as conn, conn.cursor() as cur:
        if intraday:
            # Never serve a 5-min page that reaches below the floor: the clamp lives in the WHERE
            # clause, so no client mistake and no crafted cursor can produce a mixed-regime axis.
            # Last N bars, anchored on MAX(ts): these tables are fed historically, so anchoring on
            # NOW() would empty the chart the moment the feed is a day behind — and on a weekend
            # it would empty it every time (the cc#940 defect, not repeated here).
            cur.execute(
                """
                SELECT ts, open AS o, high AS h, low AS l, close AS c FROM (
                  SELECT DISTINCT ON (ts) ts, open, high, low, close FROM {t}
                  WHERE ts >= %(floor)s
                    AND (%(cur)s::timestamptz IS NULL OR ts < %(cur)s::timestamptz)
                  ORDER BY ts DESC, close
                  LIMIT %(n)s
                ) x ORDER BY ts ASC
                """.format(t=table), {"floor": floor_ts, "cur": cursor, "n": bars})
            rows5 = _rows(cur)
            # has_more asks the DB, it does not infer from a full page — a page can be exactly
            # `bars` long with nothing behind it, and inferring would show a pan-back that
            # then dead-ends.
            if rows5:
                cur.execute(
                    "SELECT count(*) AS n FROM (SELECT DISTINCT ts FROM {t} "
                    "WHERE ts >= %s AND ts < %s) z".format(t=table),
                    (floor_ts, rows5[0]["ts"]))
                older = int((_rows(cur)[0] or {}).get("n") or 0)
            series = [{"d": r["ts"].strftime("%Y-%m-%d %H:%M"),
                       "t": r["ts"].strftime("%H:%M"), "day": r["ts"].strftime("%Y-%m-%d"),
                       "o": float(r["o"]), "h": float(r["h"]),
                       "l": float(r["l"]), "c": float(r["c"])} for r in rows5]
            # Pins snap to a SESSION here, not to a bar minute: the *_5m_test_data time base and
            # v10_trades are two different clocks (see /api/v10/candles), and pretending to a
            # one-minute alignment we have not proven would put a marker on the wrong candle.
            # Date containment is the claim the data supports, so the date is what we match on.
            in_win = set(p["day"] for p in series)
        else:
            # daily OHLC, built from the same 5-min bars the web chart uses (one source of truth).
            cur.execute(
                """
                SELECT ts::date AS d,
                       (array_agg(open  ORDER BY ts ASC ))[1] AS o,
                       MAX(high) AS h,
                       MIN(low)  AS l,
                       (array_agg(close ORDER BY ts DESC))[1] AS c
                FROM {t}
                WHERE ts::date > (SELECT MAX(ts)::date FROM {t}) - %s
                GROUP BY 1 ORDER BY 1
                """.format(t=table), (days,))
            series = [{"d": str(r["d"]), "day": str(r["d"]), "o": float(r["o"]),
                       "h": float(r["h"]), "l": float(r["l"]), "c": float(r["c"])}
                      for r in _rows(cur)]
            in_win = set(p["day"] for p in series)

        cur.execute("""
            SELECT id, side, leg, opt_strike, opt_type,
                   entry_price, exit_price, exit_reason, pnl, points,
                   (entry_ts AT TIME ZONE 'Asia/Kolkata')::date          AS ed,
                   to_char(entry_ts AT TIME ZONE 'Asia/Kolkata','HH24:MI') AS et,
                   (exit_ts  AT TIME ZONE 'Asia/Kolkata')::date          AS xd,
                   to_char(exit_ts  AT TIME ZONE 'Asia/Kolkata','HH24:MI') AS xt
            FROM v10_trades
            WHERE symbol = %s AND entry_ts IS NOT NULL
            ORDER BY entry_ts ASC
        """, (sym,))
        closed_rows = _rows(cur)

        cur.execute("""
            SELECT id, side, leg, opt_strike, opt_type, opt_expiry, entry_price, stop, target,
                   lot_size,
                   (entry_ts AT TIME ZONE 'Asia/Kolkata')::date          AS ed,
                   to_char(entry_ts AT TIME ZONE 'Asia/Kolkata','HH24:MI') AS et
            FROM v10_positions
            WHERE status = 'OPEN' AND symbol = %s
            ORDER BY entry_ts ASC
        """, (sym,))
        open_rows = _rows(cur)

        # cc#1386 — OPT leg's live premium and both legs' lot_size, for the Home hero deck's
        # merged Index Positions card (FUT range bar + OPT decay bar need real rupee P&L, which
        # needs a real lot size; the decay bar also needs a CURRENT premium, which nothing in this
        # payload carried before this task). Two real gaps, both confirmed by direct DB read this
        # session, not assumed:
        #   lot_size — already a column on v10_positions, just never selected above. No new query.
        #   live_premium (OPT only) — NOT exposed anywhere on the frontend before this. Real
        #   source: option_chain(underlying, strike, option_type, expiry, ltp, ts), refreshed
        #   every 5 minutes. CC's own call on where this lands: extended here, on the SAME
        #   open-trade dict this endpoint already builds, rather than a new endpoint — one extra
        #   per-row lookup on an endpoint the card already fetches, versus a second round trip for
        #   one field. NAMING MISMATCH, confirmed the same session: v10_positions.symbol=
        #   'NIFTY50' but option_chain.underlying='NIFTY' — BANKNIFTY matches as-is in both. Same
        #   swap this file's own ticker builder already makes (~line 845), not a new convention.
        #   Looked up per open OPT row (normally 0 or 1 per symbol) rather than a bulk join.
        opt_underlying = "NIFTY" if sym == "NIFTY50" else sym
        live_premium = {}
        for r in open_rows:
            if (r["leg"] or "").upper() != "OPT" or r["opt_strike"] is None or not r["opt_type"] \
               or not r["opt_expiry"]:
                continue
            cur.execute("""
                SELECT ltp FROM option_chain
                WHERE underlying = %s AND strike = %s AND option_type = %s AND expiry = %s
                ORDER BY ts DESC LIMIT 1
            """, (opt_underlying, r["opt_strike"], r["opt_type"], r["opt_expiry"]))
            lp = _rows(cur)
            live_premium[r["id"]] = float(lp[0]["ltp"]) if lp and lp[0]["ltp"] is not None else None

    def f(x):
        return None if x is None else float(x)

    def _paired(sym_):
        """cc#941: imported, never re-derived. If the shared helper is unavailable the chart still
        renders — the cards simply say so — rather than the whole payload failing."""
        try:
            from v10_endpoints import paired_trades
            return paired_trades(sym_, 200)
        except Exception as e:
            return {"error": "%s: %s" % (type(e).__name__, str(e)[:120]),
                    "trades": [], "count": 0, "summary": {}}

    trades, pins, outside = [], [], 0
    for r in closed_rows:
        ed = str(r["ed"]) if r["ed"] else None
        xd = str(r["xd"]) if r["xd"] else None
        # cc#973: COUNT a trade outside the chart window, but do NOT DROP it. This `continue` used
        # to skip the row entirely, so `trades` was the CHART WINDOW rather than the log — and the
        # OPTIONS tab, which is the only consumer built from `trades`, showed 3 of NIFTY50's 18
        # option legs. FUTURES escaped the bug purely because it reads d.paired instead, which is
        # a separate unwindowed query: two sources for two tabs of one log, and only one of them
        # was windowed. The pin appends below already test `in_win` individually, so the window
        # still governs what is PLOTTED — it just no longer governs what is LISTED.
        if ed not in in_win and xd not in in_win:
            outside += 1
        pnl = f(r["pnl"])
        win = (pnl is not None and pnl >= 0)
        side = (r["side"] or "").upper()
        leg = (r["leg"] or "FUT").upper()
        t = {"id": r["id"], "side": side, "leg": leg,
             "opt_strike": f(r["opt_strike"]), "opt_type": r["opt_type"],
             "entry_d": ed, "entry_t": r["et"],
             "entry_price": f(r["entry_price"]), "exit_d": xd, "exit_t": r["xt"],
             "exit_price": f(r["exit_price"]), "reason": r["exit_reason"] or "EXIT",
             "pnl": pnl, "points": f(r["points"]), "win": win, "open": False}
        trades.append(t)
        # cc#928: PINS STAY FUT-ONLY, deliberately. An option leg's price is a PREMIUM (220), not
        # an index level (24,500) — plotting it on the spot chart would put a marker at a price the
        # index never traded. Options legs live in the log, where a premium is the right number.
        if leg != "FUT":
            continue
        if ed in in_win:
            pins.append({"d": ed, "id": r["id"], "kind": "entry", "side": side,
                         "price": f(r["entry_price"]), "t": r["et"], "win": win})
        if xd in in_win:
            pins.append({"d": xd, "id": r["id"], "kind": "exit", "side": side,
                         "price": f(r["exit_price"]), "t": r["xt"], "win": win,
                         "reason": r["exit_reason"] or "EXIT", "pnl": pnl})

    for r in open_rows:
        ed = str(r["ed"]) if r["ed"] else None
        side = (r["side"] or "").upper()
        leg = (r["leg"] or "FUT").upper()
        t = {"id": r["id"], "side": side, "leg": leg,
             "opt_strike": f(r["opt_strike"]), "opt_type": r["opt_type"],
             "entry_d": ed, "entry_t": r["et"],
             "entry_price": f(r["entry_price"]), "exit_d": None, "exit_t": None,
             "exit_price": None, "reason": "OPEN", "pnl": None, "points": None,
             "win": None, "open": True, "stop": f(r["stop"]), "target": f(r["target"]),
             # cc#1386: lot_size on BOTH legs (real rupee P&L needs it either way); live_premium
             # is OPT-only by construction (live_premium only ever has entries for OPT ids, see
             # the lookup above) and is None for a FUT row or an OPT row option_chain has no
             # matching snapshot for yet — absence over a guess, same rule as everywhere else.
             "lot_size": r["lot_size"], "live_premium": live_premium.get(r["id"])}
        trades.append(t)
        if leg != "FUT":
            continue
        if ed in in_win:
            pins.append({"d": ed, "id": r["id"], "kind": "open", "side": side,
                         "price": f(r["entry_price"]), "t": r["et"], "win": None})
        elif ed:
            outside += 1

    # per-leg totals so each log section can state its own real numbers
    by_leg = {}
    for t in trades:
        b = by_leg.setdefault(t["leg"], {"trades": 0, "open": 0, "net_pnl": 0.0})
        if t["open"]:
            b["open"] += 1
        else:
            b["trades"] += 1
            if t["pnl"] is not None:
                b["net_pnl"] += t["pnl"]
    for b in by_leg.values():
        b["net_pnl"] = round(b["net_pnl"], 2)

    closed = [t for t in trades if not t["open"] and t["pnl"] is not None]
    net = round(sum(t["pnl"] for t in closed), 2) if closed else None
    return {
        "symbol": sym, "label": "BANK NIFTY" if sym == "BANKNIFTY" else "NIFTY 50",
        "table": table, "resolution": "5m" if intraday else "daily",
        "bars": bars if intraday else None,
        # cc#949 paging contract. `oldest_ts` is the cursor to send back as `before` for the next
        # page; `has_more` is false at the floor and the client then shows the daily handoff
        # instead of a dead pan. Both are None in daily mode, which does not page.
        "oldest_ts": (series[0]["d"] if (intraday and series) else None),
        "has_more": (older > 0) if intraday else None,
        "floor_ts": _V10_5M_FLOOR if intraday else None,
        "floor_note": ("5-min history stops at %s — older bars were written on a different time "
                       "base (UTC vs IST-tagged) and splicing them onto one intraday axis would "
                       "shift them 5h30m. The daily view carries the full 14 months safely."
                       % _V10_5M_FLOOR) if intraday else None,
        "series": series, "count": len(series),
        "from": series[0]["d"] if series else None,
        "to": series[-1]["d"] if series else None,
        # cc#941: the PAIRED log ships on the same payload, from the shared v10_endpoints helper —
        # one derivation for the mobile cards and the desktop table (DISPLAY_PARITY 16202). The
        # per-leg `trades` array stays for the pins, which are FUT-only by construction.
        "paired": _paired(sym),
        "trades": trades, "pins": pins, "by_leg": by_leg,
        "outside_window": outside,
        "stats": {"closed": len(closed), "open": len(open_rows),
                  "wins": sum(1 for t in closed if t["win"]),
                  "losses": sum(1 for t in closed if not t["win"]),
                  "net_pnl": net},
        "note": (("Last %d five-minute bars from %s; V10 FUT-leg trades pinned to their IST "
                  "session." % (len(series), table)) if intraday else
                 ("Daily candles built from %s 5-min bars; V10 FUT-leg trades pinned to their "
                  "IST session." % table)),
    }


@router.get("/api/mobile/home2")
@_json_safe
def mobile_home2(request: Request):
    g = _guard(request)
    if g:
        return g

    # ── indices + mood: reuse the web implementations (DISPLAY_PARITY 16202) ────────────────
    idx = {}
    try:
        from v8_endpoints import domestic_live
        idx = domestic_live() or {}
    except Exception as e:
        log.warning("home2: domestic_live unavailable (%s)", e)
    mood = None
    try:
        from v8_endpoints import market_mood
        mood = market_mood()
    except Exception as e:
        log.warning("home2: market_mood unavailable (%s)", e)

    now = _ist_now()
    is_td = now.weekday() < 5            # holiday table read lives in /api/mobile/now; weekday is
                                         # enough for the rails here and never lies bullish
    with _conn() as conn, conn.cursor() as cur:
        # cc#970: the cutover lookup that used to live here is gone with the local book maths —
        # book_canon() resolves the era itself, and nothing else on this endpoint needs it.

        # cc#984: the feed rail needs the REAL trading day, not the weekday shortcut above. A
        # weekday holiday has no ticks by design, and calling that "Feed stale" is precisely the
        # cc#841 false positive. nse_holidays is a list OF holidays — presence means shut.
        # Deliberately scoped to the feed rail: the other rails keep the weekday rule they
        # shipped with (this card's do_not_touch), and that gap is flagged in the result.
        cur.execute("SELECT 1 FROM nse_holidays WHERE holiday_date = %s", (now.date(),))
        is_td_feed = is_td and cur.fetchone() is None

        # 0 · ticker tail: latest global row per name (global_indices holds daily history;
        #     DISTINCT ON quote_date-desc is the honest "latest print", with its own date)
        # cc#1123: `symbol` joins the SELECT so the tape can ask global_heatstrip which symbols are
        # inverted. Without it the tape had no way to know India VIX is a volatility gauge, which
        # is exactly how it ended up painting a CALMING market red while the Digest tile painted
        # the same number green.
        cur.execute("""
            SELECT DISTINCT ON (name) name, symbol, price, chg_pct, category, quote_date
            FROM global_indices
            ORDER BY name, quote_date DESC
        """)
        glob_rows = _rows(cur)

        # cc#910: which tape names actually HAVE a 5-min series. Read from the table, never a
        # hardcoded list — a name added to the intraday feed tomorrow gains its 1D pill with no
        # code change, and a name that stops being fed loses it.
        cur.execute("SELECT DISTINCT name FROM global_intraday")
        intraday_names = set((r["name"] or "").upper() for r in _rows(cur))

        # 0b · V10 index state for the hero line
        v10 = _v10_state(cur)

        # 0c · PCR + VIX latest for the hero chips (server-side; frontend never guesses keys)
        # cc#927: price_date comes along so card 2 can CITE the as-of beside the mood label it
        # derives. Same row, same query, one extra column — not a second fetch (18024: "same PCR
        # the hero chip reads — one derivation").
        # cc#1140 · THE CARD WENT LIVE. Founder screenshot 09:29 with the market OPEN and the feed
        # LIVE, while this card read PCR 0.68 · 08-19 — yesterday's close. pcr_intraday already had
        # today's bars for both underlyings; the composer was live and only the READ was stale.
        # STALE PATH (before): pcr_daily, ORDER BY price_date DESC LIMIT 1 — an EOD table, so
        #   during the session it can only ever return yesterday.
        # LIVE PATH (after): pcr_intraday.pcr_total for NIFTY, latest bar, when one exists for
        #   TODAY. Outside market hours, or before the first bar lands, it falls back to exactly
        #   the pcr_daily read it used before, unchanged.
        # SAME COLUMN THE REST OF THE APP USES. pcr_total, not pcr_atm5 — this is the column
        # cc#1121 reconciled against pcr_daily.pcr (NIFTY daily 0.678 vs intraday pcr_total 0.679,
        # while pcr_atm5 was 0.844). Reading the other one here would put a number on this card
        # that disagrees with the Digest chart for the same moment.
        # NO NEW COMPOSER AND NO CLIENT MATHS: this reads the series the PCR scheduler already
        # writes. Nothing computes a PCR here.
        # AS-OF IS NOT OPTIONAL. pcr_asof carries the bar time and pcr_basis says LIVE or EOD, so
        # the label can never show a live-looking number without saying which bar it is. That is
        # why they are returned together from ONE query rather than assembled by the caller — a
        # value and its timestamp that can be fetched separately will eventually be shown apart.
        pcr_latest = pcr_date = pcr_asof = None
        pcr_basis = "EOD"
        pcr_stale = False
        try:
            cur.execute("""
                SELECT pcr_total, ts FROM pcr_intraday
                WHERE underlying='NIFTY' AND pcr_total IS NOT NULL
                  AND ts::date = (SELECT MAX(ts)::date FROM pcr_intraday WHERE underlying='NIFTY')
                ORDER BY ts DESC LIMIT 1
            """)
            _i = cur.fetchone()
            if _i and _i[0] is not None and _i[1] is not None and _i[1].date() == now.date():
                pcr_latest = float(_i[0])
                pcr_date = _i[1].date().isoformat()
                pcr_asof = _i[1].strftime("%H:%M")
                pcr_basis = "LIVE"
                # STALENESS GUARD (pcr_guard convention): a bar older than 15 minutes during the
                # session is shown WITH its time and tagged, never presented as current. It is not
                # suppressed — a 20-minute-old PCR is still the last thing that happened.
                if is_td and now.time() >= dt_time(9, 15) and now.time() <= dt_time(15, 30):
                    pcr_stale = (now - _i[1]).total_seconds() > 900
        except Exception as e:
            log.warning("cc#1140 intraday PCR unavailable, falling back to EOD: %s", e)
            try:
                cur.connection.rollback()
            except Exception:
                pass
        if pcr_latest is None:
            cur.execute("""
                SELECT pcr, price_date FROM pcr_daily
                WHERE underlying='NIFTY' AND pcr IS NOT NULL
                ORDER BY price_date DESC LIMIT 1
            """)
            _p = cur.fetchone()
            pcr_latest = float(_p[0]) if _p and _p[0] is not None else None
            pcr_date = _p[1].isoformat() if _p and _p[1] is not None else None
            pcr_basis = "EOD"
        # ── cc#1083 · India VIX: LIVE level + previous-session close + the change ──────────────
        # WHY THIS MOVED OFF global_indices. That table's India VIX row is a DAILY close and it
        # lags: read 17-Aug 12:11 IST, its newest quote_date was 14-Aug at 11.305, so the hero
        # chip was serving a three-day-old level as the live one. global_intraday has carried
        # India VIX 5-min bars since cc#1067 (72 bars today), so the live leg comes from there.
        #
        # THE PREVIOUS SESSION IS DERIVED FROM THE BARS THEMSELVES, not from a calendar. Taking
        # the two most recent DISTINCT bar dates skips weekends and holidays by construction —
        # there are simply no bars on a day the market did not trade — which is stronger than
        # "yesterday" and needs no holiday table lookup. cc#1083 asks not to assume yesterday;
        # this cannot, because it never computes a date at all.
        #
        # NULL-HONEST: if either side is missing, vix_chg is None. Never 0 for missing — a flat
        # VIX and an unknown VIX are different facts, and the chip rule turns on which it is.
        vix_latest = vix_prev_close = vix_chg = vix_chg_pct = None
        try:
            cur.execute("""
                WITH b AS (
                    SELECT ts::date AS d, close, ts FROM global_intraday
                    WHERE symbol = 'INDIAVIX' AND close IS NOT NULL
                ),
                d2 AS (SELECT DISTINCT d FROM b ORDER BY d DESC LIMIT 2)
                SELECT DISTINCT ON (b.d) b.d, b.close
                FROM b JOIN d2 ON d2.d = b.d
                ORDER BY b.d DESC, b.ts DESC
            """)
            # NAMED vix_rows, NOT _rows. cc#1083 called this local `_rows` and took /m/home
            # down: `_rows` is the SHARED helper imported at the top of this module, and a local
            # assignment anywhere in a function makes the name local for the WHOLE function — so
            # the two earlier `_rows(cur)` calls in this same function (the global tape and the
            # intraday-names lookup, ~50 lines above) started raising UnboundLocalError before
            # execution ever reached the VIX block. Python scoping is function-wide, not
            # line-ordered, which is why the bug appeared upstream of the line that caused it.
            vix_rows = cur.fetchall()
            if vix_rows:
                vix_latest = float(vix_rows[0][1]) if vix_rows[0][1] is not None else None
            if len(vix_rows) > 1 and vix_rows[1][1] is not None:
                vix_prev_close = float(vix_rows[1][1])
            if vix_latest is not None and vix_prev_close is not None:
                vix_chg = round(vix_latest - vix_prev_close, 4)
                # cc#1123: the same move as a PERCENT, because the founder's rule is stated in
                # percent (down 5%+ / up 5%+) and vix_chg is points. Derived from the two values
                # already fetched above — no second query, no new source, and null-honest for the
                # same reason vix_chg is: a missing previous close means the rule cannot fire, it
                # does not mean zero.
                if vix_prev_close:
                    vix_chg_pct = round((vix_latest / vix_prev_close - 1.0) * 100.0, 2)
        except Exception as e:
            log.warning("cc#1083 India VIX intraday unavailable, falling back to daily: %s", e)
            try:
                cur.connection.rollback()
            except Exception:
                pass
        if vix_latest is None:
            # Degradation path only — the daily close, which is what this used to serve outright.
            # cc#1123: chg_pct comes along on this path too. It is the same symbol's own daily
            # close-to-close move — exactly what the Digest tile and the tape colour from — so on
            # a day the intraday feed is unavailable the chip degrades to the same figure the rest
            # of the app is using, instead of losing its colour rule entirely.
            cur.execute("""
                SELECT price, chg_pct FROM global_indices
                WHERE name='India VIX' AND price IS NOT NULL
                ORDER BY quote_date DESC LIMIT 1
            """)
            _v = cur.fetchone()
            vix_latest = float(_v[0]) if _v and _v[0] is not None else None
            if vix_chg_pct is None and _v and _v[1] is not None:
                vix_chg_pct = round(float(_v[1]), 2)

        # 1 · today's signals, newest 3, with the live price beside the signal price
        cur.execute("""
            SELECT q.symbol, q.basket, q.signal_ts, q.cmp AS signal_cmp,
                   lp.cmp AS live_cmp
            FROM v8_qualified q
            LEFT JOIN LATERAL (
                SELECT close AS cmp FROM intraday_prices
                WHERE symbol = q.symbol AND """ + NOT_FUT_SQL + """
                ORDER BY ts DESC LIMIT 1
            ) lp ON true
            WHERE q.signal_date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
            ORDER BY q.signal_ts DESC
            LIMIT 3
        """)
        sig_rows = _rows(cur)
        cur.execute("""
            SELECT COUNT(*) AS n, MAX(signal_ts) AS newest FROM v8_qualified
            WHERE signal_date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
        """)
        sig_head = _rows(cur)[0]

        # 2 · V8 book — cc#970: THE CANON, not a local recomputation.
        #     This block used to carry its own two queries (open-side split + closed ledger) with
        #     's1_reclaim_obs' hardcoded and buy_s1_bounce never excluded. Under rule 13 there is
        #     exactly ONE book formula and Home reads it like every other surface, so a change to
        #     the doctrine cannot reach Home and miss /m/v8, which is the drift that caused this.
        book = book_canon(conn, era="fresh")

        # 3 · my portfolio (SmartGain) — rows + aggregate. TIMESTAMPTZ converted in SQL.
        cur.execute("""
            SELECT symbol, direction, qty, entry_price, ltp, mtm,
                   (updated_at AT TIME ZONE 'Asia/Kolkata') AS updated_at
            FROM smartgain_holdings
            ORDER BY ABS(COALESCE(mtm, 0)) DESC
        """)
        pf_rows = _rows(cur)

        # 4 · Live News (founder 08-Aug, cc#899): scroll up to the LAST 100 — 2 editorials
        #     pinned first, then 98 newest shorts (no 24h cap on the tail; older items carry
        #     their date in the template). count_24h stays the honest 24h count.
        cur.execute("""
            (SELECT headline, category, display_time FROM v_polished_articles
             WHERE category = 'AI Editorial'
             ORDER BY display_time DESC LIMIT 2)
            UNION ALL
            (SELECT headline, category, display_time FROM v_polished_articles
             WHERE category <> 'AI Editorial'
             ORDER BY display_time DESC LIMIT 98)
        """)
        reads = _rows(cur)
        cur.execute("""
            SELECT COUNT(*) FROM v_polished_articles
            WHERE display_time >= NOW() - INTERVAL '24 hours'
        """)
        news_24h = cur.fetchone()[0]

        # 5 · cc#984 · FEED freshness. Separate from the session clock: on 10-Aug the market was
        #     open from 09:15 and the first tick of the day did not land until 10:40, and nothing
        #     on this screen said so. MAX(ts) over the three live 5-min legs, TODAY only, naive
        #     IST read raw (intraday_prices.ts is already IST — converting it is the cc#887 trap
        #     in reverse). The auction tags join in only inside their own window.
        _auction_ok = feed_auction_ok(now, is_td_feed)
        cur.execute("""
            SELECT MAX(ts) FROM intraday_prices
            WHERE ts >= %s::date
              AND (source = ANY(%s) OR (%s::boolean AND source = ANY(%s)))
        """, (now.date(), list(FEED_SOURCES), _auction_ok, list(FEED_AUCTION_SOURCES)))
        feed_last = cur.fetchone()[0]

    def f(v):
        return float(v) if v is not None else None

    # ── ticker: NIFTY/BANKNIFTY first (live), then the global tail in Indian-investor order ──
    # cc#904: every row now carries as_of and has_series.
    #   as_of      — the honest date behind the number. Domestic rows carry domestic_live's own
    #                price_date; global rows carry the quote_date of the row that was selected.
    #                The tape shows a value; the table has to say WHEN, or a five-day-old Nikkei
    #                print reads as this morning's.
    #   has_series — whether a per-name chart is actually reachable, decided HERE from real data
    #                rather than guessed in the template. NIFTY/BANKNIFTY have always been True
    #                here (their intraday session always gave them a chart) — cc#1408 is what
    #                changed daily_series below, not this flag. The card is explicit: never a
    #                dead button.
    ticker = []
    for k, v in (idx.get("indices") or {}).items():
        if isinstance(v, dict):
            # cc#940 task 3 (ORIGINAL STATE, superseded by cc#1408 below): NIFTY and BANKNIFTY had
            # NO chart at all because /api/global/history read global_indices and they are not in
            # it. What WAS available was their 5-min session in intraday_prices, so they got an
            # INTRADAY-ONLY chart: the 1D pill and nothing else, daily_series=False.
            # cc#1408: daily_series is now True. /api/global/history/{name} (gvm_market_endpoints)
            # gained a NIFTY/BANKNIFTY path reading raw_prices directly — the same table
            # domestic_live() (this tile) and v10_maxpain() (Max Pain's spot) already read for
            # these two symbols, so the daily series here cannot drift from what those two
            # surfaces already show. The tape template reads this flag to build the intraday+daily
            # 1D/15D/30D/1Y range set (cc#1407's TAPE_RANGES_ALL) instead of the one-pill bar —
            # never a dead button (cc#904), and never a pill that leads nowhere either.
            ticker.append({"name": "NIFTY" if k == "NIFTY50" else "BANKNIFTY",
                           "price": v.get("close"), "chg_pct": v.get("chg_pct"),
                           "category": "domestic",
                           "as_of": v.get("price_date"),
                           "live": v.get("source") == "live_intraday",
                           "has_series": True,
                           "daily_series": True,
                           "intraday_name": k})
    _pos = {n: i for i, n in enumerate(_TICKER_NAME_ORDER)}
    for r in sorted(glob_rows, key=lambda r: (_pos.get(r["name"], 99), r["name"])):
        # cc#904: WTI drops off the tape — Brent already carries oil for an Indian investor, and
        # two crude prints spend two pills saying one thing. Dropped at the BUILD, so it leaves
        # the payload rather than being hidden in the template.
        if r["name"] in _TICKER_SKIP:
            continue
        # cc#1123: the tape carries the SAME band the Digest tile reads, from the same composer
        # (global_heatstrip._band). India VIX sits FIRST on this tape, so the surface most likely
        # to be glanced at was the one telling the opposite story: chg_pct is negative on a calming
        # day, the template coloured by raw sign, and the first pill on the Home screen went red
        # while the Digest called the same move green. The template now prefers this band and only
        # falls back to the sign when a row ships none.
        ticker.append({"name": r["name"], "price": f(r["price"]),
                       "chg_pct": f(r["chg_pct"]), "category": r["category"],
                       "band": _hs_band(f(r["chg_pct"]), r["symbol"] in _HS_INVERTED),
                       "as_of": r["quote_date"].isoformat() if r["quote_date"] else None,
                       "live": False,
                       "has_series": True,
                       "daily_series": True,
                       "intraday_name": _intraday_name(r["name"], intraday_names)})

    # hero chips straight from market_mood's own checks[] — value + pass, no invented names
    chips = []
    for c in (mood or {}).get("checks") or []:
        chips.append({
            "label": c.get("filter"),
            "value": f(c.get("value")),
            "ok": bool(c.get("pass")),
            "indeterminate": bool(c.get("indeterminate")),
        })
    fails = (mood or {}).get("fails")

    # cc#1369: the breadth counts market_mood() already computed for the ADR check (adr_detail —
    # advances/declines/unchanged) exposed on hero for the Home advance-decline bar. No new query:
    # this is the SAME call that built `chips` above, just a field of it nobody read yet. The ratio
    # itself is not recomputed either — it is the ADR chip's own value, found by label so a change
    # to checks[] ordering can never desync the two.
    adr_detail = (mood or {}).get("adr_detail") or {}
    adr_ratio = next((c["value"] for c in chips if c["label"] == "ADR"), None)

    signals = []
    for s in sig_rows:
        since = None
        sc, lc = f(s["signal_cmp"]), f(s["live_cmp"])
        if sc and lc:
            raw = (lc - sc) / sc * 100.0
            since = round(-raw if (s["basket"] or "").startswith(_SELL_PREFIX) else raw, 2)
        signals.append({
            "symbol": s["symbol"],
            "basket": basket_label(s["basket"]),
            "at": s["signal_ts"].strftime("%H:%M") if s["signal_ts"] else None,
            "price": lc if lc is not None else sc,
            "since_pct": since,
        })

    pf_empty = not pf_rows
    pf_mtm = sum((f(r["mtm"]) or 0.0) for r in pf_rows) if pf_rows else None
    pf_newest = max((r["updated_at"] for r in pf_rows if r["updated_at"]), default=None)

    t = now.time()
    market_open = bool(is_td and MARKET_OPEN <= t <= SESSION_END)

    # cc#970: every per-side derivation that used to live here is GONE — _side(), _record(),
    # the brokerage deduction and the rate arithmetic all moved into v8_book_canon.book_canon().
    # Home now consumes the figures; it does not produce them.

    return {
        # cc#1390: what build IS ACTUALLY RUNNING right now, on this live no-store call — compared
        # client-side against window.__SCORR_BUILD (the same BUILD_ID, stamped into the PAGE this
        # response is feeding) to catch a stale-served document a caching layer this app does not
        # control let through despite no-store/cache-busting already being correct everywhere this
        # app itself controls. See mobile/home.html's own comment for the comparison + self-heal.
        "build_id": BUILD_ID,
        "session": {
            "market_open": market_open,
            "time": now.strftime("%H:%M"),
            "date": now.strftime("%a %d %b"),
            "label": ("Market open" if market_open else "Market closed") + " · " + now.strftime("%H:%M IST"),
        },
        # cc#984: the tick feed, told apart from the session clock. Computed here, server-side —
        # the client never derives freshness from the phone's clock.
        "feed": {
            **feed_rail(feed_last, now, is_td_feed),
            "last_bar": feed_last.strftime("%H:%M") if feed_last else None,
            "sources": list(FEED_SOURCES),
        },
        "ticker": ticker,
        # cc#1231: which chip series have a 5-min source, from the one definition above. The page
        # needs this BEFORE it opens a popout, to decide whether a 1D tab should exist at all —
        # asking /api/mobile/trends would mean drawing the wrong tabs first and correcting them
        # after the response. Shipped as a list rather than a per-kind flag so the client holds no
        # list of its own: it tests membership, it does not restate the set.
        "trend_intraday": list(_INTRADAY_KINDS),
        # retained one deploy for any cached template; the ticker replaces this grid
        "indices": [{
            "name": "Nifty 50" if k == "NIFTY50" else "Bank Nifty",
            "close": v.get("close"), "chg_pct": v.get("chg_pct"),
        } for k, v in (idx.get("indices") or {}).items() if isinstance(v, dict)],
        "hero": {
            "mood": (mood or {}).get("mood"),
            "fails": fails,
            "why": (f"{fails} of {len(chips)} checks failed today." if fails and chips
                    else "All checks passed today." if chips and fails == 0
                    else "Gate state unavailable."),
            "chips": chips,
            "pcr": pcr_latest,
            "pcr_date": pcr_date,
            # cc#1140: the as-of travels WITH the value, always. pcr_basis is LIVE or EOD,
            # pcr_asof is the bar time when live, pcr_stale flags a bar older than 15 min during
            # the session. The card cannot render a live-looking number without them because they
            # arrive in the same object from the same query.
            "pcr_asof": pcr_asof,
            "pcr_basis": pcr_basis,
            "pcr_stale": pcr_stale,
            "vix": vix_latest,
            # cc#1083: completes VIX_COLOR_RULE_V1's confirming-fear half. Both sides are exposed,
            # not just the delta, so the chip (and anyone reading the payload) can see what the
            # change was measured against rather than trusting a bare number.
            "vix_prev_close": vix_prev_close,
            "vix_chg": vix_chg,
            "vix_chg_pct": vix_chg_pct,   # cc#1123: the same move in percent, for the tier rule
            "v10": v10,
            "as_of": (mood or {}).get("checked_at"),
            # cc#1369: advance-decline for the Home mood card's breadth bar. advances/declines/
            # unchanged null together when the breadth feed is dead (same condition the ADR check
            # already goes indeterminate on) — the client draws no bar rather than a guessed one.
            "adr_detail": {
                "advances": adr_detail.get("advances"),
                "declines": adr_detail.get("declines"),
                "unchanged": adr_detail.get("unchanged"),
                "ratio": adr_ratio,
            },
        },
        "signals": {
            "today": sig_head["n"],
            "top": signals,
            "rail": rail_state(sig_head["newest"], 5, now, is_td),
        },
        # cc#970: shipped verbatim from the canon — Home adds no arithmetic of its own.
        # Keys are unchanged so the template needs no edit; they are simply no longer derived here.
        "book": {
            "open": book["open"],
            "unrealised": book["unrealised"],
            "deployed": book["deployed"],
            "unrealised_pct": book["unrealised_pct"],
            "long": book["long"],
            "short": book["short"],
            "realised": book["realised"],
            "gross": book["gross"],
            "brokerage": book["brokerage"],
            "wins": book["wins"], "losses": book["losses"], "trades": book["trades"],
            "rec_long": book["rec_long"],
            "rec_short": book["rec_short"],
            "era": book["era"],
            "canon": book["canon"],
            "retired_baskets": book["retired_baskets"],
        },
        "portfolio": {
            "empty": pf_empty,
            "positions": len(pf_rows),
            "mtm": round(pf_mtm, 2) if pf_mtm is not None else None,
            "rows": [{
                "symbol": r["symbol"],
                "direction": (r["direction"] or "").upper(),
                "qty": f(r["qty"]),
                "ltp": f(r["ltp"]),
                "mtm": f(r["mtm"]),
            } for r in pf_rows],
            "message": "No positions open." if pf_empty else None,
            "rail": rail_state(pf_newest, 1440, now, is_td),
        },
        "reading": {
            "count_24h": news_24h,
            "items": [{
                "headline": r["headline"],
                "category": r["category"],
                "when": (None if not r["display_time"]
                         else r["display_time"].strftime("%H:%M")
                         if r["display_time"].date() == now.date()
                         else r["display_time"].strftime("%d %b %H:%M")),
            } for r in reads],
        },
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
