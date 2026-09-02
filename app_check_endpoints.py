"""app_check_endpoints.py — cc#1593: the app Check tab's three reads (APP_CHECK_PAGE_V2, session_log 36637).

WHY THIS FILE EXISTS
    The app Check page read /api/mobile/check, which SELECTs tc_screener_cache — the OLD v3.4-family
    screener table. The founder's ruling (02-Sep, cc_task_logs 4488) is that Trade Check is the
    four-bucket /100 scorer, best of four, and that the app must show it. No app-facing endpoint
    served that engine, so this file is the one place the app reads it from. App-facing endpoint
    changes are backend, so they are CC's (ROLE_CHARTER_V4).

WHAT IT SERVES
    GET /api/mobile/check/tc?symbol=X        one symbol, all four cards, best of four, rules per card
    GET /api/mobile/check/scan?universe=     nifty50 | futures — a FRESH universe scan, best card per
                                             symbol, Day % on the prev-close basis, GVM
    GET /api/mobile/check/invest?symbol=X    invest_check_v2 payload + CMP for the hero

WHAT IT DOES NOT DO
    It computes no score. Every number is the engine's own: tc_resolver.get_primary_styles() for the
    cards (the same call the web Check page makes), get_primary_scan() for the universe, and
    invest_check_v2.compute for the invest score. Band cuts come from get_primary_style_bands(),
    registry weight sums from tc_rule_weights at request time — nothing here is a literal a rule
    change could leave stale (TC_SCORE_100_V1, 29138).

HONESTY
    A card whose bucket has no live registry weighting (score10_weighted False) ships weighted=false
    and band=null; the page prints "unweighted" there instead of banding a number the registry did
    not calibrate. The scan states its own computed_at and runtime; the Nifty 50 universe is the
    top-50 active futures by market cap and is labelled as that proxy, never as the index.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Request

from mobile_endpoints import _guard, _json_safe, _conn
from tc_resolver import get_primary_styles, get_primary_style_bands, get_primary_scan
from invest_check_v2 import compute as invest_compute
from cmp_resolver import resolve_cmp, resolve_cmp_many

router = APIRouter()
log = logging.getLogger("app_check")

NIFTY50_N = 50
UNIVERSES = {
    "nifty50": "Nifty 50 (mcap proxy)",
    "futures": "All Futures",
}


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _weight_sums(cur):
    """Registry SUM(weight) per bucket, read now — never a literal (29138)."""
    cur.execute("SELECT bucket, SUM(weight) FROM tc_rule_weights WHERE active GROUP BY bucket")
    return {b: float(w) for b, w in cur.fetchall()}


def _company(cur, symbol):
    cur.execute("""SELECT company_name, segment, gvm_score, market_cap
                   FROM gvm_scores WHERE symbol = %s ORDER BY score_date DESC LIMIT 1""", (symbol,))
    r = cur.fetchone()
    if not r:
        return {"company": None, "segment": None, "gvm": None, "market_cap_cr": None}
    return {"company": r[0], "segment": r[1], "gvm": _f(r[2]),
            "market_cap_cr": (round(float(r[3]), 0) if r[3] is not None else None)}


def _cuts100():
    b = get_primary_style_bands()
    return {k: round(v * 10.0, 1) for k, v in b.items()}


def _band_word(verdict10):
    """The scorer's /10 verdict word on the /100 display: REJECT reads FAIL (29138 bands)."""
    if verdict10 is None:
        return None
    return "FAIL" if verdict10 == "REJECT" else verdict10


def _cmp_line(cur, symbol):
    try:
        p = resolve_cmp(cur, symbol) or {}
    except Exception as e:
        log.warning("resolve_cmp %s: %s", symbol, e)
        p = {}
    return {"cmp": _f(p.get("cmp")), "prev_close": _f(p.get("prev_close")),
            "day_pct": _f(p.get("day_pct")), "source": p.get("source"),
            "ts": (str(p.get("ts")) if p.get("ts") else None), "live": bool(p.get("live"))}


# ── TRADE CHECK ────────────────────────────────────────────────────────────────────────────────
@router.get("/api/mobile/check/tc")
@_json_safe
def app_check_tc(request: Request, symbol: str = ""):
    g = _guard(request)
    if g:
        return g
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol required"}
    r = get_primary_styles()(sym, "ALL")
    if not isinstance(r, dict) or r.get("error"):
        return {"error": (r or {}).get("error", "trade check failed"), "symbol": sym}
    with _conn() as conn, conn.cursor() as cur:
        sums = _weight_sums(cur)
        meta = _company(cur, sym)
        cmp_line = _cmp_line(cur, sym)
    cards = []
    for c in r.get("cards") or []:
        label = c.get("label")
        rules = []
        earned = 0.0
        for x in c.get("rules") or []:
            mx = _f(x.get("max")) or 0.0
            w = _f(x.get("weight"))
            cr = _f(x.get("credit")) or 0.0
            if mx > 0 and w is not None:
                earned += w * (cr / mx)
            rules.append({"rule": x.get("rule"), "label": x.get("label"), "credit": cr, "max": mx,
                          "weight": w, "value": x.get("value"), "required": x.get("required")})
        cards.append({
            "label": label, "side": c.get("side"), "style": c.get("style"),
            "score100": _f(c.get("score100")), "score10": _f(c.get("score10")),
            "weighted": bool(c.get("score10_weighted")),
            "band": _band_word(c.get("verdict10")),
            "raw_score": _f(c.get("score")), "raw_max": _f(c.get("max")),
            "weight_sum": sums.get(label),
            "weighted_earned": round(earned, 2),
            "unmapped_rules": c.get("score10_unmapped_rules") or [],
            "n_rules": len(rules), "rules": rules,
        })
    best = r.get("best") or {}
    return {
        "symbol": sym, "company": meta["company"], "segment": meta["segment"], "gvm": meta["gvm"],
        "cmp": _f(r.get("cmp")), "cmp_line": cmp_line,
        "computed_at": r.get("computed_at"),
        "best_label": best.get("label"),
        "best_score100": _f(best.get("score100")),
        "best_band": _band_word(best.get("verdict10")),
        "best_weighted": bool(best.get("score10_weighted")),
        "cuts": _cuts100(),
        "weight_sums": sums,
        "alerts": r.get("alerts") or [],
        "pivots": r.get("pivots") or {},
        "cards": cards,
        "engine": "tc_v4_dual four-bucket via tc_resolver.get_primary_styles",
        "scale": "score100 = score10 x 10 = 100 x sum(w x credit/max) / sum(w), w from tc_rule_weights (active), read now",
        "price_basis": "spot price, not futures",
        "spec_ref": r.get("spec_ref"), "version": r.get("version"),
    }


# ── RUN SCAN ───────────────────────────────────────────────────────────────────────────────────
def _nifty50_symbols(cur):
    cur.execute("""SELECT f.symbol FROM futures_universe f
                   JOIN gvm_scores g ON g.symbol = f.symbol
                   WHERE f.is_active = TRUE AND g.market_cap IS NOT NULL
                   ORDER BY g.market_cap DESC NULLS LAST LIMIT %s""", (NIFTY50_N,))
    return [r[0] for r in cur.fetchall()]


@router.get("/api/mobile/check/scan")
@_json_safe
def app_check_scan(request: Request, universe: str = "nifty50"):
    g = _guard(request)
    if g:
        return g
    uni = (universe or "nifty50").strip().lower()
    if uni not in UNIVERSES:
        return {"error": "universe must be nifty50 or futures"}
    sc = get_primary_scan()("ALL", "ALL", None, 400)
    if not isinstance(sc, dict) or sc.get("error"):
        return {"error": (sc or {}).get("error", "scan failed")}
    results = sc.get("results") or []
    with _conn() as conn, conn.cursor() as cur:
        keep = None
        if uni == "nifty50":
            keep = set(_nifty50_symbols(cur))
            results = [x for x in results if x.get("symbol") in keep]
        syms = [x["symbol"] for x in results]
        try:
            cmps = resolve_cmp_many(cur, syms) if syms else {}
        except Exception as e:
            log.warning("resolve_cmp_many: %s", e)
            cmps = {}
        sums = _weight_sums(cur)
    rows = []
    for x in results:
        p = cmps.get(x["symbol"]) or {}
        band = _band_word(x.get("best_verdict10"))
        rows.append({
            "symbol": x["symbol"], "segment": x.get("segment"),
            "best_label": x.get("best_label"),
            "score100": _f(x.get("best_score100")),
            "weighted": bool(x.get("best_score10_weighted")),
            "band": band,
            "cmp": _f(p.get("cmp")) if p.get("cmp") is not None else _f(x.get("cmp")),
            "day_pct": _f(p.get("day_pct")),
            "day_basis": "prev_close" if p.get("day_pct") is not None else None,
            "gvm": _f(x.get("gvm")),
        })
    rows.sort(key=lambda z: (-(z["score100"] if z["score100"] is not None else -1.0),
                             -(z["gvm"] or 0.0), z["symbol"]))
    counts = {"STRONG": 0, "VALID": 0, "WATCH": 0, "FAIL": 0, "UNWEIGHTED": 0}
    for z in rows:
        if z["band"] in counts:
            counts[z["band"]] += 1
        else:
            counts["UNWEIGHTED"] += 1
    return {
        "universe": uni, "label": UNIVERSES[uni],
        "universe_note": ("top-50 active futures by market cap — a proxy, not the index"
                          if uni == "nifty50" else "every active symbol in futures_universe"),
        "universe_count": (len(keep) if keep is not None else int(sc.get("universe") or 0)),
        "scored": len(rows), "counts": counts,
        "computed_at": sc.get("computed_at"), "runtime_s": sc.get("runtime_s"),
        "as_of": sc.get("as_of"), "session_bars_as_of": sc.get("session_bars_as_of"),
        "cuts": _cuts100(), "weight_sums": sums,
        "engine": "tc_v4_scan (shared score_card) via tc_resolver.get_primary_scan",
        "day_basis": "cmp_resolver prev-close basis (cc#1565)",
        "rows": rows,
    }


# ── INVEST CHECK ───────────────────────────────────────────────────────────────────────────────
@router.get("/api/mobile/check/invest")
@_json_safe
def app_check_invest(request: Request, symbol: str = ""):
    g = _guard(request)
    if g:
        return g
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol required"}
    with _conn() as conn, conn.cursor() as cur:
        d = invest_compute(cur, sym)
        if not isinstance(d, dict):
            return {"error": "invest check failed", "symbol": sym}
        if d.get("error"):
            return d
        d["cmp_line"] = _cmp_line(cur, sym)
    d["bands100"] = None   # invest stays on the /10 scale by its own lock (27979); stated, not scaled
    return d
