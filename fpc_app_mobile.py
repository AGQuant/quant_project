"""
fpc_app_mobile.py — Simple Financial Planning Calculator, the maths. Fable single-mode build
10-Sep-2026 from Finkhoz_Simple_FPC_Concept_Note_v0_1 (07-Sep-2026). The web /fpc page carries its
maths inside the HTML; this module is the first server-side copy so the app and, later, the web read
one formula set.

  POST /api/mobile/fpc/calc  {inputs, risk}  → headline, fitness score, corpus, gap, extra SIP with
                                                the affordability check and two costed alternatives,
                                                risk profile (lower of situation/comfort bands), basket
                                                recommendation from the live registry

Assumptions are fixed per the note (section 3) and returned with every result so the page can show
them behind one tap. Nothing is asked twice: three risk answers are derived from the inputs.
"""
import logging
import os
from typing import Optional, Dict, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
import psycopg

from mobile_endpoints import _guard, _json_safe

log = logging.getLogger("scorr.mobile.fpc")
router = APIRouter()

ASSUMPTIONS = {
    "inflation": 0.06, "return_post": 0.07, "sip_stepup": 0.10, "plan_to_age": 85,
    "emergency_months": 6, "cover_multiple": 10, "cover_multiple_no_dependants": 5,
    "returns": {"Conservative": 0.09, "Balanced": 0.11, "Growth": 0.13},
    "notes": ["Inflation 6% a year on spending and goal cost.", "Return after retirement 7% a year, fixed.",
              "SIP steps up 10% a year — every SIP figure here uses the same convention.",
              "Plan checked to age 85.", "Emergency fund target = 6 × (spending + EMIs).",
              "Life cover target = 10 × annual take-home (5× with no dependants). A rule of thumb.",
              "EMIs are assumed to end by retirement. Tax is not modelled."],
}

# basket recommendation — from the note's section 6; founder still to confirm the risk labels.
BASKETS = {
    "Conservative": ["finz_etf", "finz_stable", "finz_dividend"],
    "Balanced": ["finz_stable", "finz_wcb", "finz_dividend"],
    "Growth": ["finz_wcb", "finz_helios", "finz_defence"],
}
BASKET_LABEL = {"finz_etf": "FINZ ETF", "finz_stable": "FINZ Stable", "finz_dividend": "FINZ Dividend",
                "finz_wcb": "FINZ Wealth Compounder", "finz_helios": "FINZ Helios", "finz_defence": "FINZ Defence"}


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


class FPCInputs(BaseModel):
    age: int = 35
    retire_at: int = 60
    dependants: int = 1
    take_home: float = 100000
    spending: float = 50000
    emis: float = 0
    cash: float = 500000
    investments: float = 1000000
    life_cover: float = 0
    sip: float = 20000
    goal: Optional[str] = None          # education | marriage | home | None
    goal_cost: Optional[float] = None
    goal_years: Optional[int] = None


class FPCRisk(BaseModel):
    """Seven tapped answers, scored 1–4 each. A2, A3, A5 are derived from the inputs."""
    a1: int = 3   # when will you need this money
    a4: int = 3   # income steadiness
    b1: int = 2
    b2: int = 2
    b3: int = 2
    b4: int = 2
    b5: int = 2


class FPCReq(BaseModel):
    inputs: FPCInputs
    risk: FPCRisk = FPCRisk()


def _band(score):
    return "Low" if score <= 9 else ("Medium" if score <= 15 else "High")


_BAND_PROFILE = {"Low": "Conservative", "Medium": "Balanced", "High": "Growth"}
_RANK = {"Conservative": 0, "Balanced": 1, "Growth": 2}


def _corpus_required(spend_at_ret_monthly, years_in_ret, infl, r_post):
    """PV at retirement of a monthly spend rising with inflation, earning r_post, for years_in_ret."""
    total = 0.0
    for y in range(years_in_ret):
        annual = spend_at_ret_monthly * 12 * ((1 + infl) ** y)
        total += annual / ((1 + r_post) ** y)
    return total


def _fv_sip(monthly, years, r, stepup):
    """Future value of a monthly SIP stepping up yearly, annual compounding on each year's contribution."""
    fv = 0.0
    for y in range(years):
        fv += monthly * ((1 + stepup) ** y) * 12 * ((1 + r) ** (years - y - 1))
    return fv


def _sip_for_target(target, years, r, stepup):
    if target <= 0 or years <= 0:
        return 0.0
    unit = _fv_sip(1.0, years, r, stepup)
    return target / unit if unit > 0 else 0.0


def _corpus_lasts_to(corpus, spend_at_ret_monthly, retire_at, infl, r_post, cap=110):
    """Age at which the corpus runs out funding inflation-rising spend at r_post."""
    bal = corpus
    age = retire_at
    while age < cap:
        bal = bal * (1 + r_post) - spend_at_ret_monthly * 12 * ((1 + infl) ** (age - retire_at))
        if bal < 0:
            return age
        age += 1
    return cap


def compute(i: FPCInputs, rk: FPCRisk) -> Dict[str, Any]:
    A = ASSUMPTIONS
    infl, r_post, step, to_age = A["inflation"], A["return_post"], A["sip_stepup"], A["plan_to_age"]
    yrs = max(1, i.retire_at - i.age)
    yrs_ret = max(1, to_age - i.retire_at)
    # ── derived risk answers (A2 months of runway, A3 EMI share, A5 dependants) ──
    burn = i.spending + i.emis
    months = (i.cash / burn) if burn > 0 else 12
    a2 = 1 if months <= 0 else (2 if months < 3 else (3 if months <= 6 else 4))
    emi_share = (i.emis / i.take_home) if i.take_home > 0 else 0
    a3 = 1 if emi_share > 0.5 else (2 if emi_share >= 0.3 else (3 if emi_share >= 0.1 else 4))
    a5 = 1 if i.dependants >= 3 else (2 if i.dependants == 2 else (3 if i.dependants == 1 else 4))
    part_a = rk.a1 + a2 + a3 + rk.a4 + a5
    part_b = rk.b1 + rk.b2 + rk.b3 + rk.b4 + rk.b5
    band_a, band_b = _band(part_a), _band(part_b)
    prof_a, prof_b = _BAND_PROFILE[band_a], _BAND_PROFILE[band_b]
    profile = prof_a if _RANK[prof_a] <= _RANK[prof_b] else prof_b
    held_back = _RANK[prof_b] > _RANK[prof_a]
    r_pre = A["returns"][profile]
    # ── fitness (4 pillars, retirement kept out) ──
    emg_target = A["emergency_months"] * burn
    p_emg = min(1.0, i.cash / emg_target) if emg_target > 0 else 1.0
    p_debt = max(0.0, 1.0 - emi_share)
    spare = i.take_home - i.spending - i.emis
    save_rate = (spare / i.take_home) if i.take_home > 0 else 0
    p_save = min(1.0, max(0.0, save_rate / 0.20))
    cover_target = i.take_home * 12 * (A["cover_multiple"] if i.dependants > 0 else A["cover_multiple_no_dependants"])
    p_prot = min(1.0, i.life_cover / cover_target) if cover_target > 0 else 1.0
    fitness = round((p_emg + p_debt + p_save + p_prot) * 25)
    # ── retirement ──
    spend_at_ret = i.spending * ((1 + infl) ** yrs)
    required = _corpus_required(spend_at_ret, yrs_ret, infl, r_post)
    cash_free = max(0.0, i.cash - emg_target)
    fv_assets = (i.investments + cash_free) * ((1 + r_pre) ** yrs)
    fv_sip = _fv_sip(i.sip, yrs, r_pre, step)
    projected = fv_assets + fv_sip
    gap = required - projected
    lasts_to = _corpus_lasts_to(projected, spend_at_ret, i.retire_at, infl, r_post)
    extra_sip = _sip_for_target(gap, yrs, r_pre, step) if gap > 0 else 0.0
    # ── goal (optional) ──
    goal = None
    goal_sip = 0.0
    if i.goal and i.goal_cost and i.goal_years:
        gy = max(1, int(i.goal_years))
        g_fv = i.goal_cost * ((1 + infl) ** gy)
        goal_sip = _sip_for_target(g_fv, gy, r_pre, step)
        goal = {"name": i.goal, "cost_today": i.goal_cost, "years": gy, "cost_then": round(g_fv), "sip": round(goal_sip)}
    # ── affordability ──
    spare_after = spare - i.sip - goal_sip
    affordable = extra_sip <= spare_after
    short_by = max(0.0, extra_sip - spare_after)
    alternatives = []
    if gap > 0 and not affordable:
        alt_age = None
        for ra in range(i.retire_at + 1, 71):
            y2 = ra - i.age
            yr2 = max(1, to_age - ra)
            s2 = i.spending * ((1 + infl) ** y2)
            req2 = _corpus_required(s2, yr2, infl, r_post)
            proj2 = (i.investments + cash_free) * ((1 + r_pre) ** y2) + _fv_sip(i.sip, y2, r_pre, step)
            need2 = _sip_for_target(req2 - proj2, y2, r_pre, step) if req2 > proj2 else 0
            if need2 <= max(0.0, spare_after):
                alt_age = ra
                break
        if alt_age:
            alternatives.append({"kind": "retire_later", "retire_at": alt_age,
                                 "text": f"Retire at {alt_age} instead of {i.retire_at} and the plan fits what you have spare."})
        cut = short_by / 2.0 if short_by > 0 else 0
        for _ in range(8):
            s3 = (i.spending - cut) * ((1 + infl) ** yrs)
            req3 = _corpus_required(s3, yrs_ret, infl, r_post)
            need3 = _sip_for_target(req3 - projected, yrs, r_pre, step) if req3 > projected else 0
            spare3 = spare_after + cut
            diff = need3 - spare3
            if abs(diff) < 100:
                break
            cut = max(0.0, cut + diff / 2.0)
        if cut > 0:
            alternatives.append({"kind": "spend_less", "amount": round(cut, -2),
                                 "text": f"Spend ₹{round(cut, -2):,.0f} less a month and the same plan becomes affordable."})
    # ── baskets ──
    baskets = []
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT r.basket_name, r.max_stocks, r.rebalance_freq,
                                  (SELECT COUNT(*) FROM quant_paper_positions p WHERE p.basket_name=r.basket_name AND p.status='open')
                           FROM quant_basket_registry r WHERE r.basket_name = ANY(%s) AND r.is_active""", (BASKETS[profile],))
            live = {r[0]: {"max": r[1], "freq": r[2], "open": r[3]} for r in cur.fetchall()}
    except Exception as e:
        log.warning("fpc baskets: %s", e)
        live = {}
    for b in BASKETS[profile]:
        L = live.get(b) or {}
        baskets.append({"basket": b, "label": BASKET_LABEL.get(b, b), "holdings": L.get("open"), "rebalance": L.get("freq"),
                        "live": bool(L)})
    headline = (f"Your money lasts to age {lasts_to}. You wanted {to_age}. Short by {to_age - lasts_to} years."
                if lasts_to < to_age else f"Your money lasts past {to_age}. You are on track.")
    return {
        "headline": headline, "lasts_to": lasts_to, "plan_to": to_age,
        "profile": {"name": profile, "return_pre": r_pre, "part_a": part_a, "part_b": part_b, "band_a": band_a, "band_b": band_b,
                    "held_back": held_back,
                    "why": ("Your comfort with risk is high, but your safety net is thin. Build the emergency fund first — then this moves up."
                            if held_back else "Your situation and your comfort point the same way."),
                    "derived": {"a2": a2, "a3": a3, "a5": a5}},
        "fitness": {"score": fitness, "pillars": {"emergency": round(p_emg * 100), "debt": round(p_debt * 100),
                                                    "savings": round(p_save * 100), "protection": round(p_prot * 100)},
                    "emergency_target": round(emg_target), "cover_target": round(cover_target), "save_rate_pct": round(save_rate * 100, 1)},
        "retirement": {"years_to": yrs, "years_in": yrs_ret, "spend_at_ret_monthly": round(spend_at_ret), "required": round(required),
                       "projected": round(projected), "gap": round(gap), "from_assets": round(fv_assets), "from_sip": round(fv_sip),
                       "cash_counted": round(cash_free)},
        "sip": {"extra": round(extra_sip), "spare": round(spare_after), "affordable": affordable, "short_by": round(short_by),
                "current": i.sip, "goal_sip": round(goal_sip), "alternatives": alternatives},
        "goal": goal,
        "baskets": {"profile": profile, "rows": baskets, "note": "Suggested split is equal across the baskets; the founder sets the final split."},
        "assumptions": ASSUMPTIONS["notes"],
    }


@router.post("/api/mobile/fpc/calc")
@_json_safe
def mobile_fpc_calc(request: Request, body: FPCReq):
    g = _guard(request)
    if g:
        return g
    return compute(body.inputs, body.risk)
