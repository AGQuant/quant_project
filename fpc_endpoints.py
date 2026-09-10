"""
fpc_endpoints.py — Simple Financial Planning Calculator backend. Fable single-mode build 10-Sep-2026,
from Finkhoz_Simple_FPC_Concept_Note_v0_1 (07-Sep-2026, founder). The web /fpc page (fpc_v11.html)
carries its maths in browser JS with no API; this file is the ONE engine from here on, and the app page
(mobile/fpc.html) renders from it. Nothing here reads a number the note does not state.

  GET  /api/fpc/config   → slider ranges, defaults, questions, assumptions — the page builds its UI from this
  POST /api/fpc/compute  → the whole result: headline, fitness, corpus, gap, extra SIP with the affordability
                           check, costed alternatives, goal SIP, risk profile (lower of situation and comfort),
                           FINZ basket suggestion with live construction facts from the registry

Assumptions (note §3): inflation 6%; return before retirement by profile 9/11/13%; after retirement 7%;
SIP step-up 10% a year; plan checked to age 85; emergency target 6 × (spending + EMIs); life cover target
10 × annual take-home (5× with no dependants); EMIs end at retirement; tax not modelled.
Risk (note §4): Part A situation, Part B comfort, each 5–20, banded 5–9 / 10–15 / 16–20; final = LOWER band.
Fitness (note §5.1): four pillars each capped at 100, equal weight; retirement kept out.
"""
import os
import logging
from typing import Optional, Dict

from fastapi import APIRouter
from pydantic import BaseModel
import psycopg

log = logging.getLogger("scorr.fpc")
router = APIRouter()

INFLATION = 0.06
RET_POST = 0.07
STEP_UP = 0.10
PLAN_TO_AGE = 85
RETURN_BY_PROFILE = {"Conservative": 0.09, "Balanced": 0.11, "Growth": 0.13}
BASKETS_BY_PROFILE = {
    "Conservative": ["finz_etf", "finz_stable", "finz_dividend"],
    "Balanced": ["finz_stable", "finz_wcb", "finz_dividend"],
    "Growth": ["finz_wcb", "finz_helios", "finz_defence"],
}
BASKET_LABEL = {"finz_etf": "FINZ ETF", "finz_stable": "FINZ Stable", "finz_dividend": "FINZ Dividend",
                "finz_wcb": "FINZ Wealth Compounder", "finz_helios": "FINZ Helios", "finz_defence": "FINZ Defence"}

CONFIG = {
    "inputs": [
        {"key": "age", "label": "Your age", "control": "slider", "min": 22, "max": 60, "step": 1, "default": 35},
        {"key": "retire_age", "label": "Retire at", "control": "slider", "min": 45, "max": 70, "step": 1, "default": 60},
        {"key": "dependants", "label": "People depending on you", "control": "tap", "options": [0, 1, 2, 3], "labels": ["0", "1", "2", "3+"], "default": 1},
        {"key": "income", "label": "Monthly take-home", "control": "money", "min": 25000, "max": 1000000, "default": 100000},
        {"key": "spending", "label": "Monthly spending", "control": "money", "min": 10000, "max": 800000, "default": 50000},
        {"key": "emi", "label": "Monthly EMIs", "control": "money", "min": 0, "max": 500000, "default": 0},
        {"key": "cash", "label": "Cash & FD", "control": "money", "min": 0, "max": 10000000, "default": 500000},
        {"key": "investments", "label": "Investments (MF, shares, EPF, NPS)", "control": "money", "min": 0, "max": 50000000, "default": 1000000},
        {"key": "life_cover", "label": "Life cover owned", "control": "money", "min": 0, "max": 50000000, "default": 0},
        {"key": "sip", "label": "Monthly SIP today", "control": "money", "min": 0, "max": 500000, "default": 20000},
        {"key": "goal", "label": "Goal (optional)", "control": "tap", "options": ["skip", "education", "marriage", "home"], "labels": ["Skip", "Education", "Marriage", "Home"], "default": "skip"},
        {"key": "goal_cost", "label": "Goal cost today", "control": "money", "min": 100000, "max": 20000000, "default": 2000000, "only_if_goal": True},
        {"key": "goal_years", "label": "Years away", "control": "slider", "min": 1, "max": 25, "step": 1, "default": 10, "only_if_goal": True},
    ],
    "money_steps": [[100000, 1000], [1000000, 10000], [None, 100000]],
    "risk": {
        "A": [  # situation — A2, A3, A5 auto-filled from inputs
            {"key": "A1", "q": "When will you need this money?", "options": ["In under 3 years", "3 to 7 years", "7 to 15 years", "After 15 years"]},
            {"key": "A2", "q": "If your income stopped, how many months could you manage from savings?", "options": ["None", "Under 3", "3 to 6", "More than 6"], "auto": True},
            {"key": "A3", "q": "How much of your pay goes to EMIs?", "options": ["More than half", "30 to 50%", "10 to 30%", "Almost nothing"], "auto": True},
            {"key": "A4", "q": "How steady is your income?", "options": ["Changes a lot", "Some months vary", "Mostly steady", "Fixed salary"]},
            {"key": "A5", "q": "How many people depend on your income?", "options": ["3 or more", "2", "1", "Nobody"], "auto": True},
        ],
        "B": [  # comfort
            {"key": "B1", "q": "Where is your money kept today?", "options": ["All in FD and savings", "Mostly FD, some funds", "Mostly funds", "Mostly shares"]},
            {"key": "B2", "q": "How long have you invested in shares or funds?", "options": ["Never", "Under 2 years", "2 to 5 years", "More than 5 years"]},
            {"key": "B3", "q": "The last time the market fell badly, what did you do?", "options": ["Was not invested", "Sold", "Did nothing", "Bought more"]},
            {"key": "B4", "q": "Your ₹1,00,000 becomes ₹85,000 in one month. You…", "options": ["Sell everything", "Sell some", "Wait it out", "Invest more"]},
            {"key": "B5", "q": "Which would you pick?", "options": ["7%, never falls", "10%, small falls", "12%, can fall 15%", "16%, can fall 40%"]},
        ],
        "bands": [[5, 9, "Low", "Conservative"], [10, 15, "Medium", "Balanced"], [16, 20, "High", "Growth"]],
    },
    "assumptions": [
        {"k": "Inflation", "v": "6% a year", "n": "on spending and the goal"},
        {"k": "Return before retirement", "v": "Conservative 9% · Balanced 11% · Growth 13%", "n": "set by your risk profile"},
        {"k": "Return after retirement", "v": "7% a year", "n": "not adjustable"},
        {"k": "SIP step-up", "v": "10% a year", "n": "same for every SIP figure shown"},
        {"k": "Plan checked to age", "v": "85", "n": "the gap is measured against this"},
        {"k": "Emergency fund target", "v": "6 × (spending + EMIs)", "n": ""},
        {"k": "Life cover target", "v": "10 × yearly take-home", "n": "5× with no dependants — a rule of thumb"},
        {"k": "EMIs in retirement", "v": "assumed ended", "n": ""},
        {"k": "Tax", "v": "not modelled", "n": ""},
    ],
}


class FPCReq(BaseModel):
    age: int = 35
    retire_age: int = 60
    dependants: int = 1
    income: float = 100000
    spending: float = 50000
    emi: float = 0
    cash: float = 500000
    investments: float = 1000000
    life_cover: float = 0
    sip: float = 20000
    goal: str = "skip"
    goal_cost: Optional[float] = None
    goal_years: Optional[int] = None
    risk: Optional[Dict[str, int]] = None   # {"A1":1..4, "A4":.., "B1".."B5"}; A2/A3/A5 auto


def _band(score):
    for lo, hi, band, prof in CONFIG["risk"]["bands"]:
        if lo <= score <= hi:
            return band, prof
    return ("Low", "Conservative") if score < 5 else ("High", "Growth")


def _fv_sip_stepup(monthly, years, r):
    """Future value at the end of `years` of a monthly SIP that steps up 10% each year. Yearly
    contribution C_y = 12·sip·1.1^y, compounded from mid-year y to the end (a plain, stated convention)."""
    fv = 0.0
    for y in range(int(years)):
        c = 12.0 * monthly * ((1 + STEP_UP) ** y)
        fv += c * ((1 + r) ** (years - y - 0.5))
    return fv


def _corpus_required(spend_today, years_to_ret, years_in_ret):
    """Amount at retirement that funds spending (grown to retirement at 6%), rising 6% a year, earning 7%,
    for `years_in_ret` years — a growing annuity, yearly. Returns (yearly spend at retirement, corpus)."""
    s_ret = spend_today * ((1 + INFLATION) ** years_to_ret) * 12.0
    q = (1 + INFLATION) / (1 + RET_POST)
    n = max(0, int(years_in_ret))
    factor = n if abs(q - 1) < 1e-9 else (1 - q ** n) / (1 - q)
    return s_ret, s_ret * factor


def _lasts_to_age(corpus, spend_ret_yearly, retire_age):
    """Draw the corpus down from retirement: spend rises 6%, balance earns 7%. Returns the age the money
    runs out, capped at 100 (meaning: it lasts)."""
    bal, spend, age = corpus, spend_ret_yearly, retire_age
    while age < 100:
        bal = bal * (1 + RET_POST) - spend
        if bal < 0:
            return age
        spend *= (1 + INFLATION)
        age += 1
    return 100


def _basket_facts(names):
    out = []
    try:
        with psycopg.connect(os.getenv("DATABASE_URL")) as conn, conn.cursor() as cur:
            cur.execute("""SELECT r.basket_name, r.rebalance_freq, r.capital,
                                  (SELECT COUNT(*) FROM quant_paper_positions p WHERE p.basket_name=r.basket_name AND p.status='OPEN') AS n
                           FROM quant_basket_registry r WHERE r.basket_name = ANY(%s)""", (names,))
            facts = {r[0]: {"rebalance": r[1], "capital": float(r[2]) if r[2] is not None else None, "holdings": int(r[3] or 0)} for r in cur.fetchall()}
    except Exception as e:
        log.warning("fpc basket facts: %s", e)
        facts = {}
    for n in names:
        f = facts.get(n, {})
        out.append({"basket": n, "label": BASKET_LABEL.get(n, n), "holdings": f.get("holdings"), "rebalance": f.get("rebalance"),
                    "capital": f.get("capital"), "in_registry": bool(f)})
    return out


@router.get("/api/fpc/config")
def fpc_config():
    return CONFIG


@router.post("/api/fpc/compute")
def fpc_compute(b: FPCReq):
    age, ret = int(b.age), int(b.retire_age)
    if ret <= age:
        ret = age + 1
    n = ret - age
    years_in_ret = max(0, PLAN_TO_AGE - ret)
    income, spend, emi = float(b.income), float(b.spending), float(b.emi)
    cash, inv, cover, sip = float(b.cash), float(b.investments), float(b.life_cover), float(b.sip)

    # ── risk profile: Part A situation (A2/A3/A5 auto), Part B comfort; final = lower band ──
    r = dict(b.risk or {})
    months = cash / (spend + emi) if (spend + emi) > 0 else 99
    r["A2"] = 1 if months <= 0 else (2 if months < 3 else (3 if months <= 6 else 4))
    emi_share = emi / income if income > 0 else 0
    r["A3"] = 1 if emi_share > 0.5 else (2 if emi_share >= 0.3 else (3 if emi_share >= 0.1 else 4))
    r["A5"] = 1 if b.dependants >= 3 else (2 if b.dependants == 2 else (3 if b.dependants == 1 else 4))

    def score(part):
        keys = [q["key"] for q in CONFIG["risk"][part]]
        return sum(max(1, min(4, int(r.get(k, 2)))) for k in keys), all(k in r for k in keys)
    sa, a_full = score("A")
    sb, b_full = score("B")
    band_a, prof_a = _band(sa)
    band_b, prof_b = _band(sb)
    order = {"Low": 0, "Medium": 1, "High": 2}
    profile = prof_a if order[band_a] <= order[band_b] else prof_b
    reason = None
    if order[band_b] > order[band_a]:
        thin = []
        if r["A2"] <= 2:
            thin.append("your safety net is thin")
        if r["A3"] <= 2:
            thin.append("EMIs take a big share of your pay")
        if r["A5"] == 1:
            thin.append("many people depend on you")
        reason = "Your comfort with risk is high, but " + (" and ".join(thin) if thin else "your situation cannot carry it yet") + ". Fix that first — then this moves up."
    R = RETURN_BY_PROFILE[profile]

    # ── fitness (four pillars, each capped at 100, equal weight) ──
    emerg_target = 6 * (spend + emi)
    cover_target = (10 if b.dependants > 0 else 5) * 12 * income
    p_emerg = min(1.0, cash / emerg_target) * 100 if emerg_target > 0 else 100.0
    p_debt = max(0.0, 1 - emi / income) * 100 if income > 0 else 0.0
    sav_rate = (income - spend - emi) / income if income > 0 else 0.0
    p_sav = max(0.0, min(1.0, sav_rate / 0.20)) * 100
    p_prot = min(1.0, cover / cover_target) * 100 if cover_target > 0 else 100.0
    fitness = round((p_emerg + p_debt + p_sav + p_prot) / 4)

    # ── goal (optional): cost grown at 6%, SIP with step-up at the profile return ──
    goal = None
    goal_sip = 0.0
    if b.goal and b.goal != "skip" and b.goal_cost and b.goal_years:
        gy = max(1, int(b.goal_years))
        g_cost = float(b.goal_cost) * ((1 + INFLATION) ** gy)
        fac = _fv_sip_stepup(1.0, gy, R)
        goal_sip = g_cost / fac if fac > 0 else 0.0
        goal = {"type": b.goal, "cost_today": float(b.goal_cost), "years": gy, "cost_then": round(g_cost), "sip": round(goal_sip)}

    # ── retirement corpus ──
    spend_ret_yearly, required = _corpus_required(spend, n, years_in_ret)
    free_cash = max(0.0, cash - emerg_target)
    lump = (inv + free_cash) * ((1 + R) ** n)
    sip_fv = _fv_sip_stepup(sip, n, R)
    projected = lump + sip_fv
    gap = required - projected
    fac_n = _fv_sip_stepup(1.0, n, R)
    extra_sip = gap / fac_n if (gap > 0 and fac_n > 0) else 0.0
    lasts = _lasts_to_age(projected, spend_ret_yearly, ret) if years_in_ret > 0 else ret
    short_years = max(0, PLAN_TO_AGE - lasts)
    headline = (f"Your money lasts to age {lasts}. You wanted {PLAN_TO_AGE}. Short by {short_years} years."
                if lasts < PLAN_TO_AGE else f"Your money lasts past {PLAN_TO_AGE}. You are covered to {lasts if lasts < 100 else '100 and beyond'}.")

    # ── affordability ──
    spare = income - spend - emi - sip - goal_sip
    affordable = extra_sip <= spare
    verdict = "You can do this." if affordable else f"Short by ₹{round(extra_sip - spare):,} a month."
    alternatives = []
    if not affordable and gap > 0:
        for ra in range(ret + 1, 71):   # retire later: smallest age where the gap closes on the current SIP
            n2 = ra - age
            _, req2 = _corpus_required(spend, n2, max(0, PLAN_TO_AGE - ra))
            proj2 = (inv + free_cash) * ((1 + R) ** n2) + _fv_sip_stepup(sip, n2, R)
            if proj2 >= req2:
                alternatives.append({"kind": "retire_later", "text": f"Retire at {ra} instead of {ret}.", "value": ra})
                break
        if required > 0:   # spend less: required scales with spending
            cut = spend - spend * projected / required
            if cut > 0:
                alternatives.append({"kind": "spend_less", "text": f"Reduce spending by ₹{round(cut / 1000) * 1000:,} a month.", "value": round(cut)})

    return {
        "headline": headline, "lasts_to_age": lasts, "plan_to_age": PLAN_TO_AGE, "years_to_retirement": n,
        "profile": {"final": profile, "return_pct": round(R * 100), "situation": {"score": sa, "band": band_a, "complete": a_full},
                    "comfort": {"score": sb, "band": band_b, "complete": b_full}, "auto": {"A2": r["A2"], "A3": r["A3"], "A5": r["A5"]},
                    "reason": reason},
        "fitness": {"score": fitness, "pillars": [
            {"k": "Emergency fund", "v": round(p_emerg), "note": f"₹{round(cash):,} against a target of ₹{round(emerg_target):,} (6 × spending + EMIs)"},
            {"k": "Debt", "v": round(p_debt), "note": f"EMIs take {round(emi_share * 100)}% of take-home"},
            {"k": "Savings rate", "v": round(p_sav), "note": f"{round(sav_rate * 100)}% of take-home saved, against a 20% benchmark"},
            {"k": "Protection", "v": round(p_prot), "note": f"₹{round(cover):,} cover against a target of ₹{round(cover_target):,}"}]},
        "corpus": {"spending_at_retirement_monthly": round(spend_ret_yearly / 12), "required": round(required),
                   "projected": round(projected), "from_lump": round(lump), "from_sip": round(sip_fv),
                   "cash_counted": round(free_cash), "cash_kept_as_buffer": round(min(cash, emerg_target)),
                   "gap": round(gap) if gap > 0 else 0, "surplus": round(-gap) if gap < 0 else 0},
        "sip": {"extra_needed": round(extra_sip), "spare": round(spare), "verdict": verdict, "affordable": affordable,
                "lines": [f"Invest ₹{round(extra_sip):,} more each month to reach {PLAN_TO_AGE}." if extra_sip > 0 else f"No extra SIP needed to reach {PLAN_TO_AGE}.",
                          f"You have ₹{round(spare):,} spare after spending, EMIs and your current SIP" + (" and the goal SIP." if goal_sip else "."),
                          verdict],
                "alternatives": alternatives},
        "goal": goal,
        "baskets": {"profile": profile, "rows": _basket_facts(BASKETS_BY_PROFILE[profile]),
                    "note": "Equal split across the baskets is the default; the founder sets the final split. Paper baskets, research only."},
        "assumptions": CONFIG["assumptions"],
    }
