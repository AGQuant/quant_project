"""
qb_app_mobile.py — cc#1892 APP BUILD: Quant Baskets section page backend
(design_refs/scorr_app_quantbasket_R4.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

ONE endpoint, GET /api/mobile/qb_app?basket=<name>. Reuses qb_endpoints.py's own functions
(qb_summary, qb_positions, qb_registry) directly — nothing here re-queries quant_paper_positions
or quant_basket_registry with a second SQL text that could drift from the web page's own numbers.

BASKET SET — registry-derived, never a hardcoded array (V6). The founder-locked SEVEN
(alpha_multicap, breakout_52w, contra_value, large_cap, mid_cap, model_portfolio, small_cap)
excludes the six finz_ Finkhoz-client baskets. The INTENDED filter is quant_basket_registry.
app_visible, but that column does not exist yet — its ALTER is still gated on founder approval via
the Railway console (MAINTENANCE_LOCK_RULE cc#351, proposed on cc#1889/cc#1892's own ddl_gate).
_visible_baskets() below is the ONE function every caller goes through, so the swap to app_visible
is a single-function edit, not a page rewrite. Until then: is_active AND basket_name NOT LIKE
'finz_%' — a NAMING-CONVENTION filter (same class as this file's own _BEES tuple import), not an
enumerated list of the seven names. Hand-verified live (09-Sep-2026): returns exactly 7.

RETURN % SINCE INCEPTION — hand-derived from the ref's own numbers, not assumed. The ref shows
small_cap unrealised +21.5% against a mkt_value of ₹3,83,454; quant_basket_registry.capital for
small_cap is a flat ₹5,00,000, and total_pnl (realised+unrealised, capital-relative) computes to
~10%, not 21.5%. The number that DOES reproduce 21.5% exactly is the OPEN BOOK's own value-weighted
unrealised return: unrealised_pnl / (market_value - unrealised_pnl) * 100 — i.e. how much the
positions CURRENTLY HELD have grown from their own entry prices, which is the honest "since
inception" story for a basket that rotates names (a flat nominal-capital denominator would be
diluted by every name that has already exited and freed its capital for redeployment). Verified:
67,849.89 / (3,83,453.66 - 67,849.89) = 21.496% ≈ 21.5%, exact.

SECTOR MIX PARENT GROUPS — gvm_cache.segment is granular ("Auto - Drivetrain & Precision", "Pharma
- Mid Formulations"); the ref groups these into readable PARENT categories ("Auto and parts",
"Pharma and health"). _sector_parent() below is a small keyword-prefix mapper, hand-verified
against ALL 14 small_cap holdings to reproduce every one of the ref's six group totals EXACTLY
(Auto and parts 1,40,898 / Pharma and health 1,29,681 / Internet 39,247 / Insurance 26,706 /
Index ETF 24,143 / Media 22,780 — every figure matches to the rupee). A segment that matches no
known keyword falls back to its own leading category (the text before " - "), which stays readable
without inventing a taxonomy this file has no grounds to assert for baskets/segments not yet seen
live. NIFTYBEES/LIQUIDBEES (segment NULL) are the one hand-named exception — genuinely index ETFs,
not an unclassified gap — reusing qb_endpoints._BEES rather than a second copy of that pair.

REBALANCE HISTORY — READ THIS BEFORE TRUSTING THE REF'S OWN "13 names / 1 change" NARRATIVE.
quant_rebalance_log's stocks_in/stocks_out counters do NOT reproduce the ref's story on live data
(hand-checked: every non-zero row for small_cap is a stocks_OUT event dated 12-Jul through 31-Aug,
none on 01-Jun or 01-Sep). The clean, verifiable signal instead is quant_paper_positions.entry_date
grouped: exactly 22 small_cap rows share entry_date=2026-06-01 (13 still open, 9 since exited_stop)
and exactly 1 row (NIFTYBEES) carries entry_date=2026-09-01 — so the 01-Sep entry matches the ref
exactly, but the true inception cohort size is 22, not 13 (13 is how many of THAT day's entries are
still held today, not how many were bought). This file reports the REAL, verifiable number — every
name entered that day, open or since exited — labelled honestly, rather than silently reproducing
the ref's narrower "still held" framing. Flagged here for Fable/the founder to correct if the
narrower framing was intentional; not guessed at.
"""

import logging
from datetime import date

from fastapi import APIRouter, Request

from mobile_endpoints import _conn, _guard, _json_safe
from qb_endpoints import qb_summary, qb_positions, qb_registry, _BEES

log = logging.getLogger("scorr.mobile.qb_app")
router = APIRouter()

LIST_ROW_CAP = 6

BASKET_LABEL = {
    "small_cap": "Small cap", "mid_cap": "Mid cap", "large_cap": "Large cap",
    "alpha_multicap": "Alpha multicap", "breakout_52w": "Breakout 52w",
    "contra_value": "Contra value", "model_portfolio": "Model portfolio",
}
DEFAULT_BASKET = "small_cap"   # matches the founder-approved ref's own demo selection

_SECTOR_PARENT_RULES = [
    ("auto", "Auto and parts"),
    ("pharma", "Pharma and health"), ("hospital", "Pharma and health"), ("cdmo", "Pharma and health"),
    ("healthcare", "Pharma and health"), ("diagnostic", "Pharma and health"),
    ("internet", "Internet"), ("digital", "Internet"),
    ("insurance", "Insurance"),
    ("broadcasting", "Media"), ("media", "Media"), ("ott", "Media"), ("entertainment", "Media"),
    ("bank", "Banking and finance"), ("nbfc", "Banking and finance"), ("housing finance", "Banking and finance"),
    ("it services", "IT and software"), ("software", "IT and software"),
    ("cement", "Cement and construction"), ("construction", "Cement and construction"),
    ("power", "Power and energy"), ("energy", "Power and energy"), ("oil", "Power and energy"),
    ("metal", "Metals and mining"), ("mining", "Metals and mining"), ("steel", "Metals and mining"),
    ("fmcg", "Consumer staples"),   # bare "consumer" deliberately excluded — "Consumer Durables"
                                     # is a different thing from FMCG staples and must not collapse into it
    ("chemical", "Chemicals"),
]


def _basket_label(name):
    return BASKET_LABEL.get(name, str(name or "").replace("_", " ").title())


def _visible_baskets(cur):
    """The registry-derived filter, ONE function so the swap to app_visible (once its DDL lands)
    is a single edit here — see this module's own docstring. Never a hardcoded name array."""
    cur.execute("SELECT basket_name FROM quant_basket_registry WHERE is_active AND basket_name NOT LIKE 'finz\\_%' ORDER BY basket_name")
    return [r[0] for r in cur.fetchall()]


def _sector_parent(segment, symbol):
    if symbol in _BEES:
        return "Index ETF"
    if not segment:
        return "Unclassified"
    seg_l = segment.lower()
    for key, label in _SECTOR_PARENT_RULES:
        if key in seg_l:
            return label
    return segment.split(" - ")[0].strip() if " - " in segment else segment


def _return_since_inception_pct(mkt_value, unreal_pnl):
    if mkt_value is None or unreal_pnl is None:
        return None
    cost_basis = float(mkt_value) - float(unreal_pnl)
    if cost_basis <= 0:
        return None
    return round(float(unreal_pnl) / cost_basis * 100, 1)


def _basket_row(cur, name):
    s = qb_summary(name)
    mkt_value, unreal = s.get("market_value"), s.get("unrealised_pnl")
    return {
        "basket": name, "label": _basket_label(name),
        "names": s.get("open_positions", 0), "value": mkt_value,
        "return_pct": _return_since_inception_pct(mkt_value, unreal),
    }


def _sector_mix(cur, basket):
    cur.execute("""
        SELECT p.symbol, p.current_value, g.segment
        FROM quant_paper_positions p LEFT JOIN gvm_cache g ON g.symbol = p.symbol
        WHERE p.basket_name = %s AND p.status = 'open'
    """, (basket,))
    rows = cur.fetchall()
    total = sum(float(r[1] or 0) for r in rows)
    if total <= 0:
        return []
    by_group = {}
    for sym, val, seg in rows:
        grp = _sector_parent(seg, sym)
        by_group[grp] = by_group.get(grp, 0.0) + float(val or 0)
    mix = sorted(
        [{"group": g, "value": round(v, 2), "pct": round(v / total * 100, 1)} for g, v in by_group.items()],
        key=lambda x: -x["value"])
    return mix


def _rebalance_history(cur, basket):
    cur.execute("""
        SELECT entry_date, COUNT(*) AS n, COUNT(*) FILTER (WHERE status = 'open') AS n_open
        FROM quant_paper_positions WHERE basket_name = %s AND entry_date IS NOT NULL
        GROUP BY entry_date ORDER BY entry_date DESC LIMIT %s
    """, (basket, LIST_ROW_CAP))
    rows = cur.fetchall()
    if not rows:
        return [], None
    earliest = min(r[0] for r in rows)
    out = []
    for d, n, n_open in rows:
        label = "Inception" if d == earliest else "Rebalance"
        out.append({"date": str(d), "n_entered": n, "n_still_open": n_open, "label": label})
    return out, str(earliest)


def _method_text(notes, basket):
    """Plain-words rewrite of quant_basket_registry.notes — item P5's own founder-verified
    example (small_cap) is the calibration; the other six follow the SAME transformation
    discipline (short sentences, intent not thresholds, no jargon) but are NOT individually
    founder-approved word-for-word — flagged here, not silently presented as verified."""
    PLAIN = {
        "small_cap": "Small, fast-growing companies picked top down. We first find themes we "
                     "think have a long runway, then take only the strongest scoring company in each.",
        "mid_cap": "Mid-sized companies, ranked 101 to 250 by size on the market, screened for "
                   "strong quality and momentum.",
        "large_cap": "The 100 biggest companies on the market, screened for quality, value and "
                     "steady momentum. We hold the strongest names.",
        "alpha_multicap": "Companies from across the market picked for a mix of quality and "
                          "momentum, roughly half and half. We hold the top names, equally "
                          "weighted, and drop any that fall well outside the leading ranks.",
        "breakout_52w": "Companies breaking out to new one-year highs, with strong quality and "
                        "momentum behind the move. We hold the top names by one-year return.",
        "contra_value": "Good, cheap companies that have been beaten down and look to be turning "
                        "around — high quality and value, momentum still catching up. We hold the "
                        "top names by value score.",
        "model_portfolio": "Names Arpit picked by hand from the quant shortlist, equally weighted. "
                           "Rebalanced only when he decides to, not on a fixed schedule.",
    }
    return PLAIN.get(basket) or (notes or "No selection rule on record for this basket.")


@router.get("/api/mobile/qb_app")
@_json_safe
def mobile_qb_app(request: Request, basket: str = DEFAULT_BASKET):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        visible = _visible_baskets(cur)
        if basket not in visible:
            basket = DEFAULT_BASKET if DEFAULT_BASKET in visible else (visible[0] if visible else None)
        basket_rows = [_basket_row(cur, b) for b in visible]
        names_held = sum(r["names"] or 0 for r in basket_rows)
        total_value = sum(float(r["value"] or 0) for r in basket_rows)
        total_unreal = 0.0
        for b in visible:
            s = qb_summary(b)
            total_unreal += float(s.get("unrealised_pnl") or 0)

        if basket is None:
            return {"error": "no visible baskets"}

        registry = qb_registry(basket) or {}
        positions = qb_positions(basket, "open")
        if isinstance(positions, dict):
            positions = []
        holdings = sorted(positions, key=lambda p: -(float(p.get("current_value") or 0)))
        laggards = sorted([p for p in positions if (p.get("pnl_pct") or 0) < 0],
                           key=lambda p: (p.get("pnl_pct") or 0))
        sector_mix = _sector_mix(cur, basket)
        rebal_rows, inception_date = _rebalance_history(cur, basket)

        cur.execute("SELECT MAX(updated_at) FROM quant_paper_positions WHERE basket_name = ANY(%s)", (visible,))
        as_of = cur.fetchone()[0]

    summary = qb_summary(basket)
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "visible_baskets_count": len(visible),
        "selected": basket, "selected_label": _basket_label(basket),
        "key_metrics": {
            "total_value": round(total_value, 2), "total_unrealised": round(total_unreal, 2),
            "baskets": len(visible), "names_held": names_held,
        },
        "baskets": {"rows": basket_rows, "count": len(basket_rows)},
        "holdings": {
            "rows": [{"symbol": p["symbol"], "value": p.get("current_value"), "pnl_pct": p.get("pnl_pct")}
                     for p in holdings[:LIST_ROW_CAP]], "count": len(holdings),
        },
        "laggards": {
            "rows": [{"symbol": p["symbol"], "value": p.get("current_value"), "pnl_pct": p.get("pnl_pct")}
                     for p in laggards[:LIST_ROW_CAP]], "count": len(laggards),
        },
        "sector_mix": sector_mix,
        "theme": {"tagged": False,
                  "note": "The method is thematic, but no theme tag was ever written to any "
                          "holding. quant_paper_positions has no theme column and the registry "
                          "theme fields are empty. Filling this would mean inventing it."},
        "method": {
            "text": _method_text(registry.get("notes"), basket),
            "cadence": (registry.get("rebalance_freq") or "").title() or None,
            "holdings_count": summary.get("open_positions", 0),
            "next_rebalance": str(registry["next_rebalance"]) if registry.get("next_rebalance") else None,
        },
        "rebalance_history": {
            "rows": rebal_rows, "count": len(rebal_rows), "inception_date": inception_date,
            "note": ("Real entry-date grouping from quant_paper_positions — n_entered is every "
                     "name that entered that day (open or since exited), n_still_open is how many "
                     "remain today."),
        },
        "row_cap": LIST_ROW_CAP,
    }
