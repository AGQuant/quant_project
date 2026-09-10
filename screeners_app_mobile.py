"""
screeners_app_mobile.py — cc#1899 APP BUILD: Screeners section page backend
(design_refs/scorr_app_screeners_R1.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

ONE endpoint, GET /api/mobile/screeners_app. Reuses screeners_endpoints.py's own
screeners_list() DIRECTLY for the registry-derived tab-strip data (id, name, members,
last_run, source, newest) — nothing here re-derives v13_presets/v13_screen_results with a
second SQL text that could drift from the web Screeners page's own numbers.

SCREEN COUNT — READ THIS BEFORE TRUSTING THE REF'S "6 screens" NUMBER. The ref
(scorr_app_screeners_R1.html) shows Screens live=6, Names surfaced=142, Stale screens=2,
against a global v13_presets registry that on 09-Sep-2026 hand-verifies to 15 global screens
(7 'scorr' + 8 'finz'), not 6 — the ref is a stale/partial snapshot from before finz screens
were added to scope='global'. One number DOES still reproduce exactly across both counts: the
SUM of every screen's member rows is 224 either way (6-screen ref subset and the live 15-screen
set both sum to 224 — hand-verified, coincidental, not forced). This build reports the TRUE,
current, registry-derived count (15 screens / 173 distinct names / 8 stale) rather than the
ref's narrower snapshot, per this card's own item 3 ("derived from the table, not hardcoded").
FLAGGED for Fable/founder — not silently reconciled.

STALE COUNT — a screen is stale if its own last_run is behind the group's most recent last_run
(MAX(last_run) across all 15, currently 2026-09-08). 7 screens ran 08-Sep, 1 (SIP) ran 05-Sep,
7 ran 31-Aug — 8 behind the newest, not the ref's 2. Same-day figure, same "derived not
hardcoded" reasoning as above.

NAMES SURFACED — screeners_list() only carries per-screen counts + a 3-deep "newest" slice, not
the full member list, so "distinct names across every global screen" needed one extra light
COUNT(DISTINCT symbol) query here (JOIN v13_screen_results to v13_presets WHERE scope='global') —
not a second copy of screeners_list()'s own logic, just the one aggregate it does not already
return. Hand-verified: 173 distinct symbols / 224 total rows across the 15 screens.

WHAT CAME UP TODAY — built from screeners_list()'s own `newest` field (up to 3 most-recent-
first_seen symbols per screen, cc#1677), filtered to screens whose last_run equals the group max
(today's actual run, never a stale screen's "newest" presented as if it just ran), deduped by
symbol, capped at the layout law's 6-row rail limit.

COVERAGE / UNIVERSE CARD — READ THIS BEFORE TRUSTING THE REF'S "Screener base 92% / Trendlyne
overlap 37%" FIGURES. Exhaustively checked for a live source and found NONE that reproduces
either number: distinct input_raw.nse_code (2008) against nifty500_universe (100%),
investment_scanner_universe distinct symbols (540), gvm_cache overlap with screener_raw (1793/
1793 = 100%) and with trendlyne_symbol_map (821/1793 = 45.8%) — no denominator in this database
lands on 92% or 37%. These two figures do not exist as a stored or derivable fact here; they read
as an external, non-DB vendor-coverage claim baked into the ref's own copy. Rather than invent a
number to match, this card renders the one relationship that IS live and verifiable — Screener.in
coverage of the GVM-tracked universe (screener_raw against gvm_cache, currently 1793/1793 = 100%)
and Trendlyne's verification-only overlap of that same universe (821/1793 = 45.8%) — labelled by
what it actually measures, not silently relabelled to match the ref's copy. FLAGGED for Fable/
founder to say whether the ref's 92%/37% are external facts to hardcode as prose (never as a
"live" badge) or a stale figure to correct.

SAVED SCREENS / RUN HISTORY — confirmed via information_schema (no saved_screen / screen_run /
screener_run / custom_screen table anywhere in the schema): both render as honest-empty, naming
the gap rather than a fake zero (V4/V5 of the card).

do_not_touch honoured: screeners_endpoints.py (the web /api/screeners + /api/screeners/{id}
pair) — read-only import, zero edits. v13_presets / v13_screen_results / the nightly job that
fills them — read-only, zero writes. mobile/screeners.html's OLD chip-strip UI is retired by
this card's own page rewrite (P14-equivalent), not by this file.
"""
import logging
import os

from fastapi import APIRouter, Request

from mobile_endpoints import _conn, _guard, _json_safe
from screeners_endpoints import screeners_list

log = logging.getLogger("scorr.screeners_app")
router = APIRouter()

RAIL_ROW_CAP = 6


def _universe_coverage(cur):
    """Screener.in / Trendlyne coverage of the GVM-tracked universe. See module docstring —
    this does NOT reproduce the design ref's 92%/37%; those figures are not derivable from any
    table this query touched."""
    cur.execute("SELECT COUNT(*) FROM gvm_cache")
    gvm_total = cur.fetchone()[0] or 0
    cur.execute("""SELECT COUNT(*) FROM gvm_cache g
                   WHERE EXISTS (SELECT 1 FROM screener_raw s WHERE s.nse_code = g.symbol)""")
    screener_covered = cur.fetchone()[0] or 0
    cur.execute("""SELECT COUNT(*) FROM gvm_cache g
                   WHERE EXISTS (SELECT 1 FROM trendlyne_symbol_map t WHERE t.nse_code = g.symbol)""")
    trendlyne_covered = cur.fetchone()[0] or 0
    return {
        "basis": "GVM-tracked universe",
        "gvm_total": gvm_total,
        "screener_covered": screener_covered,
        "screener_pct": round(screener_covered / gvm_total * 100, 1) if gvm_total else None,
        "trendlyne_covered": trendlyne_covered,
        "trendlyne_pct": round(trendlyne_covered / gvm_total * 100, 1) if gvm_total else None,
        "note": ("Screener.in is the base layer; Trendlyne checks the overlap, never the base. "
                 "Both measured against the GVM-tracked universe, not the full NSE list."),
    }


@router.get("/api/mobile/screeners_app")
@_json_safe
def mobile_screeners_app(request: Request):
    g = _guard(request)
    if g:
        return g

    data = screeners_list()
    rows = data.get("screens") or []

    last_runs = [r["last_run"] for r in rows if r.get("last_run")]
    top_run = max(last_runs) if last_runs else None
    stale = [r for r in rows if r.get("last_run") and r["last_run"] != top_run]
    never_run = [r for r in rows if not r.get("last_run")]

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(DISTINCT r.symbol)
            FROM v13_screen_results r JOIN v13_presets p ON p.id = r.screen_id
            WHERE COALESCE(p.scope,'global') = 'global'
        """)
        names_surfaced = cur.fetchone()[0] or 0
        universe = _universe_coverage(cur)

    total_rows_stored = sum(r.get("members") or 0 for r in rows)

    ready = sorted(rows, key=lambda r: -(r.get("members") or 0))
    ready_top = [{"name": r["name"], "last_run": r.get("last_run"), "members": r.get("members") or 0,
                  "stale": bool(r.get("last_run") and r["last_run"] != top_run)}
                 for r in ready[:RAIL_ROW_CAP]]

    today_names = []
    seen = set()
    for r in rows:
        if r.get("last_run") != top_run:
            continue
        for sym in (r.get("newest") or []):
            if sym in seen:
                continue
            seen.add(sym)
            today_names.append({"symbol": sym, "screen_name": r["name"]})
            if len(today_names) >= RAIL_ROW_CAP:
                break
        if len(today_names) >= RAIL_ROW_CAP:
            break

    return {
        "status": "ok",
        "as_of": top_run,
        "key_metrics": {
            "screens_live": len(rows),
            "names_surfaced": names_surfaced,
            "last_run": top_run,
            "stale_screens": len(stale),
            "saved_screens": 0,
        },
        "ready_screens": {
            "rows": ready_top, "count": len(rows),
            "more": max(0, len(rows) - len(ready_top)),
            "message": (f"{len(stale)} screen(s) last ran before {top_run}." if stale else None),
        },
        "today": {
            "rows": today_names,
            "rows_stored_total": total_rows_stored,
        },
        "custom_builder": {
            "message": "Pick your own filters and run them across the universe. Your last four "
                       "filter choices are remembered.",
            "footnote": "A TC score column is being added under cc#1881.",
        },
        "saved_screens_card": {
            "count": 0,
            "message": "Screens you build are not stored yet. There is no saved-screen table, "
                       "so this names the gap instead of pretending it is empty by choice.",
        },
        "universe": universe,
        "run_history": {
            "available": False,
            "message": "Only the current result set per screen is kept, plus first-seen and "
                       "last-seen dates per name. There is no history of past runs to chart.",
        },
        "never_run": bool(never_run) and len(never_run) == len(rows),
    }


# ═══ QB_APP-STYLE V2 (SCREENERS_APP_R2_LOCK, session_log 42826; Fable 10-Sep-2026) ═════════════════
# Two additive endpoints. The cc#1899 endpoint above is untouched.
#   GET /api/mobile/screeners_app/list          → quant screens only, grouped, rule in plain words
#   GET /api/mobile/screeners_app/screen?id=<n> → the screen's names for the sortable table
# QUANT vs client list: filters non-empty vs empty on v13_presets (rule 1). Groups from the filters
# (rule 2). Never a name list.

_LABEL = {
    "gvm_score": "GVM", "g_score": "G", "v_score": "V", "m_score": "M", "roce": "ROCE",
    "market_cap": "mcap", "week_index_52": "52-wk pos", "month_index": "month pos",
    "rsi_month": "monthly RSI", "vol_ratio": "volume", "vol_ratio_21": "volume", "dma_50": "vs 50-DMA",
    "return_1y": "1-yr return", "year_return": "1-yr return", "return_3y": "3-yr return",
    "month_return": "month return", "sector_month": "sector month",
}
_PCT = {"return_1y", "year_return", "return_3y", "month_return", "sector_month"}


def _cr(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return ("₹%.0fL Cr" % (v / 100000)) if v >= 100000 else ("₹%sk Cr" % int(v / 1000) if v >= 1000 else "₹%s Cr" % int(v))


def _rule_words(filters):
    parts = []
    for k, spec in (filters or {}).items():
        if not isinstance(spec, dict):
            continue
        lab = _LABEL.get(k, k.replace("_", " "))
        lo, hi = spec.get("min"), spec.get("max")
        if k == "market_cap":
            if lo is not None and hi is not None:
                parts.append("%s %s–%s" % (lab, _cr(lo), _cr(hi)))
            elif lo is not None:
                parts.append("%s ≥ %s" % (lab, _cr(lo)))
            elif hi is not None:
                parts.append("%s ≤ %s" % (lab, _cr(hi)))
            continue
        if k in ("week_index_52", "month_index") and lo is not None and float(lo) >= 90:
            parts.append("near 52-wk high" if k == "week_index_52" else "near month high")
            continue
        if k == "dma_50" and lo is not None and float(lo) >= 0:
            parts.append("above 50-DMA")
            continue
        if k in ("vol_ratio", "vol_ratio_21") and lo is not None:
            parts.append("volume %s×" % (("%g" % float(lo))))
            continue
        sfx = "%" if k in _PCT else ""
        def fmt(x):
            x = float(x)
            return ("%d" % x if x == int(x) else "%g" % x) + sfx
        if lo is not None and hi is not None:
            parts.append("%s %s–%s" % (lab, fmt(lo), fmt(hi)))
        elif lo is not None:
            parts.append("%s ≥ %s" % (lab, fmt(lo)))
        elif hi is not None:
            parts.append("%s ≤ %s" % (lab, fmt(hi)))
    return " · ".join(parts)


def _group_of(filters):
    f = filters or {}
    v = (f.get("v_score") or {}).get("min")
    if v is not None and float(v) >= 7.5:
        return "value"
    if any(k in f for k in ("week_index_52", "month_index", "rsi_month")) or (f.get("m_score") or {}).get("min") is not None:
        return "momentum"
    return "quality"


_GROUPS = [("momentum", "Momentum & breakouts", "what is moving now"),
           ("quality", "Quality & growth", "built to compound"),
           ("value", "Value", "priced below their quality")]


@router.get("/api/mobile/screeners_app/list")
@_json_safe
def mobile_screeners_list(request: Request):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT p.id, p.name, p.filters, p.sort_key,
                   COUNT(r.symbol) AS members, MAX(r.last_seen) AS last_run
            FROM v13_presets p LEFT JOIN v13_screen_results r ON r.screen_id = p.id
            WHERE COALESCE(p.scope,'global') = 'global'
              AND p.filters IS NOT NULL AND p.filters::text NOT IN ('{}', 'null')
            GROUP BY p.id, p.name, p.filters, p.sort_key ORDER BY p.name
        """)
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        ids = [r["id"] for r in rows]
        top = max([r["last_run"] for r in rows if r["last_run"]] or [None])
        names_today = 0
        if ids and top:
            cur.execute("SELECT COUNT(DISTINCT symbol) FROM v13_screen_results WHERE screen_id = ANY(%s) AND last_seen = %s", (ids, top))
            names_today = cur.fetchone()[0] or 0
    cards = []
    for r in rows:
        stale = bool(r["last_run"] and top and r["last_run"] != top)
        cards.append({"id": r["id"], "name": r["name"], "members": int(r["members"] or 0),
                      "last_run": str(r["last_run"]) if r["last_run"] else None, "stale": stale,
                      "rule": _rule_words(r["filters"]), "group": _group_of(r["filters"])})
    groups = []
    for key, title, hint in _GROUPS:
        gs = sorted([c for c in cards if c["group"] == key], key=lambda c: -c["members"])
        groups.append({"key": key, "title": title, "hint": hint, "rows": gs})
    return {"as_of": str(top) if top else None,
            "key_metrics": {"screens": len(cards), "names_today": names_today, "stale": sum(1 for c in cards if c["stale"])},
            "groups": groups}


@router.get("/api/mobile/screeners_app/screen")
@_json_safe
def mobile_screeners_screen(request: Request, id: int = 0):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, filters, sort_key FROM v13_presets WHERE id=%s", (id,))
        p = cur.fetchone()
        if not p:
            return {"error": "no such screen"}
        cur.execute("""
            SELECT r.symbol, r.rank, r.last_seen, r.first_seen, r.snapshot,
                   g.gvm_score, g.growth, g.value, g.momentum, g.segment
            FROM v13_screen_results r LEFT JOIN gvm_cache g ON g.symbol = r.symbol
            WHERE r.screen_id=%s ORDER BY r.rank NULLS LAST, r.symbol
        """, (id,))
        rows = []
        for sym, rank, ls, fs, snap, gvm, gr, va, mo, seg in cur.fetchall():
            snap = snap or {}
            rows.append({"symbol": sym, "rank": rank, "last_seen": str(ls) if ls else None, "first_seen": str(fs) if fs else None,
                         "gvm": float(gvm) if gvm is not None else (float(snap["gvm_score"]) if snap.get("gvm_score") is not None else None),
                         "g": float(gr) if gr is not None else None, "v": float(va) if va is not None else None,
                         "m": float(mo) if mo is not None else None, "sector": seg,
                         "mcap": snap.get("market_cap")})
    return {"id": p[0], "name": p[1], "rule": _rule_words(p[2]), "sort_key": p[3], "rows": rows, "count": len(rows),
            "last_run": max([r["last_seen"] for r in rows if r["last_seen"]] or [None])}
