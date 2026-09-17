"""
trade_alerts_app.py — /m/myalerts, the ONE My Alerts page (cc#2195, founder 17-Sep-2026 15:13 IST), and its
endpoint GET /api/mobile/myalerts.

HISTORY. cc#1896 built this file as the Dashboard "My Alerts" section (three rails + a key-metrics card,
design_refs/scorr_app_myalerts_R3.html); Fable's 10-Sep rewrite kept the payload and showed custom alerts
only. cc#2195 replaces the page and the payload: the Home bell popover (SET ALERT / VIEW ALERTS / CUSTOM
ALERT) and the counter-card page are gone, and both entry points — the bell on /m/home and the My Alerts
grid tile — open this page. Three sections: TRIGGERED (every fired alert, newest first), PENDING (set and
not yet fired, edit / delete), CREATE (the cc#2095 multi-condition builder, full-screen).

WHAT COUNTS AS "MY ALERT" — the bell's own isolation rule (scorr_bell.js, founder correction): only
alerts the founder set himself. That is (a) every custom_alerts row (cc#2095) and (b) trade_alerts rows
with source_engine IS NULL — the one verified discriminator (kind does not distinguish manual from engine;
an approve_signal row and a manual alert both use kind='entry'). Engine-raised rows (V8 approvals) and QB
rebalance notices (source_engine 'qb') are NOT alerts the user set and stay off this page, exactly as the
bell already excluded them.

NOTHING RE-QUERIED. trade_alerts rows come from trade_alerts_endpoints.list_alerts() (same live cmp
attachment, same seen flags, same schema-ensure); custom alerts from custom_alerts.list_custom_alerts()
and their metric labels from custom_alerts.get_registry() — the same functions the web and the bell read.
The shaping below (shape / cond_text / price_text / ist) is pure and unit-tested.

TRIGGERED sort key = triggered_at (the bell's own reasoning, cc#2030): a dismissal carries no timestamp
of its own, so triggered_at is the one honest key across every outcome; a manual price alert approved
straight from pending never triggered and is not "fired".
"""
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mobile_endpoints import _guard, _json_safe, _page

log = logging.getLogger("scorr.mobile.myalerts")
router = APIRouter()

IST = timezone(timedelta(hours=5, minutes=30))
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MAX_CONDITIONS = 5   # the builder's cap (spec item 3: up to 5 condition rows)


def _dt(ts):
    """A DB timestamp (datetime or its str()) → aware datetime, or None. Naive values are UTC (the
    tables are timestamptz; str() carries +00:00)."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, datetime):
        d = ts
    else:
        s = str(ts).replace("T", " ")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def ist(ts):
    """("15 Sep · 19:30", "2026-09-15", iso) for the page; (None, None, None) when absent."""
    d = _dt(ts)
    if d is None:
        return None, None, None
    i = d.astimezone(IST)
    return f"{i.day} {MON[i.month - 1]} · {i:%H:%M}", i.strftime("%Y-%m-%d"), i.isoformat()


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return int(x) if x == int(x) and abs(x) < 1e12 else round(x, 2)


def _fmt(v):
    n = _num(v)
    if n is None:
        return "—"
    return f"{n:,}" if isinstance(n, int) else f"{n:,.2f}".rstrip("0").rstrip(".")


def cond_text(c, registry):
    """'Daily RSI below 50' — the same phrasing custom_alerts.triggered_for_bell() prints in the bell."""
    r = (registry or {}).get(c.get("metric_key")) or {}
    label = r.get("label") or c.get("metric_key") or "metric"
    unit = "%" if r.get("unit") == "%" else ""
    return f"{label} {c.get('operator') or ''} {_fmt(c.get('threshold'))}{unit}".strip()


def price_text(a, fired=False):
    """'crosses ≥ ₹1,500' / 'crossed ≥ ₹1,500' — trade_alerts_web.html's own ≥/≤ phrasing."""
    cond = "≥" if str(a.get("trigger_condition") or "").upper() == "ABOVE" else "≤"
    return f"{'crossed' if fired else 'crosses'} {cond} ₹{_fmt(a.get('trigger_price'))}"


def _outcome(a):
    if a.get("status") == "approved":
        px = a.get("approved_price")
        when = ist(a.get("approved_at"))[0]
        return "approved" + (f" @ ₹{_fmt(px)}" if px is not None else "") + (f" · {when}" if when else "")
    if a.get("status") == "dismissed":
        return "dismissed"
    return "awaiting a decision"


def shape(trade_rows, custom_rows, registry, today_ist=None):
    """The page payload from the three sources. Pure."""
    today = today_ist or datetime.now(IST).strftime("%Y-%m-%d")
    triggered, pending = [], []
    for a in custom_rows or []:
        conds = []
        for c in a.get("conditions") or []:
            conds.append({"text": cond_text(c, registry), "join": c.get("join_operator") if c.get("position", 1) > 1 else None,
                          "metric_key": c.get("metric_key"), "operator": c.get("operator"), "threshold": _num(c.get("threshold")),
                          "last_value": _num(c.get("last_value")), "last_seen": ist(c.get("last_evaluated_at"))[0]})
        text = " ".join(((cc["join"] + " ") if cc["join"] else "") + cc["text"] for cc in conds)
        base = {"type": "custom", "id": a.get("id"), "key": f"c{a.get('id')}", "symbol": a.get("symbol"), "label": a.get("label"),
                "text": text, "conditions": conds, "status": a.get("status")}
        set_ist, set_day, set_iso = ist(a.get("created_at"))
        base.update({"set_ist": set_ist, "set_at": set_iso})
        if a.get("status") == "triggered" and a.get("triggered_at"):
            f_ist, f_day, f_iso = ist(a.get("triggered_at"))
            triggered.append({**base, "fired_ist": f_ist, "day": f_day, "fired_at": f_iso, "today": f_day == today,
                              "outcome": None, "seen": None})
        elif a.get("status") == "active":
            pending.append(base)
    for a in trade_rows or []:
        if a.get("source_engine") or a.get("alert_type") == "custom" or a.get("kind") not in (None, "entry", "exit"):
            continue   # engine rows, QB notices and the bell's own merged custom rows are not this list's
        d = str(a.get("direction") or "").upper()
        base = {"type": "price", "id": a.get("id"), "key": f"p{a.get('id')}", "symbol": a.get("symbol"), "direction": d,
                "trigger_price": _num(a.get("trigger_price")), "status": a.get("status"), "seen": bool(a.get("seen"))}
        set_ist, set_day, set_iso = ist(a.get("created_at"))
        base.update({"set_ist": set_ist, "set_at": set_iso})
        if a.get("triggered_at"):
            f_ist, f_day, f_iso = ist(a.get("triggered_at"))
            triggered.append({**base, "text": price_text(a, fired=True), "fired_ist": f_ist, "day": f_day, "fired_at": f_iso,
                              "today": f_day == today, "outcome": _outcome(a)})
        elif a.get("status") == "pending":
            cmp_ = _num(a.get("cmp"))
            pending.append({**base, "text": price_text(a), "cmp": cmp_, "cmp_live": a.get("cmp_live"),
                            "live_text": (f"live ₹{_fmt(cmp_)}" + ("" if a.get("cmp_live") else ", last close") if cmp_ is not None else "no live price right now")})
    triggered.sort(key=lambda r: r.get("fired_at") or "", reverse=True)
    pending.sort(key=lambda r: r.get("set_at") or "", reverse=True)
    return {
        "triggered": triggered, "pending": pending,
        "counts": {"triggered": len(triggered), "pending": len(pending),
                   "triggered_today": sum(1 for r in triggered if r.get("today")),
                   "unseen_price": sum(1 for r in triggered + pending if r["type"] == "price" and not r.get("seen"))},
        "today": today, "max_conditions": MAX_CONDITIONS,
    }


def _registry_map(reg):
    out = {}
    for cat in (reg or {}).get("categories") or []:
        for m in cat.get("metrics") or []:
            out[m.get("metric_key")] = {"label": m.get("label"), "unit": m.get("unit"), "cadence": m.get("cadence"),
                                        "category": cat.get("category")}
    return out


@router.get("/api/mobile/myalerts")
@_json_safe
def mobile_myalerts(request: Request):
    g = _guard(request)
    if g:
        return g
    from trade_alerts_endpoints import list_alerts
    import custom_alerts

    listing = list_alerts(status="all", limit=500)
    trade_rows = (listing.get("alerts") or []) if isinstance(listing, dict) else []
    customs = custom_alerts.list_custom_alerts(status="active,triggered", limit=500)
    custom_rows = (customs.get("alerts") or []) if isinstance(customs, dict) else []
    registry = _registry_map(custom_alerts.get_registry())
    out = shape(trade_rows, custom_rows, registry)
    out["registry"] = registry
    out["sources"] = {"price": "/api/alerts/list (trade_alerts, source_engine IS NULL)", "custom": "/api/custom_alerts/list",
                      "delete_custom": "/api/custom_alerts/delete", "dismiss_price": "/api/alerts/dismiss", "seen": "/api/alerts/seen"}
    return out


@router.get("/m/myalerts", response_class=HTMLResponse)
def m_myalerts():
    """cc#2195: the one My Alerts page — the Home bell and the My Alerts grid tile both open it."""
    return _page("myalerts")
