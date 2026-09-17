"""
aicio_app_mobile.py — cc#1904 APP BUILD: AI CIO section page backend
(design_refs/scorr_app_aicio_R1.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

GATE CHECKED FIRST, per the card's own instruction: is max_chats really the AI CIO store?
Queried information_schema for every table matching chat/cio/conversation/ai_/assistant in the
public schema — max_chats is the ONLY one. Fable's premise holds; the card's evidence (2 rows) is
confirmed live: exactly 2 rows, "max_scorr_arpit" (Max session) and "ask_scorr_arpit" (Ask
session).

"LAST USED" — READ THIS BEFORE TRUSTING THE REF'S "13 Jun" FIGURE. The ref's own closing note
explicitly invites this check: "If the count is wrong... that is a finding worth having before
the build, not after." The COUNT (2) is right. The DATE is not what it looks like: 13-Jun-2026 is
max_scorr_arpit's created_at (when the session row was first made), not when it was last USED.
That same row's updated_at is 2026-08-05 01:54:08 — the chat kept being used for nearly two
months after its creation date. MAX(updated_at) across both rows is 05-Aug-2026, not 13-Jun. This
build reports the TRUE last-activity date (05-Aug), not the ref's created_at-based one — the
surface is still barely used (2 stored conversations, over a month idle as of today), just not
AS stale as "13 Jun" implies. Flagged, not silently reproduced — same discipline as every other
ref-vs-live discrepancy this session (cc#1892, cc#1899).

REACH COUNTS — sectors (128) and result write-ups (774) reused from the SAME live counts cc#1900
and cc#1901 already verified this session (sector_ratings latest score_date, result_analysis_v2
total), not re-derived with a second query text that could drift. Baskets (7) reused from the
same is_active AND basket_name NOT LIKE 'finz_%' registry filter cc#1892's qb_app_mobile.py
already established as the ONE function every caller goes through — mirrored here as a single
query, not imported, since this file has no other qb_app_mobile dependency to justify the import.
Ratings/Trade-scores rows carry no live count in the ref (their "b" slot is a qualitative label,
GVM/TC, not a number) and stay that way here — never inventing a count the ref itself did not ask
for.

do_not_touch honoured: the CIO answering pipeline (scorr_chat_endpoint.py) and the web /cio page —
read-only reference only (grepped to confirm no reusable helper exists for max_chats; none did,
so this file queries it directly rather than importing anything from that pipeline).
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
import psycopg

from mobile_endpoints import _guard, _json_safe, _page

router = APIRouter()


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


@router.get("/m/aicio", response_class=HTMLResponse)
def m_aicio():
    """cc#1904: AI CIO section page. Reachable via the NAV array's mobile More sheet — no prior
    /m/ AI CIO route or Home-grid tile existed (same shape as cc#1903's Mutual Funds page)."""
    return _page("aicio")


@router.get("/api/mobile/aicio_app")
@_json_safe
def mobile_aicio_app(request: Request):
    g = _guard(request)
    if g:
        return g

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MAX(updated_at) FROM max_chats")
        conv_count, last_used = cur.fetchone()

        cur.execute("SELECT COUNT(*) FROM sector_ratings WHERE score_date = (SELECT MAX(score_date) FROM sector_ratings)")
        sectors = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM result_analysis_v2")
        results = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM quant_basket_registry WHERE is_active AND basket_name NOT LIKE 'finz\\_%'")
        baskets = cur.fetchone()[0] or 0

    return {
        "status": "ok",
        "key_metrics": {
            "conversations": conv_count or 0,
            "last_used": last_used.date().isoformat() if last_used else None,
            "ratings_reach": "full",
            "sectors": sectors,
            "result_writeups": results,
        },
        "what_it_is": {
            "message": "Ask a question about a stock, a sector or your own portfolio and get an "
                       "answer built from the same data the rest of the app uses.",
            "footnote": "Answers cite what they were built from.",
        },
        "your_conversations": {
            "count": conv_count or 0,
            "last_used": last_used.date().isoformat() if last_used else None,
            "message": (f"Only {conv_count} chats are stored and the last one was active "
                       f"{last_used.date().isoformat()}. This card shows the real count rather "
                       "than an inviting empty state, because the honest read is that the "
                       "surface is barely used.") if conv_count else
                       "No conversations stored yet.",
        },
        "reach": {
            "rows": [
                {"label": "Ratings", "note": "full universe", "value": "GVM"},
                {"label": "Trade scores", "note": "all futures names", "value": "TC"},
                {"label": "Baskets", "note": f"{baskets} on the app", "value": "Quant"},
                {"label": "Sectors", "note": None, "value": str(sectors)},
                {"label": "Results write-ups", "note": None, "value": str(results)},
            ],
            "message": "It answers from stored data only. It does not invent a number that is not there.",
        },
        "limits": {
            "message": "It will not give a number it cannot source, and it will not tell you "
                       "what to buy. It explains what the data says and leaves the decision with you.",
        },
    }


# ══ cc#2171 AICIO V1 -- the cards rail ═══════════════════════════════════════════════════════════
# THREE test cards for the founder's glimpse (Market Mood, Index Intel, V8 open book). Each builder
# calls the web's OWN function IN-PROCESS (the cc#977 v8lower pattern) -- never a second SQL text
# that could drift from the page it mirrors. Registry, not a hardcoded list: the next five cards
# are an append to AICIO_CARDS. A builder that raises, or runs past CARD_TIMEOUT_S, comes back as
# available:false with the reason; the rail hides it and the bubble says 'N of M cards available'.
# NO chat, NO model call, NO max_chats write in V1 -- the V2 routing is recorded in the Fable Room.
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout
from datetime import datetime, timedelta, timezone

log = logging.getLogger("scorr.aicio_app")
_IST = timezone(timedelta(hours=5, minutes=30))
CARD_TIMEOUT_S = 8.0


def _now_ist():
    return datetime.now(_IST).replace(tzinfo=None)


def _inr(v):
    """Signed rupees, whole, Indian grouping (12,34,567). None -> em dash."""
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    n = int(round(abs(x)))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("−" if x < 0 else "") + "₹" + s


def _card_market_mood():
    """MARKET MOOD -- v8_endpoints.market_mood(), the same call behind /api/v8/market_mood."""
    from v8_endpoints import market_mood
    m = market_mood()
    checks = m.get("checks") or []
    passed = sum(1 for c in checks if c.get("pass"))
    indet = sum(1 for c in checks if c.get("indeterminate"))
    adr = next((c.get("value") for c in checks if c.get("filter") == "ADR"), None)
    ad = m.get("adr_detail") or {}
    line1 = "%d of %d checks passed" % (passed, len(checks))
    line1 += (" · %d indeterminate" % indet) if indet else ""
    line1 += (" · ADR %.2f" % float(adr)) if adr is not None else " · ADR — (breadth feed down)"
    line2 = "%s buy / %s sell slots" % (m.get("buy_slots"), m.get("sell_slots"))
    if ad.get("advances") is not None and ad.get("declines") is not None:
        line2 += " · A/D %s/%s" % (ad.get("advances"), ad.get("declines"))
    src = "live intraday" if m.get("nifty_source") == "live_intraday" else "EOD fallback"
    return {"headline": str(m.get("mood") or "—"), "lines": [line1, line2],
            "as_of": "%s · Nifty from %s" % (m.get("checked_at") or "—", src),
            "raw": {"mood": m.get("mood"), "fails": m.get("fails"), "buy_slots": m.get("buy_slots"),
                    "sell_slots": m.get("sell_slots"), "checks": len(checks), "passed": passed}}


def _card_index_intel():
    """INDEX INTEL -- v10_endpoints.v10_signal(symbol), the same call behind /api/v10/signal."""
    from v10_endpoints import v10_signal
    out = {}
    for sym in ("NIFTY50", "BANKNIFTY"):
        s = v10_signal(sym)
        if not isinstance(s, dict) or s.get("error") or (s.get("status") not in (None, "ok")):
            raise RuntimeError("%s: %s" % (sym, (s or {}).get("error") or (s or {}).get("status") or "no signal"))
        out[sym] = s

    def line(label, s):
        p = s.get("price")
        if p is None:
            return "%s — (no closed bar yet)" % label
        return "%s %s · trend %s · signal %s" % (label, format(float(p), ",.0f"), s.get("st_dir") or "—", s.get("signal") or "—")
    n, b = out["NIFTY50"], out["BANKNIFTY"]
    head = ("NIFTY %s" % format(float(n["price"]), ",.0f")) if n.get("price") is not None else "NIFTY —"
    if n.get("st_dir"):
        head += " · " + str(n["st_dir"])
    return {"headline": head, "lines": [line("NIFTY50", n), line("BANKNIFTY", b)],
            "as_of": "%s · latest closed 10m bar" % (n.get("as_of") or b.get("as_of") or "—"),
            "raw": {"nifty": {k: n.get(k) for k in ("price", "st_dir", "signal", "gate_zone", "as_of")},
                    "banknifty": {k: b.get(k) for k in ("price", "st_dir", "signal", "gate_zone", "as_of")}}}


def _card_v8_book():
    """V8 OPEN BOOK -- book_canon (V8_PNL_CANON_V1, rule 13) + the era caption, the same source as
    /api/mobile/v8book's summary block. Reused, never recomputed."""
    from mobile_endpoints import _conn as _mconn
    from v8_book_canon import book_canon
    from v8_era import era_block
    with _mconn() as conn:
        canon = book_canon(conn, era="fresh")
        with conn.cursor() as cur:
            era = era_block(cur)
    if not isinstance(canon, dict) or canon.get("error"):
        raise RuntimeError((canon or {}).get("error") or "book_canon returned nothing")
    n_open = int(((canon.get("long") or {}).get("n") or 0) + ((canon.get("short") or {}).get("n") or 0))
    unrl, real = canon.get("unrealised"), canon.get("realised")
    wr, dec = canon.get("win_rate"), canon.get("decided")
    line1 = "%d open · unrealised %s" % (n_open, _inr(unrl))
    line2 = "realised %s · win rate %s of %s decided" % (_inr(real), ("%s%%" % wr) if wr is not None else "—", dec if dec is not None else "—")
    head = _inr(unrl) if unrl is not None else ("%d open" % n_open)
    now = _now_ist()
    return {"headline": head, "lines": [line1, line2],
            "as_of": "%s · live CMP at %s IST" % (era.get("era_label") or "fresh era", now.strftime("%H:%M")),
            "raw": {"open": n_open, "unrealised": unrl, "realised": real, "win_rate": wr, "decided": dec,
                    "trades": canon.get("trades"), "era_label": era.get("era_label")}}


AICIO_CARDS = [
    {"key": "market_mood", "title": "Market Mood", "builder": _card_market_mood, "href": "/m/home",
     "source": "V8 market-mood gate · /api/v8/market_mood"},
    {"key": "index_intel", "title": "Index Intel", "builder": _card_index_intel, "href": "/m/v10",
     "source": "V10 ST+EMA signal · /api/v10/signal"},
    {"key": "v8_book", "title": "V8 open book", "builder": _card_v8_book, "href": "/m/v8",
     "source": "book_canon (V8_PNL_CANON_V1) · /api/mobile/v8book"},
]


def build_cards(registry=None, timeout_s: float = CARD_TIMEOUT_S):
    """Every registry card, in registry order. A builder that raises or overruns comes back
    available:false with the reason -- the other cards are untouched."""
    registry = AICIO_CARDS if registry is None else registry
    ex = ThreadPoolExecutor(max_workers=max(1, len(registry)))
    futs = [(c, ex.submit(c["builder"])) for c in registry]
    cards = []
    for c, fut in futs:
        base = {"key": c["key"], "title": c["title"], "href": c["href"], "source": c.get("source", "")}
        t0 = time.time()
        try:
            d = fut.result(timeout=timeout_s)
            if not isinstance(d, dict):
                raise RuntimeError("builder returned %s" % type(d).__name__)
            cards.append({**base, "available": True, **d, "built_ms": int((time.time() - t0) * 1000)})
        except _FutTimeout:
            cards.append({**base, "available": False, "reason": "timed out after %.0f s" % timeout_s})
        except Exception as e:                       # one bad card never takes the rail down
            log.warning("aicio card %s failed: %s", c["key"], e)
            cards.append({**base, "available": False, "reason": "%s: %s" % (type(e).__name__, str(e)[:160])})
    ex.shutdown(wait=False)   # never wait on a hung builder
    return cards


@router.get("/api/mobile/aicio_app/cards")
@_json_safe
def mobile_aicio_cards(request: Request):
    """cc#2171: the V1 cards rail. Additive -- the cc#1904 endpoint above stays for any other reader."""
    g = _guard(request)
    if g:
        return g
    from mobile_endpoints import rail_state
    from nse_holidays import is_trading_day
    now = _now_ist()
    cards = build_cards()
    last_ts = None
    for c in cards:                                  # the freshest tick the cards carry: the 10m bar
        if c.get("key") == "index_intel" and c.get("available"):
            try:
                last_ts = datetime.strptime(str((c.get("raw") or {}).get("nifty", {}).get("as_of"))[:16], "%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                last_ts = None
    feed = rail_state(last_ts, 10, now, is_trading_day(now.date()))
    return {"version": "V1 -- cards only; chat lands in version 2",
            "as_of": now.strftime("%Y-%m-%d %H:%M IST"), "feed": feed,
            "available": sum(1 for c in cards if c.get("available")), "total": len(cards), "cards": cards,
            "basis": "each card calls the web's own function in-process (no second SQL); a card that fails says why"}
