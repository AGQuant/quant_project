"""
global_heatstrip.py — cc#842 GLOBAL HEAT STRIP V2.

Spec source of truth: session_log id=14406 (spec_locked, GLOBAL_HEATSTRIP_V2, founder 03-Aug 02:00).

HARD CONSTRAINTS (14406): READ-ONLY. No new tables, no new fetch jobs, no new scheduler entries.
Pure render off global_indices + global_intraday + the existing India VIX path.

TWO OPEN ITEMS THE SPEC ASKED ME TO VERIFY BEFORE WIRING — both answered against the live DB:

  (1) 52-WEEK METRICS FOR ALL 17: yes. Every symbol carries 5 years of daily history in
      global_indices (1,241-1,871 rows) with 241+ rows inside the last year. The five EOD symbols
      spot-checked specifically: Shanghai 1,241 rows / 241 in 1y, ^FTSE 1,292 / 252, ^GDAXI
      1,305 / 252, INDIAVIX 1,271 / 243, ^VIX 1,286 / 252. All 52w metrics are computable.

  (2) INDIA VIX BRIDGE — THE SPEC'S TABLE NAME IS WRONG. There is no `v8_indiavix_intraday` table
      anywhere in the schema or the code. The real live path, as used by v10_endpoints.v10_vix, is
      `intraday_prices WHERE symbol='INDIAVIX'` (fed by the worker's INDEX_LTP_SYMBOLS map,
      worker/fyers_feed.py: 'INDIAVIX' -> 'NSE:INDIAVIX-INDEX') plus `cmp_prices` for the live LTP.
      This module bridges to THAT, exactly as the spec intended — no new fetch.

WHY THE PER-TILE AS-OF STAMP IS LOAD-BEARING, NOT DECORATION. Quote dates genuinely diverge across
these markets: on 03-Aug the commodities/FX/crypto rows carry 2026-08-03, the US rows 2026-08-01,
and Shanghai/FTSE/DAX/Nikkei/India VIX 2026-07-31. A strip that prints one timestamp for all of them
is asserting something false. Every tile carries its own.

WHY THE WEEK LEG USES EACH SYMBOL'S OWN SESSION CALENDAR (founder-locked). "5 trading sessions ago"
is computed per symbol from that symbol's own rows, never from a shared calendar. Shanghai, FTSE and
NSE keep different holidays; a shared calendar would silently compare across a different span for
each market and mis-date the week change.
"""

import os
import logging
from typing import Dict, Any, List, Optional

import psycopg2
from fastapi import APIRouter

log = logging.getLogger("scorr.heatstrip")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")

WEEK_SESSIONS = 5

# ── cc#852 TIER IS DERIVED FROM DATA, NOT FROM SET MEMBERSHIP ─────────────────────────────────
# THE BUG THIS FIXES. EOD_ONLY used to be a static 5-symbol set and build_strip filed every tile
# purely on `sym in EOD_ONLY`. India VIX ticks live all session (worker/fyers_feed.py subscribes
# NSE:INDIAVIX-INDEX via INDEX_LTP_SYMBOLS; 1,181 rows in intraday_prices), and this module already
# bridged to that feed and computed a LIVE price for it — and then filed the tile under eod_tiles
# anyway, because the set said so. A live price rendered in the "no intraday feed" block.
#
# So membership no longer decides the tier. `eod_only` on each tile is now TRUE IFF that symbol has
# no live tick, which is the same doctrine as ENGINE_LIVENESS_RULE (13829): the badge follows the
# data, never the registration.
#
# NO_INTRADAY_FEED stays as a STATIC declaration of which symbols have no 5-minute feed wired at
# all. It drives the 5m chart button ONLY (cc#849), never the tier — a transient feed gap must not
# silently disable 5m charting for a market that genuinely has a feed.
# cc#852 PROBE PASSED 05-Aug 07:29 IST — the set is now EMPTY. Every symbol on the tape has a
# wired, verified 5-minute feed, so the 5m chart button is enabled everywhere.
# Probe evidence (global_intraday, after triggering the live fetch on the deployed app because
# Yahoo is unreachable from the build container):
#     ^FTSE    511 bars  0 null closes  0.0% gap   12:30-21:00 IST  (Europe session)
#     ^GDAXI   510 bars  0 null closes  0.0% gap   12:30-21:00 IST  (Europe session)
#     ^HSI     348 bars  0 null closes  0.0% gap   07:30-07:12+     (ticking live at probe time)
# The set is KEPT (not deleted) because it is the correct home for any future symbol added without
# a 5m feed — the 5m button must never be enabled on a market that cannot fill it.
NO_INTRADAY_FEED: set = set()

# cc#852: symbols retired from the tape DISPLAY. Their history is deliberately retained in
# global_indices (^VIX has 1,287 daily rows) so the decision is reversible without a backfill.
# 000001.SS (Shanghai) is dropped outright — founder, unconditional, no probe — and is removed
# from GLOBAL_TICKERS in global_indices.py as well. THAT removal is the load-bearing half: the
# nightly Yahoo daily fetch would otherwise re-insert Shanghai and it would silently reappear.
TAPE_HIDDEN = {"^VIX", "000001.SS"}

# Inverted tiles: for volatility, UP is RISK-OFF. The inversion applies to BOTH the day and week
# legs (founder-locked) — a green VIX week would say the opposite of what happened.
# INDIAVIX stays here after cc#852 moved it out of the EOD block; only its TIER changed.
INVERTED = {"^VIX", "INDIAVIX"}

CATEGORY_ORDER = ["index", "volatility", "commodity", "currency", "crypto"]
CATEGORY_LABEL = {"index": "Indices", "volatility": "Volatility", "commodity": "Commodities",
                  "currency": "Currency", "crypto": "Crypto"}


def _conn():
    return psycopg2.connect(_DB)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct(now_v, base_v) -> Optional[float]:
    if now_v is None or base_v in (None, 0):
        return None
    return round((float(now_v) / float(base_v) - 1) * 100.0, 2)


def _band(chg: Optional[float], inverted: bool) -> str:
    """Colour band. Inversion flips the SIGN, not the thresholds, so a +2% VIX day lands in the
    same deep bucket a -2% equity day does."""
    if chg is None:
        return "none"
    v = -chg if inverted else chg
    if v == 0:
        return "flat"
    if v >= 1.5:
        return "up-strong"
    if v > 0:
        return "up"
    if v <= -1.5:
        return "down-strong"
    return "down"


def build_strip(cur) -> Dict[str, Any]:
    # ── latest daily row per symbol, plus the close 5 of ITS OWN sessions back ────────────────
    cur.execute("""
        WITH ranked AS (
            SELECT symbol, name, category, price, prev_close, chg_pct, quote_date,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY quote_date DESC) rn
            FROM global_indices WHERE price IS NOT NULL
        )
        SELECT symbol, name, category, price, prev_close, chg_pct, quote_date, rn
        FROM ranked WHERE rn <= %s ORDER BY symbol, rn
    """, (WEEK_SESSIONS + 1,))
    per_sym: Dict[str, List[tuple]] = {}
    for r in cur.fetchall():
        if r[0] in TAPE_HIDDEN:      # cc#852: retired from the tape; history kept in the table
            continue
        per_sym.setdefault(r[0], []).append(r)

    # ── live intraday last tick for the 12 covered symbols ───────────────────────────────────
    # COLUMN NAME (cc#848): global_intraday stores OHLC — open/high/low/CLOSE. It has NO `price`
    # column; `price` belongs to global_indices (daily), which is why every daily query in this
    # file worked while both intraday reads raised UndefinedColumn.
    #
    # ISOLATED (cc#848): this lookup is its own try/except because the only handler used to be at
    # the endpoint, which returns {tiles: [], eod_tiles: [], error}. One bad column therefore
    # blanked BOTH legs and all 17 tiles. The live tick is an ENRICHMENT over the daily row — if it
    # fails, every tile still renders from global_indices with as_of_is_live=False, i.e. the strip
    # degrades to EOD instead of disappearing. `live_error` is surfaced so a silent degrade is
    # still visible rather than looking like a quiet market.
    live, live_error = {}, None
    try:
        cur.execute("""SELECT DISTINCT ON (symbol) symbol, close, ts
                       FROM global_intraday WHERE close IS NOT NULL
                       ORDER BY symbol, ts DESC""")
        live = {r[0]: (_f(r[1]), r[2]) for r in cur.fetchall()}
    except Exception as e:
        live_error = f"{type(e).__name__}: {str(e)[:160]}"
        log.warning("cc#848 live intraday tick unavailable, degrading strip to EOD: %s", e)
        cur.connection.rollback()

    # ── India VIX bridge: the REAL path (see module docstring), never a new fetch ─────────────
    vix_live, vix_ts = None, None
    try:
        cur.execute("""SELECT close, ts FROM intraday_prices
                       WHERE symbol='INDIAVIX' AND close IS NOT NULL
                       ORDER BY ts DESC LIMIT 1""")
        r = cur.fetchone()
        if r:
            vix_live, vix_ts = _f(r[0]), r[1]
        cur.execute("SELECT cmp FROM cmp_prices WHERE symbol='INDIAVIX'")
        c = cur.fetchone()
        if c and c[0] is not None:
            vix_live = _f(c[0])
    except Exception as e:
        log.warning("cc#842 India VIX bridge unavailable: %s", e)

    tiles = []
    for sym, rows in per_sym.items():
        head = rows[0]
        name, cat = head[1], head[2]
        daily_px, prev_close, stored_chg, qdate = _f(head[3]), _f(head[4]), _f(head[5]), head[6]

        lp, lts = live.get(sym, (None, None))
        if sym == "INDIAVIX" and vix_live is not None:
            lp, lts = vix_live, vix_ts

        # DAY = latest tick vs prev_close. Falls back to the stored daily chg_pct when there is no
        # tick — which is exactly the case for the five EOD symbols, and why they get their own
        # block rather than sitting in the day row pretending to be live.
        # cc#852: liveness is simply "did we get a tick". The old form ANDed in set membership,
        # which is what suppressed India VIX despite its Fyers tick arriving correctly.
        is_live = lp is not None
        price = lp if lp is not None else daily_px
        day_chg = _pct(lp, prev_close) if (lp is not None and prev_close) else stored_chg

        # WEEK = latest close vs the close 5 of THIS symbol's own sessions back. If a symbol has
        # fewer than 6 stored sessions the answer is None, never a shorter window silently relabelled.
        week_chg, week_base_date = None, None
        if len(rows) >= WEEK_SESSIONS + 1:
            base = rows[WEEK_SESSIONS]
            week_chg = _pct(price if price is not None else daily_px, _f(base[3]))
            week_base_date = base[6]

        inv = sym in INVERTED
        tiles.append({
            "symbol": sym, "name": name, "category": cat,
            "price": price,
            "day_chg_pct": day_chg, "day_band": _band(day_chg, inv),
            "week_chg_pct": week_chg, "week_band": _band(week_chg, inv),
            "week_base_date": str(week_base_date) if week_base_date else None,
            "week_sessions": WEEK_SESSIONS,
            "as_of": str(lts) if (lts and is_live) else str(qdate),
            "as_of_is_live": bool(lts and is_live),
            # cc#852: DATA-DERIVED. No tick -> it renders in the EOD block, whoever it is.
            "eod_only": not is_live,
            "inverted": inv,
            "sessions_available": len(rows),
            # ── cc#849 merge fields ───────────────────────────────────────────────────────────
            # tick_ts: the RAW naive-IST tick, handed over untouched so the client can recompute
            # the age on every paint. A server-computed age would freeze at render time and start
            # lying within a minute — and the per-tile age is the load-bearing capability of the
            # tab being retired.
            "tick_ts": str(lts) if lts else None,
            "prev_close": prev_close,          # PREV CLOSE badge for markets that are shut
            "quote_date": str(qdate) if qdate else None,
            # 5m availability is a STATIC declaration of what is wired, NOT whether the feed
            # happened to write a row in the last few minutes — a transient gap must not silently
            # turn the 5m button off for a market that has a feed. Deliberately independent of
            # `eod_only` above: a market can be closed (no tick, EOD tier) and still have 5m history.
            "has_intraday": sym not in NO_INTRADAY_FEED,
        })

    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    tiles.sort(key=lambda t: (order.get(t["category"], 99), t["name"] or t["symbol"]))

    # ── cc#849: the five capabilities the retiring Global Indices tab carries ─────────────────
    cat_counts: Dict[str, int] = {}
    for t in tiles:
        cat_counts[t["category"]] = cat_counts.get(t["category"], 0) + 1
    stamps = [t["tick_ts"] for t in tiles if t.get("tick_ts")]
    freshest = max(stamps) if stamps else None

    return {
        "tiles": [t for t in tiles if not t["eod_only"]],
        "eod_tiles": [t for t in tiles if t["eod_only"]],
        "category_order": CATEGORY_ORDER, "category_label": CATEGORY_LABEL,
        "week_basis": f"rolling {WEEK_SESSIONS} sessions, per symbol's own session calendar",
        "inverted_symbols": sorted(INVERTED),
        "count": len(tiles),
        "live_ticks": len(live),
        "live_error": live_error,
        "freshest_tick": freshest,
        "category_counts": cat_counts,
        # Honest disclosure, inherited verbatim in substance from the tab (cc#849 item (e)).
                # cc#852: US VIX retired from the tape, so the line that disambiguated it is gone too.
        # Market windows are stated because a tile outside its own session shows PREV CLOSE, and
        # CLOSED IS NOT STALE — the reader needs to know which is which.
        "disclosure": ("Approx IST windows \u2014 Asia 07:00\u201313:30 \u00b7 Europe 12:30\u201321:30 \u00b7 US 19:00\u201301:40. "
                       "A market outside its own hours shows PREV CLOSE with its last tick time; "
                       "that is closed, not stale. India VIX ticks live off the NSE feed."),
        # Every tick timestamp in this payload is naive IST wall-clock (global_intraday.ts and
        # intraday_prices.ts are both stored that way). Note that global_intraday.updated_at is
        # naive UTC — the two must never be compared. Ages are computed against IST now.
        "tick_tz": "Asia/Kolkata",
    }


def build_detail(cur, symbol: str) -> Dict[str, Any]:
    """Click-drawer payload: a price series plus the return block, all from daily history."""
    cur.execute("""SELECT name, category FROM global_indices WHERE symbol=%s
                   ORDER BY quote_date DESC LIMIT 1""", (symbol,))
    head = cur.fetchone()
    if not head:
        return {"error": f"unknown symbol: {symbol}"}
    name, cat = head

    cur.execute("""SELECT quote_date, price FROM global_indices
                   WHERE symbol=%s AND price IS NOT NULL
                   ORDER BY quote_date""", (symbol,))
    hist = [(r[0], float(r[1])) for r in cur.fetchall()]
    if not hist:
        return {"error": f"no daily history for {symbol}"}

    last_d, last_p = hist[-1]

    def _back(n_sessions):
        i = len(hist) - 1 - n_sessions
        return hist[i][1] if i >= 0 else None

    def _on_or_before(target):
        prev = None
        for d, p in hist:
            if d <= target:
                prev = p
            else:
                break
        return prev

    from datetime import date as _date
    jan1 = _date(last_d.year, 1, 1)

    rets = {
        "1D": _pct(last_p, _back(1)),
        "1W": _pct(last_p, _back(WEEK_SESSIONS)),
        "1M": _pct(last_p, _on_or_before(last_d.replace(day=1)) if last_d.day == 1
                   else _on_or_before(_date(last_d.year if last_d.month > 1 else last_d.year - 1,
                                            last_d.month - 1 if last_d.month > 1 else 12, min(last_d.day, 28)))),
        "3M": _pct(last_p, _on_or_before(_date(last_d.year - (1 if last_d.month <= 3 else 0),
                                               (last_d.month - 3 - 1) % 12 + 1, min(last_d.day, 28)))),
        "6M": _pct(last_p, _on_or_before(_date(last_d.year - (1 if last_d.month <= 6 else 0),
                                               (last_d.month - 6 - 1) % 12 + 1, min(last_d.day, 28)))),
        "YTD": _pct(last_p, _on_or_before(jan1)),
        "1Y": _pct(last_p, _on_or_before(_date(last_d.year - 1, last_d.month, min(last_d.day, 28)))),
    }

    yr = [p for d, p in hist if (last_d - d).days <= 365]
    hi52, lo52 = (max(yr), min(yr)) if yr else (None, None)

    # The drawer's chart: intraday for the covered symbols, daily-only for the five EOD ones —
    # the spec is explicit that these are different series, not one with a gap.
    series, series_kind = [], "daily"
    if symbol not in NO_INTRADAY_FEED:   # cc#852
        # cc#848: same column fix as build_strip — global_intraday is OHLC, the series reads CLOSE.
        # The INDIAVIX branch below already read `close` (from intraday_prices) and was the working
        # reference that made the mismatch obvious.
        try:
            cur.execute("""SELECT ts, close FROM global_intraday
                           WHERE symbol=%s AND close IS NOT NULL ORDER BY ts""", (symbol,))
            series = [{"t": str(r[0]), "p": float(r[1])} for r in cur.fetchall()]
            if series:
                series_kind = "intraday"
        except Exception as e:
            log.warning("cc#848 intraday drawer series for %s, falling back to daily: %s", symbol, e)
            cur.connection.rollback()
            series = []
    if symbol == "INDIAVIX":
        try:
            cur.execute("""SELECT ts, close FROM intraday_prices
                           WHERE symbol='INDIAVIX' AND close IS NOT NULL
                             AND ts >= NOW() - INTERVAL '8 days' ORDER BY ts""")
            s = [{"t": str(r[0]), "p": float(r[1])} for r in cur.fetchall()]
            if s:
                series, series_kind = s, "intraday"
        except Exception as e:
            log.warning("cc#842 INDIAVIX drawer series: %s", e)
    if not series:
        series = [{"t": str(d), "p": p} for d, p in hist[-260:]]
        series_kind = "daily"

    return {
        "symbol": symbol, "name": name, "category": cat,
        "price": last_p, "as_of": str(last_d),
        "returns": rets,
        "high_52w": hi52, "low_52w": lo52,
        "from_52w_high_pct": _pct(last_p, hi52),
        "sessions_in_52w": len(yr),
        "series": series, "series_kind": series_kind,
        "inverted": symbol in INVERTED,
        "eod_only": symbol in NO_INTRADAY_FEED,   # cc#852: no wired 5m feed
        "basis": "global_indices daily history" + (" + global_intraday" if series_kind == "intraday" else ""),
    }


# ── cc#849 CHART SERIES — 5m / 1D / 1W / 1M ───────────────────────────────────────────────────
# Feeds TradingView LIGHTWEIGHT CHARTS (v4.1.3, MIT), the library already vendored in this repo by
# scorr_chart_card.js / v8_dashboard / v10_dashboard / scorr_holdings — reused, not a second lib.
# Deliberately NOT the TradingView embeddable widget: that renders TradingView's own market data,
# which would print a different Nikkei inside the drawer than the tile shows beside it — a second
# source of truth, directly against the digest's per-tile-honesty thesis.
#
# THE ONE RULE HERE: DO NOT FABRICATE OHLC. global_indices carries price / prev_close / chg_pct and
# NO open-high-low. So 1D/1W/1M are honest CLOSE LINES, never candles built by pretending
# open=high=low=close. Only 5m is candles, because global_intraday genuinely stores OHLC.
#
# TIME BASIS. global_intraday.ts is naive IST wall-clock (verified: max ts sits ~2 min behind
# NOW() AT TIME ZONE 'Asia/Kolkata', and ~-328 min against plain NOW()). Lightweight Charts plots
# UNIX seconds on a UTC axis, so intraday bars are emitted as the IST wall-clock reinterpreted as
# epoch seconds — which makes the rendered axis read IST, matching every other time on the page.
# Daily/weekly/monthly use LWC business-day strings ('YYYY-MM-DD') and need no such handling.

_TF = ("5m", "1D", "1W", "1M")
_EPOCH_BASIS = "1970-01-01"


def build_chart(cur, symbol: str, tf: str) -> Dict[str, Any]:
    sym = (symbol or "").strip()
    tf = (tf or "1D").strip()
    if tf not in _TF:
        return {"error": f"unsupported timeframe: {tf}", "timeframes": list(_TF)}

    cur.execute("""SELECT name, category FROM global_indices WHERE symbol=%s
                   ORDER BY quote_date DESC LIMIT 1""", (sym,))
    head = cur.fetchone()
    if not head:
        return {"error": f"unknown symbol: {sym}"}
    name, cat = head
    common = {"symbol": sym, "name": name, "category": cat, "timeframe": tf,
              "inverted": sym in INVERTED, "has_intraday": sym not in NO_INTRADAY_FEED}

    if tf == "5m":
        # Never silently fall back to daily here. A disabled button plus a plain reason is the
        # honest answer; a daily series relabelled "5m" is not.
        if sym in NO_INTRADAY_FEED:
            return dict(common, kind="unavailable", bars=[], basis=None,
                        reason="No intraday feed for this market — 5-minute candles are not available.")
        cur.execute("""SELECT ts, open, high, low, close FROM global_intraday
                       WHERE symbol=%s AND close IS NOT NULL ORDER BY ts""", (sym,))
        bars = []
        for ts, o, h, l, c in cur.fetchall():
            cf = _f(c)
            if cf is None:
                continue
            # Any missing leg degrades that ONE bar to a flat close, rather than dropping the bar
            # or inventing a range.
            bars.append({"time": int(ts.timestamp()) + _ist_shift(ts),
                         "open": _f(o) if o is not None else cf,
                         "high": _f(h) if h is not None else cf,
                         "low": _f(l) if l is not None else cf,
                         "close": cf})
        return dict(common, kind="candles", bars=bars, count=len(bars),
                    basis="5-minute candles from global_intraday OHLC (~8-day rolling window) · IST",
                    depth_note="global_intraday keeps roughly 8 days of 5-minute bars.")

    if tf == "1D":
        cur.execute("""SELECT quote_date, price FROM global_indices
                       WHERE symbol=%s AND price IS NOT NULL ORDER BY quote_date""", (sym,))
        rows = cur.fetchall()
        basis = "daily close from global_indices — close-line (no OHLC is stored for daily)"
    elif tf == "1W":
        cur.execute("""SELECT DISTINCT ON (date_trunc('week', quote_date))
                              quote_date, price
                       FROM global_indices WHERE symbol=%s AND price IS NOT NULL
                       ORDER BY date_trunc('week', quote_date), quote_date DESC""", (sym,))
        rows = sorted(cur.fetchall(), key=lambda r: r[0])
        basis = "weekly close-line — last close of each ISO week, resampled from the daily series"
    else:  # 1M
        cur.execute("""SELECT DISTINCT ON (date_trunc('month', quote_date))
                              quote_date, price
                       FROM global_indices WHERE symbol=%s AND price IS NOT NULL
                       ORDER BY date_trunc('month', quote_date), quote_date DESC""", (sym,))
        rows = sorted(cur.fetchall(), key=lambda r: r[0])
        basis = "monthly close-line — last close of each calendar month, resampled from the daily series"

    bars = [{"time": str(d), "value": _f(p)} for d, p in rows if _f(p) is not None]
    if not bars:
        return dict(common, kind="unavailable", bars=[], basis=basis,
                    reason=f"No daily history stored for {sym}.")
    return dict(common, kind="line", bars=bars, count=len(bars), basis=basis)


def _ist_shift(ts) -> int:
    """Bars are stored as naive IST wall-clock. `datetime.timestamp()` on a naive value interprets
    it in the SERVER's local zone, so the offset is corrected back out here — the number emitted is
    the IST wall-clock read as epoch seconds, which is what makes the Lightweight Charts UTC axis
    print IST. Deriving the correction rather than hardcoding 19800 keeps it right whether the
    container runs on UTC or on IST."""
    off = ts.astimezone().utcoffset()
    return int(off.total_seconds()) if off else 0


@router.get("/api/global/chart/{symbol:path}")
def global_chart(symbol: str, tf: str = "1D"):
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_chart(cur, symbol, tf)
    except Exception as e:
        log.exception("global chart failed")
        return {"symbol": symbol, "timeframe": tf, "kind": "unavailable", "bars": [],
                "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.get("/api/global/heatstrip")
def heatstrip():
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_strip(cur)
    except Exception as e:
        log.exception("heatstrip failed")
        return {"tiles": [], "eod_tiles": [], "error": f"{type(e).__name__}: {str(e)[:200]}"}


@router.get("/api/global/heatstrip/{symbol:path}")
def heatstrip_detail(symbol: str):
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_detail(cur, symbol)
    except Exception as e:
        log.exception("heatstrip detail failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
