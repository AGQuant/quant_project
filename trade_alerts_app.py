"""
trade_alerts_app.py — cc#1896 APP BUILD: My Alerts section page backend
(design_refs/scorr_app_myalerts_R3.html, APP_CARD_LAYOUT_LAW_V1 session_log 42536).

THE WHOLE POINT OF THIS CARD: trade_alerts holds TWO different kinds of record, and the web page
(trade_alerts_web.html, cc#1885) renders both through one price-alert template — which is why it
once showed WELCORP "crossing ₹4,57,174" when the stock trades near ₹2,600 (WELCORP is a
kind='rebalance_due' row; trigger_price on that kind is a basket-level rupee figure, never a price
level — cc#1885's own finding, confirmed again here against the live table: ids 16-20 range
24,317 to 5,84,565 against stocks trading in the low thousands). cc#1885 fixed the WEB render.
THIS file makes the split structural on the APP side, one endpoint ahead of render time, so no
future app screen can make the same mistake by reaching for trigger_price on the wrong kind.

ONE endpoint, GET /api/mobile/myalerts. Reuses trade_alerts_endpoints.list_alerts() directly for
the raw rows (same live-cmp attachment, same schema-ensure) — nothing here re-queries trade_alerts
with a second, possibly-drifting SQL text.

THE SPLIT, exactly:
  - Price alerts (Futures / Equity rails): every kind IN ('entry','exit') row, ANY source_engine —
    "what price triggers currently exist", the operational view. Futures vs Equity is DERIVED
    (never a hardcoded symbol list, per item 6): a symbol with any native fyers_fut row, ever,
    is Futures; everything else is Equity — the same fut_ever_existed existence check
    scorr_endpoints.smartgain_m2m() already uses for cc#161's structurally-fut-less distinction.
  - Basket notices (Rebalance due rail): every kind='rebalance_due' row. trigger_price NEVER
    appears anywhere in this section's output (item 5) — candidate count is parsed from
    trigger_condition's own "N candidate(s)" prose (the same regex trade_alerts_web.html's
    rowHtml() already uses, cc#1885), basket label from notes via the SAME REBAL_BASKET_LABEL
    map that file defines (duplicated here, not imported — a page-local JS/py pair, same
    per-page-owns-its-own-small-helpers convention every other mobile page in this codebase
    follows; the FIVE-basket map itself is copied verbatim so the label never reads differently
    on the two surfaces).
  - Your own alerts (Custom / History rails): kind IN ('entry','exit') AND source_engine IS NULL
    ONLY — alerts the founder set himself through the Custom Alert grid tile, as distinct from
    engine-raised ones (the live TATAELXSI row is source_engine='V8' and correctly stays OUT of
    both these cards, appearing only in Price Alerts — this is what keeps the split from
    double-counting the one alert that currently exists). Custom = status='pending' (still
    waiting); History = status IN ('triggered','approved','dismissed') (already resolved) — the
    ref's own wording ("alerts that trigger or get dismissed land here") is what fixes this
    boundary. NOT FOUNDER-CONFIRMED ON THE CARD — stated here per house doctrine (a spec gap is
    implemented with a default and the default is named), consistent with every real row today.
"""

import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mobile_endpoints import _conn, _guard, _json_safe, _page

log = logging.getLogger("scorr.mobile.myalerts")
router = APIRouter()

LIST_ROW_CAP = 6

# cc#1885's own map, copied verbatim (trade_alerts_web.html REBAL_BASKET_LABEL) so the label
# cannot read differently between the web fix and this app page.
REBAL_BASKET_LABEL = {"mid_cap": "Mid cap", "large_cap": "Large cap", "contra_value": "Contra value",
                       "breakout_52w": "Breakout 52w", "alpha_multicap": "Alpha multicap"}

_CAND_RE = re.compile(r"(\d+)\s*candidate", re.IGNORECASE)


def _basket_label(notes, source_ref):
    key = notes or (source_ref or "").split(":")[0]
    return REBAL_BASKET_LABEL.get(key, str(key or "").replace("_", " ").title() or "Basket")


def _candidate_count(trigger_condition):
    m = _CAND_RE.search(trigger_condition or "")
    return int(m.group(1)) if m else None


@router.get("/api/mobile/myalerts")
@_json_safe
def mobile_myalerts(request: Request):
    g = _guard(request)
    if g:
        return g
    from trade_alerts_endpoints import list_alerts

    listing = list_alerts(status="all", limit=500)
    rows = listing.get("alerts") or []

    rebal_rows = [a for a in rows if a.get("kind") == "rebalance_due"]
    price_rows = [a for a in rows if a.get("kind") in ("entry", "exit")]

    # Futures vs Equity — DERIVED (item 6), never a hardcoded list.
    price_syms = sorted({a["symbol"] for a in price_rows if a.get("symbol")})
    fut_syms = set()
    if price_syms:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM intraday_prices WHERE source='fyers_fut' AND symbol = ANY(%s)",
                        (price_syms,))
            fut_syms = {r[0] for r in cur.fetchall()}

    futures_out, equity_out = [], []
    for a in price_rows:
        dest = futures_out if a["symbol"] in fut_syms else equity_out
        dest.append({
            "symbol": a["symbol"], "direction": a.get("direction"), "status": a.get("status"),
            "trigger_price": a.get("trigger_price"), "created_at": a.get("created_at"),
            "source_engine": a.get("source_engine"),
        })

    rebal_out = []
    for a in sorted(rebal_rows, key=lambda r: r.get("notes") or ""):
        rebal_out.append({
            "basket": _basket_label(a.get("notes"), a.get("source_ref")),
            "candidate_count": _candidate_count(a.get("trigger_condition")),
            "raised_at": a.get("created_at"),
        })

    own_rows = [a for a in price_rows if not a.get("source_engine")]
    custom_out = [a for a in own_rows if a.get("status") == "pending"]
    history_out = [a for a in own_rows if a.get("status") in ("triggered", "approved", "dismissed")]

    triggered_count = sum(1 for a in price_rows if a.get("status") == "triggered")

    return {
        "as_of_source": "/api/alerts/list",
        "key_metrics": {
            "total": len(rows), "futures": len(futures_out), "equity": len(equity_out),
            "rebalance_due": len(rebal_out), "triggered": triggered_count,
        },
        "price_alerts": {
            "futures": futures_out[:LIST_ROW_CAP], "futures_count": len(futures_out),
            "equity": equity_out[:LIST_ROW_CAP], "equity_count": len(equity_out),
        },
        "basket_notices": {"rows": rebal_out[:LIST_ROW_CAP], "count": len(rebal_out)},
        "own_alerts": {
            "custom": [{"symbol": a["symbol"], "direction": a.get("direction"),
                        "trigger_price": a.get("trigger_price"), "created_at": a.get("created_at")}
                       for a in custom_out[:LIST_ROW_CAP]], "custom_count": len(custom_out),
            "history": [{"symbol": a["symbol"], "direction": a.get("direction"), "status": a.get("status"),
                         "trigger_price": a.get("trigger_price"), "created_at": a.get("created_at")}
                        for a in history_out[:LIST_ROW_CAP]], "history_count": len(history_out),
        },
        "row_cap": LIST_ROW_CAP,
    }


@router.get("/m/myalerts", response_class=HTMLResponse)
def m_myalerts():
    """cc#1896: My Alerts section page, reached from the Home Dashboard section — a grid-tile
    destination, same discovery pattern as /m/myportfolio (cc#1895). Distinct from /m/alerts
    (trade_alerts_endpoints.m_alerts, the existing Approve/Dismiss feed), which is UNCHANGED and
    still the fuller operational surface this page does not duplicate."""
    return _page("myalerts")
