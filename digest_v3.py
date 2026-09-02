"""
digest_v3.py — cc#846 DAILY DIGEST V3.

Pixel reference: design_refs/scorr_digest_v3_R2.html (commit 120a80b) — a DESIGN reference with a
hardcoded 04-Aug 06:45 IST snapshot. This module supplies the live bindings behind those pixels.

BINDING SPEC: session_log id=965 (daily_digest_format_v2 v2.3). V3 is a PRESENTATION upgrade, not a
re-spec, so every section 965 defines still ships. The reference consolidates ten sections into
seven panels; nothing is dropped. The mapping, stated once so a future reader can audit it:

    965 §1  Global Indices      -> 01 Global Tape        (two-level DAY/WEEK, cc#842 component)
    965 §2  Domestic Indices    -> 02 Index Ladders      (CMP row inside the ladder)
    965 §3  Support Levels      -> 02 Index Ladders      (S1/S2 rungs)
    965 §4  Pivot Points        -> 02 Index Ladders      (PP/R1/R2 rungs)
    965 §5  PCR Trend           -> 03 Internals          (with ADR breadth)
    965 §6  Top Domestic News   -> 05 What Moved
    965 §7  Events/Results Today-> 04 Reporting Today
    965 §8  Results Yesterday   -> 07 Yesterday's Results · Scored   (the V3 upgrade)
    965 §9  Global Events       -> 06 Global Events
    965 §10 Market Read         -> 08 Market Read        (kept as its own closing section)

HARD CONSTRAINTS (cc#846): READ-ONLY. No new tables, no new fetch jobs, no new scheduler entries.

THE RULE THIS MODULE EXISTS TO ENFORCE — RESULT-STORY MATCHING.
Matching polished_news to a ticker on mentioned_symbols ALONE produces WRONG CONTENT. Verified
against the live DB while building this: KEI had 1 loose match (a POLYCAB headline), JSL 2, UPL 2,
and ATHERENERG 7 — the loose rule would have attached a confident, wrong story to each. All three
conditions are therefore required together: ticker in mentioned_symbols AND a result-shaped headline
AND published within 20 hours. Under the strict rule those four drop to 0 matches and render an
explicit PENDING pill. They are NEVER dropped from the table — dropping silently misrepresents a
"top 10 by market cap" list, which is the one thing that list promises.

SECTION READS are computed from the numbers already on the page (965 MARKET_READ_FRAMEWORK:
"every claim tied to a number already shown above"). They are deterministic, not model-generated —
consistent with the $0/native doctrine and with "no new fetch jobs".
"""

import os
import logging
from datetime import datetime, timedelta, timezone

from price_sources import not_fut   # cc#1053 INDEX_SYMBOL_CONVENTION_V1
from pcr_mood import compose_live   # cc#1568: ONE PCR mood composer (session_log 36200)
from typing import Dict, Any, List, Optional

import psycopg2
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

log = logging.getLogger("scorr.digest_v3")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

INDEXES = ["NIFTY50", "BANKNIFTY"]

# cc#852: the EOD set is no longer a hardcoded list. Shanghai is dropped, US VIX is retired from
# the tape, and India VIX ticks live off the NSE feed — so the old five-symbol constant named three
# symbols that either no longer exist on the tape or are not EOD at all. The tier now comes from
# global_heatstrip.build_strip(), which derives it per tile from whether a tick actually arrived,
# and the amber note is built from THAT result rather than from a constant that has to be
# remembered. A hardcoded list is exactly what went stale here.

RESULT_KEYWORDS = r"(profit|loss|revenue|income|Q1|results|rises|narrows|falls)"
RESULT_WINDOW_HOURS = 20


def _conn():
    return psycopg2.connect(_DB)


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _pct(a, b) -> Optional[float]:
    a, b = _f(a), _f(b)
    return round((a / b - 1) * 100, 2) if (a is not None and b) else None


def _ist_now() -> datetime:
    return datetime.now(IST)


def market_state(now: datetime) -> Dict[str, Any]:
    """PRE-OPEN (with countdown) / OPEN / CAS / CLOSED — computed from the clock, never assumed."""
    t = now.time()
    wd = now.weekday() < 5
    if not wd:
        return {"state": "CLOSED", "label": "CLOSED · WEEKEND", "mins_to_open": None}
    if t < datetime.strptime("09:15", "%H:%M").time():
        target = now.replace(hour=9, minute=15, second=0, microsecond=0)
        mins = int((target - now).total_seconds() // 60)
        return {"state": "PRE-OPEN", "label": f"PRE-OPEN · {mins//60}h {mins%60}m TO BELL",
                "mins_to_open": mins}
    if t < datetime.strptime("15:15", "%H:%M").time():
        return {"state": "OPEN", "label": "OPEN", "mins_to_open": 0}
    if t < datetime.strptime("15:30", "%H:%M").time():
        return {"state": "CAS", "label": "CAS · CLOSING AUCTION", "mins_to_open": 0}
    return {"state": "CLOSED", "label": "CLOSED", "mins_to_open": None}


# ── cc#1228 · FRESHNESS, NOT A LITERAL ───────────────────────────────────────────────────────
# WHAT WAS WRONG. The ladders section returned `"tier": "LIVE"` as a hardcoded string, and each
# index flipped its own tier to LIVE the moment ANY intraday_prices row existed for it — with no
# test of how old that row was. On a Sunday the newest row is Friday's 15:30 close, so the card
# showed a green LIVE pill beside an honest "as of 21 Aug, 15:30". The as-of line was right and
# the badge contradicted it.
#
# THE NAIVE-COLUMN TRAP, which is the whole reason this is a function and not an inline compare.
# intraday_prices.ts is `timestamp WITHOUT time zone` and its values are IST WALL CLOCK — the
# newest NIFTY50 row reads 2026-08-21 15:30:00, a real NSE close. Comparing that against a UTC
# now() is 330 minutes wrong, which is exactly the mistake cc#1218 was filed over. So the compare
# is done against _ist_now() with its tzinfo dropped: naive IST on both sides, one convention.
#
# THE VOCABULARY IS BORROWED, NOT INVENTED. The V8 header already says NSE CLOSED and Frozen
# 15:20, so this returns CLOSED and FROZEN and adds no third word.
#   LIVE   — the tick is from TODAY'S session and the market is OPEN or in the closing auction
#   FROZEN — the tick is from today's session but the bell has gone
#   CLOSED — the tick is from an earlier session: weekend, holiday, or simply stale
#   EOD    — there is no intraday tick at all, only a daily close. That was already honest.
def freshness_tier(as_of: Optional[str], now: Optional[datetime] = None) -> str:
    """One rule, used by every ladder pill and by the section header. See the note above."""
    if not as_of:
        return "STATIC"
    now = now or _ist_now()
    naive_now = now.replace(tzinfo=None)
    txt = str(as_of).strip()
    try:
        if len(txt) <= 10:
            # A DATE, so it came from raw_prices: a daily close, and EOD is the honest word.
            # PARSED, not measured by length — "not a date" is also ten characters, and returning
            # EOD for it would be the same generous default this whole function exists to remove.
            datetime.strptime(txt, "%Y-%m-%d")
            return "EOD"
        stamp = datetime.fromisoformat(txt.replace("Z", "")[:26])
        if stamp.tzinfo is not None:            # defensive: normalise anything aware into IST
            stamp = stamp.astimezone(IST).replace(tzinfo=None)
    except Exception:
        return "STATIC"                         # unparseable is not a licence to claim LIVE
    if stamp.date() != naive_now.date():
        return "CLOSED"
    return "LIVE" if market_state(now)["state"] in ("OPEN", "CAS") else "FROZEN"


_TIER_RANK = {"LIVE": 4, "FROZEN": 3, "EOD": 2, "CLOSED": 1, "STATIC": 0}


def _section_tier(tiers) -> str:
    """The section pill is the FRESHEST of its cards, never a literal. Freshest and not stalest on
    purpose: the header answers "is anything on this section live", and a reader who sees LIVE then
    reads each card's own pill to see which."""
    best = max(tiers, key=lambda t: _TIER_RANK.get(t, 0), default=None)
    return best or "STATIC"


# ── 01 GLOBAL TAPE — reuse the cc#842 component, never a second implementation ────────────────
def _global_tape(cur) -> Dict[str, Any]:
    try:
        import global_heatstrip
        strip = global_heatstrip.build_strip(cur)
    except Exception as e:
        log.warning("cc#846 global tape unavailable: %s", e)
        return {"tiles": [], "eod_tiles": [], "error": str(e)[:200], "tier": "EOD"}
    strip["tier"] = "LIVE"
    # cc#852: the note is built from the ACTUAL eod_tiles the component returned, naming the
    # symbols that really have no tick right now. It is omitted entirely when the set is empty —
    # after this card that is the normal case during Asia/Europe hours, and an empty amber banner
    # saying "no intraday feed for " would be worse than no banner.
    _eod = strip.get("eod_tiles") or []
    strip["eod_note"] = (
        "No tick yet for " + ", ".join((t.get("name") or t.get("symbol")) for t in _eod) +
        " — these carry their last CLOSE with its own date. A market outside its own hours is "
        "CLOSED, not stale."
    ) if _eod else None
    tiles = (strip.get("tiles") or [])
    ups = sum(1 for t in tiles if (t.get("day_chg_pct") or 0) > 0)
    strip["read"] = (f"{ups} of {len(tiles)} live tiles higher on the day."
                     if tiles else "No live global tiles available.")
    return strip


# ── 02 INDEX LADDERS ──────────────────────────────────────────────────────────────────────────
def _ladders(cur) -> Dict[str, Any]:
    out = []
    for sym in INDEXES:
        cur.execute("""SELECT pp, r1, r2, s1, s2, pivot_date FROM v8_paper_pivots
                       WHERE symbol=%s ORDER BY pivot_date DESC LIMIT 1""", (sym,))
        p = cur.fetchone()
        cur.execute("""SELECT close, price_date FROM raw_prices WHERE symbol=%s AND close>0
                       ORDER BY price_date DESC LIMIT 1""", (sym,))
        c = cur.fetchone()
        cmp_v = _f(c[0]) if c else None
        as_of, tier = (str(c[1]) if c else None), "EOD"
        # Live CMP during the session, from the same intraday table every other surface reads.
        # cc#1053: cash leg only. The rungs below come from v8_paper_pivots, which are cash
        # pivots — so a BANKNIFTY futures print landing here measured the distance from a
        # cash S1/R1 to a price ~200 pts away on a different instrument. BANKNIFTY carries
        # both legs under one symbol (price_sources.py); NIFTY50 was safe by accident.
        cur.execute("""SELECT close, ts FROM intraday_prices
                       WHERE symbol=%s AND close IS NOT NULL
                         AND COALESCE(source,'') <> ALL(%s)
                       ORDER BY ts DESC LIMIT 1""", (sym, not_fut()))
        iv = cur.fetchone()
        if iv and iv[0] is not None:
            # cc#1228: the tick's AGE decides the badge, not the mere existence of a row.
            cmp_v, as_of = _f(iv[0]), str(iv[1])
            tier = freshness_tier(as_of)
        # cc#1306: day high/low + day change, for the horizontal ladder's two outer rungs and the
        # CMP blink. Cash leg only (same not_fut() filter as the live CMP tick above, for the same
        # cc#1053 reason — BANKNIFTY carries both legs under one symbol) and IST wall-clock "today"
        # (intraday_prices.ts is naive IST per the module note above; CURRENT_DATE is UTC-anchored
        # in Postgres and would clip the first ~5.5 hours of an IST session near midnight).
        _today = _ist_now().date()
        cur.execute("""SELECT MAX(high), MIN(low) FROM intraday_prices
                       WHERE symbol=%s AND ts::date=%s
                         AND COALESCE(source,'') <> ALL(%s)""", (sym, _today, not_fut()))
        hl = cur.fetchone()
        day_high, day_low = (_f(hl[0]), _f(hl[1])) if hl else (None, None)
        cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date < %s
                       ORDER BY price_date DESC LIMIT 1""", (sym, _today))
        pcr = cur.fetchone()
        prev_close = _f(pcr[0]) if pcr else None
        day_chg_pct = _pct(cmp_v, prev_close)
        if not p:
            out.append({"symbol": sym, "cmp": cmp_v, "as_of": as_of, "tier": tier,
                        "day_high": day_high, "day_low": day_low, "day_chg_pct": day_chg_pct,
                        "rungs": [], "reason": "no stored pivots"})
            continue
        pp, r1, r2, s1, s2, pdate = (_f(p[0]), _f(p[1]), _f(p[2]), _f(p[3]), _f(p[4]), p[5])
        # cc#1306: DAY HIGH / DAY LOW ride the same sorted band as S/R/PP rather than being pinned
        # to the two ends by assumption — a session high can sit inside R1/R2 on a quiet day, and
        # forcing it to the visual edge would misstate where it actually falls relative to the
        # pivot levels. One sort (line below), one source of order, same as cc#1257 already
        # established for S/R/PP.
        rungs = [("R2", r2), ("R1", r1), ("PP", pp), ("S1", s1), ("S2", s2),
                 ("DAY HIGH", day_high), ("DAY LOW", day_low)]
        # Distance % on every rung, and the CMP's true position between them — the ladder must show
        # WHERE price sits, not just list levels.
        band = [{"label": k, "value": v,
                 "dist_pct": (round((v / cmp_v - 1) * 100, 2) if (v and cmp_v) else None)}
                for k, v in rungs if v is not None]
        # cc#1257 · THE LADDER IS SORTED HERE AND NOWHERE ELSE, ascending by value, per rule 29630
        # and the founder's line: people read the lower number on top, so supports come first.
        # This reverses cc#1187, which put R2 at the top "the way a ladder hangs" — that reasoning
        # was sound and is simply overruled; recording it so the next reader knows it was a
        # decision and not an accident.
        # SORTED BY VALUE, NOT BY A LABEL SEQUENCE. A hardcoded ['S2','S1','PP','R1','R2'] would be
        # a second place for the order to live and a second place for it to drift. Value order is
        # the rule itself, it needs no list to stay in step, and it stays correct even on a day the
        # pivot maths puts two levels in an unexpected relation.
        band.sort(key=lambda b: b["value"])
        # cmp_slot is the INSERTION INDEX for the CMP row, so it must be recomputed for the new
        # order rather than carried over. It used to be len(above) because the descending list put
        # the higher levels first; ascending puts the LOWER ones first, so it is now the count of
        # levels strictly below the price. Getting this wrong does not crash anything — it silently
        # parks CMP in the wrong slot, which is the one thing this card must not do.
        below = [b for b in band if b["value"] < (cmp_v or 0)]
        out.append({"symbol": sym, "cmp": cmp_v, "as_of": as_of, "tier": tier,
                    "pivot_date": str(pdate) if pdate else None,
                    "day_high": day_high, "day_low": day_low, "day_chg_pct": day_chg_pct,
                    "rungs": band, "cmp_slot": len(below)})
    reads = []
    for l in out:
        if l.get("cmp") and l.get("rungs"):
            pp = next((r for r in l["rungs"] if r["label"] == "PP"), None)
            if pp and pp["dist_pct"] is not None:
                side = "above" if l["cmp"] > pp["value"] else "below"
                reads.append(f"{l['symbol']} {side} PP by {abs(pp['dist_pct']):.2f}%")
    # cc#1228: was a hardcoded "LIVE". Now the freshest of the cards it is summarising.
    return {"indexes": out, "tier": _section_tier([l.get("tier") for l in out]),
            "read": ("; ".join(reads) if reads else
            "No pivot ladder available — v8_paper_pivots has no recent row for these indexes.")}


# ── 03 INTERNALS: ADR breadth + PCR ───────────────────────────────────────────────────────────
def _internals(cur) -> Dict[str, Any]:
    cur.execute("""SELECT price_date, advances, declines, unchanged, adr FROM adr_daily
                   ORDER BY price_date DESC LIMIT 1""")
    a = cur.fetchone()
    cur.execute("""SELECT price_date, underlying, pcr, quality, quality_note FROM pcr_daily
                   WHERE price_date=(SELECT MAX(price_date) FROM pcr_daily) ORDER BY underlying""")
    pcr_rows = [{"date": str(r[0]), "underlying": r[1], "pcr": _f(r[2]),
                 "quality": r[3], "note": r[4]} for r in cur.fetchall()]
    # cc#1568: the mood word on the tile comes from the ONE composer (pcr_mood.py, session_log
    # 36200) — same label the app hero and the web card print. Never banded here.
    for r in pcr_rows:
        try:
            r["mood"] = compose_live(cur, r["pcr"])
        except Exception:
            r["mood"] = None
    cur.execute("""SELECT price_date, underlying, pcr FROM pcr_daily
                   WHERE price_date >= (SELECT MAX(price_date) FROM pcr_daily) - 10
                   ORDER BY price_date DESC, underlying""")
    trend = [{"date": str(r[0]), "underlying": r[1], "pcr": _f(r[2])} for r in cur.fetchall()]

    adr = {"date": str(a[0]), "advances": a[1], "declines": a[2],
           "unchanged": a[3], "adr": _f(a[4])} if a else None

    # 965 stale_flag_rule: >2 trading days old gets flagged, never shown bare.
    def _stale(dstr):
        if not dstr:
            return False
        try:
            d = datetime.strptime(dstr, "%Y-%m-%d").date()
            return (_ist_now().date() - d).days > 4      # >2 trading days, weekend-tolerant
        except Exception:
            return False

    bits = []
    if adr and adr["adr"] is not None:
        bits.append(f"breadth {adr['adr']:.2f} ({adr['advances']}A/{adr['declines']}D)")
    for r in pcr_rows:
        if r["pcr"] is not None:
            who = "put writers" if r["pcr"] > 1 else "call writers"
            m = r.get("mood") or {}
            lbl = f" · {m['label']}" if m.get("label") else ""
            bits.append(f"{r['underlying']} PCR {r['pcr']:.2f}{lbl} — {who} in control")
    return {"adr": adr, "adr_stale": _stale(adr["date"]) if adr else False,
            "pcr": pcr_rows, "pcr_stale": _stale(pcr_rows[0]["date"]) if pcr_rows else False,
            "pcr_trend": trend, "tier": "LIVE",
            "read": ("; ".join(bits) if bits else "No breadth or options data available.")}


# ── cc#1096 R7-D3 · A BARE BSE CODE IS NOT A COMPANY ──────────────────────────────────────────
# Founder recording, frame 3:12: REPORTING TODAY read 540027 · 521137 · TVVISION · ANNAPURNA ·
# BAGFILMS. Frame 3:20: YESTERDAY'S RESULTS carried 512379, 531515, 539175 among real tickers.
#
# THE RESOLUTION PATH, since the card asks for it by name: earnings_calendar.ticker is rendered
# straight to screen. That column holds a BSE scrip code for BSE-only companies, and the payload
# LEFT JOINs screener_raw on nse_code, which cannot match one — so the join fails silently, the
# market cap comes back NULL, and the raw code reaches the chip.
#
# I CHECKED WHETHER THEY COULD BE RESOLVED PROPERLY FIRST, because a real symbol always beats a
# name. screener_raw carries a "BSE Code" column, so the mapping looked possible. Measured:
#   earnings_calendar rows                                   2,470
#   rows whose ticker is all digits                            163
#   of those 163, resolvable to an nse_code via "BSE Code"       0
# Zero, because screener_raw IS the NSE-listed universe (1,816 rows) and these companies are not
# on the NSE at all. There is no NSE symbol to resolve to, and inventing one is forbidden.
#
# SO THE NAME IT IS, and the evidence supports it without a gap: company_name is populated on
# 2,470 of 2,470 rows — zero nulls. Every BSE-only reporter therefore has something honest to
# show. The ticker is KEPT in the payload untouched, because the news match in
# _yesterday_results keys on it against mentioned_symbols and that must not change.
_BSE_CODE_RE = None


def _label(ticker: str, company: str):
    """(display_label, is_symbol) — never a bare numeric code where a ticker belongs."""
    global _BSE_CODE_RE
    if _BSE_CODE_RE is None:
        import re as _re
        _BSE_CODE_RE = _re.compile(r"^\d+$")
    t = (ticker or "").strip()
    if t and not _BSE_CODE_RE.match(t):
        return t, True
    name = (company or "").strip()
    # If a numeric code somehow arrives with no name, show the code rather than an em-dash — the
    # reader can at least look it up, and a blank would hide that a company reported at all.
    return (name or t or "—"), False


# ── 04 REPORTING TODAY / 05 WHAT MOVED / 06 GLOBAL EVENTS ─────────────────────────────────────
def _reporting_today(cur) -> Dict[str, Any]:
    cur.execute("""SELECT UPPER(e.ticker), e.company_name, s.market_cap
                   FROM earnings_calendar e
                   LEFT JOIN screener_raw s ON s.nse_code = UPPER(e.ticker)
                   WHERE e.ex_date = CURRENT_DATE
                   ORDER BY s.market_cap DESC NULLS LAST LIMIT 25""")
    rows = []
    for r in cur.fetchall():
        lbl, is_sym = _label(r[0], r[1])
        rows.append({"ticker": r[0], "company": r[1], "market_cap": _f(r[2]),
                     "label": lbl, "is_symbol": is_sym})
    return {"companies": rows, "count": len(rows), "tier": "STATIC",
            "read": (f"{len(rows)} companies report today."
                     if rows else "No scheduled reporters today.")}


# cc#1321: WHAT MOVED chip filter, mobile digest only. Domestic/AI Editorial/IPO/Stock Views --
# founder's exact list, verbatim. GLOBAL is deliberately excluded here: global_events already has
# its own separate section on the same page, and mixing it in would double-count it under a chip.
# "Stock Views" (plural) is the real polished_news.category value -- confirmed live (5 rows/60d)
# and confirmed in code: news_endpoints.py's own comment says AI Editorial and Stock Views "ride
# the same stream, tagged" as ordinary headline+summary articles. It is NOT the separate ranked-list
# stock_views_shortlist pipeline (STOCK_VIEWS_FRAMEWORK_V1) -- that pipeline's output never lands in
# polished_news, so there is no ranked-list shape to reconcile here; a Stock Views row from _news()
# renders through the exact same card template as every other category.
_WHAT_MOVED_CATEGORIES = ["Domestic", "AI Editorial", "IPO", "Stock Views"]


def _what_moved_all(cur, limit_per_cat: int = 20) -> Dict[str, Any]:
    """Combined WHAT MOVED feed across all four categories, each row carrying its own `category`
    so the client can filter, plus category_counts for the chip labels -- the same convention
    mobile/intel.html already ships (cc#1001's category_counts + tagged-item pattern), not a
    second implementation of the same idea.

    A NEW key, not a change to `what_moved` -- scorr_digest_v3.html's secNews() reads
    what_moved.items and its own cc#1239 comment pins it to "same six items, same order, same
    everything" as the app. Mixing in AI Editorial/IPO/Stock Views there would silently change
    what the web digest's WHAT MOVED section shows, which nothing asked for. This card's chips are
    mobile-only, so they get their own key and `what_moved` stays exactly as it always has.
    """
    items, counts = [], {}
    for cat in _WHAT_MOVED_CATEGORIES:
        rows = _news(cur, cat, limit_per_cat)
        counts[cat] = len(rows)
        for r in rows:
            r["category"] = cat
        items.extend(rows)
    # one combined feed reads chronologically, not grouped by category -- the per-category limit
    # above still applies per category, so a quiet category cannot crowd out a busy one.
    items.sort(key=lambda r: r["published"] or "", reverse=True)
    return {"items": items, "category_counts": counts, "tier": "LIVE"}


def _news(cur, category: str, limit: int = 20) -> List[Dict[str, Any]]:
    # cc#853: sentiment added so the R3 news rows can carry their .sdot colour and .nsent label.
    # It is READ from polished_news, never inferred here — the column is populated but its
    # vocabulary is inconsistent (Bullish/Positive/positive, Bearish/Negative/negative, Cautious,
    # Neutral/neutral), so it is passed through raw and normalised once, on the client.
    #
    # cc#1239 · SOURCE IS NO LONGER SELECTED, and that is the whole of the "no ET/Mint labels"
    # change on this surface. The rows were ALREADY polished-only — polished_news is the only table
    # this query has ever read — so there was never a raw item to remove here; what the founder
    # photographed was the outlet label riding along on a polished row. Dropping the column from the
    # payload is a stronger fix than hiding it in CSS or teaching the shared row component to omit
    # it: scorr_news_row.js renders the label only `if (n.source)`, so with no source on the payload
    # nothing renders and the SHARED component is not touched for the surfaces still using it.
    #
    # full_summary and mentioned_symbols are new, and only for the bottom sheet. The list keeps
    # reading `summary` so a row is still one short line; the sheet is where the full piece lives.
    # limit 6 -> 20 per the card. Verified live at 225 Domestic / 74 Global rows over 7 days, so 20
    # is always a full deck rather than a target the data cannot meet.
    cur.execute("""SELECT id, headline_clean, COALESCE(summary, full_summary), published_time,
                          sentiment, impact, full_summary, mentioned_symbols
                   FROM polished_news WHERE category=%s
                   ORDER BY published_time DESC LIMIT %s""", (category, limit))
    out = []
    for r in cur.fetchall():
        syms = r[7]
        if isinstance(syms, str):                      # text column on some rows, list on others
            syms = [x.strip() for x in syms.split(",") if x.strip()]
        out.append({"id": r[0], "headline": r[1], "summary": r[2],
                    "published": r[3].isoformat() if r[3] else None,
                    "sentiment": r[4], "impact": r[5], "full_summary": r[6],
                    "symbols": list(syms) if syms else []})
    return out


# ── 07 YESTERDAY'S RESULTS · SCORED — the strict three-condition match ────────────────────────
_BULL = r"(rises|jumps|surges|beats|narrows|record|expands|grows|up \d)"
_BEAR = r"(falls|drops|slumps|misses|widens|declines|loss|down \d)"


def _prev_trading_date(cur, ref=None):
    """cc#1109: the previous TRADING day, not CURRENT_DATE - 1.

    `CURRENT_DATE - 1` scores Sunday every Monday and scores a holiday on the day after one —
    both return an empty earnings set that reads as "nothing reported" rather than "no session".
    Walk back from `ref` (IST today by default) over weekends and over notified NSE closures.
    The holiday set comes from the nse_holidays TABLE, so a calendar update needs no deploy;
    weekends are pure date arithmetic and need no lookup at all.

    Bounded at 10 steps. The longest real NSE gap is a Diwali/weekend cluster of about four
    days, so 10 is slack, not a guess — and a bound means a bad calendar row can never spin.
    """
    d = ref or _ist_now().date()
    for _ in range(10):
        d = d - timedelta(days=1)
        if d.weekday() >= 5:                      # Sat/Sun
            continue
        cur.execute("SELECT 1 FROM nse_holidays WHERE holiday_date = %s LIMIT 1", (d,))
        if cur.fetchone():
            continue
        return d
    return (ref or _ist_now().date()) - timedelta(days=1)


def _yesterday_results(cur, basis) -> Dict[str, Any]:
    cur.execute("""SELECT UPPER(e.ticker), e.company_name, s.market_cap
                   FROM earnings_calendar e
                   LEFT JOIN screener_raw s ON s.nse_code = UPPER(e.ticker)
                   WHERE e.ex_date = %s
                   ORDER BY s.market_cap DESC NULLS LAST LIMIT 10""", (basis,))
    top = []
    for r in cur.fetchall():
        lbl, is_sym = _label(r[0], r[1])
        top.append({"ticker": r[0], "company": r[1], "market_cap": _f(r[2]),
                    "label": lbl, "is_symbol": is_sym})
    cur.execute("SELECT COUNT(*) FROM earnings_calendar WHERE ex_date = %s", (basis,))
    total = cur.fetchone()[0]

    out, tally = [], {"bullish": 0, "cautious": 0, "neutral": 0, "pending": 0}
    for c in top:
        # ALL THREE conditions, together. See the module docstring for the verified failures that
        # any looser rule produces.
        cur.execute("""SELECT headline_clean, COALESCE(summary, full_summary), published_time
                       FROM polished_news
                       WHERE %s = ANY(mentioned_symbols)
                         AND headline_clean ~* %s
                         AND published_time >= NOW() - make_interval(hours => %s)
                       ORDER BY published_time DESC LIMIT 1""",
                    (c["ticker"], RESULT_KEYWORDS, RESULT_WINDOW_HOURS))
        n = cur.fetchone()
        if not n:
            tally["pending"] += 1
            out.append({**c, "headline": None, "summary": None, "read": "PENDING",
                        "pending_reason": "Reported — summary not yet on the desk"})
            continue
        head = n[0] or ""
        import re as _re
        bull = bool(_re.search(_BULL, head, _re.I))
        bear = bool(_re.search(_BEAR, head, _re.I))
        read = "BULLISH" if (bull and not bear) else ("CAUTIOUS" if bear else "NEUTRAL")
        tally["bullish" if read == "BULLISH" else "cautious" if read == "CAUTIOUS" else "neutral"] += 1
        out.append({**c, "headline": head, "summary": n[1], "read": read,
                    "published": n[2].isoformat() if n[2] else None})

    cur.execute("SELECT COUNT(DISTINCT symbol) FROM result_analysis_v2")
    l2 = cur.fetchone()[0]
    return {"companies": out, "reported_total": total, "tally": tally, "l2_count": l2,
            "tier": "STATIC",
            # cc#1109: the page states which session this scored, so an empty Monday reads as
            # "Friday had none" and never as "today had none".
            "basis_date": basis.isoformat(),
            "match_rule": ("ticker in mentioned_symbols AND result-shaped headline AND published "
                           f"within {RESULT_WINDOW_HOURS}h — all three, or the row shows PENDING"),
            "read": (f"{tally['bullish']} bullish · {tally['cautious']} cautious · "
                     f"{tally['neutral']} neutral · {tally['pending']} awaiting desk")}


# ── cc#1190 · LATEST RESULT ANALYSIS ──────────────────────────────────────────────────────────
# The mobile digest's results card reads earnings_calendar for one session and matches it against
# news headlines, so on a thin day it says "Top 2 of 2 · 2 awaiting desk" and shows nothing worth
# reading. result_analysis_v2 holds 633 rows of written analysis. This builds a payload from
# THAT, as a NEW key. _yesterday_results is not touched and the web page keeps reading it.
#
# cc#1414 · THE DECK IS NO LONGER RA2-ONLY. This SQL still supplies the written-analysis half,
# unchanged; _RESULTS_L1_SQL below supplies the rest of the reported GVM universe as L1 rows, and
# _results_analysed() merges the two — expand, never shrink: every row this query ships today
# still ships, the founder's ask ADDS the reported names that have no written entry yet.
#
# THE VERDICT LINE HAS THREE SHAPES, NOT ONE, AND I ONLY KNOW THAT BECAUSE I COUNTED. The card
# says "after VERDICT: to first newline". Measured across all 633 rows:
#     511  start "VERDICT:"  or "Verdict:"   <- BOTH casings, 248 upper and 263 mixed
#     102  start "Headline:"
#      20  carry no label at all, just prose
# A case-SENSITIVE match on 'VERDICT:' is the trap here and it does not fail loudly: Postgres
# position() returns 0 for a miss, so `substring(from position + 8)` starts at character 8 of the
# row and returns a string beginning mid-word — ": Very strong growth across every line". That is
# 385 of 633 rows, 61%, silently mangled into something that still looks like a sentence. The
# regex below is case-insensitive and takes Headline: too, with the first non-empty line as the
# fallback for the unlabelled 20. Verified: 633 extracted, ZERO starting with a colon, zero empty.
#
# EXTRACTED IN SQL ON PURPOSE. analysis_text averages ~4KB; pulling all 633 into Python to slice
# one line off each would move ~2.5MB per digest request to throw nearly all of it away. The
# first line crosses the wire and nothing else, which is also how the card's "no analysis_text"
# rule stays true by construction rather than by remembering to delete a key.
_RESULTS_ANALYSED_SQL = """
WITH latest_gvm AS (
    SELECT DISTINCT ON (symbol) symbol, segment, market_cap
    FROM gvm_scores ORDER BY symbol, score_date DESC
), ranked AS (
    -- The cap tier is a rank over the LIVE gvm universe, not a stored column. input_raw carries an
    -- mcap_rank, and it was the obvious candidate, but it disagrees with the card: it leaves 10 of
    -- these 633 unranked where the card's own verify line says 4. Ranking gvm_scores.market_cap
    -- reproduces 4 exactly, so that is the source the card meant.
    SELECT symbol, RANK() OVER (ORDER BY market_cap DESC NULLS LAST) AS mcap_rank
    FROM latest_gvm WHERE market_cap IS NOT NULL
-- cc#1310: the founder wants the result DATE on every card in this list, not just the quarter
-- label. reported-status only, latest ex_date per symbol — the same "reported" gate results_card
-- uses for the same field elsewhere, so a symbol never shows a rescheduled/upcoming date here.
), ex AS (
    SELECT DISTINCT ON (UPPER(ticker)) UPPER(ticker) AS symbol, ex_date
    FROM earnings_calendar
    WHERE status = 'reported'
    ORDER BY UPPER(ticker), ex_date DESC
)
SELECT r.symbol,
       s.company_name,
       k.mcap_rank,
       g.segment,
       r.quarter,
       r.polished_at,
       btrim(split_part(
           regexp_replace(r.analysis_text, '^\\s*(verdict|headline)\\s*:\\s*', '', 'i'),
           E'\\n', 1)) AS verdict,
       e.ex_date
FROM result_analysis_v2 r
LEFT JOIN screener_raw s ON s.nse_code = r.symbol
LEFT JOIN latest_gvm    g ON g.symbol  = r.symbol
LEFT JOIN ranked        k ON k.symbol  = r.symbol
LEFT JOIN ex            e ON e.symbol  = r.symbol
-- cc#1319: ex_date descending is the deck's actual order now — latest result first, per the
-- founder's screenshot (was accidentally polished_at order, which reads as alphabetical-by-symbol
-- whenever a batch polishes together). polished_at stays as the tiebreak for same-date rows (or
-- rows with no ex_date on file), so the order is still fully deterministic; symbol closes it out.
ORDER BY e.ex_date DESC NULLS LAST, r.polished_at DESC, r.symbol
"""


def _tier(rank) -> Optional[str]:
    """Large 1-100 / Mid 101-250 / Small 251+, per the card. No rank = no tier, never a guess."""
    if rank is None:
        return None
    r = int(rank)
    return "LARGE" if r <= 100 else ("MID" if r <= 250 else "SMALL")


# ── cc#1414 · L1 ROWS FOR THE REST OF THE REPORTED UNIVERSE ───────────────────────────────────
# Founder: the deck's universe is the FULL GVM-scored universe (1796), not the ~632 with a written
# result_analysis_v2 entry. This query is the OTHER HALF of that deck: every symbol that is
# (a) in the live GVM universe, (b) reported this quarter per earnings_calendar (status='reported',
# verified<>'false', ex_date >= the completed quarter end — the SAME three-part gate
# results_endpoints._SEGMENT_RESULTS_SQL already uses, per the card), and (c) has NO
# result_analysis_v2 row. The RA2 half keeps _RESULTS_ANALYSED_SQL above, untouched.
#
# WHAT THE DATA ACTUALLY SUPPORTS, measured before writing this rather than taken from the card's
# own optimistic note: fundamentals_history quarters covers 837 symbols, NOT the full 1796 — so of
# the ~998 reported-no-RA2 names, only ~184 can carry real L1 numbers and a computed sentence.
# The rest ship as name/segment/tier/date rows with verdict NULL — present in the deck per the
# founder's explicit "do not omit the company", never a fabricated sentence.
#
# THE INPUT DERIVATIONS MIRROR THE CARD'S OWN, deliberately:
#   pat_yoy   — (now-was)/abs(was)*100, was<>0 — _l1_quarter.pct()'s own formula (abs base,
#               negative year-ago allowed), NOT the dot SQL's positive-base guard, because the
#               sentence must match what the R-card's own L1 block shows for the same symbol.
#   margin    — "OPM percent" first, else "Financing Margin percent" (the literal % in the SQL
#               below is doubled to %% for psycopg2's param mode — that is escaping, not a
#               different key name), key-PRESENCE tested on the CURRENT
#               quarter's metrics (jsonb ?), insurers excluded via screener_raw.industry_group —
#               each rule lifted from _l1_quarter, stated there with its own evidence.
#   vs-est    — expected_quarterly_net_profit with the +/-2% bands — the set-based restatement
#               _segment_results already made of _expectations' bands (cc#1192: "the SAME bands,
#               not a second set"), reused here on the same precedent.
# The SENTENCE itself is NOT re-derived: _results_analysed() feeds these inputs to the real
# results_endpoints._auto_verdict(), so the deck row and the R-card cannot disagree on wording.
_RESULTS_L1_SQL = """
WITH latest_gvm AS (
    SELECT DISTINCT ON (symbol) symbol, segment, market_cap
    FROM gvm_scores ORDER BY symbol, score_date DESC
), ranked AS (
    SELECT symbol, RANK() OVER (ORDER BY market_cap DESC NULLS LAST) AS mcap_rank
    FROM latest_gvm WHERE market_cap IS NOT NULL
), rep AS (
    SELECT DISTINCT ON (UPPER(ticker)) UPPER(ticker) AS sym, ex_date
    FROM earnings_calendar
    WHERE status = 'reported' AND verified <> 'false' AND ex_date >= %(q_start)s
    ORDER BY UPPER(ticker), ex_date DESC
), tgt AS (
    SELECT r.sym, r.ex_date FROM rep r JOIN latest_gvm g ON g.symbol = r.sym
    WHERE NOT EXISTS (SELECT 1 FROM result_analysis_v2 a WHERE a.symbol = r.sym)
), q AS (
    SELECT UPPER(f.symbol) AS sym, f.period_end, f.metrics,
           NULLIF(replace(f.metrics->>'Net Profit', ',', ''), '')::numeric AS pat
    FROM fundamentals_history f
    WHERE f.section='quarters' AND f.period_type='quarter'
      AND UPPER(f.symbol) IN (SELECT sym FROM tgt)
), latest AS (
    SELECT sym, period_end, metrics, pat, MAX(period_end) OVER (PARTITION BY sym) AS max_pe FROM q
), calc AS (
    SELECT c.sym, c.period_end,
           CASE WHEN p.pat IS NOT NULL AND p.pat <> 0
                THEN (c.pat - p.pat) / abs(p.pat) * 100 END AS pat_yoy,
           c.pat AS act_pat,
           CASE WHEN (c.metrics::jsonb ? 'OPM %%')
                THEN NULLIF(replace(replace(c.metrics->>'OPM %%',',',''),'%%',''),'')::numeric
                WHEN (c.metrics::jsonb ? 'Financing Margin %%')
                THEN NULLIF(replace(replace(c.metrics->>'Financing Margin %%',',',''),'%%',''),'')::numeric
           END AS m_now,
           CASE WHEN (c.metrics::jsonb ? 'OPM %%')
                THEN NULLIF(replace(replace(y.metrics->>'OPM %%',',',''),'%%',''),'')::numeric
                WHEN (c.metrics::jsonb ? 'Financing Margin %%')
                THEN NULLIF(replace(replace(y.metrics->>'Financing Margin %%',',',''),'%%',''),'')::numeric
           END AS m_yr
    FROM latest c
    LEFT JOIN q p ON p.sym = c.sym AND p.period_end = (c.period_end - INTERVAL '1 year')::date
    LEFT JOIN q y ON y.sym = c.sym AND y.period_end = (c.period_end - INTERVAL '1 year')::date
    WHERE c.period_end = c.max_pe
)
SELECT t.sym, s.company_name, k.mcap_rank, g.segment, t.ex_date,
       c.period_end, c.pat_yoy, c.m_now, c.m_yr, c.act_pat,
       NULLIF(replace(s.expected_quarterly_net_profit::text, ',', ''), '')::numeric AS exp_pat,
       lower(btrim(COALESCE(s.industry_group,''))) = 'insurance' AS is_insurer
FROM tgt t
LEFT JOIN latest_gvm g ON g.symbol = t.sym
LEFT JOIN ranked k ON k.symbol = t.sym
LEFT JOIN screener_raw s ON UPPER(s.nse_code) = t.sym
LEFT JOIN calc c ON c.sym = t.sym
ORDER BY t.ex_date DESC NULLS LAST, t.sym
"""


# ── cc#1238 · RESULT TRAFFIC DOT (RESULT_DOT_RULE_V1, session_log 29519) ──────────────────────
# Two binary checks, and neither invents a source. CHECK A is the company's PAT YoY against its
# SEGMENT MEDIAN PAT YoY for the same quarter, struck on fundamentals_history alone — that is
# RESULT_PEER_SOURCE_RULE_V1 (cc#1192), which killed the screener qoq_* columns for result surfaces
# because the CSV profit line is not the post-minority Net Profit every other result surface uses.
# CHECK B is the actual PAT against screener_raw.expected_quarterly_net_profit, which is the SAME
# field the "vs est." surface reads (results_endpoints _SEGMENT_RESULTS_SQL). No new estimate
# pathway, and the label stays honest: it is a Screener projected run-rate, not a broker consensus.
#
# THE YoY BASE GUARD IS COPIED, NOT REINVENTED: `WHEN p.pat > 0`. A year-ago loss makes a percentage
# change meaningless rather than merely large, so it yields NULL and the check goes uncomputable —
# which the rule already has an answer for. Every other result surface strikes it the same way.
#
# THE MEDIAN IS PER (segment, period_end), never per segment alone. A company whose latest filed
# quarter is older than the season is then compared against peers on THAT quarter, not against a
# different quarter's cohort. n_used travels with it so a median resting on one or two reporters can
# be named rather than quietly trusted.
_RESULT_DOT_SQL = """
WITH latest_gvm AS (
    SELECT DISTINCT ON (symbol) symbol, segment
    FROM gvm_scores ORDER BY symbol, score_date DESC
), q AS (
    SELECT UPPER(f.symbol) AS sym, f.period_end,
           NULLIF(replace(f.metrics->>'Net Profit', ',', ''), '')::numeric AS pat
    FROM fundamentals_history f
    WHERE f.section = 'quarters' AND f.period_type = 'quarter'
), latest AS (
    SELECT sym, period_end, pat, MAX(period_end) OVER (PARTITION BY sym) AS max_pe FROM q
), yoy AS (
    SELECT c.sym, c.period_end, c.pat AS act_pat,
           CASE WHEN p.pat > 0 THEN (c.pat - p.pat) / p.pat * 100 END AS pat_yoy
    FROM latest c
    LEFT JOIN q p ON p.sym = c.sym
                 AND p.period_end = (c.period_end - INTERVAL '1 year')::date
    WHERE c.period_end = c.max_pe
), withseg AS (
    SELECT y.sym, y.period_end, y.act_pat, y.pat_yoy, g.segment
    FROM yoy y LEFT JOIN latest_gvm g ON UPPER(g.symbol) = y.sym
), med AS (
    SELECT segment, period_end,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY pat_yoy) AS seg_med,
           COUNT(pat_yoy) AS n_used
    FROM withseg
    WHERE segment IS NOT NULL AND pat_yoy IS NOT NULL
    GROUP BY segment, period_end
)
SELECT w.sym, w.period_end, w.pat_yoy, m.seg_med, m.n_used, w.act_pat,
       NULLIF(replace(sr.expected_quarterly_net_profit::text, ',', ''), '')::numeric AS exp_pat
FROM withseg w
LEFT JOIN med m ON m.segment = w.segment AND m.period_end = w.period_end
LEFT JOIN screener_raw sr ON UPPER(sr.nse_code) = w.sym
"""


def _fq_label(period_end) -> Optional[str]:
    """'Q1FY27' for a quarter period-end, in result_analysis_v2's own spelling (no space).

    This exists so a dot can only ever be attached to the quarter it was actually computed for.
    The dot is struck on the symbol's LATEST filed quarter; result_analysis_v2 carries one row on a
    superseded quarter (KIMS Q4FY26, already named in duplicate_symbols). Without this guard that
    older row would wear a dot describing a different quarter, which is exactly the kind of quiet
    mislabelling the null rule is written to prevent. No match, no dot.
    """
    if not period_end:
        return None
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(period_end.month)
    if not q:
        return None
    fy = period_end.year + 1 if period_end.month in (6, 9, 12) else period_end.year
    return "Q%dFY%02d" % (q, fy % 100)


def _result_dots(cur) -> Dict[str, Dict[str, Any]]:
    """{SYMBOL: {quarter, dot, basis, ...}} — the colour plus every number it was struck on.

    The inputs ship alongside the colour on purpose. A dot with no way to see the four numbers
    behind it is a claim, and the sheet has to be able to say which comparison it is based on when
    only one check was computable.
    """
    cur.execute(_RESULT_DOT_SQL)
    out: Dict[str, Dict[str, Any]] = {}
    for sym, pe, pat_yoy, seg_med, n_used, act_pat, exp_pat in cur.fetchall():
        a = None if (pat_yoy is None or seg_med is None) else (pat_yoy >= seg_med)
        b = None if (act_pat is None or exp_pat is None) else (act_pat >= exp_pat)
        if a is None and b is None:
            dot, basis = None, None            # absent, never a fabricated neutral
        elif a is None or b is None:
            one = a if b is None else b
            dot = "green" if one else "red"    # amber is unreachable on a single check
            basis = "peers" if b is None else "estimate"
        else:
            dot = "green" if (a and b) else ("red" if not (a or b) else "amber")
            basis = "both"
        out[sym] = {
            "quarter": _fq_label(pe), "dot": dot, "dot_basis": basis,
            "pat_yoy": None if pat_yoy is None else round(float(pat_yoy), 2),
            "seg_median_pat_yoy": None if seg_med is None else round(float(seg_med), 2),
            "seg_n": n_used,
            "act_pat": None if act_pat is None else round(float(act_pat), 2),
            "exp_pat": None if exp_pat is None else round(float(exp_pat), 2),
        }
    return out


def _results_analysed(cur) -> Dict[str, Any]:
    """cc#1255 · DOTS ONLY — RESULT_ROW_DOTS_ONLY_RULE_V1 (session_log 29663).

    THE THREE WORDS ARE GONE FROM THIS PAYLOAD, and the reason is worth keeping written down. The
    verdict tag was derived HERE, by running the _BULL/_BEAR prose regex over the analyst's
    written verdict — a second, independent reading of text that the R button already reads a
    different way. The founder's complaint was that digest verdicts did not match the web R
    button, and the fix turned out not to be "make them agree": the R button emits no word at all,
    only a SENTENCE, so there was nothing to agree WITH. Rather than invent a numbers-to-words
    mapping to make the two match, the ruling retires the word everywhere.

    WHAT REPLACES IT IS ALREADY HERE AND ALREADY EARNED. The dot (rule 29519) is computed from
    real numbers — profit growth against the segment median, and actual against the run-rate
    estimate — not from adjectives in prose. A row is now the dot plus the analyst's own sentence,
    which is the exact text the R button shows. Nothing is derived twice.
    """
    cur.execute(_RESULTS_ANALYSED_SQL)
    rows = cur.fetchall()
    dots = _result_dots(cur)
    dot_cov = {"green": 0, "amber": 0, "red": 0, "no_dot": 0,
               "single_check": 0, "median_from_2_or_fewer": 0}

    out, tiers = [], {"LARGE": 0, "MID": 0, "SMALL": 0}
    unranked = 0
    seen = {}

    # cc#1414: the dot-attach block, hoisted into ONE helper so the RA2 loop and the new L1 loop
    # below cannot drift on the quarter-match guard. Semantics byte-identical to the inline block
    # it replaces. The dot is attached ONLY when the quarter it was computed for is the quarter
    # this row is about — see _fq_label for why that guard exists rather than a plain lookup.
    def _attach_dot(rowd, quarter):
        d = dots.get(rowd["symbol"])
        if d and d["quarter"] and quarter and d["quarter"] == quarter and d["dot"]:
            rowd["dot"] = d["dot"]
            rowd["dot_basis"] = d["dot_basis"]
            rowd["dot_inputs"] = {k: d[k] for k in
                                  ("pat_yoy", "seg_median_pat_yoy", "seg_n", "act_pat", "exp_pat")}
            dot_cov[d["dot"]] += 1
            if d["dot_basis"] != "both":
                dot_cov["single_check"] += 1
            if d["seg_n"] is not None and d["seg_n"] <= 2:
                dot_cov["median_from_2_or_fewer"] += 1
        else:
            dot_cov["no_dot"] += 1

    for sym, company, rank, segment, quarter, polished, verdict, ex_date in rows:
        tier = _tier(rank)
        if tier:
            tiers[tier] += 1
        else:
            unranked += 1
        v = verdict or ""
        # cc#1255: the _BULL/_BEAR pass over this text is DELETED, not disabled. `read` is gone
        # from the row with it, so no surface can print a word this payload no longer carries.
        seen[sym] = seen.get(sym, 0) + 1
        out.append({
            "symbol": sym,
            "company": company,
            "tier": tier,
            "segment": segment,
            "quarter": quarter,
            # IST, like every other time on this payload. polished_at is timestamptz, so this is a
            # real conversion and not a relabelling of a UTC clock.
            "polished_at": polished.astimezone(IST).isoformat() if polished else None,
            "polished_ist": polished.astimezone(IST).strftime("%d %b") if polished else None,
            "verdict": v,
            # cc#1310: result date capsule chip. ex_date is a date, not a timestamp — no tz
            # conversion needed. None when the symbol has no reported ex_date on file (never
            # fabricated); the row renders without the chip rather than a guessed date.
            "ex_date": ex_date.isoformat() if ex_date else None,
            "l2": True,   # cc#1414: written long-form exists for this row
        })
        _attach_dot(out[-1], quarter)

    # ── cc#1414 · THE REST OF THE REPORTED UNIVERSE, as L1 rows ──────────────────────────────
    # Everything reported this quarter in the GVM universe with NO result_analysis_v2 row. The
    # sentence is the REAL results_endpoints._auto_verdict() — imported here, at the call site,
    # never re-derived (do-not-touch) — fed set-based inputs whose derivations mirror the card's
    # own (see _RESULTS_L1_SQL's header). A symbol with no computable PAT YoY ships with
    # verdict "" — present in the deck per the founder's explicit "do not omit the company",
    # never a fabricated sentence. q_start reuses _completed_quarter_end, the same gate
    # _segment_results uses (the card's own named pattern).
    from results_endpoints import _auto_verdict, _completed_quarter_end
    cur.execute(_RESULTS_L1_SQL, {"q_start": _completed_quarter_end(datetime.now(IST).date())})
    l1_raw = cur.fetchall()
    l1_with_sentence = 0
    for (sym, company, rank, segment, ex_date, pe, pat_yoy, m_now, m_yr,
         act_pat, exp_pat, is_insurer) in l1_raw:
        tier = _tier(rank)
        if tier:
            tiers[tier] += 1
        else:
            unranked += 1
        quarter = _fq_label(pe)   # the FILED quarter — the chip states which quarter the numbers
        #                           describe, so a scrape-lagged symbol is labelled, never passed
        #                           off as the just-reported quarter
        pp = None
        if (not is_insurer) and m_now is not None and m_yr is not None:
            pp = round(float(m_now) - float(m_yr), 1)   # _l1_quarter's own margin delta
        band = None
        if act_pat is not None and exp_pat is not None and float(exp_pat) != 0:
            dev = (float(act_pat) - float(exp_pat)) / abs(float(exp_pat)) * 100.0
            band = "BEAT" if dev > 2 else ("MISS" if dev < -2 else "IN-LINE")
        verdict = _auto_verdict(
            {"pat": {"yoy": None if pat_yoy is None else float(pat_yoy)},
             "margin": ({"pp": pp} if pp is not None else None)},
            ({"profit": {"tag": band}} if band else None))
        if verdict:
            l1_with_sentence += 1
        seen[sym] = seen.get(sym, 0) + 1
        out.append({
            "symbol": sym,
            "company": company,
            "tier": tier,
            "segment": segment,
            "quarter": quarter,
            "polished_at": None, "polished_ist": None,   # nothing was polished — never faked
            "verdict": verdict or "",
            "ex_date": ex_date.isoformat() if ex_date else None,
            "l2": False,   # cc#1414: L1/structured row — the card renders Not Available for L2
        })
        _attach_dot(out[-1], quarter)

    # cc#1414: ONE deck order across both halves — the same ex_date DESC NULLS LAST,
    # polished_at DESC, symbol ASC the SQL used when the deck was RA2-only (cc#1319's latest-
    # result-first ruling), applied to the merged list via stable multi-pass sorts. Within one
    # result date, polished (L2) rows sort ahead of L1 rows, which is the right read: the
    # written analysis is the richer row for the same day.
    out.sort(key=lambda r: r["symbol"])
    out.sort(key=lambda r: r.get("polished_at") or "", reverse=True)
    out.sort(key=lambda r: r.get("ex_date") or "", reverse=True)

    # KIMS carries TWO rows, Q4FY26 and Q1FY27, so 633 rows are 632 companies. The card says "all
    # result_analysis_v2 rows" and its verify pins total to COUNT(*), so all 633 ship and total is
    # 633 — but a deck titled LATEST RESULT ANALYSIS showing a superseded quarter beside the
    # current one is a real question, not a rounding detail, so the duplicate is NAMED in the
    # payload rather than left for someone to notice on a phone.
    dupes = sorted(s for s, n in seen.items() if n > 1)
    return {
        "companies": out,
        "total": len(out),
        "distinct_symbols": len(seen),
        "duplicate_symbols": dupes,
        "tiers": tiers,
        "unranked": unranked,
        # cc#1414: composition of the expanded deck, so the strip (and any audit) can say what
        # portion is written analysis vs computed L1 vs name-and-date-only. l2_rows + l1_rows ==
        # total by construction; l1_with_sentence <= l1_rows because a symbol outside the
        # fundamentals scrape has no computable YoY and ships with an empty verdict rather than
        # a fabricated one. NOTE the tiers/tally denominators above now cover this FULL merged
        # deck, not the old RA2-only 633 — the universe expansion changes those denominators by
        # construction, stated here per the card's own do-not-touch clause.
        "l2_rows": len(rows),
        "l1_rows": len(l1_raw),
        "l1_with_sentence": l1_with_sentence,
        # cc#1319: no longer out[0] -- the deck is sorted by ex_date now, not polished_at, so the
        # first row is not necessarily the most recently polished one any more. polished_at is
        # astimezone(IST).isoformat() with a fixed offset on every row, so a plain string max()
        # is a correct chronological max without re-parsing back to a datetime.
        "latest_polished": max((c["polished_at"] for c in out if c["polished_at"]), default=None),
        # cc#1255 · THE TALLY IS NOW DOT COUNTS, and every number in it comes off the dot table
        # rather than off a second reading of prose. green = beat BOTH checks, amber = beat one,
        # red = beat neither, exactly the rule-29519 meaning and nothing new invented.
        #
        # no_dot IS REPORTED, NOT HIDDEN. A row with no computable dot is not a fourth outcome and
        # must not be folded into "neither" — that would state a company missed both checks when
        # the truth is that neither check could be run. It is stated separately so the three
        # counts always add up to the rows that actually have a dot.
        #
        # THE HONEST CAVEAT, kept beside the number it qualifies: on a single-check row amber is
        # unreachable by construction, so its green means "beat the one check that could be
        # computed", not "beat both". single_check already counts those and travels in
        # dot_coverage, so the strip can qualify itself instead of overstating.
        "tally": {"beat_both": dot_cov["green"],
                  "beat_one": dot_cov["amber"],
                  "beat_neither": dot_cov["red"],
                  "no_dot": dot_cov["no_dot"],
                  "single_check": dot_cov["single_check"]},
        "read": (f"{dot_cov['green']} beat both · {dot_cov['amber']} beat one · "
                 f"{dot_cov['red']} beat neither · {dot_cov['no_dot']} no dot"),
        # cc#1238 scope 6: the coverage report ships WITH the payload rather than living only in a
        # task thread, so the split can be re-read on any day without re-running a one-off query.
        "dot_coverage": dot_cov,
        "tier": "STATIC",
    }


def build_digest(cur) -> Dict[str, Any]:
    now = _ist_now()
    prev_trading = _prev_trading_date(cur, now.date())   # cc#1109: one basis, used everywhere
    tape = _global_tape(cur)
    lad = _ladders(cur)
    internals = _internals(cur)

    # 965 §10 MARKET READ. The SUPPORT numbers are still composed only from what is already on
    # the page — that half of the framework is unchanged.
    bias_bits = []
    tiles = tape.get("tiles") or []
    if tiles:
        ups = sum(1 for t in tiles if (t.get("day_chg_pct") or 0) > 0)
        bias_bits.append(f"global tape {ups}/{len(tiles)} higher")
    if internals.get("adr") and internals["adr"].get("adr") is not None:
        bias_bits.append(f"breadth {internals['adr']['adr']:.2f}")

    # cc#1109 VERDICT UNIFICATION — founder R7 defect 4. The bias WORD is now SOURCED, not
    # composed here. This module used to derive its own word from ADR alone while Home read
    # v8_endpoints.market_mood(); two composers produce two words for the same minute by
    # construction, and no amount of tuning either one fixes that. 965's MARKET_READ rule is
    # amended by this card: support numbers stay local, the word comes from the one composer.
    bias, bias_source, bias_note = None, "market_mood", None
    try:
        from v8_endpoints import market_mood as _market_mood
        bias = (_market_mood() or {}).get("mood") or None
    except Exception as e:                       # noqa: BLE001 — any failure falls back, none raises
        log.warning("cc#1109 market_mood unavailable, falling back to local ADR rule: %s", e)
        bias = None
    if not bias:
        # The fallback is the OLD rule, kept verbatim, and it says so on the page. A silent
        # fallback would put the two words back out of step with nothing on screen to show it.
        adr = (internals.get("adr") or {}).get("adr") or 0
        bias = "Range"
        if adr > 1.2:
            bias = "Bullish continuation"
        elif 0 < adr < 0.8:
            bias = "Cautious"
        bias_source = "digest_local_adr_fallback"
        bias_note = ("Market mood unavailable — this word is composed locally from breadth "
                     "alone and may differ from Home.")

    return {
        "generated_at": now.isoformat(),
        "date_ist": now.strftime("%d %b %Y"),
        "market": market_state(now),
        "sections": {
            "global_tape": tape,
            "ladders": lad,
            "internals": internals,
            "reporting_today": _reporting_today(cur),
            "what_moved": {"items": _news(cur, "Domestic"), "tier": "LIVE"},
            # cc#1321: mobile-only combined feed for the new chip filter -- see _what_moved_all's
            # own docstring for why this is a new key and not a change to what_moved above.
            "what_moved_all": _what_moved_all(cur),
            "global_events": {"items": _news(cur, "Global"), "tier": "LIVE"},
            "yesterday_results": _yesterday_results(cur, prev_trading),
            # cc#1190: ADDED beside yesterday_results, not in place of it. The web digest reads
            # yesterday_results and is untouched by this card; the mobile deck reads this one.
            "results_analysed": _results_analysed(cur),
        },
        "prev_trading_date": prev_trading.isoformat(),   # cc#1109: for the page's date filters
        "market_read": {
            "bias": bias,
            "bias_source": bias_source,
            "support": bias_bits,
            # cc#1276 scope 3: the note is founder-facing page copy — internal ids (session_log
            # 965, cc#1109) stay in code comments, never on the live page.
            "note": bias_note or ("Bias word from the shared market mood; support numbers "
                                  "composed only from what is shown above."),
        },
        "spec": "cc#846 V3 · binding content spec session_log 965 v2.3",
    }


@router.get("/digest", response_class=HTMLResponse)
def digest_page():
    """cc#846: the V3 page. Read-only render; all data comes from /api/digest/v3."""
    import pathlib
    return HTMLResponse(pathlib.Path("scorr_digest_v3.html").read_text(encoding="utf-8"))


@router.get("/api/digest/v3")
def digest_v3():
    try:
        with _conn() as conn, conn.cursor() as cur:
            return build_digest(cur)
    except Exception as e:
        log.exception("digest_v3 failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "sections": {}}


# ── cc#1121 · INTERNALS intraday series ───────────────────────────────────────────────────────
# Served SEPARATELY from /api/digest/v3 on purpose. Four series of 200 points is ~800 points, and
# the digest payload is fetched on every open of the page — the cards fetch this only when the
# section renders, so the first paint does not pay for a chart nobody has swiped to yet.
#
# COLUMN CHOICE, RECONCILED AGAINST THE BIG NUMBER. pcr_daily.pcr is total put OI over total call
# OI across the whole chain (scheduler.py / pcr_backfill.py both build it that way), so the
# intraday column that matches it is pcr_total, NOT pcr_atm5. Checked against live rows before
# wiring: NIFTY daily 0.678 vs last intraday pcr_total 0.679; pcr_atm5 for the same moment is
# 0.844, which would have put a chart on screen that disagreed with the number printed above it.
#
# BREADTH charts advances MINUS declines. The ADR card already charts the adr ratio, and charting
# advances alone next to it would be two pictures of one thing; net A-D is the reading the ratio
# cannot show — how WIDE the day was, not just which side won. Both columns are stored, so this is
# presentation of existing values, not a new computation (the card's do_not_touch stands).
_SERIES_SQL = {
    "adr": """SELECT ts, adr::float FROM adr_intraday
              WHERE adr IS NOT NULL ORDER BY ts DESC LIMIT %s""",
    "breadth": """SELECT ts, (advances - declines)::float FROM adr_intraday
                  WHERE advances IS NOT NULL AND declines IS NOT NULL ORDER BY ts DESC LIMIT %s""",
    "pcr_nifty": """SELECT ts, pcr_total::float FROM pcr_intraday
                    WHERE underlying='NIFTY' AND pcr_total IS NOT NULL ORDER BY ts DESC LIMIT %s""",
    "pcr_banknifty": """SELECT ts, pcr_total::float FROM pcr_intraday
                        WHERE underlying='BANKNIFTY' AND pcr_total IS NOT NULL
                        ORDER BY ts DESC LIMIT %s""",
}
_SERIES_META = [
    ("adr",           "ADR",            2),
    ("breadth",       "BREADTH A-D",    0),
    ("pcr_nifty",     "PCR NIFTY",      2),
    ("pcr_banknifty", "PCR BANKNIFTY",  2),
]
_SESSION_OPEN_MIN = 9 * 60 + 15      # 09:15 IST
_SESSION_MIN = 375                   # 09:15 -> 15:30


def _trading_dates(cur, d_from, d_to) -> List[str]:
    """Every trading date in the span: weekdays minus the nse_holidays table.

    THE AXIS IS BUILT FROM THIS, NOT FROM THE DATES THAT HAPPEN TO HAVE ROWS. That is the whole
    point. NIFTY PCR has ZERO rows for 18-Aug while BANKNIFTY has 71 — plot by index, or by only
    the dates present, and 17-Aug is drawn touching 19-Aug and a missing session reads as a
    continuous market. Enumerating the calendar instead gives that session its own empty slot, so
    a hole in the feed looks like a hole.
    """
    cur.execute("SELECT holiday_date FROM nse_holidays WHERE holiday_date BETWEEN %s AND %s",
                (d_from, d_to))
    hol = {r[0] for r in cur.fetchall()}
    out, d = [], d_from
    while d <= d_to:
        if d.weekday() < 5 and d not in hol:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


@router.get("/api/digest/internals/series")
def digest_internals_series(bars: int = 200):
    """cc#1121: the 5-min intraday history behind each INTERNALS card.

    NEVER PADDED. Each series reports the bar count it actually has and the range it actually
    covers; a series with 60 bars ships 60 and says so on the card. `bars` is a ceiling, not a
    promise — all four currently hold a true 200 (verified on live rows: ADR and BREADTH 2,881
    stored, PCR NIFTY 1,932, PCR BANKNIFTY 1,729).
    """
    bars = max(10, min(int(bars or 200), 500))
    try:
        with _conn() as conn, conn.cursor() as cur:
            out = []
            for key, label, dp in _SERIES_META:
                cur.execute(_SERIES_SQL[key], (bars,))
                rows = list(reversed(cur.fetchall()))       # oldest first for drawing
                if not rows:
                    out.append({"key": key, "label": label, "dp": dp, "bars": 0,
                                "points": [], "sessions": [], "note": "no rows stored"})
                    continue
                d_from, d_to = rows[0][0].date(), rows[-1][0].date()
                sessions = _trading_dates(cur, d_from, d_to)
                pos = {d: i for i, d in enumerate(sessions)}
                pts = []
                for ts, v in rows:
                    ds = ts.date().isoformat()
                    if ds not in pos:        # a bar on a day the calendar calls closed: keep it,
                        continue             # but never invent a slot for it
                    mins = ts.hour * 60 + ts.minute - _SESSION_OPEN_MIN
                    pts.append({"s": pos[ds],
                                "f": round(max(0.0, min(1.0, mins / _SESSION_MIN)), 4),
                                "v": round(float(v), 4)})
                have = sorted({p["s"] for p in pts})
                out.append({
                    "key": key, "label": label, "dp": dp,
                    "bars": len(pts),
                    "points": pts,
                    "sessions": sessions,                       # every trading date in the span
                    "empty_sessions": [sessions[i] for i in range(len(sessions))
                                       if i not in set(have)],  # feed holes, named not hidden
                    "first": rows[0][0].strftime("%d-%b %H:%M"),
                    "last": rows[-1][0].strftime("%d-%b %H:%M"),
                })
            return {"series": out, "resolution": "5-min", "asked": bars}
    except Exception as e:
        log.exception("digest_internals_series failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "series": []}
