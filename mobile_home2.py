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
  * Live News: reading.items widened to 2 editorials + 8 shorts for the scrollable card.
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

# Ticker order: the market experience reads domestic first, then world indices, then the
# commodity/currency/crypto tail — same families the Daily Digest global section carries.
_TICKER_ORDER = ["index", "volatility", "commodity", "currency", "crypto"]


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
    return [
        {"name": "Nifty", "state": word("NIFTY50")},
        {"name": "Bank Nifty", "state": word("BANKNIFTY")},
    ]


@router.get("/api/mobile/trends")
@_json_safe
def mobile_trends(request: Request, kind: str = "adr", days: int = 30):
    """Uniform daily series for the Home chip chart popups. One shape, three sources:
      adr -> adr_daily.adr                  (price_date)
      pcr -> pcr_daily.pcr, NIFTY           (price_date)
      vix -> global_indices.price, name='India VIX' (quote_date IS the daily history axis)
    Returns {kind, series:[{d:'YYYY-MM-DD', v:float}], latest} oldest-first. Empty source
    returns series:[] — the popup states 'no data', never draws a fake line."""
    g = _guard(request)
    if g:
        return g
    kind = (kind or "adr").lower()
    days = max(2, min(int(days or 30), 120))
    with _conn() as conn, conn.cursor() as cur:
        if kind == "pcr":
            cur.execute("""
                SELECT price_date AS d, pcr AS v FROM pcr_daily
                WHERE underlying = 'NIFTY' AND pcr IS NOT NULL
                ORDER BY price_date DESC LIMIT %s
            """, (days,))
        elif kind == "vix":
            cur.execute("""
                SELECT quote_date AS d, price AS v FROM global_indices
                WHERE name = 'India VIX' AND price IS NOT NULL
                ORDER BY quote_date DESC LIMIT %s
            """, (days,))
        else:
            kind = "adr"
            cur.execute("""
                SELECT price_date AS d, adr AS v FROM adr_daily
                WHERE adr IS NOT NULL
                ORDER BY price_date DESC LIMIT %s
            """, (days,))
        rows = _rows(cur)
    series = [{"d": str(r["d"]), "v": float(r["v"])} for r in reversed(rows)]
    return {"kind": kind, "series": series,
            "latest": series[-1]["v"] if series else None}


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

        # 0b · V10 index state for the hero line
        v10 = _v10_state(cur)

        # 0c · PCR + VIX latest for the hero chips (server-side; frontend never guesses keys)
        cur.execute("""
            SELECT pcr FROM pcr_daily WHERE underlying='NIFTY' AND pcr IS NOT NULL
            ORDER BY price_date DESC LIMIT 1
        """)
        _p = cur.fetchone()
        pcr_latest = float(_p[0]) if _p and _p[0] is not None else None
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
        cur.execute("""
            SELECT COUNT(*) AS trades, COALESCE(SUM(pnl), 0) AS gross,
                   COUNT(*) FILTER (WHERE result = 'TARGET') AS wins,
                   COUNT(*) FILTER (WHERE result = 'SL') AS losses
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

        # 4 · Live News (founder 08-Aug): scrollable card — 2 editorials + 8 newest shorts, 24h
        cur.execute("""
            (SELECT headline, category, display_time FROM v_polished_articles
             WHERE display_time >= NOW() - INTERVAL '24 hours' AND category = 'AI Editorial'
             ORDER BY display_time DESC LIMIT 2)
            UNION ALL
            (SELECT headline, category, display_time FROM v_polished_articles
             WHERE display_time >= NOW() - INTERVAL '24 hours' AND category <> 'AI Editorial'
             ORDER BY display_time DESC LIMIT 8)
        """)
        reads = _rows(cur)
        cur.execute("""
            SELECT COUNT(*) FROM v_polished_articles
            WHERE display_time >= NOW() - INTERVAL '24 hours'
        """)
        news_24h = cur.fetchone()[0]

    def f(v):
        return float(v) if v is not None else None

    # ── ticker: NIFTY/BANKNIFTY first (live), then the global tail in family order ─────────
    ticker = []
    for k, v in (idx.get("indices") or {}).items():
        if isinstance(v, dict):
            ticker.append({"name": "NIFTY" if k == "NIFTY50" else "BANKNIFTY",
                           "price": v.get("close"), "chg_pct": v.get("chg_pct"),
                           "category": "domestic"})
    _fam = {c: i for i, c in enumerate(_TICKER_ORDER)}
    for r in sorted(glob_rows, key=lambda r: (_fam.get(r["category"], 99), r["name"])):
        ticker.append({"name": r["name"], "price": f(r["price"]),
                       "chg_pct": f(r["chg_pct"]), "category": r["category"]})

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
                "when": r["display_time"].strftime("%H:%M") if r["display_time"] else None,
            } for r in reads],
        },
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
