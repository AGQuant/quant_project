"""
mobile_endpoints.py — cc#874 MOBILE PROMOTION (founder 06-Aug-2026, P1).

THE PROMOTION PRINCIPLE (PREVIEW_PROMOTION_DECISION_V1, session_log 16230): real data NEVER goes
into previews/*.html. Each approved screen is REBUILT here as its own routed template that imports
shared modules. The previews stay frozen dummy forever — they are the founder's review record and
Claude.ai owns them (16159).

SCOPE — 11 WIRABLE IMP SCREENS, NOT 18. session_log 16916 (MOBILE_SCREEN_TRIAGE_V1) is later than
cc#874's filing and names the card explicitly: wire the IMP list; MAY screens keep their previews
and their contract entries but are NOT wired here; scanners waits for its redesigned preview;
v8_surfaces folds under v8/positions. So the MAY five (models, models_tools, holdings, fpc, sector)
and scanners are deliberately not routed.

16916 lists TWELVE IMP items, but symbol_bottom_sheet is marked "NOT YET DESIGNED — design next".
There is no preview to promote, so it is a Claude.ai design dependency and not wirable here.
Founder confirmed the wirable set is ELEVEN (session_log 17021).

ARCHITECTURE, carried forward (17021, and 16915): the mobile app is a LIGHTER SUBSET OF THE ONE WEB
PRODUCT. There is no second dashboard, now or later. The split is depth and presentation, never
accuracy — DISPLAY_PARITY 16202 means the app never shows a DIFFERENT number from the web, only
fewer of them, with the same marker, meaning and colours. Retail language throughout: plain, short.

FIELD NAMES COME FROM ONE PLACE. docs/PREVIEW_DATA_CONTRACT.md (cc#868, commit 115ffa3) is the only
source for endpoint paths and column names. A value that is not in the contract renders `--` with a
title attribute naming the gap; no field name is ever invented.

FOUR TRAPS, EACH OF WHICH HAS ALREADY COST A SESSION:
  a) quant_paper_positions.status is LOWERCASE ('open' / 'exited_stop'). An uppercase filter
     silently returns nothing.
  b) v8_paper_positions.entry_ts is NAIVE IST. Apply NO timezone conversion — the cc#844 phantom
     330-minute class comes from exactly this.
  c) tc_cache has been stale since 18-Jun. The Check screen PRINTS computed_at on the verdict card.
     Stale stated, never dressed as fresh.
  d) v8_filter_state is never read for anything. cc#871 removed its last reader and cc#873
     documented the drift; cc#868 flagged it BLOCKED_SOURCE.

PERFORMANCE — THIS FILE IS BORN CLEAN (cc#869 findings 2+3, enforced by cc#879):
  * every handler is `def`, never `async def` over psycopg. FastAPI runs them in a threadpool.
  * NO DDL anywhere — not at import, not in a handler, not in a startup hook. This module creates
    nothing; it reads what already exists.
  * ONE query per SECTION, not per value.
"""

import os
import functools
import logging
from datetime import datetime, time as dt_time, timedelta, timezone

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from scorr_auth import _is_authed

log = logging.getLogger("scorr.mobile")
router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobile")

MARKET_OPEN = dt_time(9, 15)
EQ_CONTINUOUS_END = dt_time(15, 15)   # cc#855 segment boundaries
SESSION_END = dt_time(15, 40)

# Only used to coerce a stray tz-aware value in rail_state. Every timestamp in this module is
# naive IST by convention; this exists so a forgotten SQL conversion degrades to a warning.
_IST = timezone(timedelta(hours=5, minutes=30))


def _conn():
    return psycopg.connect(DATABASE_URL)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# cc#887 — THE ESCAPE ROUTE, CLOSED ONCE INSTEAD OF ELEVEN TIMES
#
# Every handler here already returns {"error": ...} when its QUERY fails, and mobile/*.html
# renders that as the retry box. But every handler also BUILT ITS RESPONSE outside that try — an
# AST scan found this in all ELEVEN API handlers, not the eight the card listed (mobile_v8,
# mobile_digest and mobile_now were missed). Anything raising while the payload is assembled
# escapes as FastAPI's plain-text 500, the phone's r.json() dies on "Unexpected token I", and the
# failure box that exists for exactly this never renders.
#
# The card asks for a try/except around each return. This decorator is that requirement met once,
# and it is deliberately different from the literal instruction for two reasons: eleven hand-edited
# try blocks is eleven chances to indent one wrong, and — the reason that actually matters — a
# per-handler fix does nothing for the twelfth handler someone adds next month. This is the same
# argument cc#878 made when it put the timeout backstop in one place rather than patching twelve
# call sites.
#
# It returns HTTP 200 with an {"error": ...} body on purpose: that is the shape the in-try path
# already returns and the shape every template's failBox reads. A 500 here would be more correct
# in the abstract and would break every screen that already handles this correctly.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _json_safe(fn):
    """Guarantee a mobile API handler answers with JSON, never a raw 500."""
    @functools.wraps(fn)          # keeps the signature FastAPI introspects for its dependencies
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            log.exception("%s escaped with an unhandled exception", fn.__name__)
            return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return wrapper


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _ist_now() -> datetime:
    """Server IST, naive — the same convention v8_paper_positions.entry_ts already uses (trap b)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT NOW() AT TIME ZONE 'Asia/Kolkata'")
        return cur.fetchone()[0]


# ── state rail (cc#874 item 5, ENGINE_LIVENESS_RULE 13829) ───────────────────────────────────
# Four states told apart by SHAPE, never by opacity alone. Nothing here is hardcoded: the state is
# derived from a real timestamp and the real session clock every time.
def rail_state(last_ts, cadence_min: float, now_ist: datetime, is_trading_day: bool = True) -> dict:
    """LIVE / STALE / CLOSED / REFERENCE from data, never from registration.

    CLOSED beats STALE after the close. A 5-minute engine at 21:00 is not stale, it is shut — and
    calling it stale is the cc#841 false-positive class that lights every surface red every evening.
    """
    t = now_ist.time()
    in_session = is_trading_day and MARKET_OPEN <= t <= SESSION_END
    if last_ts is None:
        return {"state": "CLOSED" if not in_session else "STALE",
                "age_min": None,
                "why": "no data yet today" if in_session else "market closed"}
    # cc#887, defence in depth. Every caller is supposed to hand this a NAIVE IST value, converted
    # in SQL. One did not — smartgain_holdings.updated_at is timestamptz — and the bare TypeError
    # from subtracting an aware value took the whole home screen down with a raw 500. The
    # conversion still belongs in SQL, so this does not silently absorb the mistake: it logs a
    # warning naming the omission and carries on, because a screen that renders while telling us
    # about a bug beats a screen that dies to prove one.
    if getattr(last_ts, "tzinfo", None) is not None:
        log.warning("rail_state got a tz-AWARE timestamp (%s) — convert it in SQL with "
                    "AT TIME ZONE 'Asia/Kolkata'. Coercing to naive IST for this call.", last_ts)
        last_ts = last_ts.astimezone(_IST).replace(tzinfo=None)
    age = (now_ist - last_ts).total_seconds() / 60.0
    if not in_session:
        return {"state": "CLOSED", "age_min": round(age, 1),
                "why": f"market closed · last update {_ago(age)}"}
    if age <= cadence_min * 2:
        return {"state": "LIVE", "age_min": round(age, 1), "why": f"updated {_ago(age)}"}
    return {"state": "STALE", "age_min": round(age, 1),
            "why": f"last update {_ago(age)} — expected every {cadence_min:g} min"}


# ── FEED rail (cc#984) ────────────────────────────────────────────────────────────────────────
# The session clock and the DATA FEED are two different things. On 10-Aug the tape was dead from
# 09:15 to 10:40 (first bar of the day landed at 10:40, verified in intraday_prices) and the app
# said "Market open · 09:40 IST" the whole time — the outage was invisible. This tells the truth
# about the tick feed, and it does it by EXTENDING rail_state, not by cloning it.
FEED_SESSION_END = dt_time(15, 30)   # cash close. Deliberately EARLIER than SESSION_END (15:40):
                                     # the fut leg's 15:30-15:40 tail is thin, and an amber badge
                                     # in that tail would be the cc#841 false positive again.
                                     # CLOSED beats STALE at the boundary.
FEED_WARM_UNTIL  = dt_time(9, 25)    # first 5-min bucket + margin
FEED_AUCTION_FROM = dt_time(15, 15)  # cc#912/980 auction window ...
FEED_AUCTION_TO   = dt_time(15, 45)  # ... 15:15-15:35, plus the 10-min freshness lookback

# The live 5-min legs. fyers_fut_rest is EXCLUDED on purpose: it is the REST-degraded fallback
# (cc#883/884) on its own slower cadence, so counting it would report a healthy feed while the
# websocket is down — the exact failure this badge exists to catch.
FEED_SOURCES = ("fyers_eq", "fyers_fut", "fyers_ext")
# Category I cash between 15:15 and 15:35 has no continuous market; its bars are re-tagged by the
# worker (cc#855). They are still the feed breathing, so they count — but ONLY inside that window,
# which is the same containment rule worker/fyers_feed.py's auction_ok applies. Importing that
# rule from the worker is not possible here (the module opens a websocket at import), so the
# window is restated, not the counting logic: it lives in one SQL predicate below.
FEED_AUCTION_SOURCES = ("fyers_eq_auction", "auction")

_FEED_LABEL = {"LIVE": "Feed live", "STALE": "Feed stale", "CLOSED": "Feed closed"}


def feed_auction_ok(now_ist: datetime, is_trading_day: bool = True) -> bool:
    """Does the freshness window overlap the 15:15-15:35 auction? (cc#912 containment rule.)"""
    return bool(is_trading_day and FEED_AUCTION_FROM <= now_ist.time() <= FEED_AUCTION_TO)


def feed_rail(last_ts, now_ist: datetime, is_trading_day: bool = True,
              cadence_min: float = 5.0) -> dict:
    """LIVE / STALE / CLOSED / warming, for the tick feed. Same shape as rail_state plus `label`.

    Reuses rail_state for the state machine. The feed session is narrowed to 09:15-15:30 by
    handing rail_state `in_session` as its trading-day flag — an intersection, not a second copy
    of the clock. Two things are added on top:
      * the WARMING grace (founder amendment 10-Aug): before 09:25 no bucket has closed yet, so
        an empty MAX(ts) is normal, not a fault. Amber there would cry wolf every morning.
      * a plain-language `label`, because "REFERENCE" means nothing to a reader.
    """
    t = now_ist.time()
    in_session = bool(is_trading_day and MARKET_OPEN <= t <= FEED_SESSION_END)
    if in_session and t < FEED_WARM_UNTIL and (
            last_ts is None or getattr(last_ts, "date", lambda: None)() != now_ist.date()):
        return {"state": "REFERENCE", "label": "Feed warming", "age_min": None,
                "why": "session just opened · first 5-min bar due by 09:25"}
    r = rail_state(last_ts, cadence_min, now_ist, in_session)
    r["label"] = _FEED_LABEL.get(r["state"], "Feed " + r["state"].lower())
    return r


def _ago(m) -> str:
    if m is None:
        return "never"
    if m < 1:
        return "just now"
    if m < 60:
        return f"{int(m)}m ago"
    if m < 1440:
        return f"{m/60:.0f}h ago"
    return f"{m/1440:.0f}d ago"


def _page(name: str) -> HTMLResponse:
    """Serve a promoted template. No caching — these change per deploy and a stale shell is the
    cc#867 class."""
    path = os.path.join(TEMPLATE_DIR, name + ".html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return HTMLResponse(f"Mobile screen '{name}' is not wired yet.", status_code=404,
                            headers={"Cache-Control": "no-store"})


def _guard(request: Request):
    """401 JSON rather than a redirect — these are XHR endpoints behind a rendered page."""
    if not _is_authed(request):
        return JSONResponse({"error": "unauthorized", "login_url": "/login"}, status_code=401)
    return None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# INTEL — contract: 12 values, 12 SOURCED, 0 empty, 0 unsourced. The cleanest screen in the set.
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/intel")
@_json_safe   # cc#887
def mobile_intel(request: Request, hours: int = 48, limit: int = 40, cursor: str = ""):
    """ONE call for the whole screen: header, chips and both content sections (item 6).

    Reads v_polished_articles, the canonical view — so cc#870's suppression is inherited for free
    and a culled story cannot reappear here.
    TRAP (e): the sort key is published_time, which IS the polish time (spec 8188). Sorting on the
    raw article date would bury a story polished today under one published last week.
    """
    g = _guard(request)
    if g:
        return g
    hours = max(1, min(hours, 168))
    limit = max(1, min(limit, 100))
    # ── cc#983: KEYSET CURSOR, not OFFSET ────────────────────────────────────────────────────
    # The sort key is (display_time DESC, polished_id DESC). An OFFSET would silently skip or
    # repeat rows the moment a new story is polished mid-scroll — every later page shifts by one.
    # A keyset cursor is anchored to the last row the client actually holds, so a story arriving
    # during a scroll cannot corrupt the page after it. Format "<iso>|<id>", opaque to the client:
    # it only ever echoes back what the previous response handed it.
    cur_time = cur_id = None
    if cursor:
        try:
            _t, _i = str(cursor).rsplit("|", 1)
            cur_time, cur_id = _t, int(_i)
        except Exception:
            # a malformed cursor must not silently serve page 1 again — that is an infinite
            # scroll that never advances and looks like duplicate content.
            return {"error": "bad cursor", "editorials": [], "feed": [], "has_more": False}
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS n, MAX(display_time) AS newest
                FROM v_polished_articles
                WHERE display_time >= NOW() - (%s || ' hours')::interval
            """, (hours,))
            head = _rows(cur)[0]
            cur.execute("""
                SELECT category, COUNT(*) AS n FROM v_polished_articles
                WHERE display_time >= NOW() - (%s || ' hours')::interval
                GROUP BY category ORDER BY n DESC
            """, (hours,))
            chips = _rows(cur)
            # +1 row is fetched to answer has_more without a second COUNT: if the extra row
            # comes back there is another page, and it is trimmed before shaping.
            if cur_time is None:
                cur.execute("""
                    SELECT polished_id, headline, summary, full_summary, category, sentiment, impact,
                           mentioned_symbols, source_name, display_time
                    FROM v_polished_articles
                    WHERE display_time >= NOW() - (%s || ' hours')::interval
                    ORDER BY display_time DESC NULLS LAST, polished_id DESC
                    LIMIT %s
                """, (hours, limit + 1))
            else:
                # written as two comparisons rather than a row-constructor so it cannot trip on
                # the column's exact timestamp type. display_time is never NULL inside this
                # window — the WHERE above already excludes it — so NULLS LAST cannot bite here.
                cur.execute("""
                    SELECT polished_id, headline, summary, full_summary, category, sentiment, impact,
                           mentioned_symbols, source_name, display_time
                    FROM v_polished_articles
                    WHERE display_time >= NOW() - (%s || ' hours')::interval
                      AND (display_time < %s::timestamptz
                           OR (display_time = %s::timestamptz AND polished_id < %s))
                    ORDER BY display_time DESC NULLS LAST, polished_id DESC
                    LIMIT %s
                """, (hours, cur_time, cur_time, cur_id, limit + 1))
            arts = _rows(cur)
            has_more = len(arts) > limit
            arts = arts[:limit]
    except Exception as e:
        log.exception("mobile intel failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "articles": [], "count": 0}

    def shape(a):
        return {
            "id": a["polished_id"],
            "headline": a["headline"],
            "summary": a["summary"],
            "body": a["full_summary"],
            "category": a["category"],
            "sentiment": (a["sentiment"] or "").upper() or None,
            "impact": (a["impact"] or "").upper() or None,
            "symbols": a["mentioned_symbols"] or [],
            "source": a["source_name"],
            "when": a["display_time"].strftime("%H:%M") if a["display_time"] else None,
            "when_full": a["display_time"].strftime("%d-%b %H:%M IST") if a["display_time"] else None,
        }

    editorials = [shape(a) for a in arts if (a["category"] or "") == "AI Editorial"]
    feed = [shape(a) for a in arts if (a["category"] or "") != "AI Editorial"]
    newest = head["newest"]
    return {
        "header": {
            # The age of the NEWEST HEADLINE, never a refresh clock — a dead feed still shows a
            # fresh clock, but it can never show a fresh headline (16170).
            "newest": newest.strftime("%H:%M IST") if newest else None,
            "count": head["n"],
            "hours": hours,
        },
        "chips": [{"label": c["category"] or "Uncategorised", "n": c["n"]} for c in chips],
        "editorials": editorials,
        "feed": feed,
        "shown": len(editorials) + len(feed),
        # cc#983: the cursor for the NEXT page, built from the last row actually returned. None
        # when there is nothing further, so the client stops observing instead of spinning.
        "next_cursor": (f"{arts[-1]['display_time'].isoformat()}|{arts[-1]['polished_id']}"
                        if (arts and has_more) else None),
        "has_more": has_more,
        "total": head["n"],
        # kept for the first paint; the client recomputes it as pages append (item 5), because
        # after a scroll this string would otherwise still claim "showing 40".
        "coverage": f"showing {len(editorials) + len(feed)} of {head['n']}",
    }


@router.get("/m/intel", response_class=HTMLResponse)
def m_intel():
    return _page("intel")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# V8 POSITIONS — contract: 14 values, 13 SOURCED, 1 SOURCED_BUT_EMPTY (the pivot star).
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/v8_positions")
@_json_safe   # cc#887
def mobile_v8_positions(request: Request):
    """The open book. ONE query for the rows, one for the star, one for the rail.

    TRAP (b): entry_ts is NAIVE IST and is read raw — no AT TIME ZONE anywhere in this function.
    TRAP (d): v8_filter_state is not read. A basket's presence here comes from the trades
    themselves, never from that table.
    """
    g = _guard(request)
    if g:
        return g
    try:
        now = _ist_now()
        with _conn() as conn, conn.cursor() as cur:
            # CMP is NOT a column on v8_paper_positions — it is resolved by a LATERAL against
            # the newest intraday bar, exactly as main.py:1620 does for the desktop book. Same
            # resolver, same source exclusion (fyers_fut is the futures leg, not the cash price),
            # so the two surfaces can never disagree about a price.
            cur.execute("""
                SELECT p.symbol, p.side, p.basket, p.entry_price, p.entry_ts,
                       p.target, p.stop_loss, p.qty, p.pivot_date,
                       COALESCE(lp.cmp, p.entry_price) AS cmp, lp.ts AS cmp_updated_at
                FROM v8_paper_positions p
                LEFT JOIN LATERAL (
                    SELECT close AS cmp, ts FROM intraday_prices
                    WHERE symbol = p.symbol AND source <> 'fyers_fut' ORDER BY ts DESC LIMIT 1
                ) lp ON true
                WHERE p.status = 'OPEN'
                ORDER BY p.entry_ts DESC
            """)
            pos = _rows(cur)
            # The pivot star. SOURCED_BUT_EMPTY per the contract — cc#856 shipped the engine and
            # bg_pivot_star runs, but the table is empty until the rule first fires. Empty renders
            # as no star, never as a spinner and never as a fake one.
            cur.execute("""
                SELECT symbol, star_color FROM v8_pivot_star_log
                WHERE star_date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
            """)
            stars = {r["symbol"]: r["star_color"] for r in _rows(cur)}
    except Exception as e:
        log.exception("mobile v8_positions failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "positions": [], "count": 0}

    out, net = [], 0.0
    newest_tick = None
    for p in pos:
        entry = float(p["entry_price"]) if p["entry_price"] is not None else None
        cmp_ = float(p["cmp"]) if p["cmp"] is not None else None
        qty = float(p["qty"]) if p["qty"] is not None else None
        pnl = ret = None
        if entry is not None and cmp_ is not None and qty is not None:
            direction = -1.0 if (p["side"] or "").upper() == "SHORT" else 1.0
            pnl = (cmp_ - entry) * qty * direction
            ret = ((cmp_ - entry) / entry * 100.0 * direction) if entry else None
            net += pnl
        if p["cmp_updated_at"] and (newest_tick is None or p["cmp_updated_at"] > newest_tick):
            newest_tick = p["cmp_updated_at"]
        out.append({
            "symbol": p["symbol"], "side": (p["side"] or "").upper(), "basket": p["basket"],
            "entry": entry, "cmp": cmp_, "qty": qty,
            "target": float(p["target"]) if p["target"] is not None else None,
            "stop": float(p["stop_loss"]) if p["stop_loss"] is not None else None,
            "pnl": round(pnl, 2) if pnl is not None else None,
            "ret_pct": round(ret, 2) if ret is not None else None,
            # entry_ts is naive IST — formatted, never converted (trap b).
            "entry_ts": p["entry_ts"].strftime("%d %b %H:%M") if p["entry_ts"] else None,
            "is_new": bool(p["entry_ts"] and p["entry_ts"].date() == now.date()),
            "star": stars.get(p["symbol"]),      # None = no star today; the designed empty state
        })
    return {
        "positions": out,
        "count": len(out),
        "net_pnl": round(net, 2) if out else None,
        "star_source_empty": not stars,
        "rail": rail_state(newest_tick, 5, now),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/m/positions", response_class=HTMLResponse)
def m_positions():
    return _page("positions")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# QUANT BASKETS — 16916 calls this "ready-made baskets, easiest retail product".
# ══════════════════════════════════════════════════════════════════════════════════════════════
BASKET_LABELS = {
    # cc#874: slug -> human name, mapped ONCE here and shared (cc#880 item 4 asks for exactly this
    # single mapping point, so the mobile and web surfaces cannot drift apart on a label).
    "large_cap": "Large Cap", "mid_cap": "Mid Cap", "small_cap": "Small Cap",
    "alpha_multicap": "Alpha Multicap", "breakout_52w": "52w Breakout",
    "contra_value": "Contra Value",
    "buy_reversal": "Buy Reversal", "buy_momentum": "Buy Momentum",
    "sell_reversal": "Sell Reversal", "sell_momentum": "Sell Momentum",
    "buy_s1_bounce": "S1 Bounce", "sell_overbought": "Sell Overbought",
}


def basket_label(slug):
    """Human name for a basket slug. An UNKNOWN slug degrades to a de-slugged version rather than
    to '--': the basket genuinely exists in the data, we just have not named it yet, and hiding a
    real basket behind a dash would be worse than showing an imperfect label."""
    if not slug:
        return "--"
    return BASKET_LABELS.get(slug) or slug.replace("_", " ").title()


@router.get("/api/mobile/qb")
@_json_safe   # cc#887
def mobile_qb(request: Request):
    """Quant Baskets — one row per basket, plus its open holdings.

    TRAP (a): quant_paper_positions.status is LOWERCASE ('open' / 'exited_stop'). Verified live:
    62 open / 22 exited_stop. An uppercase filter here returns silently nothing, which is the
    worst kind of wrong — a real book that renders as an empty one.
    """
    g = _guard(request)
    if g:
        return g
    try:
        now = _ist_now()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT basket_name,
                       COUNT(*)                                   AS positions,
                       SUM(current_value)                         AS market_value,
                       SUM(pnl)                                   AS unrealised,
                       SUM(qty * entry_price)                     AS invested,
                       MAX(updated_at)                            AS updated_at
                FROM quant_paper_positions
                WHERE status = 'open'
                GROUP BY basket_name
                ORDER BY SUM(current_value) DESC NULLS LAST
            """)
            baskets = _rows(cur)
            cur.execute("""
                SELECT basket_name, symbol, qty, entry_price, current_price, pnl, pnl_pct,
                       current_value, entry_date
                FROM quant_paper_positions
                WHERE status = 'open'
                ORDER BY basket_name, current_value DESC NULLS LAST
            """)
            holdings = _rows(cur)
    except Exception as e:
        log.exception("mobile qb failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "baskets": [], "count": 0}

    def f(v):
        return float(v) if v is not None else None

    by_basket = {}
    for h in holdings:
        by_basket.setdefault(h["basket_name"], []).append({
            "symbol": h["symbol"], "qty": f(h["qty"]),
            "entry": f(h["entry_price"]), "cmp": f(h["current_price"]),
            "pnl": f(h["pnl"]), "pnl_pct": f(h["pnl_pct"]),
            "value": f(h["current_value"]),
            "since": h["entry_date"].strftime("%d %b") if h["entry_date"] else None,
        })

    newest = max([b["updated_at"] for b in baskets if b["updated_at"]], default=None)
    out = []
    for b in baskets:
        inv, mv = f(b["invested"]), f(b["market_value"])
        out.append({
            "slug": b["basket_name"], "label": basket_label(b["basket_name"]),
            "positions": b["positions"], "market_value": mv,
            "unrealised": f(b["unrealised"]),
            "invested": inv,
            "ret_pct": round((mv - inv) / inv * 100.0, 2) if inv else None,
            "holdings": by_basket.get(b["basket_name"], []),
        })
    tot_mv = sum(x["market_value"] or 0 for x in out)
    tot_un = sum(x["unrealised"] or 0 for x in out)
    return {
        "baskets": out,
        "count": len(out),
        "total_value": round(tot_mv, 2) if out else None,
        "total_unrealised": round(tot_un, 2) if out else None,
        # Baskets are marked once a day, not every 5 minutes — the rail cadence says so rather
        # than judging a daily job against an intraday clock (the cc#841 false-positive class).
        "rail": rail_state(newest, 1440, now),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/m/qb", response_class=HTMLResponse)
def m_qb():
    return _page("qb")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# GVM — 16916: "score+verdict, most retail-friendly surface".
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/gvm")
@_json_safe   # cc#887
def mobile_gvm(request: Request, q: str = "", limit: int = 25):
    """Search or top-ranked. ONE query either way.

    gvm_score is the EOD frozen value (V8 architecture, locked 18-Jun) — the rail says CLOSED
    rather than STALE out of hours, because a nightly score is not late at lunchtime.
    """
    g = _guard(request)
    if g:
        return g
    limit = max(1, min(limit, 50))
    q = (q or "").strip().upper()
    try:
        now = _ist_now()
        with _conn() as conn, conn.cursor() as cur:
            # cc#888 item 4 — RANK IS DERIVED, because the stored column is empty. gvm_scores.rank
            # is NULL on ALL 1,793 rows, so every card printed "Rank --". The number is trivially
            # computable and the ordering is already the product's own.
            #
            # The window sits in a CTE that runs over the WHOLE table BEFORE any filter. That
            # matters for search: ranking after filtering would tell a searched company it is
            # rank 1 of the two rows that matched, which is a confident lie. This way it gets its
            # true position out of 1,793.
            # Verified single-date: gvm_scores holds one row per symbol for one score_date, so the
            # window needs no date partition. If history is ever added this needs PARTITION BY
            # score_date — noted here rather than left to be discovered.
            ranked = """
                WITH r AS (
                    SELECT symbol, company_name, gvm_score, g_score, v_score, m_score, verdict,
                           gvm_overall_label, growth_label, value_label, momentum_label,
                           segment, price, punchline, score_date,
                           RANK() OVER (ORDER BY gvm_score DESC NULLS LAST) AS rank
                    FROM gvm_scores
                )
                SELECT * FROM r
            """
            if q:
                cur.execute(ranked + """
                    WHERE symbol LIKE %s OR UPPER(company_name) LIKE %s
                    ORDER BY (symbol = %s) DESC, gvm_score DESC NULLS LAST
                    LIMIT %s
                """, (f"{q}%", f"%{q}%", q, limit))
            else:
                cur.execute(ranked + """
                    ORDER BY rank
                    LIMIT %s
                """, (limit,))
            rows = _rows(cur)
    except Exception as e:
        log.exception("mobile gvm failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "results": [], "count": 0}

    def f(v):
        return float(v) if v is not None else None

    newest = max([r["score_date"] for r in rows if r["score_date"]], default=None)
    newest_dt = datetime.combine(newest, dt_time(22, 0)) if newest else None
    return {
        "query": q or None,
        "results": [{
            "symbol": r["symbol"], "name": r["company_name"],
            "gvm": f(r["gvm_score"]), "g": f(r["g_score"]), "v": f(r["v_score"]), "m": f(r["m_score"]),
            "verdict": r["verdict"], "label": r["gvm_overall_label"],
            "growth": r["growth_label"], "value": r["value_label"], "momentum": r["momentum_label"],
            "segment": r["segment"], "price": f(r["price"]), "rank": r["rank"],
            "punchline": r["punchline"],
            "as_of": r["score_date"].strftime("%d %b") if r["score_date"] else None,
        } for r in rows],
        "count": len(rows),
        # cadence 1440: GVM is scored nightly at 22:00 (V8 architecture, EOD frozen).
        "rail": rail_state(newest_dt, 1440, now),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/m/gvm", response_class=HTMLResponse)
def m_gvm():
    return _page("gvm")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# V8 — the signal surface. 16916: "signal surface, approved".
# DISPLAY_PARITY 16202 is enforced BY CONSTRUCTION here: the mood gate reuses v8_endpoints
# .market_mood() rather than re-deriving it. Two implementations of one number is how the app and
# the web start disagreeing, and the rule says they never may.
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/v8")
@_json_safe   # cc#887
def mobile_v8(request: Request):
    g = _guard(request)
    if g:
        return g
    mood = None
    try:
        from v8_endpoints import market_mood
        mood = market_mood()
    except Exception as e:
        log.warning("mobile v8: market_mood unavailable (%s)", e)
    try:
        now = _ist_now()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM futures_universe WHERE is_active")
            universe = cur.fetchone()[0]
            # Basket counts come from v8_qualified — what actually signalled today. NEVER from
            # v8_filter_state (trap d): cc#868 flagged it BLOCKED_SOURCE because all six baskets
            # read disabled while four were producing signals.
            cur.execute("""
                SELECT basket, COUNT(*) AS n, MAX(signal_ts) AS newest
                FROM v8_qualified
                WHERE signal_date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                GROUP BY basket ORDER BY n DESC
            """)
            baskets = _rows(cur)
            # cc#888 item 3: WHICH stocks signalled, not just how many. A count answers a
            # dashboard question; a retail user wants the names. Capped at 20 per basket in SQL
            # rather than in Python, so a heavy day never ships 400 rows to a phone to throw most
            # of them away.
            # signal_ts is NAIVE IST (verified: timestamp without time zone) — no conversion, and
            # the format is applied here so the template never parses a timestamp.
            cur.execute("""
                SELECT basket, symbol, signal_ts FROM (
                    SELECT basket, symbol, signal_ts,
                           ROW_NUMBER() OVER (PARTITION BY basket ORDER BY signal_ts DESC) AS rn
                    FROM v8_qualified
                    WHERE signal_date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                ) s WHERE rn <= 20
                ORDER BY basket, signal_ts DESC
            """)
            sig_rows = _rows(cur)
            cur.execute("SELECT COUNT(*) FROM v8_paper_positions WHERE status='OPEN'")
            open_pos = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) AS n, SUM(pnl) AS pnl FROM v8_paper_trades")
            led = _rows(cur)[0]
    except Exception as e:
        log.exception("mobile v8 failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "baskets": []}

    newest = max([b["newest"] for b in baskets if b["newest"]], default=None)
    return {
        "universe": universe,
        # cc#887 follow-up — THE SAME MISTAKE, QUIETER. This read "verdict", "gate_open" and
        # "as_of"; market_mood() returns NONE of those three. Its actual keys are checked_at,
        # checks, fails, mood, buy_slots, sell_slots, total_slots, slot_note, breadth_source,
        # nifty_source, adr_detail. So the gate card has been printing three nulls since cc#874 —
        # no crash, no error box, just silently empty, which is the worse failure of the two
        # because nothing ever told anyone.
        # Found by checking the source of every endpoint this module reuses after the home screen
        # turned out to have assumed a shape too. `open` is derived: the gate is open when no
        # check failed. fails is a count, so a falsy 0 means open — written explicitly rather than
        # with `not fails`, because `not None` would also read as open if the key ever vanished.
        "gate": {
            "verdict": (mood or {}).get("mood"),
            "open": ((mood or {}).get("fails") == 0) if (mood or {}).get("fails") is not None else None,
            "as_of": (mood or {}).get("checked_at"),
        } if mood else {"verdict": None, "open": None, "as_of": None},
        # cc#888 item 3: each basket carries its symbols so the row can expand inline. `truncated`
        # is stated rather than left implicit — if a basket signalled more than the 20 cap, the
        # screen says so instead of quietly showing a subset as if it were the whole list.
        "baskets": [{
            "slug": b["basket"], "label": basket_label(b["basket"]), "signals": b["n"],
            "symbols": [{"symbol": s["symbol"],
                         "at": s["signal_ts"].strftime("%H:%M") if s["signal_ts"] else None}
                        for s in sig_rows if s["basket"] == b["basket"]],
            "truncated": max(0, b["n"] - 20),
        } for b in baskets],
        "signals_today": sum(b["n"] for b in baskets),
        "open_positions": open_pos,
        "ledger": {"trades": led["n"], "gross_pnl": float(led["pnl"]) if led["pnl"] is not None else None},
        "rail": rail_state(newest, 5, now),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/m/v8", response_class=HTMLResponse)
def m_v8():
    return _page("v8")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# CHECK — 16916: "should-I-take-this-trade is core retail value".
# TRAP (c), AND IT IS THE WHOLE POINT OF THIS SCREEN: tc_cache has not been written since
# 18-Jun-2026. The verdict card PRINTS computed_at and how old it is. Stale stated, never dressed
# as fresh — a 50-day-old verdict presented as today's answer is worse than no answer.
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/check")
@_json_safe   # cc#887
def mobile_check(request: Request, symbol: str = "", side: str = "LONG", limit: int = 20):
    g = _guard(request)
    if g:
        return g
    sym = (symbol or "").strip().upper()
    side = (side or "LONG").strip().upper()
    if side not in ("LONG", "SHORT"):
        side = "LONG"
    limit = max(1, min(limit, 50))
    try:
        now = _ist_now()
        with _conn() as conn, conn.cursor() as cur:
            # cc#888 item 1 — SOURCE SWAP. This read tc_cache, whose newest row is 18-Jun: fifty
            # days dead, no writer, and the app's core retail question ("should I take this
            # trade?") was being answered with June's verdicts. bg_tc_lite writes
            # tc_screener_cache EVERY session — 418 rows today, computed 16:00 IST. The right
            # answers were one table over the whole time.
            # tc_cache itself is untouched: its web consumers keep it (card do_not_touch).
            #
            # computed_at is TIMESTAMPTZ — converted in SQL, never in Python. That is the cc#887
            # class, and it cost the home screen a raw 500 yesterday.
            #
            # ORDER BY carries an explicit `symbol` tiebreak. Scores tie often (three symbols at
            # 17.5 today), and `ORDER BY score DESC` alone leaves the top-5 to physical row order —
            # so the same query could answer differently on two runs. A parity check against a
            # non-deterministic list proves nothing.
            base = """
                SELECT symbol, side, score, verdict, cmp, pivot_zone, failed_rules,
                       (computed_at AT TIME ZONE 'Asia/Kolkata') AS computed_at
                FROM tc_screener_cache
                WHERE run_date = (SELECT MAX(run_date) FROM tc_screener_cache)
            """
            if sym:
                # A searched symbol returns BOTH sides — the retail question about one stock is
                # "is there a trade here at all", not "is there a long here".
                cur.execute(base + " AND symbol = %s ORDER BY side, score DESC, symbol", (sym,))
            else:
                cur.execute(base + " AND side = %s ORDER BY score DESC, symbol LIMIT %s",
                            (side, limit))
            rows = _rows(cur)
            cur.execute("""
                SELECT MAX(computed_at AT TIME ZONE 'Asia/Kolkata')
                FROM tc_screener_cache
                WHERE run_date = (SELECT MAX(run_date) FROM tc_screener_cache)
            """)
            newest = cur.fetchone()[0]
    except Exception as e:
        log.exception("mobile check failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "results": []}

    age_days = ((now - newest).total_seconds() / 86400.0) if newest else None
    def f(v):
        return float(v) if v is not None else None
    return {
        "query": sym or None,
        "side": None if sym else side,
        "results": [{
            "symbol": r["symbol"], "side": r["side"], "cmp": f(r["cmp"]),
            "score": f(r["score"]),
            # ARRAY column -> one readable line. Empty means nothing failed, which is a real
            # answer and renders as nothing rather than as an empty label.
            "not_passed": ", ".join(r["failed_rules"]) if r["failed_rules"] else None,
            "pivot_zone": r["pivot_zone"],
            "verdict": r["verdict"],
            "computed_at": r["computed_at"].strftime("%d-%b %H:%M") if r["computed_at"] else None,
        } for r in rows],
        "count": len(rows),
        # cc#888: n_pass / n_fail / n_watch and `total` DO NOT EXIST in tc_screener_cache. They are
        # removed rather than derived — a pass/fail count invented from failed_rules would look
        # like a measurement and be a guess. The card is explicit: do not fabricate.
        #
        # The staleness banner STAYS even though it now reads fresh. It is data, not decoration:
        # if bg_tc_lite ever dies, this is what tells the founder honestly instead of a screen
        # that keeps showing yesterday's verdicts as if they were today's.
        "freshness": {
            "computed_at": newest.strftime("%d-%b-%Y %H:%M IST") if newest else None,
            "age_days": round(age_days, 1) if age_days is not None else None,
            "stale": bool(age_days is not None and age_days > 1),
            "note": ("Trade Check has not been recomputed since "
                     + (newest.strftime("%d-%b-%Y") if newest else "never")
                     + ". These verdicts are historical, not today's.") if age_days and age_days > 1 else None,
        },
        # cc#888: was REFERENCE against a 1440-minute cadence, which described the dead table.
        # bg_tc_lite runs every 5 minutes in session, so the rail is judged on that — and after
        # the close it reads CLOSED, not stale (cc#841).
        "rail": rail_state(newest, 5, now),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/m/check", response_class=HTMLResponse)
def m_check():
    return _page("check")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# HOME — the front door. Contract: 24 values, 18 of them SOURCED_BUT_EMPTY because
# smartgain_holdings has zero rows. The designed EMPTY STATE renders; never a spinner, never a
# fabricated zero. domestic_live is reused from v8_endpoints for parity.
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/home")
@_json_safe   # cc#887
def mobile_home(request: Request):
    g = _guard(request)
    if g:
        return g
    idx = {}
    try:
        from v8_endpoints import domestic_live
        idx = domestic_live() or {}
    except Exception as e:
        log.warning("mobile home: domestic_live unavailable (%s)", e)
    try:
        now = _ist_now()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                -- cc#887: smartgain_holdings.updated_at is TIMESTAMPTZ — the ONLY tz-aware column
                -- any mobile handler reads. Verified against information_schema: intraday_prices.ts,
                -- quant_paper_positions.updated_at, earnings_calendar.last_updated and
                -- v8_paper_positions.entry_ts are all naive. Passing it raw to rail_state, whose
                -- `now` is naive IST, raises "can't subtract offset-naive and offset-aware
                -- datetimes". Converted HERE, in SQL, once — the same doctrine the models handler
                -- already uses for scheduler_master.last_run_at.
                SELECT COUNT(*) AS n, SUM(mtm) AS mtm, SUM(qty * ltp) AS value,
                       MAX(updated_at AT TIME ZONE 'Asia/Kolkata') AS updated_at
                FROM smartgain_holdings
            """)
            sg = _rows(cur)[0]
            cur.execute("SELECT COUNT(*) FROM v8_paper_positions WHERE status='OPEN'")
            v8_open = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM quant_paper_positions WHERE status='open'")
            qb_open = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(*) FROM v_polished_articles
                WHERE display_time >= NOW() - INTERVAL '24 hours'
            """)
            news_24h = cur.fetchone()[0]
    except Exception as e:
        log.exception("mobile home failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    def f(v):
        return float(v) if v is not None else None
    book_empty = not sg["n"]
    return {
        # cc#887 follow-up — THE SECOND BUG ON THIS SCREEN, and the one the founder actually saw.
        # domestic_live() returns a WRAPPER: {"as_of": "<string>", "indices": {"NIFTY50": {...},
        # "BANKNIFTY": {...}}}. This comprehension iterated the TOP level, so it hit "as_of",
        # whose value is a str, and called .get on it -> AttributeError: 'str' object has no
        # attribute 'get'. It also would have labelled the "indices" key itself "Bank Nifty".
        #
        # Why it hid behind the timezone crash: on 07-Aug the feed was down, so domestic_live()
        # raised, the except left idx = {}, and an empty .items() produced no error — leaving
        # rail_state to raise the TypeError instead. With the feed healthy the wrapper comes back
        # populated and THIS fires first, because a dict literal evaluates its values in source
        # order and "indices" is built before "rail". Fixing one revealed the other; that is the
        # cc#887 guard doing its job, not a regression from it.
        #
        # Reading the "indices" key by name also makes the shape explicit, so a future change to
        # the wrapper is a KeyError-free empty list rather than a crash.
        "indices": [{
            "name": "Nifty 50" if k == "NIFTY50" else "Bank Nifty",
            "close": v.get("close"), "chg_pct": v.get("chg_pct"), "source": v.get("source"),
        } for k, v in (idx.get("indices") or {}).items() if isinstance(v, dict)],
        # SOURCED_BUT_EMPTY, per the contract. The source is correct and returns nothing today, so
        # the screen says the book is flat rather than printing 0.00 as if it were a measurement.
        "book": {
            "empty": book_empty,
            "positions": sg["n"],
            "mtm": f(sg["mtm"]),
            "value": f(sg["value"]),
            "state": "FLAT" if book_empty else "LIVE",
            "message": ("No positions open." if book_empty else None),
        },
        "shortcuts": {"v8_open": v8_open, "qb_open": qb_open, "news_24h": news_24h},
        "rail": rail_state(sg["updated_at"], 1440, now),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%a %d %b"),
    }


@router.get("/m/home", response_class=HTMLResponse)
def m_home():
    return _page("home")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# DIGEST — 16916: "morning brief, daily habit". Reuses digest_v3.build_digest so the app and the
# web /digest render the SAME brief (DISPLAY_PARITY 16202). No second builder.
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/digest")
@_json_safe   # cc#887
def mobile_digest(request: Request):
    g = _guard(request)
    if g:
        return g
    try:
        now = _ist_now()
        # Call the WEB endpoint function itself, not just the builder. Two reasons, both learned
        # the hard way:
        #   1. build_digest(cur) is written against a psycopg2 cursor. This module is psycopg3.
        #      Handing a foreign driver's cursor to code that was never tested with it is exactly
        #      how a screen half-renders. digest_v3.digest_v3() opens its OWN psycopg2 connection.
        #   2. It already carries digest_v3's own try/except shape, so the app and the web fail
        #      the same way as well as succeed the same way.
        # This is the same code path /digest runs — DISPLAY_PARITY 16202 by construction.
        from digest_v3 import digest_v3 as _web_digest
        d = _web_digest() or {}
    except Exception as e:
        log.exception("mobile digest failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "sections": {}}
    if d.get("error"):
        return {"error": d["error"], "sections": {}}
    # The builder owns the content; this endpoint only reshapes it for a phone and never invents a
    # section. Whatever build_digest returns is what the web shows.
    return {"digest": d, "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market": d.get("market") or {}, "date_ist": d.get("date_ist"),
            "rail": {"state": "REFERENCE", "why": "daily brief, built once per morning"}}


@router.get("/m/digest", response_class=HTMLResponse)
def m_digest():
    return _page("digest")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# RESULTS — 16916: "results season, every retail holder".
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/results")
@_json_safe   # cc#887
def mobile_results(request: Request, days: int = 10, limit: int = 60):
    """Upcoming result dates, nearest first, with the V8 universe flagged."""
    g = _guard(request)
    if g:
        return g
    days = max(1, min(days, 60))
    limit = max(1, min(limit, 200))
    try:
        now = _ist_now()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT e.ticker, e.company_name, e.ex_date, e.event_type, e.status,
                       EXISTS (SELECT 1 FROM futures_universe f
                               WHERE f.symbol = e.ticker AND f.is_active) AS in_v8
                FROM earnings_calendar e
                WHERE e.ex_date >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                  AND e.ex_date <= (NOW() AT TIME ZONE 'Asia/Kolkata')::date + %s
                ORDER BY e.ex_date, (NOT EXISTS (SELECT 1 FROM futures_universe f
                               WHERE f.symbol = e.ticker AND f.is_active)), e.ticker
                LIMIT %s
            """, (days, limit))
            rows = _rows(cur)
            cur.execute("""
                SELECT COUNT(*) FROM earnings_calendar
                WHERE ex_date >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                  AND ex_date <= (NOW() AT TIME ZONE 'Asia/Kolkata')::date + %s
            """, (days,))
            total = cur.fetchone()[0]
            cur.execute("SELECT MAX(last_updated) FROM earnings_calendar")
            newest = cur.fetchone()[0]
    except Exception as e:
        log.exception("mobile results failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "results": []}

    by_day = {}
    for r in rows:
        by_day.setdefault(r["ex_date"].strftime("%Y-%m-%d"), []).append({
            "symbol": r["ticker"], "name": r["company_name"],
            "event": r["event_type"], "status": r["status"], "in_v8": r["in_v8"],
        })
    return {
        "days": [{"date": k,
                  "label": datetime.strptime(k, "%Y-%m-%d").strftime("%a %d %b"),
                  "companies": v, "n": len(v)} for k, v in sorted(by_day.items())],
        "count": len(rows),
        "total_in_window": total,
        "window_days": days,
        "coverage": f"showing {len(rows)} of {total} in the next {days} days",
        # The calendar is loaded nightly, so it is REFERENCE against a daily cadence rather than
        # judged against the intraday clock.
        "rail": rail_state(newest, 1440, now),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/m/results", response_class=HTMLResponse)
def m_results():
    return _page("results")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# LOGIN — 16916: "necessary". Deliberately the THINNEST screen in the set.
#
# It posts to the EXISTING /login endpoint in scorr_auth.py. No auth logic is duplicated here:
# the password check, the 5-per-10-min IP rate limit, the session token mint and the cookie flags
# all stay in the one place that already owns them. A second implementation of a login is how an
# auth bypass gets built by accident.
#
# NOT in PROTECTED, per card item 7 — gating the login page behind the login is a lockout.
# scorr_auth randomises its field name per page load to defeat autofill (cc#709); this form uses
# the plain `password` name, which login_post already accepts as its documented defensive
# fallback. Stated rather than silently relied on.
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/m/login", response_class=HTMLResponse)
def m_login():
    return _page("login")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# MODELS — cc#886 slot 5. The launcher, and the app's "everything else" surface.
#
# REGISTRY-DERIVED, NEVER A NAME LIST (ENGINE_LIVENESS_RULE 13829). The tiles come from
# model_registry joined to scheduler_master on job_name. Adding a model to the registry adds a
# tile; nothing here needs editing. A hardcoded list here would be the exact defect rule 9 was
# written for — three engines were found built-but-never-breathing on 02-Aug because enumeration
# and reality had drifted apart.
#
# THE BADGE FOLLOWS THE DATA. A tile is LIVE only when its job actually ran inside its cadence
# during the session. Registered-but-never-run reads NOT RUN YET, in words, and never green.
# Outside market hours a 5-minute job is CLOSED, not stale — calling it stale after the close is
# the cc#841 false-positive that lights every surface red every evening.
# ══════════════════════════════════════════════════════════════════════════════════════════════

# Where a model's web route has a wired mobile twin, the tile goes to the twin (card item 2:
# "each tile linking to its wired /m/* or web page"). This is a ROUTE TRANSLATION, not an
# enumeration of models — the models themselves still come entirely from model_registry.
_WEB_TO_MOBILE = {
    "/dashboard": "/m/v8",
    "/check": "/m/check",
    "/": "/m/home",
    # cc#888 item 5. Both keys verified against model_registry.route with a SELECT before being
    # written here, as the card requires — id 8 is exactly "/quant-basket" and id 7 is exactly
    # "/cio2?model=gvm", query string included. A near-miss key would silently fall through to the
    # desktop page and look like a design choice rather than a typo.
    "/quant-basket": "/m/qb",
    "/cio2?model=gvm": "/m/gvm",
}

# The app's own screens, for the "Everything else" section. Derived from THIS module's routes —
# mobile_endpoints owns them, so this is its own source, not a second copy of the NAV array.
_MOBILE_DESTINATIONS = [
    ("/m/home", "Home"), ("/m/v8", "V8"), ("/m/positions", "Open Book"),
    ("/m/qb", "Baskets"), ("/m/gvm", "GVM"), ("/m/check", "Trade Check"),
    ("/m/intel", "Intel"), ("/m/digest", "Daily Digest"), ("/m/results", "Results"),
    # cc#893 item 3, final piece. cc#892 shipped these four as a STATIC `EXTRA` array inside
    # mobile/models.html, with a note saying CC folds them in here and deletes the client-side
    # list — Fable could not edit this file safely at the time. Done: the server is the one
    # source again, so adding a screen is one line here rather than one line here AND one in a
    # template that would drift out of step the first time someone forgot.
    ("/m/screeners", "Screeners"), ("/m/sector", "Sector"),
    ("/m/holdings", "My Portfolio"), ("/m/fpc", "FPC Planner"),
    ("/preview", "Previews"),
]

MODEL_STALE_MIN = 15.0        # a 5-min job is late once it is three cycles behind


@router.get("/api/mobile/models")
@_json_safe   # cc#887
def mobile_models(request: Request):
    """Model tiles from model_registry, with a state derived from real run rows."""
    g = _guard(request)
    if g:
        return g
    try:
        now = _ist_now()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                -- last_run_at is tz-aware; every other clock in this module is NAIVE IST. The
                -- conversion happens HERE, once, in SQL, so nothing downstream ever compares a
                -- naive value with an aware one (the cc#844 phantom-330-minute class).
                SELECT m.display_name, m.description, m.route, m.job_name, m.is_public,
                       s.active, s.last_status, s.cadence_human,
                       (s.last_run_at AT TIME ZONE 'Asia/Kolkata') AS last_run_ist
                FROM model_registry m
                LEFT JOIN scheduler_master s ON s.job_name = m.job_name
                ORDER BY m.sort_order, m.id
            """)
            rows = _rows(cur)
    except Exception as e:
        log.exception("mobile models failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "models": [], "count": 0}

    t = now.time()
    in_session = now.weekday() < 5 and MARKET_OPEN <= t <= SESSION_END
    out, hidden = [], 0
    for r in rows:
        if not r["is_public"]:
            hidden += 1
            continue
        last = r["last_run_ist"]        # already naive IST, converted in SQL above
        age_min = None if last is None else (now - last).total_seconds() / 60.0

        if not r["active"]:
            state, why = "OFF", "not scheduled — this model is registered but switched off"
        elif last is None:
            # Registered is not running. This is the state rule 9 exists to make visible.
            state, why = "NOT RUN YET", "registered, but no run has ever been recorded"
        elif not in_session:
            state, why = "CLOSED", f"market closed · last run {_ago(age_min)}"
        elif age_min is not None and age_min <= MODEL_STALE_MIN:
            state, why = "LIVE", f"ran {_ago(age_min)}"
        else:
            state, why = "STALE", f"last run {_ago(age_min)} — expected every few minutes"
        if r["last_status"] and r["last_status"] != "ok" and state in ("LIVE", "CLOSED"):
            state, why = "ERROR", f"last run reported {r['last_status']}"

        web = r["route"] or ""
        out.append({
            "name": r["display_name"],
            "description": r["description"],
            "href": _WEB_TO_MOBILE.get(web, web),
            "on_mobile": web in _WEB_TO_MOBILE,
            "web_route": web,
            "job": r["job_name"],
            "state": state,
            "why": why,
            "last_run": last.strftime("%d %b %H:%M") if last else None,
        })

    return {
        "models": out,
        "count": len(out),
        "internal_hidden": hidden,
        "destinations": [{"href": h, "label": l} for h, l in _MOBILE_DESTINATIONS],
        "in_session": in_session,
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@router.get("/m/models", response_class=HTMLResponse)
def m_models():
    return _page("models")


# ── cc#886 item 3: THE FRONT DOOR ─────────────────────────────────────────────────────────────
# cc#874 wired eleven /m/* screens and no card ever repointed the entry, so the installed app
# still opened `/` — the old desktop home. The founder was looking at the old product with live
# data in it, which is why it read as a caching problem and was not one.
#
# The redirect is the half that reaches ALREADY-INSTALLED apps: a WebAPK's manifest is frozen at
# install time, so changing start_url alone would fix only future installs. Both ship together.
#
# NAVIGATIONS ONLY. Sec-Fetch-Mode tells a top-level navigation apart from a fetch/XHR; without
# that check an API call to `/` would be answered with a redirect. Desktop is untouched by
# construction — no UA match, no redirect, byte-identical behaviour (cc#882 m-flag rule stands).
_MOBILE_UA_HINTS = ("iphone", "android", "ipod", "windows phone", "mobile safari", "iemobile")


def wants_mobile_home(request: Request) -> bool:
    """True when this is a phone making a top-level navigation to the site root."""
    try:
        mode = (request.headers.get("sec-fetch-mode") or "").lower()
        dest = (request.headers.get("sec-fetch-dest") or "").lower()
        # If the browser sends the hints, respect them: only a real page load qualifies. If it
        # sends none (older browsers), fall through to the UA test rather than refuse outright.
        if mode and mode != "navigate":
            return False
        if dest and dest not in ("document", "empty", ""):
            return False
        ua = (request.headers.get("user-agent") or "").lower()
        if "ipad" in ua or "tablet" in ua:
            return False        # tablets get the full web product (16915)
        return any(h in ua for h in _MOBILE_UA_HINTS)
    except Exception:
        return False            # any doubt at all -> serve the existing page, never a redirect loop


# ══════════════════════════════════════════════════════════════════════════════════════════════
# SHARED — server clock, so no promoted screen ever derives a session state from the phone's clock.
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/now")
@_json_safe   # cc#887
def mobile_now():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT NOW() AT TIME ZONE 'Asia/Kolkata'")
            now = cur.fetchone()[0]
            # nse_holidays has NO is_trading_day column — it is a list OF holidays, so presence
            # in the table means the market is shut. Checked against the real schema rather than
            # assumed; inventing that column is exactly the failure this card warns about.
            cur.execute("SELECT 1 FROM nse_holidays WHERE holiday_date = %s", (now.date(),))
            is_holiday = cur.fetchone() is not None
    except Exception as e:
        log.warning("mobile now failed: %s", e)
        return {"error": str(e)[:200]}
    t = now.time()
    is_td = (now.weekday() < 5) and not is_holiday
    return {
        "ist": now.strftime("%Y-%m-%d %H:%M:%S"),
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%d %b %Y"),
        "is_trading_day": is_td,
        "market_open": bool(is_td and MARKET_OPEN <= t <= SESSION_END),
        "cash_continuous": bool(is_td and MARKET_OPEN <= t <= EQ_CONTINUOUS_END),
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════
# THE SHARED SHELL — built ONCE and linked by every promoted screen (item 1).
# The previews each carry their own copy of these tokens because each is a standalone review file.
# A promoted page must NOT inherit that duplication: one stylesheet, one definition, so a token
# change lands on every screen at once. Tokens live on .screen/.bnav, never bare :root (15913).
# ══════════════════════════════════════════════════════════════════════════════════════════════
MOBILE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#0A0F1E;color:#E9EEFB;font-family:'Sora',sans-serif;
  -webkit-font-smoothing:antialiased;-webkit-tap-highlight-color:transparent}
.screen,.bnav{
  --bg:#0A0F1E;--panel:#121A33;--panel2:#182241;
  --line:rgba(148,166,210,.14);--line2:rgba(148,166,210,.24);
  --txt:#E9EEFB;--mut:#8C99BD;--dim:#5E6B8F;
  --grn:#2FD48B;--grn-d:rgba(47,212,139,.14);
  --red:#FF5C6C;--blu:#4D7CFE;--cyan:#37D3E8;--amber:#F5B94A;
}
.screen{max-width:430px;margin:0 auto;padding-bottom:78px;min-height:100vh;
  background:var(--bg);color:var(--txt)}
.head{padding:15px 14px 12px;border-bottom:1px solid var(--line)}
.head h1{font-size:19px;font-weight:800;letter-spacing:-.01em}
.head .s{font-size:10.5px;color:var(--dim);margin-top:4px;
  font-family:'IBM Plex Mono',ui-monospace,monospace}
.chips{display:flex;gap:7px;overflow-x:auto;padding:11px 12px 3px;
  scrollbar-width:none;-webkit-overflow-scrolling:touch}
.chips::-webkit-scrollbar{display:none}
/* cc#880 item 5: 44px tap targets. Raised from 34px GLOBALLY rather than only on the new filter
   rows — 44px is the accessibility floor, and card item 1 asks for one pattern everywhere, so a
   34px chip on Intel next to a 44px chip on the Open Book would be the drift this card exists to
   remove. Affects every .chip row in the app (Intel categories, the Retry buttons). */
.chip{flex:0 0 auto;min-height:44px;display:inline-flex;align-items:center;gap:6px;
  padding:0 12px;border-radius:17px;border:1px solid var(--line2);background:transparent;
  font-size:11.5px;font-weight:700;color:var(--mut);white-space:nowrap;
  text-decoration:none;font-family:inherit;cursor:pointer}
.chip .n{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--dim)}
.chip.on{color:var(--txt);border-color:rgba(77,124,254,.55);background:rgba(77,124,254,.14)}
.chip.on .n{color:var(--blu)}
.body{padding:11px 12px 0}
.c{position:relative;background:var(--panel);border:1px solid var(--line);
  border-radius:14px;padding:13px 13px 13px 17px;margin-bottom:9px;overflow:hidden}
.c::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--line2)}
.c.bull::before,.c.long::before{background:var(--grn)}
.c.bear::before,.c.short::before{background:var(--red)}
.c.caut::before{background:var(--amber)}
.c.ed{background:var(--panel2);border-color:var(--line2)}
.meta{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-bottom:7px}
.tag{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:8.5px;font-weight:700;
  letter-spacing:.11em;padding:3px 7px;border-radius:4px;border:1px solid var(--line2);
  color:var(--mut);white-space:nowrap}
.tag.cat{color:var(--cyan);border-color:rgba(55,211,232,.4)}
.tag.edt{color:var(--blu);border-color:rgba(77,124,254,.5);background:rgba(77,124,254,.12)}
.tag.hi{color:var(--amber);border-color:rgba(245,185,74,.45)}
.tag.bull{color:var(--grn);border-color:rgba(47,212,139,.45);background:var(--grn-d)}
.tag.neut{color:var(--mut)}
.tag.caut{color:var(--amber);border-color:rgba(245,185,74,.45)}
.tag.bear{color:var(--red);border-color:rgba(255,92,108,.45)}
.when{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:9.5px;color:var(--dim);white-space:nowrap}
.hl{font-size:14.5px;font-weight:800;line-height:1.4;letter-spacing:-.005em}
.sum{font-size:12.5px;color:var(--mut);line-height:1.55;margin-top:6px}
.read{font-size:13.5px;color:var(--txt);line-height:1.68;margin-top:10px}
.read p{margin-top:9px}
.read p:first-child{margin-top:0}
.fold{margin-top:10px;min-height:44px;display:flex;align-items:center;gap:6px;
  font-size:11.5px;font-weight:700;color:var(--blu);background:none;border:0;
  font-family:inherit;cursor:pointer;padding:0}
.syms{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.sy{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;font-weight:700;
  color:var(--txt);background:var(--panel);border:1px solid var(--line2);
  border-radius:5px;padding:3px 8px}
.src{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9.5px;
  color:var(--dim);margin-top:9px}
.sect{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;font-weight:700;
  letter-spacing:.14em;color:var(--dim);margin:15px 3px 8px}
.cover{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--dim);
  text-align:center;padding:13px 0 3px}
.mono{font-family:'IBM Plex Mono',ui-monospace,monospace}
.pos{color:var(--grn)}.neg{color:var(--red)}

/* THE STATE RAIL — four states told apart by SHAPE, not by opacity (15913 rule 10). */
.rail{display:inline-flex;align-items:center;gap:5px;font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:8.5px;font-weight:800;letter-spacing:.1em;padding:3px 7px;border-radius:4px;
  border:1px solid currentColor}
.rail i{width:6px;height:6px;display:inline-block}
.rail-live{color:var(--grn)}
/* ── cc#982 · COPY on mobile Intel cards ───────────────────────────────────────────────────────
   These rules live HERE and not in mobile/intel.html because that template carries NO <style>
   block at all — its CSS was folded into this sheet, the same way cc#897 folded the GVM page's.
   (I found that the hard way: an inline block written into intel.html was a silent no-op and the
   button rendered unstyled at 35x19px instead of the 44px the card requires.)
   `nc-` prefix: this app has collided four times on short class names (cc#964 .v/.m, cc#966 .m,
   cc#967 .v, cc#974 .a), and the web's own `.copy-btn` would have been the fifth. */
.src{display:flex;align-items:center;gap:8px}
.nc-copy{margin-left:auto;flex:none;min-height:44px;min-width:64px;padding:0 12px;
  background:none;border:1px solid var(--line2);border-radius:10px;color:var(--mut);
  font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;font-weight:700;
  letter-spacing:.08em;text-transform:uppercase;cursor:pointer}
.nc-copy:active{background:var(--panel2)}
.nc-copy.ok{color:var(--grn);border-color:rgba(47,212,139,.5)}
.nc-copy.bad{color:var(--amber);border-color:rgba(245,185,74,.5)}
.rail-live i{border-radius:50%;background:currentColor;animation:brea 2.4s ease-in-out infinite}
.rail-stale{color:var(--amber)}
.rail-stale i{background:repeating-linear-gradient(90deg,currentColor 0 3px,transparent 3px 5px);height:2px}
.rail-closed{color:var(--dim)}
.rail-closed i{background:radial-gradient(circle,currentColor 40%,transparent 45%);background-size:3px 3px;height:3px;width:9px}
.rail-reference{color:var(--blu)}
.rail-reference i{background:currentColor;height:2px;width:9px}
@keyframes brea{0%,100%{opacity:1}50%{opacity:.35}}

/* Loading + failure. The shimmer is sized to the final height so nothing jumps (15913 rule 7),
   and cc#878's global backstop replaces it if a request times out. */
.loading{padding:14px;color:var(--dim);font-size:12px}
.empty{padding:22px 14px;color:var(--dim);font-size:12.5px;line-height:1.6;text-align:center}
.empty b{color:var(--txt);display:block;font-size:13.5px;margin-bottom:5px}

.bnav{position:fixed;left:0;right:0;bottom:0;max-width:430px;margin:0 auto;display:flex;
  background:var(--panel);border-top:1px solid var(--line2);
  padding:7px 0 max(6px,env(safe-area-inset-bottom));z-index:50}
.bn{flex:1;text-align:center;font-size:9.5px;font-weight:700;color:var(--dim);padding:4px 0;
  min-height:44px;text-decoration:none}
.bn .i{display:block;font-size:16px;margin-bottom:3px;line-height:1}
.bn.on{color:var(--blu)}


/* ══════════════════════════════════════════════════════════════════════════════════════
   cc#893 item 3 — PER-SCREEN CSS, FOLDED IN FROM THE TEMPLATES

   Twelve /m/* templates each carried their own <style> block. That is twelve stylesheets
   the phone re-parses on every navigation and never caches, on a file it already fetches
   once. Folded here, they are one cached request for the whole app.

   THREE SELECTORS COLLIDED and were namespaced first, because a naive concatenation lets
   the last definition win globally and silently restyles two screens for each clash:
     .chev  gvm 14px / home 16px / v8 17px+flex   -> .g-chev / .h-chev / .v-chev
     .lrow  gvm row-card / v8 different layout    -> .g-lrow / .v-lrow
     .srow  screeners / sector                    -> .sc-srow / .se-srow
   Every other selector was verified unique across all twelve, or identical where shared.
   Rules are otherwise VERBATIM — this move must not change a single pixel.
   ══════════════════════════════════════════════════════════════════════════════════════ */

/* ── CHECK  (was inline in mobile/check.html) ─────────────────────────────── */
/*
  cc#890 — CHECK reskinned to previews/check.html (approved design, markup+CSS ported, live data).
  Pushed by Claude (Fable), CHARTER_OVERRIDE_08AUG2026 (session_log 17783).
  Preview design carried: search + LONG/SHORT first; the verdict is a WORD with the score beside
  it; THE GAPS ARE THE PRODUCT — every failed rule prints as its own red-edged row; passed rules
  collapse to one line; the check states its own age.
  Data honesty vs the preview, stated: the live source is tc_screener_cache (fresh daily, cc#888)
  whose verdict words are STRONG / VALID / WATCH — shown as-is (DISPLAY_PARITY 16202, same words
  as the web scanner). It carries NO pass/watch/fail counts and no per-rule value-vs-required
  detail, so the preview's count chips and gap explanations are NOT fabricated: a gap row shows
  the rule name, and the chip shows the gap COUNT, which is real. Score renders without "/total"
  because the source has no total.
  First card ships expanded; the rest expand on tap.
  cc#894 item 1 (17868): the shared web C·A·R·D strip on every verdict card
  (window.ScorrCardRow, rule cc#789 — never re-implemented). Strip taps don't fold the card.
  :root bridge maps web card CSS vars onto the dark palette.
*/
/* ── cc#963/964 ROOT CAUSE: the bridge carried THREE tokens, and the cards need the whole set ──
   The app palette is declared on `.screen,.bnav` — not on :root and not on body. Custom properties
   inherit DOWN the tree, and every shared card overlay (#scorrAnaOv, #dcOv, #scorrChartOv, the
   symbol sheet, the C·A·R·D strip) is appended to document.body, which is a SIBLING of .screen.
   So none of them ever saw --panel/--txt/--dim/--mut/--line/--line2/--grn/--red/--blu/--amber.
   Measured consequences on /m/, all three of the founder's complaints from one cause:
     var(--panel,#fff)  -> the light FALLBACK wins            -> white sheet in a dark app
     var(--dim)         -> invalid at computed-value time, so
                           `color` falls back to INHERIT      -> headers/labels take the ambient
                                                                 colour and vanish
     var(--grn)         -> same                               -> delta values render inherited-white
                                                                 on the pastel green/red heat fill
     --surface2         -> WAS bridged, so it resolved dark   -> dark tiles floating on white
   The card CSS was never wrong; it was reading tokens that were scoped out of its reach. So the
   fix is the bridge, not a retheme of three card files — one correction serves A, D, the chart
   card and the strip at once, and nothing downstream has to be kept in sync by hand.
   The three ORIGINAL tokens keep their existing values (--cyan is deliberately #3aa0ff here and
   #37D3E8 inside .screen; changing it was not asked for), so nothing that already resolved moves.
   This file is served only to /m/* templates, so web cannot be reached from here by construction. */
:root{--surface2:#1a2233;--cyan:#3aa0ff;--surface:#141b2a;
  --bg:#0A0F1E;--panel:#121A33;--panel2:#182241;
  --line:rgba(148,166,210,.14);--line2:rgba(148,166,210,.24);
  --txt:#E9EEFB;--mut:#8C99BD;--dim:#5E6B8F;
  --grn:#2FD48B;--grn-d:rgba(47,212,139,.14);
  --red:#FF5C6C;--blu:#4D7CFE;--amber:#F5B94A}
.inrow{display:flex;gap:8px;margin:12px 12px 0}
.sfld{flex:1;display:flex;align-items:center;gap:9px;background:var(--panel);
  border:1px solid var(--line2);border-radius:12px;min-height:48px;padding:0 6px 0 13px}
.sfld input{flex:1;min-height:46px;background:none;border:0;outline:none;color:var(--txt);
  font-family:inherit;font-size:16px;font-weight:700}
.sfld input::placeholder{color:var(--dim);font-weight:400;font-size:13px}
.tog{display:flex;border:1px solid var(--line2);border-radius:12px;overflow:hidden}
.tg{min-height:48px;display:flex;align-items:center;padding:0 13px;font-size:11.5px;
  font-weight:800;color:var(--dim);cursor:pointer;background:none;border:0;font-family:inherit}
.tg.on{background:rgba(47,212,139,.14);color:var(--grn)}
.tg.on.sh{background:rgba(255,92,108,.12);color:var(--red)}
.v{position:relative;background:var(--panel2);border:1px solid var(--line2);
  border-radius:15px;padding:15px 15px 12px 19px;overflow:hidden;margin-bottom:8px;cursor:pointer}
.v::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--vc,var(--amber))}
.v1{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.vsym{font-size:16.5px;font-weight:800}
.vside{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;font-weight:700;
  letter-spacing:.1em;padding:3px 8px;border-radius:4px;border:1px solid rgba(47,212,139,.45);color:var(--grn)}
.vside.sh{border-color:rgba(255,92,108,.45);color:var(--red)}
.vpx{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:11px;font-weight:700;color:var(--mut)}
.v2{display:flex;align-items:center;gap:11px;margin-top:11px}
.word{font-size:20px;font-weight:800;letter-spacing:-.01em;color:var(--vc,var(--amber))}
.score{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:19px;font-weight:800}
.counts{display:flex;gap:7px;margin-top:10px;flex-wrap:wrap}
.ct{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9.5px;font-weight:700;
  padding:4px 9px;border-radius:5px;border:1px solid var(--line2);color:var(--mut)}
.ct.f{color:var(--red);border-color:rgba(255,92,108,.4)}
.ct.p{color:var(--grn);border-color:rgba(47,212,139,.4)}
.g{display:flex;align-items:baseline;gap:9px;background:var(--panel);
  border:1px solid var(--line);border-left:3px solid var(--red);border-radius:0 11px 11px 0;
  padding:9px 12px;margin:0 0 6px 6px}
.gv{font-size:11.5px;color:var(--mut);line-height:1.5}
.gv b{color:var(--red);font-weight:700}
.pass1{font-size:11px;color:var(--dim);padding:2px 3px 12px 8px;
  font-family:'IBM Plex Mono',ui-monospace,monospace}

/* ── FPC  (was inline in mobile/fpc.html) ─────────────────────────────── */
/* cc#892 — FPC (Fable direct, 17783). Client-side planning calculator: monthly SIP + one-time
   lumpsum compounding to a horizon, and the reverse (what monthly amount reaches a goal).
   16px inputs (iOS zoom rule). Pure arithmetic on-device — no fetch, nothing to go stale. */
.fld{margin:0 0 11px}
.fld label{display:block;font-size:10px;color:var(--dim);font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;margin-bottom:5px}
.fld input{width:100%;min-height:48px;background:var(--panel);border:1px solid var(--line2);
  border-radius:11px;color:var(--txt);font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:16px;font-weight:700;padding:0 13px;outline:none}
.fld input:focus{border-color:rgba(77,124,254,.55)}
.res .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:26px;font-weight:800}
.res .k{font-size:10px;color:var(--dim);font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.split{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.sp{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:9px 11px}
.sp .k{font-size:8.5px;color:var(--dim);font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.sp .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;font-weight:800;margin-top:3px}

/* ── GVM  (was inline in mobile/gvm.html) ─────────────────────────────── */
/*
  GVM — the app's company research view (founder 08-Aug: "old app screens fine, format,
  organise and push"). The Full report now IS the web /cio2 company page, mobile-formatted:
  same endpoints (api/gvm/company, api/gvm/trend, api/news/company, api/trade-check/fibcheck,
  api/sector/brief), organized into the web's own sections. Ops-metrics deliberately absent —
  OPS_METRICS_INTERNAL_ONLY (13348). Arrays render as mini-tables (peers, financials,
  shareholding); the trend renders as a sparkline. Fable direct push, 17783.
*/
.search{display:flex;align-items:center;gap:9px;background:var(--panel);
  border:1px solid var(--line2);border-radius:12px;min-height:48px;padding:0 6px 0 14px;margin:12px 12px 0}
.search .ic{color:var(--dim);font-size:15px}
.search input{flex:1;min-height:46px;background:none;border:0;outline:none;color:var(--txt);
  font-family:inherit;font-size:16px}
.search input::placeholder{color:var(--dim);font-size:13px}
.rep{position:relative;background:var(--panel2);border:1px solid var(--line2);
  border-radius:15px;padding:15px 15px 15px 19px;overflow:hidden;margin-bottom:10px}
.rep::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--rc,var(--grn))}
.r1{display:flex;align-items:baseline;gap:9px}
.rsym{font-size:17px;font-weight:800;letter-spacing:-.01em}
.rseg{font-size:10px;color:var(--dim)}
.rpx{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:13px;font-weight:700;color:var(--mut)}
.big{display:flex;align-items:baseline;gap:10px;margin-top:11px}
.gvmv{font-size:36px;font-weight:800;letter-spacing:-.03em;color:var(--rc,var(--grn));
  font-family:'IBM Plex Mono',ui-monospace,monospace}
.gvmv .of{font-size:13px;color:var(--dim);font-weight:700}
.verd{font-size:12px;font-weight:800;color:var(--rc,var(--grn));border:1px solid var(--rc,var(--grn));
  border-radius:6px;padding:4px 10px}
.pil{margin-top:13px}
.pr{display:flex;align-items:center;gap:10px;margin-bottom:9px}
.pk{font-size:11px;font-weight:800;min-width:78px}
.bar{flex:1;height:7px;border-radius:4px;background:rgba(148,166,210,.14);overflow:hidden}
.fill{height:100%;border-radius:4px;background:var(--rc,var(--grn))}
.pv{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:800;
  min-width:32px;text-align:right}
.punch{font-size:12.5px;color:var(--mut);line-height:1.6;margin-top:11px;
  border-top:1px solid var(--line);padding-top:11px}
.punch b{color:var(--txt)}
.acts{display:flex;gap:8px;margin-top:12px}
.a{flex:1;min-height:44px;display:flex;align-items:center;justify-content:center;
  font-size:11.5px;font-weight:700;color:var(--blu);border:1px solid rgba(77,124,254,.4);
  border-radius:10px;text-decoration:none;background:none;font-family:inherit;cursor:pointer}
.g-lrow{display:flex;align-items:center;gap:10px;background:var(--panel);
  border:1px solid var(--line);border-radius:12px;padding:11px 13px;margin-bottom:7px;min-height:48px;cursor:pointer}
.rk{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:700;
  color:var(--dim);min-width:22px}
.rs{font-size:13.5px;font-weight:800}
.rg{font-size:9.5px;color:var(--dim);margin-top:1px}
.sc{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;
  font-size:14px;font-weight:800}
.g-chev{color:var(--dim);font-size:14px}
.fr{margin:-2px 0 10px;background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:12px 13px}
.frh{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;font-weight:800;
  letter-spacing:.13em;color:var(--dim);margin:12px 0 7px}
.fr .frh:first-child{margin-top:0}
.frr{display:flex;align-items:baseline;gap:8px;padding:6px 0;border-top:1px solid var(--line);min-height:32px}
.frr:first-of-type{border-top:0}
/* cc#913: sub-heading inside a report section. The payload is two levels deep (extras alone
   carries pivot, range52, percentile, earnings, volume, delivery, segment_ctx, tier1) and until
   now the second level printed "[object Object]". deepRows renders each nested object under one
   of these, lighter than .frh so a section still reads as one block. */
.frs{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:8.5px;font-weight:800;
  letter-spacing:.11em;text-transform:uppercase;color:var(--mut);padding:9px 0 3px;
  border-top:1px solid var(--line);margin-top:4px}
.frr .k2{font-size:11.5px;color:var(--mut);flex:1;text-transform:capitalize}
.frr .v2{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:700;text-align:right}
.tbl{width:100%;border-collapse:collapse;font-size:10.5px}
.tbl th{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:8.5px;font-weight:800;
  letter-spacing:.06em;color:var(--dim);text-align:right;padding:4px 4px;border-bottom:1px solid var(--line2);text-transform:uppercase}
.tbl th:first-child{text-align:left}
.tbl td{padding:5px 4px;border-bottom:1px solid var(--line);text-align:right;
  font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10.5px;color:var(--txt)}
.tbl td:first-child{text-align:left;font-family:'Sora';font-weight:700;color:var(--txt)}
.spark{width:100%;height:44px;display:block}
.prose{font-size:12.5px;color:var(--mut);line-height:1.6}
.prose b{color:var(--txt)}
.nrow{padding:7px 0;border-top:1px solid var(--line);font-size:12px;font-weight:600;line-height:1.4}
.nrow:first-of-type{border-top:0}
.nrow .w{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;color:var(--dim);margin-top:2px}

/* ── HOLDINGS  (was inline in mobile/holdings.html) ─────────────────────────────── */
/* cc#892 — HOLDINGS (Fable direct, 17783). Full My Portfolio page: header totals, one card per
   holding with entry→LTP, value, MTM, return%. cc#894 item 1: the shared web C·A·R·D strip on
   every card (window.ScorrCardRow, rule cc#789 — never re-implemented). :root bridge maps the
   web card CSS vars onto the dark palette. */
/* ── cc#963/964 ROOT CAUSE: the bridge carried THREE tokens, and the cards need the whole set ──
   The app palette is declared on `.screen,.bnav` — not on :root and not on body. Custom properties
   inherit DOWN the tree, and every shared card overlay (#scorrAnaOv, #dcOv, #scorrChartOv, the
   symbol sheet, the C·A·R·D strip) is appended to document.body, which is a SIBLING of .screen.
   So none of them ever saw --panel/--txt/--dim/--mut/--line/--line2/--grn/--red/--blu/--amber.
   Measured consequences on /m/, all three of the founder's complaints from one cause:
     var(--panel,#fff)  -> the light FALLBACK wins            -> white sheet in a dark app
     var(--dim)         -> invalid at computed-value time, so
                           `color` falls back to INHERIT      -> headers/labels take the ambient
                                                                 colour and vanish
     var(--grn)         -> same                               -> delta values render inherited-white
                                                                 on the pastel green/red heat fill
     --surface2         -> WAS bridged, so it resolved dark   -> dark tiles floating on white
   The card CSS was never wrong; it was reading tokens that were scoped out of its reach. So the
   fix is the bridge, not a retheme of three card files — one correction serves A, D, the chart
   card and the strip at once, and nothing downstream has to be kept in sync by hand.
   The three ORIGINAL tokens keep their existing values (--cyan is deliberately #3aa0ff here and
   #37D3E8 inside .screen; changing it was not asked for), so nothing that already resolved moves.
   This file is served only to /m/* templates, so web cannot be reached from here by construction. */
:root{--surface2:#1a2233;--cyan:#3aa0ff;--surface:#141b2a;
  --bg:#0A0F1E;--panel:#121A33;--panel2:#182241;
  --line:rgba(148,166,210,.14);--line2:rgba(148,166,210,.24);
  --txt:#E9EEFB;--mut:#8C99BD;--dim:#5E6B8F;
  --grn:#2FD48B;--grn-d:rgba(47,212,139,.14);
  --red:#FF5C6C;--blu:#4D7CFE;--amber:#F5B94A}
.tot{padding:13px 14px;border-bottom:1px solid var(--line);display:flex;gap:16px}
.tot .b{flex:1}
.tot .k{font-size:9px;color:var(--dim);font-weight:700;letter-spacing:.1em;text-transform:uppercase}
.tot .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:18px;font-weight:800;margin-top:3px}
.pc2{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px;margin-top:10px}
.kv{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:7px 9px;text-align:center}
.kv .k{font-size:8.5px;color:var(--dim);font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.kv .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:700;margin-top:2px}
.hl{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hl .sy{font-size:15px;font-weight:800;margin-right:auto}
.hl .pl{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;font-weight:800}

/* ── HOME  (was inline in mobile/home.html) ─────────────────────────────── */
/*
  cc#889 — HOME rebuilt to previews/home_v2.html. cc#894 (Fable direct, 17868) shipped the chips.
  FOUNDER COMMENTS 08-Aug batch 2 (this commit):
  (1) TICKER BAR: NIFTY/BANKNIFTY + digest global set scroll in a top ticker strip — the 2-tile
      index grid inside the hero is gone. Home only; other screens untouched.
  (2) Hero carries the V10 index line: "Nifty Long · Bank Nifty No Trade" (server-computed).
  (3) PCR/VIX values are SERVED by /api/mobile/home2 (hero.pcr/hero.vix) — the cc#894 pickers
      guessed other endpoints' keys and rendered "--". Chip tap now opens an SVG LINE CHART
      (website chart style) fed by /api/mobile/trends?kind=adr|pcr|vix.
  (4) Open Book -> V8 OPEN BOOK, trio = Unrealised / Long Unrl / Short Unrl, each with % on
      capital deployed for that side. A side with no positions renders --, never 0%.
  (5) Results Corner card REMOVED (covered by the Results tile). Intel card renamed LIVE NEWS,
      scrollable (10 items inside a fixed-height scroll box), still taps through to /m/intel.
  (6) Tools = 9-tile grid, no overlap with the six-slot nav: Baskets Screeners SectorIntel
      Results Digest FPC Dashboard AICIO PortfolioHealth (last three open the web pages —
      founder-approved exception to the 16915 split; /health IS the client Portfolio Health
      page, hr_endpoints.py, verified 08-Aug).
  BATCH 3 (this commit): ticker MOVES (marquee, seamless -50% loop, hold to pause); charts get
  FINGER SCRUB (crosshair + live value/date readout in the header); PCR/VIX chips get COLOUR
  (PCR >=1 green else red; VIX <=14 green, >=17 red, between neutral).
*/
.tkr{overflow:hidden;padding:8px 0;margin:0 -12px;border-bottom:1px solid var(--line);background:var(--panel2)}
.tkr-track{display:flex;gap:8px;width:max-content;padding:0 12px;animation:tkrMove var(--tkr-dur,40s) linear infinite}
.tkr:active .tkr-track,.tkr:hover .tkr-track{animation-play-state:paused}
@keyframes tkrMove{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){.tkr-track{animation:none}.tkr{overflow-x:auto}}
.tk{flex:none;display:flex;align-items:baseline;gap:6px;padding:4px 9px;border-radius:8px;
  background:var(--panel);border:1px solid var(--line)}
.tk .n{font-size:9px;font-weight:800;letter-spacing:.06em;color:var(--dim);text-transform:uppercase;white-space:nowrap}
.tk .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11.5px;font-weight:700;white-space:nowrap}
.tk .d{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;font-weight:700;white-space:nowrap}
.hero-mood{font-size:32px;font-weight:800;letter-spacing:-.01em;line-height:1.1}
.hero-why{color:var(--mut);font-size:12.5px;margin-top:5px}
.v10line{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11.5px;font-weight:700;
  margin-top:8px;color:var(--mut)}
.v10line b{color:var(--txt,#E9EEFB)}
.hchips{display:flex;gap:7px;margin-top:11px;flex-wrap:wrap}
.hc{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;font-weight:700;
  padding:5px 9px;border-radius:14px;border:1px solid var(--line2);color:var(--mut);background:none;cursor:default}
.hc.ok{color:var(--grn);border-color:rgba(47,212,139,.45);background:var(--grn-d)}
.hc.bad{color:var(--red);border-color:rgba(255,92,108,.45);background:rgba(255,92,108,.10)}
.hc.tap{cursor:pointer;text-decoration:underline dotted}
.chd{display:flex;align-items:center;justify-content:space-between;min-height:30px;margin-bottom:6px}
.chd .t{font-weight:800;font-size:14px}
.chd .rgt{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--mut);
  font-family:'IBM Plex Mono',ui-monospace,monospace}
.h-chev{color:var(--dim);font-size:16px}
.row{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-top:1px solid var(--line);min-height:44px}
.row:first-of-type{border-top:0}
.row .sym{font-weight:700;font-size:13.5px}
.row .sub{font-size:11px;color:var(--dim);margin-top:1px}
.row .r{text-align:right}
.row .pv{font-family:'IBM Plex Mono',ui-monospace,monospace;font-weight:700;font-size:13px}
.row .pc{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;margin-top:1px}
.trio{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.st{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:9px 8px;text-align:center}
.st .k{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;font-weight:700}
.st .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-weight:800;font-size:14px;margin-top:3px}
.st .p{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;font-weight:700;margin-top:2px}
.news{padding:9px 0;border-top:1px solid var(--line)}
.news:first-of-type{border-top:0}
.news .h{font-size:13px;font-weight:600;line-height:1.4}
.news .m{font-size:10.5px;color:var(--dim);margin-top:3px;display:flex;gap:8px;align-items:center;
  font-family:'IBM Plex Mono',ui-monospace,monospace}
.newsbox{max-height:300px;overflow-y:auto;-webkit-overflow-scrolling:touch}
.tools{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;padding:0 12px}
.tool{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 6px;
  text-align:center;min-height:44px;font-size:12px;font-weight:700;color:var(--mut);
  text-decoration:none;display:block}
.tool span{display:block;font-size:17px;margin-bottom:3px}
a.card-link{text-decoration:none;color:inherit;display:block}
.trendbox{margin-top:10px}
.trendhd{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;font-weight:800;
  letter-spacing:.13em;color:var(--dim);margin-bottom:6px;display:flex;justify-content:space-between}

/* ── MODELS  (was inline in mobile/models.html) ─────────────────────────────── */
/*
  cc#890 — MODELS reskinned to previews/models.html (approved card grammar ported, live data).
  cc#892 — the four breadth screens (Screeners, Sector, Holdings, FPC) are appended to the
  Everything-else grid as static entries here, because _MOBILE_DESTINATIONS lives inside
  mobile_endpoints.py which Fable cannot full-file-push safely — CC merges them into
  _MOBILE_DESTINATIONS on its cleanup pass (cc#893 item 3) and removes the static list.
  Pushed by Claude (Fable), CHARTER_OVERRIDE_08AUG2026 (session_log 17783).
  Preview grammar carried: every model is a card with a SHAPE-CODED left rail and a badge whose
  glyph repeats the shape (dot / ring / bar) so state never rides on colour alone (15913 rule 10).
  REGISTRY-DERIVED, never a typed list. Everything-else grid stays per rule 2987.
*/
.m{position:relative;display:block;background:var(--panel);border:1px solid var(--line);
  border-radius:14px;padding:13px 13px 13px 17px;margin-bottom:9px;overflow:hidden;
  text-decoration:none;color:var(--txt);min-height:60px}
.m::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--dim)}
.m.live::before{background:var(--grn);animation:breathe 2.6s ease-in-out infinite}
.m.closed::before{background:repeating-linear-gradient(180deg,var(--dim) 0 4px,transparent 4px 10px)}
.m.stale::before{background:repeating-linear-gradient(180deg,var(--amber) 0 9px,transparent 9px 15px)}
.m.error::before{background:var(--red)}
.m.off::before,.m.notrun::before{background:var(--dim)}
@keyframes breathe{0%,100%{opacity:.42}50%{opacity:1}}
@media (prefers-reduced-motion:reduce){.m.live::before{animation:none;opacity:1}}
.mtop{display:flex;align-items:center;gap:9px}
.mnm{font-size:14px;font-weight:800}
.badge{margin-left:auto;display:inline-flex;align-items:center;gap:5px;
  font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:8.5px;font-weight:700;
  letter-spacing:.11em;padding:3px 7px;border-radius:4px;border:1px solid;white-space:nowrap}
.badge.live{color:var(--grn);border-color:rgba(47,212,139,.45);background:var(--grn-d)}
.badge.closed{color:var(--dim);border-color:var(--line2);background:transparent}
.badge.stale{color:var(--amber);border-color:rgba(245,185,74,.45);background:rgba(245,185,74,.12)}
.badge.error{color:var(--red);border-color:rgba(255,92,108,.45);background:rgba(255,92,108,.10)}
.badge.off,.badge.notrun{color:var(--dim);border-color:var(--line2)}
.g-dot{width:5px;height:5px;border-radius:50%;background:currentColor}
.g-ring{width:5px;height:5px;border-radius:50%;border:1.5px solid currentColor}
.g-bar{width:6px;height:2px;background:currentColor;border-radius:1px}
.mds{font-size:11.5px;color:var(--mut);margin-top:5px;line-height:1.45}
.mft{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--dim);margin-top:7px}
.dest{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:2px;padding:0 0 4px}
.dest a{display:flex;align-items:center;min-height:44px;padding:0 12px;border-radius:11px;
  background:var(--panel);border:1px solid var(--line);color:var(--txt);text-decoration:none;
  font-size:12.5px;font-weight:700}

/* ── OPEN BOOK  (was inline in mobile/positions.html) ─────────────────────────────── */
/* cc#891 — Open Book in the shared card grammar. cc#894 row 31: QTY tag on every card.
   cc#894 item 1 (Fable direct, 17868): the C·A·R·D strip joins every position card — NOT a copy:
   the page loads the same shared files the web loads and renders via window.ScorrCardRow(sym),
   exactly as rule cc#789 commands ("ANY new surface renders the strip ONLY via
   ScorrCardStripHtml/Row"). Same strip, same four popups (Chart / Analysis / Result / Cockpit),
   full web functionality by construction. The :root bridge below maps the web components'
   CSS variables (--surface2, --cyan) onto the app's dark palette — the card files fall back to
   LIGHT hex values when a var is missing, which would break dark theme. */
/* ── cc#963/964 ROOT CAUSE: the bridge carried THREE tokens, and the cards need the whole set ──
   The app palette is declared on `.screen,.bnav` — not on :root and not on body. Custom properties
   inherit DOWN the tree, and every shared card overlay (#scorrAnaOv, #dcOv, #scorrChartOv, the
   symbol sheet, the C·A·R·D strip) is appended to document.body, which is a SIBLING of .screen.
   So none of them ever saw --panel/--txt/--dim/--mut/--line/--line2/--grn/--red/--blu/--amber.
   Measured consequences on /m/, all three of the founder's complaints from one cause:
     var(--panel,#fff)  -> the light FALLBACK wins            -> white sheet in a dark app
     var(--dim)         -> invalid at computed-value time, so
                           `color` falls back to INHERIT      -> headers/labels take the ambient
                                                                 colour and vanish
     var(--grn)         -> same                               -> delta values render inherited-white
                                                                 on the pastel green/red heat fill
     --surface2         -> WAS bridged, so it resolved dark   -> dark tiles floating on white
   The card CSS was never wrong; it was reading tokens that were scoped out of its reach. So the
   fix is the bridge, not a retheme of three card files — one correction serves A, D, the chart
   card and the strip at once, and nothing downstream has to be kept in sync by hand.
   The three ORIGINAL tokens keep their existing values (--cyan is deliberately #3aa0ff here and
   #37D3E8 inside .screen; changing it was not asked for), so nothing that already resolved moves.
   This file is served only to /m/* templates, so web cannot be reached from here by construction. */
:root{--surface2:#1a2233;--cyan:#3aa0ff;--surface:#141b2a;
  --bg:#0A0F1E;--panel:#121A33;--panel2:#182241;
  --line:rgba(148,166,210,.14);--line2:rgba(148,166,210,.24);
  --txt:#E9EEFB;--mut:#8C99BD;--dim:#5E6B8F;
  --grn:#2FD48B;--grn-d:rgba(47,212,139,.14);
  --red:#FF5C6C;--blu:#4D7CFE;--amber:#F5B94A}
.net{padding:13px 14px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:10px}
.net .k{font-size:10px;color:var(--dim);font-weight:700;letter-spacing:.1em}
.net .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:24px;font-weight:800}
.pc2{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px;margin-top:10px}
.kv{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:7px 9px;text-align:center}
.kv .k{font-size:8.5px;color:var(--dim);font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.kv .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:700;margin-top:2px}
.star{font-size:13px}
/* the strip sits right of the symbol; give its buttons the app's dark chrome */
.hl{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hl .sy{font-size:15px;font-weight:800;margin-right:auto}
.hl .pl{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;font-weight:800}

/* ── BASKETS  (was inline in mobile/qb.html) ─────────────────────────────── */
/* cc#891 — Quant Baskets in the shared card grammar (Claude direct push, 17783).
   Portfolio strip on top (total value + unrealised), then one card per basket: label, positions,
   market value, return% — tap expands the holdings rows. Preview diffs fold into founder review. */
.tot{padding:13px 14px;border-bottom:1px solid var(--line);display:flex;gap:16px}
.tot .b{flex:1}
.tot .k{font-size:9px;color:var(--dim);font-weight:700;letter-spacing:.1em;text-transform:uppercase}
.tot .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:18px;font-weight:800;margin-top:3px}
.bh{display:flex;align-items:center;gap:9px;cursor:pointer;min-height:44px}
.bh .n{font-size:14px;font-weight:800;flex:1}
.bh .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:14px;font-weight:800;text-align:right}
.bh .u{font-size:9px;color:var(--dim)}
.h2{display:flex;align-items:baseline;gap:8px;padding:8px 0 0;border-top:1px solid var(--line);margin-top:9px}
.h2 .sy{font-weight:700;font-size:12.5px;min-width:96px}
.h2 .q{font-size:10px;color:var(--dim)}
.h2 .p{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11.5px;font-weight:700}

/* ── RESULTS  (was inline in mobile/results.html) ─────────────────────────────── */
/* cc#891 — Results calendar in the shared card grammar.
   cc#893 — DEPTH: rows whose symbol HAS a plain-words analysis (result_analysis_v2, via
   /api/mobile/result_analysis_index) get a READ tag and expand the analysis inline on tap.
   No dead affordances: rows without analysis get no tag and no tap. (Fable direct, 17783.) */
.dy{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;font-weight:800;
  letter-spacing:.1em;color:var(--txt)}
.rrow{display:flex;align-items:baseline;gap:8px;padding:8px 0 0;border-top:1px solid var(--line);margin-top:8px}
.rrow:first-of-type{border-top:0;margin-top:6px}
.rrow .sy{font-weight:700;font-size:12.5px}
.rrow .nm{font-size:10px;color:var(--dim);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rrow.hasan{cursor:pointer}
.an{background:var(--panel2);border:1px solid var(--line2);border-radius:11px;
  padding:11px 12px;margin:8px 0 2px}
.an .read{font-size:13.5px;color:var(--txt);line-height:1.68}
.an .read p{margin-top:8px}
.an .read p:first-child{margin-top:0}
.an .src{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9.5px;color:var(--dim);margin-top:8px}

/* ── SCREENERS  (was inline in mobile/screeners.html) ─────────────────────────────── */
/* cc#892 — SCREENERS (Fable direct, 17783). Reads the EXISTING web API (/api/screeners) so both
   surfaces show identical members and values (16202). Read-only by design (cc#824, rule 3069).
   cc#894 item 1: the shared web C·A·R·D strip on every stock row (window.ScorrCardRow, rule
   cc#789). :root bridge maps web card CSS vars onto the dark palette. Rendering is defensive
   about key names; nothing is invented. */
/* ── cc#963/964 ROOT CAUSE: the bridge carried THREE tokens, and the cards need the whole set ──
   The app palette is declared on `.screen,.bnav` — not on :root and not on body. Custom properties
   inherit DOWN the tree, and every shared card overlay (#scorrAnaOv, #dcOv, #scorrChartOv, the
   symbol sheet, the C·A·R·D strip) is appended to document.body, which is a SIBLING of .screen.
   So none of them ever saw --panel/--txt/--dim/--mut/--line/--line2/--grn/--red/--blu/--amber.
   Measured consequences on /m/, all three of the founder's complaints from one cause:
     var(--panel,#fff)  -> the light FALLBACK wins            -> white sheet in a dark app
     var(--dim)         -> invalid at computed-value time, so
                           `color` falls back to INHERIT      -> headers/labels take the ambient
                                                                 colour and vanish
     var(--grn)         -> same                               -> delta values render inherited-white
                                                                 on the pastel green/red heat fill
     --surface2         -> WAS bridged, so it resolved dark   -> dark tiles floating on white
   The card CSS was never wrong; it was reading tokens that were scoped out of its reach. So the
   fix is the bridge, not a retheme of three card files — one correction serves A, D, the chart
   card and the strip at once, and nothing downstream has to be kept in sync by hand.
   The three ORIGINAL tokens keep their existing values (--cyan is deliberately #3aa0ff here and
   #37D3E8 inside .screen; changing it was not asked for), so nothing that already resolved moves.
   This file is served only to /m/* templates, so web cannot be reached from here by construction. */
:root{--surface2:#1a2233;--cyan:#3aa0ff;--surface:#141b2a;
  --bg:#0A0F1E;--panel:#121A33;--panel2:#182241;
  --line:rgba(148,166,210,.14);--line2:rgba(148,166,210,.24);
  --txt:#E9EEFB;--mut:#8C99BD;--dim:#5E6B8F;
  --grn:#2FD48B;--grn-d:rgba(47,212,139,.14);
  --red:#FF5C6C;--blu:#4D7CFE;--amber:#F5B94A}
.kvs{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.kv2{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9.5px;color:var(--mut);
  border:1px solid var(--line);border-radius:5px;padding:3px 7px}
.kv2 b{color:var(--txt);font-weight:700}
.sc-srow{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.sc-srow .sy{font-weight:800;font-size:14px;margin-right:auto}
.sc-srow .px2{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:700}
.sc-srow .dd{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:700}

/* ── SECTOR  (was inline in mobile/sector.html) ─────────────────────────────── */
/* cc#892 — SECTOR (Fable direct, 17783). Reads the EXISTING web APIs (/api/sectors +
   /api/sector/rotation) — one implementation, identical numbers (16202). Ratings list with
   score bars in the GVM pillar grammar; rotation cards above when the API answers. Defensive
   about key names; nothing invented. */
.se-srow{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--line);
  border-radius:12px;padding:11px 13px;margin-bottom:7px;min-height:48px}
.se-srow .nm{font-size:13px;font-weight:800;flex:1;min-width:0}
.se-srow .sub2{font-size:9.5px;color:var(--dim);margin-top:1px}
.se-srow .bar{flex:1.2;height:6px;border-radius:4px;background:rgba(148,166,210,.14);overflow:hidden}
.se-srow .fill{height:100%;border-radius:4px}
.se-srow .sc{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;font-weight:800;min-width:34px;text-align:right}

/* ── V8  (was inline in mobile/v8.html) ─────────────────────────────── */
/*
  cc#891 — V8 reskinned to previews/v8.html (tiered card stack, rails legend).
  cc#894 (Fable direct, 17868) — founder's row decisions: SLOTS in gate, FUNNEL per basket,
  TRADE LEDGER rows + DAY LOG, C·A·R·D strip on signal + ledger rows (shared web components,
  rule cc#789). PARITY FIX 08-Aug: the ledger card reads /api/mobile/trades' fresh-era SUMMARY
  (net of Rs.500/trade brokerage, W/L = TARGET/SL) so this card and the web Master Dashboard can
  never disagree — the founder caught the all-time-vs-fresh drift the same day it shipped.
  Raw Data / Strategy Matrix / thresholds remain web-only by founder ruling.
*/
/* ── cc#963/964 ROOT CAUSE: the bridge carried THREE tokens, and the cards need the whole set ──
   The app palette is declared on `.screen,.bnav` — not on :root and not on body. Custom properties
   inherit DOWN the tree, and every shared card overlay (#scorrAnaOv, #dcOv, #scorrChartOv, the
   symbol sheet, the C·A·R·D strip) is appended to document.body, which is a SIBLING of .screen.
   So none of them ever saw --panel/--txt/--dim/--mut/--line/--line2/--grn/--red/--blu/--amber.
   Measured consequences on /m/, all three of the founder's complaints from one cause:
     var(--panel,#fff)  -> the light FALLBACK wins            -> white sheet in a dark app
     var(--dim)         -> invalid at computed-value time, so
                           `color` falls back to INHERIT      -> headers/labels take the ambient
                                                                 colour and vanish
     var(--grn)         -> same                               -> delta values render inherited-white
                                                                 on the pastel green/red heat fill
     --surface2         -> WAS bridged, so it resolved dark   -> dark tiles floating on white
   The card CSS was never wrong; it was reading tokens that were scoped out of its reach. So the
   fix is the bridge, not a retheme of three card files — one correction serves A, D, the chart
   card and the strip at once, and nothing downstream has to be kept in sync by hand.
   The three ORIGINAL tokens keep their existing values (--cyan is deliberately #3aa0ff here and
   #37D3E8 inside .screen; changing it was not asked for), so nothing that already resolved moves.
   This file is served only to /m/* templates, so web cannot be reached from here by construction. */
:root{--surface2:#1a2233;--cyan:#3aa0ff;--surface:#141b2a;
  --bg:#0A0F1E;--panel:#121A33;--panel2:#182241;
  --line:rgba(148,166,210,.14);--line2:rgba(148,166,210,.24);
  --txt:#E9EEFB;--mut:#8C99BD;--dim:#5E6B8F;
  --grn:#2FD48B;--grn-d:rgba(47,212,139,.14);
  --red:#FF5C6C;--blu:#4D7CFE;--amber:#F5B94A}
.headx{padding:15px 14px 11px;border-bottom:1px solid var(--line);display:flex;align-items:flex-start;gap:10px}
.headx .t{flex:1;min-width:0}
.headx h1{font-size:19px;font-weight:800;letter-spacing:-.01em}
.headx .s{font-size:10.5px;color:var(--dim);margin-top:3px;font-family:'IBM Plex Mono',ui-monospace,monospace}
.gatec{font-size:9px;font-weight:800;letter-spacing:.07em;padding:3px 8px;border-radius:6px;white-space:nowrap;border:1px solid var(--line2);color:var(--dim)}
.gatec.open{background:var(--grn-d);color:var(--grn);border-color:rgba(47,212,139,.4)}
.gatec.shut{background:rgba(255,92,108,.10);color:var(--red);border-color:rgba(255,92,108,.4)}
.slots{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9.5px;color:var(--dim);margin-top:3px}
.slots b{color:var(--mut);font-weight:700}
.tierlab{font-size:9.5px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;
  color:var(--dim);padding:0 15px;margin:18px 0 8px;display:flex;align-items:center;gap:9px}
.tierlab::after{content:'';flex:1;height:1px;background:var(--line)}
.stack{padding:0 12px}
.vc{position:relative;display:block;width:100%;text-align:left;background:var(--panel);
  border:1px solid var(--line);border-radius:14px;padding:13px 13px 13px 17px;margin-bottom:9px;
  overflow:hidden;font-family:inherit;color:var(--txt);cursor:pointer;min-height:60px;
  text-decoration:none;border-width:1px}
.vc:active{background:var(--panel2)}
.vc::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px}
.vc.live::before{background:var(--grn);animation:breathe 2.6s ease-in-out infinite}
.vc.stale::before{background:repeating-linear-gradient(180deg,var(--amber) 0 9px,transparent 9px 15px)}
.vc.closed::before{background:repeating-linear-gradient(180deg,var(--dim) 0 4px,transparent 4px 10px)}
.vc.ref::before{background:var(--blu);top:14px;bottom:14px;border-radius:0 3px 3px 0}
@keyframes breathe{0%,100%{opacity:.42}50%{opacity:1}}
@media (prefers-reduced-motion:reduce){.vc.live::before{animation:none;opacity:1}}
.vrow{display:flex;align-items:center;gap:11px;min-height:44px}
.ttl{flex:1;min-width:0}
.ttl .n{font-size:13.5px;font-weight:700;letter-spacing:.01em}
.ttl .m{font-size:10.5px;color:var(--dim);margin-top:3px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.val{text-align:right;white-space:nowrap}
.val .v{font-family:'IBM Plex Mono',ui-monospace,monospace;font-variant-numeric:tabular-nums;
  font-size:17px;font-weight:700;line-height:1;letter-spacing:-.01em}
.val .u{font-size:9.5px;color:var(--dim);margin-top:4px;letter-spacing:.02em}
.v-chev{color:var(--dim);font-size:17px;flex-shrink:0;width:10px;text-align:center;line-height:1;transition:transform .15s}
.vc.openx .v-chev{transform:rotate(90deg)}
.grn{color:var(--grn)}.redx{color:var(--red)}.mut{color:var(--mut)}
.syrow{display:flex;align-items:center;gap:8px;padding:8px 0 0 2px;border-top:1px solid var(--line);margin-top:10px;flex-wrap:wrap}
.syrow .sy2{font-weight:700;font-size:12.5px;margin-right:auto}
.syrow .at{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10.5px;color:var(--dim)}
.more{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--dim);padding:8px 0 0 2px}
.fun{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--mut);
  padding:8px 0 0 2px;border-top:1px solid var(--line);margin-top:10px;line-height:1.6}
.fun b{color:var(--txt)}
/* ── cc#986 · per-basket FILTER FUNNEL on /m/v8 ───────────────────────────────────────────────
   EVERY class here carries the `fn-` prefix, including the INNER ones. I first wrote the inner
   spans as `.g/.p/.n/.v/.x` scoped under `.fn-st`/`.fn-gate`, and the render showed exactly what
   this app has now been bitten by six times (cc#964 .v/.m, cc#966 .m, cc#967 .v, cc#974 .a): the
   sheet's own generic rules won, and the stage names came out inside red-barred boxes with the
   gate values in yellow-barred pills. Descendant scoping does NOT protect a short class name from
   a rule that matches it globally. Prefix the leaves too. */
.fn-wrap{border-top:1px solid var(--line);margin-top:10px;padding-top:9px}
.fn-h{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9.5px;font-weight:800;
  letter-spacing:.11em;text-transform:uppercase;color:var(--dim);padding:0 2px 7px}
.fn-st{display:grid;grid-template-columns:minmax(0,1fr) 34px 46px;gap:3px 8px;align-items:center;
  font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;padding:0 2px}
.fn-g{color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fn-p{color:var(--txt);font-weight:700;text-align:right}
.fn-k{color:var(--dim);text-align:right}
.fn-bar{grid-column:1/-1;height:2px;background:var(--line);border-radius:2px;margin:0 0 4px}
.fn-bar i{display:block;height:100%;border-radius:2px;background:var(--grn)}
.fn-sum{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--mut);
  padding:9px 2px 2px;line-height:1.6}
.fn-sum b{color:var(--txt)}
.fn-sym{display:flex;align-items:center;gap:9px;width:100%;min-height:44px;padding:0 2px;
  background:none;border:none;border-top:1px solid var(--line);color:inherit;font-family:inherit;
  text-align:left;cursor:pointer}
.fn-s{font-weight:700;font-size:12.5px;margin-right:auto}
.fn-b{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--dim)}
.fn-x{color:var(--dim);font-size:13px}
.fn-body{padding:2px 2px 10px}
.fn-gate{display:grid;grid-template-columns:minmax(0,1fr) auto 16px;gap:2px 8px;align-items:center;
  font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;padding:4px 0;
  border-bottom:1px solid var(--line)}
.fn-n{color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fn-v{color:var(--txt);text-align:right;white-space:nowrap}
.fn-q{color:var(--dim);grid-column:1/-1;font-size:9px}
.fn-ok{color:var(--grn);text-align:center;font-weight:800}
.fn-no{color:var(--red);text-align:center;font-weight:800}
.trow{display:flex;align-items:center;gap:8px;padding:7px 0 0 2px;border-top:1px solid var(--line);margin-top:8px;font-size:11.5px;flex-wrap:wrap}
.trow .sy2{font-weight:700;margin-right:auto}
.trow .mono2{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10.5px}
.legend{margin:22px 12px 0;padding:13px 14px;background:var(--panel);border:1px solid var(--line);border-radius:12px}
.legend .h{font-size:9.5px;font-weight:800;letter-spacing:.13em;text-transform:uppercase;color:var(--dim);margin-bottom:11px}
.v-lrow{display:flex;gap:11px;align-items:flex-start;margin-bottom:10px;font-size:11.5px;color:var(--mut);line-height:1.5}
.lbar{width:3px;height:30px;border-radius:2px;flex-shrink:0;margin-top:1px}
.v-lrow b{color:var(--txt);font-weight:700}

/* ── cc#909: the two floating pills move into the Scorr logo (founder 08-Aug) ────────────────
   main.py's auth_gate injects #scorr-lo (Logout) and #scorr-th (theme) into every authenticated
   page, fixed top-right at 45% opacity. On a phone they float over the content — a ghost the
   founder keeps half-seeing. They are HIDDEN HERE rather than excluded in main.py, because this
   stylesheet is loaded by /m/* templates and nothing else: the web pills cannot be affected by
   this rule even in principle, so no auth or injection logic has to be touched to be sure of it.
   The two functions are not lost — scorr_card_common.js hangs them off a tap on the Scorr logo,
   and that menu is the .lgm below. */
#scorr-lo,#scorr-th{display:none!important}
.head{position:relative}
h1.lgt{cursor:pointer}
h1.lgt::after{content:'\\25BE';font-size:11px;color:var(--dim);margin-left:6px;vertical-align:middle}
.lgm{position:absolute;left:0;top:100%;margin-top:6px;z-index:70;min-width:180px;
  background:var(--panel);border:1px solid var(--line2);border-radius:12px;padding:6px;
  box-shadow:0 14px 34px rgba(0,0,0,.45);display:none}
.lgm.open{display:block}
.lgm button{display:flex;align-items:center;gap:10px;width:100%;min-height:44px;padding:0 12px;
  background:none;border:0;border-radius:8px;color:var(--txt);font-family:inherit;
  font-size:13px;font-weight:700;cursor:pointer;text-align:left}
.lgm button:active{background:var(--panel2)}
.lgm .ic{width:16px;text-align:center;font-size:14px;color:var(--mut)}

/* ── cc#898: the universal symbol-tap sheet ──────────────────────────────────────────────────
   A bottom sheet, not a centre modal: on a phone the reachable half of the screen is the bottom
   one, and the C/A/R/D letters are the thing being reached for. The affordance is a faint dotted
   underline on symbol text — enough to read as tappable without turning every list into links. */
.symsheet{position:fixed;inset:0;z-index:80;background:rgba(4,8,18,.62);display:none;
  align-items:flex-end;overscroll-behavior:contain}
.symsheet.open{display:flex}
.symsheet-bd{width:100%;background:var(--panel);border-top:1px solid var(--line2);
  border-radius:16px 16px 0 0;padding:6px 14px calc(18px + env(safe-area-inset-bottom))}
.symsheet-hd{display:flex;align-items:center;gap:10px;min-height:46px}
.symsheet-hd b{flex:1;font-size:15px;font-weight:800;letter-spacing:.02em}
.symsheet-x{min-width:44px;min-height:44px;background:none;border:0;color:var(--mut);
  font-size:17px;cursor:pointer;font-family:inherit}
.symsheet-strip{padding:2px 0 8px}
/* cc#933: marker legend, quiet by design — it explains, it does not compete. */
.mk-legend{margin:10px 12px 0;padding:9px 11px;background:var(--panel2);border:1px solid var(--line);
  border-radius:10px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;
  line-height:1.75;color:var(--dim)}
.sym,.sy,.sy2,.rs,.rsym,.vsym{text-decoration:underline dotted rgba(148,166,210,.35);
  text-underline-offset:3px;cursor:pointer}
[data-scorr-skip] .sym,[data-scorr-skip] .sy,[data-scorr-skip] .sy2{text-decoration:none}
"""


@router.get("/static/mobile_app.css")
def mobile_app_css():
    from fastapi.responses import Response
    return Response(MOBILE_CSS, media_type="text/css",
                    headers={"Cache-Control": "no-store"})
