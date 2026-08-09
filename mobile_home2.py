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
  * NEW /api/mobile/trends?kind=adr|pcr|vix — uniform {series:[{d,v}]} for the chip chart
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
    rail_state, basket_label, _conn, _rows, _ist_now, _guard, _json_safe, _page,
    MARKET_OPEN, SESSION_END,
)

log = logging.getLogger("scorr.mobile.home2")
router = APIRouter()

_SELL_PREFIX = "sell_"          # sign convention for since-%: a short gains when price falls
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
      vix -> NO intraday source exists, so this falls back to the daily line and SAYS so in
             `note`. The chart is never given a shape the data cannot support.

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
    if kind not in ("adr", "pcr", "vix"):
        kind = "adr"
    want = max(1, min(int(days or 30), 120))
    intraday = (want == 1 and kind in ("adr", "pcr"))
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
            else:
                cur.execute("""
                    SELECT price_date AS d, adr AS v FROM adr_daily
                    WHERE adr IS NOT NULL
                    ORDER BY price_date DESC LIMIT %s
                """, (dd,))
            rows = list(reversed(_rows(cur)))
            if want == 1 and kind == "vix":
                note = _VIX_NO_INTRADAY

    series = [{"d": str(r["d"]), "v": float(r["v"])} for r in rows if r["v"] is not None]
    as_of = None
    if intraday and rows:
        as_of = str(rows[-1]["sd"])
        note = "%s intraday · 5-min ticks" % as_of
    elif series:
        as_of = series[-1]["d"]
    return {"kind": kind, "series": series, "intraday": intraday,
            "as_of": as_of, "note": note,
            "latest": series[-1]["v"] if series else None}


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

    A trade older than the window is DROPPED and counted in `outside_window` — never clamped
    onto the first candle, which would put a pin on a price that never happened.

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
            SELECT id, side, leg, opt_strike, opt_type, entry_price, stop, target,
                   (entry_ts AT TIME ZONE 'Asia/Kolkata')::date          AS ed,
                   to_char(entry_ts AT TIME ZONE 'Asia/Kolkata','HH24:MI') AS et
            FROM v10_positions
            WHERE status = 'OPEN' AND symbol = %s
            ORDER BY entry_ts ASC
        """, (sym,))
        open_rows = _rows(cur)

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
        if ed not in in_win and xd not in in_win:
            outside += 1
            continue
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
             "win": None, "open": True, "stop": f(r["stop"]), "target": f(r["target"])}
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
        # fresh-era cutover — the same app_config key the web daylog reads (cc#510)
        cur.execute("SELECT value FROM app_config WHERE key='v8_paper_rebuild_cutover_ts'")
        _row = cur.fetchone()
        cutover = _row[0] if _row and _row[0] else None

        # 0 · ticker tail: latest global row per name (global_indices holds daily history;
        #     DISTINCT ON quote_date-desc is the honest "latest print", with its own date)
        cur.execute("""
            SELECT DISTINCT ON (name) name, price, chg_pct, category, quote_date
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
        cur.execute("""
            SELECT pcr, price_date FROM pcr_daily
            WHERE underlying='NIFTY' AND pcr IS NOT NULL
            ORDER BY price_date DESC LIMIT 1
        """)
        _p = cur.fetchone()
        pcr_latest = float(_p[0]) if _p and _p[0] is not None else None
        pcr_date = _p[1].isoformat() if _p and _p[1] is not None else None
        cur.execute("""
            SELECT price, chg_pct FROM global_indices WHERE name='India VIX' AND price IS NOT NULL
            ORDER BY quote_date DESC LIMIT 1
        """)
        _v = cur.fetchone()
        vix_latest = float(_v[0]) if _v and _v[0] is not None else None

        # 1 · today's signals, newest 3, with the live price beside the signal price
        cur.execute("""
            SELECT q.symbol, q.basket, q.signal_ts, q.cmp AS signal_cmp,
                   lp.cmp AS live_cmp
            FROM v8_qualified q
            LEFT JOIN LATERAL (
                SELECT close AS cmp FROM intraday_prices
                WHERE symbol = q.symbol AND source <> 'fyers_fut'
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

        # 2 · V8 open book — FRESH ERA, web Master Dashboard scope, now SPLIT PER SIDE
        #     (founder 08-Aug: Unrealised / Long Unrealised / Short Unrealised, each as a % of
        #     the capital deployed on that side; deployed = entry_price * qty notional).
        cur.execute("""
            SELECT
                COALESCE(SUM(
                    (COALESCE(lp.cmp, p.entry_price) - p.entry_price) * p.qty *
                    CASE WHEN UPPER(p.side) = 'SHORT' THEN -1 ELSE 1 END
                ), 0) AS unrealised,
                COUNT(*) AS open_n,
                COALESCE(SUM(
                    (COALESCE(lp.cmp, p.entry_price) - p.entry_price) * p.qty
                ) FILTER (WHERE UPPER(p.side) <> 'SHORT'), 0) AS unrl_long,
                COUNT(*) FILTER (WHERE UPPER(p.side) <> 'SHORT') AS n_long,
                COALESCE(SUM(p.entry_price * p.qty)
                    FILTER (WHERE UPPER(p.side) <> 'SHORT'), 0) AS dep_long,
                COALESCE(SUM(
                    (p.entry_price - COALESCE(lp.cmp, p.entry_price)) * p.qty
                ) FILTER (WHERE UPPER(p.side) = 'SHORT'), 0) AS unrl_short,
                COUNT(*) FILTER (WHERE UPPER(p.side) = 'SHORT') AS n_short,
                COALESCE(SUM(p.entry_price * p.qty)
                    FILTER (WHERE UPPER(p.side) = 'SHORT'), 0) AS dep_short
            FROM v8_paper_positions p
            LEFT JOIN LATERAL (
                SELECT close AS cmp FROM intraday_prices
                WHERE symbol = p.symbol AND source <> 'fyers_fut'
                ORDER BY ts DESC LIMIT 1
            ) lp ON true
            WHERE p.status = 'OPEN'
              AND (%(cut)s::timestamp IS NULL OR p.entry_ts >= %(cut)s::timestamp)
              AND p.basket IS DISTINCT FROM 's1_reclaim_obs'
        """, {"cut": cutover})
        book_live = _rows(cur)[0]
        # cc#961: the SAME query gains per-side verdict counts — FILTER clauses, not a second
        # fetch, so the totals and the split can never drift apart. Side predicate matches the
        # open-book query above (<> 'SHORT' is the long side). Verified against the live table:
        # only LONG and SHORT exist in the fresh era, no NULLs, and long_n + short_n = trades.
        cur.execute("""
            SELECT COUNT(*) AS trades, COALESCE(SUM(pnl), 0) AS gross,
                   COUNT(*) FILTER (WHERE result = 'TARGET') AS wins,
                   COUNT(*) FILTER (WHERE result = 'SL') AS losses,
                   COUNT(*) FILTER (WHERE UPPER(side) <> 'SHORT') AS trades_long,
                   COUNT(*) FILTER (WHERE UPPER(side) <> 'SHORT' AND result = 'TARGET') AS wins_long,
                   COUNT(*) FILTER (WHERE UPPER(side) <> 'SHORT' AND result = 'SL') AS losses_long,
                   COUNT(*) FILTER (WHERE UPPER(side) = 'SHORT') AS trades_short,
                   COUNT(*) FILTER (WHERE UPPER(side) = 'SHORT' AND result = 'TARGET') AS wins_short,
                   COUNT(*) FILTER (WHERE UPPER(side) = 'SHORT' AND result = 'SL') AS losses_short
            FROM v8_paper_trades
            WHERE (%(cut)s::timestamp IS NULL OR entry_ts >= %(cut)s::timestamp)
              AND basket IS DISTINCT FROM 's1_reclaim_obs'
        """, {"cut": cutover})
        led = _rows(cur)[0]

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

    def f(v):
        return float(v) if v is not None else None

    # ── ticker: NIFTY/BANKNIFTY first (live), then the global tail in Indian-investor order ──
    # cc#904: every row now carries as_of and has_series.
    #   as_of      — the honest date behind the number. Domestic rows carry domestic_live's own
    #                price_date; global rows carry the quote_date of the row that was selected.
    #                The tape shows a value; the table has to say WHEN, or a five-day-old Nikkei
    #                print reads as this morning's.
    #   has_series — whether a per-name chart is actually reachable, decided HERE from real data
    #                rather than guessed in the template. /api/global/history/{name} reads
    #                global_indices, and NIFTY/BANKNIFTY are not in that table, so those two get
    #                no chart icon. The card is explicit: never a dead button.
    ticker = []
    for k, v in (idx.get("indices") or {}).items():
        if isinstance(v, dict):
            # cc#940 task 3: NIFTY and BANKNIFTY had NO chart at all — cc#904 set has_series False
            # because /api/global/history reads global_indices and they are not in it, which is
            # still true. What IS available is their 5-min session in intraday_prices, so they now
            # get an INTRADAY-ONLY chart: the 1D pill and nothing else. daily_series=False is the
            # flag the template reads to build a one-pill range bar instead of offering 7D/15D/30D
            # windows that would resolve to an empty daily series. Never a dead button (cc#904),
            # and never a pill that leads nowhere either.
            ticker.append({"name": "NIFTY" if k == "NIFTY50" else "BANKNIFTY",
                           "price": v.get("close"), "chg_pct": v.get("chg_pct"),
                           "category": "domestic",
                           "as_of": v.get("price_date"),
                           "live": v.get("source") == "live_intraday",
                           "has_series": True,
                           "daily_series": False,
                           "intraday_name": k})
    _pos = {n: i for i, n in enumerate(_TICKER_NAME_ORDER)}
    for r in sorted(glob_rows, key=lambda r: (_pos.get(r["name"], 99), r["name"])):
        # cc#904: WTI drops off the tape — Brent already carries oil for an Indian investor, and
        # two crude prints spend two pills saying one thing. Dropped at the BUILD, so it leaves
        # the payload rather than being hidden in the template.
        if r["name"] in _TICKER_SKIP:
            continue
        ticker.append({"name": r["name"], "price": f(r["price"]),
                       "chg_pct": f(r["chg_pct"]), "category": r["category"],
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

    gross = f(led["gross"]) or 0.0
    brokerage = (led["trades"] or 0) * BROKERAGE_PER_TRADE

    # per-side book numbers — a side with no positions is nulls, never a fabricated 0%
    def _side(unrl_key, dep_key, n_key):
        n = book_live[n_key] or 0
        if n == 0:
            return {"n": 0, "unrealised": None, "deployed": None, "pct": None}
        u = round(f(book_live[unrl_key]) or 0.0, 2)
        d = round(f(book_live[dep_key]) or 0.0, 2)
        return {"n": n, "unrealised": u, "deployed": d,
                "pct": round(u / d * 100.0, 2) if d else None}
    side_long = _side("unrl_long", "dep_long", "n_long")
    side_short = _side("unrl_short", "dep_short", "n_short")

    # cc#961: per-side REALISED record. decided = TARGET + SL only — the cc#950 doctrine. The
    # 15 fresh-era trades that closed for some other reason are money in the realised figure but
    # not a verdict, so they are in `trades` and in neither `wins` nor `losses`.
    # The rate is computed HERE, once, so Home and any other surface read the same number rather
    # than each dividing for themselves (DISPLAY_PARITY). No decided trades => rate is None, and
    # the template prints a dash. A side with no verdicts has not gone 0% — it has no record.
    def _record(w_key, l_key, n_key):
        w = led[w_key] or 0
        l = led[l_key] or 0
        dec = w + l
        return {"wins": w, "losses": l, "decided": dec, "trades": led[n_key] or 0,
                "rate": round(100.0 * w / dec, 1) if dec else None}
    rec_long = _record("wins_long", "losses_long", "trades_long")
    rec_short = _record("wins_short", "losses_short", "trades_short")
    dep_total = (side_long["deployed"] or 0.0) + (side_short["deployed"] or 0.0)
    unrl_total = round(f(book_live["unrealised"]) or 0.0, 2)

    return {
        "session": {
            "market_open": market_open,
            "time": now.strftime("%H:%M"),
            "date": now.strftime("%a %d %b"),
            "label": ("Market open" if market_open else "Market closed") + " · " + now.strftime("%H:%M IST"),
        },
        "ticker": ticker,
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
            "vix": vix_latest,
            "v10": v10,
            "as_of": (mood or {}).get("checked_at"),
        },
        "signals": {
            "today": sig_head["n"],
            "top": signals,
            "rail": rail_state(sig_head["newest"], 5, now, is_td),
        },
        "book": {
            "open": book_live["open_n"],
            "unrealised": unrl_total,
            "deployed": round(dep_total, 2) if dep_total else None,
            "unrealised_pct": (round(unrl_total / dep_total * 100.0, 2) if dep_total else None),
            "long": side_long,
            "short": side_short,
            "realised": round(gross - brokerage, 2),      # NET, exactly the web card
            "gross": round(gross, 2),
            "brokerage": brokerage,
            "wins": led["wins"], "losses": led["losses"], "trades": led["trades"],
            # cc#961: realised record split by side. Kept alongside the totals above, which /m/v8
            # still reads — the split is additive, nothing existing changed shape.
            "rec_long": rec_long,
            "rec_short": rec_short,
            "era": "fresh",
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
