"""
mobile_home2.py — cc#889 HOME v2 aggregate endpoint (MOBILE_REBUILD_IN_PLACE_V1, session_log 17782).

ONE call serves the whole home_v2 screen (previews/home_v2.html, founder-directed 07-Aug:
market-first scroll, My Portfolio demoted mid-scroll, tools grid). The template makes exactly one
fetch; every section below is one query (mobile_endpoints born-clean rule).

OWN FILE, OWN ROUTER — pushed by Claude (Fable) under CHARTER_OVERRIDE_08AUG2026 (session_log
17783). Helpers are IMPORTED from mobile_endpoints, never duplicated: rail_state, basket_label,
_conn/_rows/_ist_now/_guard/_json_safe are the one implementation both files share. No circular
import: mobile_endpoints never imports this module; the wiring shim (preview_endpoints.py) does.

DATA DOCTRINE, inherited:
  * smartgain_holdings.updated_at is TIMESTAMPTZ -> converted in SQL (cc#887 class).
  * v8_qualified.signal_ts and intraday_prices.ts are NAIVE IST -> read raw, never converted.
  * market_mood() real keys: mood / fails / checks / checked_at / adr_detail (cc#888 finding) —
    hero chips are built from checks[] as returned, never from invented key names.
  * since-% on a signal uses v8_qualified.cmp (price AT signal) vs the live LATERAL close, with
    the sign flipped for sell_* baskets. Both prices real; nothing derived from a close alone.
  * A section whose source returns nothing states so (empty:true / message) — never a fake zero.
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

        # 2 · open book: live unrealised over OPEN positions (same LATERAL resolver as the
        #     positions screen, so the two surfaces cannot disagree), realised + W/L from trades
        cur.execute("""
            SELECT COALESCE(SUM(
                       (COALESCE(lp.cmp, p.entry_price) - p.entry_price) * p.qty *
                       CASE WHEN UPPER(p.side) = 'SHORT' THEN -1 ELSE 1 END
                   ), 0) AS unrealised,
                   COUNT(*) AS open_n
            FROM v8_paper_positions p
            LEFT JOIN LATERAL (
                SELECT close AS cmp FROM intraday_prices
                WHERE symbol = p.symbol AND source <> 'fyers_fut'
                ORDER BY ts DESC LIMIT 1
            ) lp ON true
            WHERE p.status = 'OPEN'
        """)
        book_live = _rows(cur)[0]
        cur.execute("""
            SELECT COUNT(*) AS trades, COALESCE(SUM(pnl), 0) AS realised,
                   COUNT(*) FILTER (WHERE pnl > 0) AS wins,
                   COUNT(*) FILTER (WHERE pnl < 0) AS losses
            FROM v8_paper_trades
        """)
        led = _rows(cur)[0]

        # 3 · my portfolio (SmartGain) — rows + aggregate. TIMESTAMPTZ converted in SQL.
        cur.execute("""
            SELECT symbol, direction, qty, entry_price, ltp, mtm,
                   (updated_at AT TIME ZONE 'Asia/Kolkata') AS updated_at
            FROM smartgain_holdings
            ORDER BY ABS(COALESCE(mtm, 0)) DESC
        """)
        pf_rows = _rows(cur)

        # 4 · worth reading: 2 editorials + 1 newest short, last 24h, canonical view
        cur.execute("""
            (SELECT headline, category, display_time FROM v_polished_articles
             WHERE display_time >= NOW() - INTERVAL '24 hours' AND category = 'AI Editorial'
             ORDER BY display_time DESC LIMIT 2)
            UNION ALL
            (SELECT headline, category, display_time FROM v_polished_articles
             WHERE display_time >= NOW() - INTERVAL '24 hours' AND category <> 'AI Editorial'
             ORDER BY display_time DESC LIMIT 1)
        """)
        reads = _rows(cur)
        cur.execute("""
            SELECT COUNT(*) FROM v_polished_articles
            WHERE display_time >= NOW() - INTERVAL '24 hours'
        """)
        news_24h = cur.fetchone()[0]

        # 5 · results corner: how many companies report today
        cur.execute("""
            SELECT COUNT(*) FROM earnings_calendar
            WHERE ex_date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
        """)
        results_today = cur.fetchone()[0]

    def f(v):
        return float(v) if v is not None else None

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

    return {
        "session": {
            "market_open": market_open,
            "time": now.strftime("%H:%M"),
            "date": now.strftime("%a %d %b"),
            "label": ("Market open" if market_open else "Market closed") + " · " + now.strftime("%H:%M IST"),
        },
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
            "as_of": (mood or {}).get("checked_at"),
        },
        "signals": {
            "today": sig_head["n"],
            "top": signals,
            "rail": rail_state(sig_head["newest"], 5, now, is_td),
        },
        "book": {
            "open": book_live["open_n"],
            "unrealised": round(f(book_live["unrealised"]) or 0.0, 2),
            "realised": round(f(led["realised"]) or 0.0, 2),
            "wins": led["wins"], "losses": led["losses"], "trades": led["trades"],
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
        "results": {"today": results_today},
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
