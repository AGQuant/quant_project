"""
mobile_endpoints.py — cc#874 MOBILE PROMOTION (founder 06-Aug-2026, P1).

THE PROMOTION PRINCIPLE (PREVIEW_PROMOTION_DECISION_V1, session_log 16230): real data NEVER goes
into previews/*.html. Each approved screen is REBUILT here as its own routed template that imports
shared modules. The previews stay frozen dummy forever — they are the founder's review record and
Claude.ai owns them (16159).

SCOPE — 12 IMP SCREENS, NOT 18. session_log 16916 (MOBILE_SCREEN_TRIAGE_V1, 12:19 IST 06-Aug) is
LATER than cc#874's filing and names the card explicitly: "Scope trims from 18 to 12 screens: wire
the IMP list; MAY screens keep their previews and their contract entries but are NOT wired in
cc#874; scanners waits for the redesigned preview; v8_surfaces content folds under v8/positions."
So the MAY five (models, models_tools, holdings, fpc, sector) and scanners are deliberately not
routed here. Under-wiring is additive to fix; over-wiring is not.

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
import logging
from datetime import datetime, time as dt_time

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


def _conn():
    return psycopg.connect(DATABASE_URL)


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
    age = (now_ist - last_ts).total_seconds() / 60.0
    if not in_session:
        return {"state": "CLOSED", "age_min": round(age, 1),
                "why": f"market closed · last update {_ago(age)}"}
    if age <= cadence_min * 2:
        return {"state": "LIVE", "age_min": round(age, 1), "why": f"updated {_ago(age)}"}
    return {"state": "STALE", "age_min": round(age, 1),
            "why": f"last update {_ago(age)} — expected every {cadence_min:g} min"}


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
def mobile_intel(request: Request, hours: int = 48, limit: int = 40):
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
            cur.execute("""
                SELECT polished_id, headline, summary, full_summary, category, sentiment, impact,
                       mentioned_symbols, source_name, display_time
                FROM v_polished_articles
                WHERE display_time >= NOW() - (%s || ' hours')::interval
                ORDER BY display_time DESC NULLS LAST, polished_id DESC
                LIMIT %s
            """, (hours, limit))
            arts = _rows(cur)
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
        "coverage": f"showing {len(editorials) + len(feed)} of {head['n']}",
    }


@router.get("/m/intel", response_class=HTMLResponse)
def m_intel():
    return _page("intel")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# V8 POSITIONS — contract: 14 values, 13 SOURCED, 1 SOURCED_BUT_EMPTY (the pivot star).
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/v8_positions")
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
# SHARED — server clock, so no promoted screen ever derives a session state from the phone's clock.
# ══════════════════════════════════════════════════════════════════════════════════════════════
@router.get("/api/mobile/now")
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
.chip{flex:0 0 auto;min-height:34px;display:inline-flex;align-items:center;gap:6px;
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
"""


@router.get("/static/mobile_app.css")
def mobile_app_css():
    from fastapi.responses import Response
    return Response(MOBILE_CSS, media_type="text/css",
                    headers={"Cache-Control": "no-store"})
