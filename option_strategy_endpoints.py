"""cc#2037 OPT sprint 2/5 — /api/options/* endpoints, wrapping cc#2036's option_strategy_engine.py
+ option_strategy_templates table. Own APIRouter, ONE include_router line in main.py (wiring only).

Also serves GET /options and GET /m/options with a one-line placeholder page so the router is
exercised end-to-end before cc#2038/cc#2039 build the real templates — do_not_touch on this card
explicitly reserves the real page files, NAV wiring (_PWA_INJECT_PATHS/PROTECTED/NAV_REGISTRY) and
main.py beyond the router import + include_router line for those two cards.

Reuses, does not invent (per this card's own instruction):
  - deriv_metrics._INDEX_OPT_ROOT / _INDEX_OPT_SPOT — the SAME NIFTY/BANKNIFTY -> option_chain
    underlying / cmp_prices symbol maps strike_chain() (cc#1576) already established. option_chain
    itself stores underlying='NIFTY'/'BANKNIFTY' (confirmed against the live table); the live spot
    for 'NIFTY' actually lives under cmp_prices.symbol='NIFTY50' (cmp_prices.symbol='NIFTY' is a
    DIFFERENT, long-stale key -- last updated 10-Jul vs NIFTY50's 11-Sep -- confirmed by direct
    query before writing this, not assumed; _INDEX_OPT_SPOT already encodes the right one).
  - option_strategy_engine.Leg / price_strategy() (cc#2036) for all payoff math -- this file does
    zero pricing of its own, only request/response shaping and DB reads.
  - The same naive-IST "now" construction gvm_market_endpoints._ist_now() already uses
    (utcnow()+5:30) for the chain staleness check -- comparing a tz-aware now against
    option_chain.ts (naive IST) would silently reintroduce the "phantom 330 minutes" class of bug
    this codebase has already hit and documented (mobile_home2.py).

NOT touched: option_chain's writer, worker/**, pcr_intraday, option_iv_daily, and main.py beyond
the one router import + include_router line (per this card's own do_not_touch).
NOT built here: Black-Scholes/Greeks (cc#2034 already owns that, a different surface) or the real
page templates (2038 web, 2039 app).
"""
import os
from datetime import date, datetime, timedelta
from typing import List, Optional

import psycopg
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from deriv_metrics import _INDEX_OPT_ROOT, _INDEX_OPT_SPOT
from option_strategy_engine import Leg, price_strategy

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL", "")

# session_log 45180: "step = strike interval (NIFTY 50, BANKNIFTY 100)" -- fixed, not derived from
# any table (no column in futures_universe or option_chain carries this).
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
VALID_KINDS = ("CE", "PE", "FUT")
VALID_SIDES = ("BUY", "SELL")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ist_now() -> datetime:
    # same construction as gvm_market_endpoints._ist_now() -- see module docstring.
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _root(underlying: Optional[str]) -> str:
    u = (underlying or "").strip().upper()
    r = _INDEX_OPT_ROOT.get(u)
    if not r:
        raise HTTPException(400, f"unsupported underlying {underlying!r} -- NIFTY or BANKNIFTY only")
    return r


def _parse_expiry(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise HTTPException(400, "expiry must be YYYY-MM-DD")


def _near_expiry(cur, root: str):
    cur.execute("SELECT MIN(expiry) FROM option_chain WHERE underlying=%s AND expiry >= CURRENT_DATE", (root,))
    r = cur.fetchone()
    return r[0] if r else None


def _latest_tick(cur, root: str, expiry):
    cur.execute("SELECT MAX(ts) FROM option_chain WHERE underlying=%s AND expiry=%s", (root, expiry))
    r = cur.fetchone()
    return r[0] if r else None


def _spot(cur, root: str):
    """(value, as_of) from cmp_prices via _INDEX_OPT_SPOT, or (None, None) -- deliberately no
    raw_prices fallback (unlike strike_chain()): session_log 45192 states this endpoint's spot is
    "live index value ... if unavailable, {value:null, as_of:null} ... never a derived number",
    a stricter rule than strike_chain()'s own dual-fallback need for pricing continuity."""
    spot_sym = _INDEX_OPT_SPOT.get(root)
    if not spot_sym:
        return None, None
    cur.execute("SELECT cmp, updated_at FROM cmp_prices WHERE symbol=%s", (spot_sym,))
    r = cur.fetchone()
    if not r or r[0] is None:
        return None, None
    return float(r[0]), r[1]


def _compute_atm(cur, root: str, expiry):
    """atm = the strike minimizing |ce.ltp - pe.ltp| at the latest tick of `expiry` -- put-call
    parity's own convergence point (session_log 45192's own definition), NOT "nearest to spot" (a
    different, coarser notion strike_chain() uses for a different purpose -- not reused here on
    purpose). A strike needs BOTH legs priced to compare; one missing a side is skipped, never
    treated as a zero gap. Returns (strike, tick_ts) or (None, None)."""
    ts = _latest_tick(cur, root, expiry)
    if not ts:
        return None, None
    cur.execute("""
        SELECT strike, option_type, ltp FROM option_chain
        WHERE underlying=%s AND expiry=%s AND ts=%s AND ltp IS NOT NULL
    """, (root, expiry, ts))
    ce, pe = {}, {}
    for strike, ot, ltp in cur.fetchall():
        k = float(strike)
        if ot == "CE":
            ce[k] = float(ltp)
        elif ot == "PE":
            pe[k] = float(ltp)
    best_strike, best_gap = None, None
    for k in sorted(set(ce) & set(pe)):
        gap = abs(ce[k] - pe[k])
        if best_gap is None or gap < best_gap:
            best_strike, best_gap = k, gap
    return best_strike, (ts if best_strike is not None else None)


def _is_stale(ts) -> bool:
    """option_chain.ts is naive IST, never tz-aware -- compared here against a naive-IST "now"
    built the SAME way (never a raw utcnow()), or this would silently reintroduce the phantom
    330-minute offset class of bug. Staleness is only meaningful DURING the session; a quiet chain
    outside 09:15-15:30 IST is expected, not stale."""
    now = _ist_now()
    session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    session_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if not (session_start <= now <= session_end):
        return False
    return (now - ts) > timedelta(minutes=20)


# ─────────────────────────── GET /api/options/meta ───────────────────────────
@router.get("/api/options/meta")
def options_meta(underlying: str = Query("NIFTY")):
    root = _root(underlying)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT lot_size FROM futures_universe WHERE symbol=%s AND is_active=true", (root,))
        r = cur.fetchone()
        lot_size = int(r[0]) if r and r[0] is not None else None
        cur.execute("""SELECT DISTINCT expiry FROM option_chain
                       WHERE underlying=%s AND expiry >= CURRENT_DATE ORDER BY expiry""", (root,))
        expiries = [row[0].isoformat() for row in cur.fetchall()]
        spot_val, spot_ts = _spot(cur, root)
        near_exp = _near_expiry(cur, root)
        atm_strike, atm_ts = (_compute_atm(cur, root, near_exp) if near_exp else (None, None))
    return {
        "underlying": root,
        "lot_size": lot_size,
        "strike_step": STRIKE_STEP[root],
        "expiries": expiries,
        "spot": {
            "value": spot_val,
            "as_of": spot_ts.isoformat() if spot_ts else None,
            "source": "cmp_prices" if spot_val is not None else None,
        },
        "atm": {"strike": atm_strike, "as_of": atm_ts.isoformat() if atm_ts else None},
    }


# ─────────────────────────── GET /api/options/chain ───────────────────────────
@router.get("/api/options/chain")
def options_chain(underlying: str = Query("NIFTY"), expiry: Optional[str] = Query(None)):
    root = _root(underlying)
    exp = _parse_expiry(expiry) if expiry else None
    with _conn() as conn, conn.cursor() as cur:
        if not exp:
            exp = _near_expiry(cur, root)
        if not exp:
            return {"as_of": None, "stale": False, "rows": []}
        ts = _latest_tick(cur, root, exp)
        if not ts:
            return {"as_of": None, "stale": False, "rows": []}
        cur.execute("""SELECT strike, option_type, ltp, oi FROM option_chain
                       WHERE underlying=%s AND expiry=%s AND ts=%s""", (root, exp, ts))
        by_strike = {}
        for strike, ot, ltp, oi in cur.fetchall():
            k = float(strike)
            row = by_strike.setdefault(k, {"strike": k, "ce_ltp": None, "pe_ltp": None, "ce_oi": None, "pe_oi": None})
            if ot == "CE":
                row["ce_ltp"] = float(ltp) if ltp is not None else None
                row["ce_oi"] = int(oi) if oi is not None else None
            elif ot == "PE":
                row["pe_ltp"] = float(ltp) if ltp is not None else None
                row["pe_oi"] = int(oi) if oi is not None else None
        rows = [by_strike[k] for k in sorted(by_strike)]
    return {"as_of": ts.isoformat(), "stale": _is_stale(ts), "rows": rows}


# ─────────────────────────── GET /api/options/templates ───────────────────────────
@router.get("/api/options/templates")
def options_templates(view: Optional[str] = Query(None)):
    v = None
    if view:
        v = view.strip().lower()
        if v not in ("bullish", "bearish", "neutral"):
            raise HTTPException(400, "view must be bullish, bearish or neutral")
    with _conn() as conn, conn.cursor() as cur:
        if v:
            cur.execute("""SELECT id, name, view, sub_view, description, risk_profile, legs, sketch
                           FROM option_strategy_templates WHERE is_active AND view=%s
                           ORDER BY view, sort_order""", (v,))
        else:
            cur.execute("""SELECT id, name, view, sub_view, description, risk_profile, legs, sketch
                           FROM option_strategy_templates WHERE is_active
                           ORDER BY view, sort_order""")
        rows = cur.fetchall()
    return [
        {"id": id_, "name": name, "view": vw, "sub_view": sub_view, "description": description,
         "risk_profile": risk_profile, "legs": legs, "sketch": sketch}
        for id_, name, vw, sub_view, description, risk_profile, legs, sketch in rows
    ]


# ─────────────────────────── POST /api/options/resolve ───────────────────────────
class ResolveRequest(BaseModel):
    template_id: int
    underlying: str
    expiry: str


@router.post("/api/options/resolve")
def options_resolve(req: ResolveRequest):
    root = _root(req.underlying)
    exp = _parse_expiry(req.expiry)
    step = STRIKE_STEP[root]
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT legs FROM option_strategy_templates WHERE id=%s AND is_active", (req.template_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, f"template {req.template_id} not found or inactive")
        tmpl_legs = r[0]  # jsonb -> already a python list of {kind,side,offset_steps,qty}

        atm_strike, atm_ts = _compute_atm(cur, root, exp)
        spot_val, _spot_ts = _spot(cur, root)

        ts = _latest_tick(cur, root, exp)
        prem_map = {}
        if ts:
            cur.execute("""SELECT strike, option_type, ltp FROM option_chain
                           WHERE underlying=%s AND expiry=%s AND ts=%s""", (root, exp, ts))
            for strike, ot, ltp in cur.fetchall():
                prem_map[(float(strike), ot)] = float(ltp) if ltp is not None else None

    legs_out = []
    for tl in tmpl_legs:
        kind, side, qty = tl["kind"], tl["side"], tl["qty"]
        if kind == "FUT":
            # session_log 45192: "FUT leg: strike field becomes entry price, premium disabled and
            # 0." No separately-tracked live futures price exists here (cmp_prices.symbol='NIFTY'
            # is the stale, unrelated key the module docstring warns about) -- the live index spot
            # is a real, already-fetched, standard small-basis proxy for a near-dated index future's
            # entry price, stated here rather than silently assumed.
            legs_out.append({"kind": "FUT", "side": side, "strike": spot_val, "premium": 0,
                              "qty": qty, "premium_as_of": None})
            continue
        strike = (atm_strike + tl["offset_steps"] * step) if atm_strike is not None else None
        prem = prem_map.get((strike, kind)) if strike is not None else None
        legs_out.append({
            "kind": kind, "side": side, "strike": strike, "premium": prem, "qty": qty,
            "premium_as_of": ts.isoformat() if (ts and prem is not None) else None,
        })
    return {"legs": legs_out}


# ─────────────────────────── POST /api/options/payoff ───────────────────────────
class PayoffLeg(BaseModel):
    kind: str
    side: str
    strike: Optional[float] = None
    premium: Optional[float] = None
    qty: int = Field(ge=1, le=50)

    @field_validator("kind")
    @classmethod
    def _kind_ok(cls, v):
        v = (v or "").strip().upper()
        if v not in VALID_KINDS:
            raise ValueError(f"kind must be one of {VALID_KINDS}")
        return v

    @field_validator("side")
    @classmethod
    def _side_ok(cls, v):
        v = (v or "").strip().upper()
        if v not in VALID_SIDES:
            raise ValueError(f"side must be one of {VALID_SIDES}")
        return v


class PayoffRequest(BaseModel):
    underlying: str
    lot_size: int = Field(gt=0)
    spot: float = Field(gt=0)
    legs: List[PayoffLeg] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _fill_and_validate_legs(self):
        # cc#2037 item 6: "strike required unless FUT" -- for CE/PE a real listed strike is
        # mandatory (the engine cannot price intrinsic value without one); for FUT the field means
        # entry price (session_log 45192) and, if the client omits it, defaults to THIS request's
        # own `spot` (already in the same payload -- no new fetch, no invention) rather than 422ing
        # a leg whose price is a reasonable, stated approximation. premium is likewise required for
        # CE/PE (a leg cannot be priced without it -- never silently defaulted to 0, which would
        # misstate a real max_profit/max_loss) and forced to 0 for FUT regardless of what is sent.
        for leg in self.legs:
            if leg.kind == "FUT":
                if leg.strike is None:
                    leg.strike = self.spot
                leg.premium = 0.0
            else:
                if leg.strike is None:
                    raise ValueError(f"{leg.kind} leg requires strike")
                if leg.premium is None:
                    raise ValueError(f"{leg.kind} leg requires premium")
        return self


@router.post("/api/options/payoff")
def options_payoff(req: PayoffRequest):
    engine_legs = [Leg(l.kind, l.side, l.strike, l.premium, l.qty) for l in req.legs]
    return price_strategy(req.spot, engine_legs, req.lot_size)


# ─────────────────────────── pages ───────────────────────────
# cc#2038: /options now serves the real page (main.py's own _HTML_CACHE/_page() pattern, read once
# at first request and cached -- a fresh deploy is a fresh process, so it reloads naturally).
_HTML_CACHE: dict = {}


def _page(filename: str) -> str:
    html = _HTML_CACHE.get(filename)
    if html is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        _HTML_CACHE[filename] = html
    return html


@router.get("/options", response_class=HTMLResponse)
def options_web_page():
    return _page("scorr_options.html")


# cc#2039 still owns /m/options's real page (mobile/options.html via the TEMPLATE_DIR convention) --
# this placeholder is unchanged from cc#2037 until that card lands.
_APP_PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8">
<title>Option Strategy Builder</title></head><body style="font-family:sans-serif;padding:40px;color:#333">
<h1>Option Strategy Builder</h1><p>The real app page ships in cc#2039. This router (cc#2037) is live:
/api/options/meta, /api/options/chain, /api/options/templates, /api/options/resolve,
/api/options/payoff.</p></body></html>"""


@router.get("/m/options", response_class=HTMLResponse)
def options_app_placeholder():
    return _APP_PLACEHOLDER
