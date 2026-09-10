"""
qb_app_mobile.py — Quant Baskets app section backend. Fable single-mode rewrite 10-Sep-2026
(QB_APP_R5_LOCK, session_log 42805; design_refs/scorr_app_quantbasket_R5.html @ c774aac).
Supersedes the cc#1892 version of this file (R4 card-index payload).

THREE reads, all assembled from the web Quant Basket tab's OWN functions in qb_endpoints.py and
qb_nav.py — nothing here re-derives a number the web already serves:
  GET /api/mobile/qb_app/list                → two decks (Curated / Quant) of spine cards
  GET /api/mobile/qb_app?basket=<name>       → the detail page payload
  GET /api/mobile/qb_app/holdings?basket=<n> → every open holding, for the sortable table

DECKS are registry-derived: /api/qb/registry marks each basket Discretionary or Quant from
app_config.qb_discretionary_baskets (cc#1677). Discretionary = CURATED deck, Quant = QUANT deck.
Never a hardcoded name list. The six finz_ client baskets are excluded by the same naming-
convention filter cc#1892 used (app_visible column still pending its founder-gated ALTER).

MIN VALUE = the cost of ONE SHARE of each open holding at current price, labelled on the page as
"Min · one share each". No min-investment field exists anywhere in the platform; this is the one
honest, reproducible definition. Founder 10-Sep: correct if a different rule is wanted.

RETURN SINCE START = qb_nav.get_series(MAX).basket_return_pct — the same number the web chart's
headline shows, NOT the cc#1892 open-book unrealised ratio. One number on both surfaces.

GAPS ARE STATED, NOT FILLED (founder 10-Sep: "design and position matters, backend not — gaps we
fill next sprint"): a basket with no NAV rows yet returns return_pct=null and the page says
"no history yet"; a basket with no scored names returns scorecard=null; model_portfolio's method
text comes from registry.notes until the Index Intel tab's copy is wired.
"""

import logging
from fastapi import APIRouter, Request

from mobile_endpoints import _conn, _guard, _json_safe
from qb_endpoints import qb_summary, qb_positions, qb_registry, qb_rebalance_history, _BEES
import qb_nav

log = logging.getLogger("scorr.mobile.qb_app")
router = APIRouter()

RAIL_CAP = 6
LABEL = {
    "small_cap": "Small Cap", "mid_cap": "Mid Cap", "large_cap": "Large Cap",
    "alpha_multicap": "Alpha Multicap", "breakout_52w": "Breakout 52w",
    "contra_value": "Contra Value", "model_portfolio": "Model Portfolio",
}
# one-line "why" per basket — plain words, cc#1892 P5 discipline; registry.notes is the fallback
WHY = {
    "alpha_multicap": "Quality and momentum, half and half. Top names equal-weighted.",
    "small_cap": "Themes with a long runway, then the strongest name in each.",
    "mid_cap": "Ranked 101–250 by size, screened for quality and momentum.",
    "large_cap": "The 100 biggest names, screened for quality, value and steady momentum.",
    "breakout_52w": "Names breaking to new one-year highs with quality behind the move.",
    "contra_value": "Good, cheap companies turning around — value first, momentum catching up.",
    "model_portfolio": "Names Arpit picked by hand from the quant shortlist, equal weight. Rebalanced when he decides.",
}
RULE = {
    "alpha_multicap": "Score every stock on <b>quality</b> and <b>momentum</b>, half and half. Hold the top 15, <b>equal weight</b>. Each month, drop any that has slipped well outside the leading ranks and take the next one in. A stop on each name closes it early if it breaks.",
    "small_cap": "Find <b>themes</b> with a long runway. In each, take only the <b>strongest scoring</b> small company. Equal weight. Reviewed each quarter; a stop closes any name that breaks.",
    "mid_cap": "Universe = companies ranked <b>101 to 250</b> by size. Keep those with strong quality, rank by <b>momentum</b>, hold the top 20 equal weight. Monthly review; stops on every name.",
    "large_cap": "Universe = the <b>100 biggest</b> companies. Score on quality and momentum, keep names whose score is <b>rising</b>. Top 12 to 15, equal weight, monthly review.",
    "breakout_52w": "A name must be at a <b>new one-year high</b> with volume behind it and a strong quality score. Hold the top 10 by one-year return. Monthly review.",
    "contra_value": "High <b>quality</b>, high <b>value</b>, momentum still low — a good company the market has not noticed yet. Must be above its 20-day average. Top 10 by value score.",
    "model_portfolio": "Hand-picked from the quant shortlist, <b>equal weight</b>. No fixed schedule — rebalanced on a <b>need basis</b> when Arpit decides.",
}
_SECTOR_PARENT_RULES = [
    ("auto", "Auto"), ("pharma", "Pharma"), ("hospital", "Pharma"), ("cdmo", "Pharma"),
    ("healthcare", "Pharma"), ("diagnostic", "Pharma"), ("internet", "Internet"), ("digital", "Internet"),
    ("e-commerce", "Internet"), ("insurance", "Insurance"), ("broadcasting", "Media"), ("media", "Media"),
    ("bank", "Finance"), ("nbfc", "Finance"), ("finance", "Finance"), ("it ", "IT"), ("software", "IT"),
    ("cement", "Cement"), ("construction", "Construction"), ("pipes", "Pipes"), ("power", "Energy"),
    ("energy", "Energy"), ("oil", "Energy"), ("refiner", "Energy"), ("metal", "Metals"), ("mining", "Metals"),
    ("steel", "Metals"), ("fmcg", "Consumer"), ("consumer", "Consumer"), ("chemical", "Chemicals"),
]


def _label(n):
    return LABEL.get(n, str(n or "").replace("_", " ").title())


def _sector_parent(segment, symbol):
    if symbol in _BEES:
        return "Index ETF"
    if not segment:
        return "Other"
    s = segment.lower()
    for k, v in _SECTOR_PARENT_RULES:
        if k in s:
            return v
    return segment.split(" - ")[0].strip() if " - " in segment else segment


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _visible(cur):
    cur.execute("SELECT basket_name FROM quant_basket_registry WHERE is_active AND basket_name NOT LIKE 'finz\\_%' ORDER BY basket_name")
    return [r[0] for r in cur.fetchall()]


def _nav(conn, basket, window="MAX"):
    try:
        s = qb_nav.get_series(conn, basket, window)
    except Exception as e:
        log.warning("qb_nav %s: %s", basket, e)
        return {"points": [], "enough_history": False}
    return s


def _spark(points, n=24):
    """Thin the series to ~n points for the 96px sparkline; last point always kept."""
    if not points:
        return []
    step = max(1, len(points) // n)
    out = points[::step]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return [{"d": p["d"], "b": p["nav"], "i": p["bench"]} for p in out]


def _fmt_freq(reg):
    f = (reg.get("rebalance_freq") or "").lower()
    if f in ("need", "need_basis", "discretionary", "as_needed"):
        return "Need basis"
    return f.title() if f else "—"


def _enrich(cur, positions):
    """segment + cap per symbol, one query each."""
    syms = [p["symbol"] for p in positions if p.get("symbol")]
    seg, cap = {}, {}
    if syms:
        cur.execute("SELECT symbol, segment FROM gvm_cache WHERE symbol = ANY(%s)", (syms,))
        seg = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT nse_code, cap_category FROM input_raw WHERE nse_code = ANY(%s)", (syms,))
        cap = {r[0]: r[1] for r in cur.fetchall()}
    for p in positions:
        p["segment"] = seg.get(p["symbol"])
        p["sector"] = _sector_parent(p["segment"], p["symbol"])
        p["cap"] = ("ETF" if p["symbol"] in _BEES else (cap.get(p["symbol"]) or "—"))
    return positions


def _card(conn, cur, basket, reg):
    pos = qb_positions(basket, "open")
    pos = pos if isinstance(pos, list) else []
    nav = _nav(conn, basket, "MAX")
    pts = nav.get("points") or []
    min_one = sum(_f(p.get("current_price")) or 0 for p in pos) if pos else None
    return {
        "basket": basket, "label": _label(basket), "why": WHY.get(basket) or (reg.get("notes") or ""),
        "type": reg.get("type") or "Quant",
        "min_one_share": round(min_one, 0) if min_one else None,
        "rebalance": _fmt_freq(reg), "next_rebalance": str(reg["next_rebalance"]) if reg.get("next_rebalance") else None,
        "since": nav.get("points", [{}])[0].get("d") if pts else None,
        "return_pct": nav.get("basket_return_pct"), "bench_pct": nav.get("benchmark_return_pct"),
        "enough_history": bool(nav.get("enough_history")), "spark": _spark(pts),
        "names": len(pos),
    }


@router.get("/api/mobile/qb_app/list")
@_json_safe
def mobile_qb_list(request: Request):
    g = _guard(request)
    if g:
        return g
    regs = {r["basket_name"]: r for r in (qb_registry() or []) if isinstance(r, dict)}
    with _conn() as conn, conn.cursor() as cur:
        visible = _visible(cur)
        cards = [_card(conn, cur, b, regs.get(b, {})) for b in visible]
        cur.execute("SELECT MAX(updated_at) FROM quant_paper_positions WHERE basket_name = ANY(%s)", (visible,))
        as_of = cur.fetchone()[0]
    curated = [c for c in cards if c["type"] == "Discretionary"]
    quant = [c for c in cards if c["type"] != "Discretionary"]
    curated.sort(key=lambda c: (c["basket"] != "model_portfolio", -(c["return_pct"] or -1e9)))
    quant.sort(key=lambda c: -(c["return_pct"] if c["return_pct"] is not None else -1e9))
    return {"as_of": as_of.isoformat() if as_of else None,
            "decks": [{"key": "curated", "title": "Curated", "hint": "picked by hand", "rows": curated},
                      {"key": "quant", "title": "Quant", "hint": "rule-driven · ranked by return", "rows": quant}],
            "count": len(cards)}


def _history(basket):
    """Real events only, from the web tab's own classifier. One node per rebalance block plus the
    hard-stop exits (hsl) folded into the block dated the same day."""
    h = qb_rebalance_history(basket, 400)
    if not isinstance(h, dict):
        return [], 0
    blocks = h.get("blocks") or []
    hsl = (h.get("hsl") or {}).get("rows") or []
    stops_by_date = {}
    for r in hsl:
        stops_by_date.setdefault(r["date"], []).append(r)
    dates = sorted(set([b["date"] for b in blocks] + list(stops_by_date.keys())), reverse=True)
    out = []
    for d in dates:
        b = next((x for x in blocks if x["date"] == d), None)
        stops = stops_by_date.get(d, [])
        buys = [x["symbol"] for x in (b or {}).get("buys", [])]
        sells = [{"symbol": x["symbol"], "why": x.get("reason") or x.get("rule")} for x in (b or {}).get("sells", [])]
        stop_names = [{"symbol": x["symbol"], "why": x.get("rule_text") or x.get("rule"), "pnl_pct": x.get("pnl_pct")} for x in stops]
        kind = "inception" if (b and not sells and not stops and buys and d == dates[-1]) else ("stop" if stops else "rebalance")
        out.append({"date": d, "kind": kind, "buys": buys, "sells": sells, "stops": stop_names,
                    "n_in": len(buys), "n_out": len(sells) + len(stop_names),
                    "state": (b or {}).get("state"), "held_after": ((b or {}).get("footer") or {}).get("held_after")})
    return out, len(out)


@router.get("/api/mobile/qb_app")
@_json_safe
def mobile_qb_detail(request: Request, basket: str = "alpha_multicap", window: str = "MAX"):
    g = _guard(request)
    if g:
        return g
    regs = {r["basket_name"]: r for r in (qb_registry() or []) if isinstance(r, dict)}
    with _conn() as conn, conn.cursor() as cur:
        visible = _visible(cur)
        if basket not in visible:
            basket = visible[0] if visible else basket
        reg = regs.get(basket, {})
        pos = qb_positions(basket, "open")
        pos = _enrich(cur, pos if isinstance(pos, list) else [])
        nav = _nav(conn, basket, window if window in ("1M", "3M", "6M", "MAX") else "MAX")
        nav_max = nav if nav.get("window") == "MAX" else _nav(conn, basket, "MAX")
        cur.execute("SELECT MAX(updated_at) FROM quant_paper_positions WHERE basket_name=%s", (basket,))
        as_of = cur.fetchone()[0]
    summ = qb_summary(basket)
    total = sum(_f(p.get("current_value")) or 0 for p in pos)
    for p in pos:
        p["weight_pct"] = round((_f(p.get("current_value")) or 0) / total * 100, 1) if total else None
    pos.sort(key=lambda p: -(p.get("weight_pct") or 0))
    scored = [p for p in pos if _f(p.get("gvm")) is not None]
    def avg(k):
        vals = [_f(p.get(k)) for p in scored if _f(p.get(k)) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    winners = [p for p in pos if (_f(p.get("pnl_pct")) or 0) > 0 and p["symbol"] not in _BEES]
    stock_pos = [p for p in pos if p["symbol"] not in _BEES]
    worst = min(stock_pos, key=lambda p: _f(p.get("pnl_pct")) or 0) if stock_pos else None
    cash = sum(_f(p.get("current_value")) or 0 for p in pos if p["symbol"] in _BEES)

    def mix(key):
        agg = {}
        for p in pos:
            agg[p[key]] = agg.get(p[key], 0.0) + (_f(p.get("current_value")) or 0)
        rows = sorted([{"name": k, "pct": round(v / total * 100, 1)} for k, v in agg.items()], key=lambda x: -x["pct"]) if total else []
        return rows

    hist, n_hist = _history(basket)
    pts = nav_max.get("points") or []
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "basket": basket, "label": _label(basket), "type": reg.get("type") or "Quant",
        "cap_type": reg.get("cap_type"), "rebalance": _fmt_freq(reg),
        "next_rebalance": str(reg["next_rebalance"]) if reg.get("next_rebalance") else None,
        "since": pts[0]["d"] if pts else None,
        "nav": {"window": nav.get("window"), "points": nav.get("points") or [], "enough_history": bool(nav.get("enough_history")),
                "basket_return_pct": nav.get("basket_return_pct"), "bench_pct": nav.get("benchmark_return_pct"),
                "alpha_pct": nav.get("alpha_pct"), "benchmark_sym": nav.get("benchmark_sym"),
                "basket_100": (pts[-1]["nav"] if pts else None), "bench_100": (pts[-1]["bench"] if pts else None),
                "note": "Paper book, live from inception. No backtest exists for this basket."},
        "evidence": {"alpha_pct": nav_max.get("alpha_pct"), "hit": len(winners), "of": len(stock_pos),
                     "worst": ({"symbol": worst["symbol"], "pnl_pct": _f(worst.get("pnl_pct"))} if worst else None)},
        "scorecard": ({"g": avg("g"), "v": avg("v"), "m": avg("m"), "gvm": avg("gvm"), "n": len(scored)} if scored else None),
        "rule": {"html": RULE.get(basket) or (reg.get("notes") or "No rule text on record for this basket."),
                 "cap": reg.get("max_stocks"), "now": len(stock_pos),
                 "cash_pct": round(cash / total * 100, 1) if total else None},
        "holdings": {"rows": [{"symbol": p["symbol"], "segment": p["segment"], "sector": p["sector"], "cap": p["cap"],
                               "weight_pct": p["weight_pct"], "pnl_pct": _f(p.get("pnl_pct")), "gvm": _f(p.get("gvm"))}
                              for p in pos[:RAIL_CAP]], "count": len(pos)},
        "mix": {"cap": mix("cap"), "sector": mix("sector")},
        "history": {"rows": hist, "count": n_hist},
        "value": summ.get("market_value"), "unrealised": summ.get("unrealised_pnl"),
    }


@router.get("/api/mobile/qb_app/holdings")
@_json_safe
def mobile_qb_holdings(request: Request, basket: str = "alpha_multicap"):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        pos = qb_positions(basket, "open")
        pos = _enrich(cur, pos if isinstance(pos, list) else [])
    total = sum(_f(p.get("current_value")) or 0 for p in pos)
    rows = []
    for p in pos:
        rows.append({"symbol": p["symbol"], "sector": p["sector"], "cap": p["cap"],
                     "weight_pct": round((_f(p.get("current_value")) or 0) / total * 100, 1) if total else None,
                     "pnl_pct": _f(p.get("pnl_pct")), "day_pct": _f(p.get("day_pct")), "gvm": _f(p.get("gvm")),
                     "entry": _f(p.get("entry_price")), "entry_date": str(p.get("entry_date") or ""),
                     "stop": _f(p.get("stop_loss_price")), "price": _f(p.get("current_price")),
                     "qty": _f(p.get("qty")), "value": _f(p.get("current_value"))})
    rows.sort(key=lambda r: -(r["weight_pct"] or 0))
    return {"basket": basket, "label": _label(basket), "rows": rows, "count": len(rows)}
