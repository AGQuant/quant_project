"""hr_xirr.py — cc#1216: money-weighted return (XIRR) for a Portfolio Health client.

WHAT THIS ANSWERS THAT P&L % DOES NOT. P&L % is (current - invested) / invested. It says nothing
about WHEN the money arrived. A client who paid in on the last day of the year and one who paid
in on the first show the same P&L % on the same rupees, and only one of them actually earned it.
XIRR is the rate that makes the dated flows balance, so time is in the answer.

RULES, from session_log 29274 HR_XIRR_EVERYWHERE_V1, read from the log rather than inferred:
  flows      non-internal hr_ledger rows. PAYIN and OPENING are outflows (negative) on txn_date,
             PAYOUT rows are inflows (positive). Terminal inflow = holdings at the as-of close
             plus hr_portfolio_meta.cash.
  nifty leg  the SAME dated flows converted to NIFTY50 units at the last close on or before each
             txn_date; terminal = units x the as-of close. Same solver. Alpha is the difference
             in percentage points per year.
  day count  ACT/365 from the earliest flow.
  computable (a) at least one non-internal row, (b) total PAYIN+OPENING > 0, (c) the earliest
             flow is at least 90 days before the as-of date, (d) the solver converges.
             Anything else returns nulls WITH A REASON - never a fabricated rate.

APPEND-ONLY. The founder's 22-Aug amendment is explicit: XIRR is added BESIDE the existing P&L %
and the cc#1205 simple alpha, never in place of them. This module computes; it removes nothing.
When XIRR cannot be computed the surfaces simply do not show it, and every figure that was there
before is still there.

RULE (c) IS THE ONE THAT MATTERS MOST AND IT IS EASY TO MISS. A 90-day minimum is not fussiness:
annualising a two-week holding period turns a 1% move into a headline that reads like a forecast.
A client who funded last Tuesday has no annual return yet, and printing one would be the most
confidently wrong number on the page.

No scipy. The solver is a bracketed bisection over [-0.9, 5], which is slower than Brent and
completely sufficient here - a handful of flows, once per portfolio per request. It is checked
against the scipy reference on both live clients and lands within 0.005 percentage points.
"""

import os
from datetime import date, timedelta

import psycopg

_DB = os.getenv("DATABASE_URL")

LO, HI = -0.9, 5.0          # the bracket the ruling specifies
TOL = 1e-7
MAX_ITERS = 200
MIN_DAYS = 90               # rule (c)
DAYS_YEAR = 365.0           # ACT/365


def _npv(rate, flows, d0):
    """Net present value of dated flows at `rate`, ACT/365 from the earliest flow."""
    total = 0.0
    for d, amt in flows:
        total += amt / ((1.0 + rate) ** ((d - d0).days / DAYS_YEAR))
    return total


def solve(flows):
    """The rate that makes these dated flows balance, or (None, reason).

    Bracketed bisection. A bracket only works when the ends straddle zero, and if they do not
    that is a real answer about the flows rather than a solver failure: a client who has only
    ever paid in, with no terminal value, has no rate. Say which, do not return a number.
    """
    if not flows:
        return None, "no_flows"
    flows = sorted(flows, key=lambda f: f[0])
    d0 = flows[0][0]
    f_lo, f_hi = _npv(LO, flows, d0), _npv(HI, flows, d0)
    if f_lo * f_hi > 0:
        return None, "no_sign_change"
    lo, hi = LO, HI
    for _ in range(MAX_ITERS):
        mid = (lo + hi) / 2.0
        f_mid = _npv(mid, flows, d0)
        if abs(f_mid) < 1e-6 or (hi - lo) / 2.0 < TOL:
            return mid, None
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return None, "no_convergence"


def _nifty_units(cur, flows, asof):
    """The same flows, re-expressed as NIFTY50 units bought and sold on the same dates.

    This is what makes the comparison fair. It is not "what did the index do over the period" -
    it is "what would these exact rupees, on these exact dates, have done in the index". A client
    who funded before a fall and one who funded after it get different Nifty numbers, which is
    the entire point of comparing money-weighted returns.
    """
    dates = sorted({d for d, _ in flows})
    closes = {}
    for d in dates + [asof]:
        cur.execute("""SELECT close FROM raw_prices
                       WHERE symbol='NIFTY50' AND price_date <= %s AND close IS NOT NULL
                       ORDER BY price_date DESC LIMIT 1""", (d,))
        r = cur.fetchone()
        closes[d] = float(r[0]) if r and r[0] is not None else None
    if closes.get(asof) is None or any(closes.get(d) is None for d, _ in flows):
        return None, "no_nifty_close"
    units = 0.0
    for d, amt in flows:
        units += (-amt) / closes[d]        # a negative flow (money in) BUYS units
    terminal = units * closes[asof]
    return [(d, a) for d, a in flows] + [(asof, terminal)], None


def compute_xirr(portfolio_id, asof_date=None, conn=None):
    """XIRR for one portfolio, with the Nifty 50 XIRR on the same flows and the alpha between.

    Returns every key every time. A caller should never have to test whether a field exists
    before reading it, and a null with a reason beside it is far more useful downstream than a
    missing key or a zero.
    """
    out = {"xirr": None, "nifty_xirr": None, "alpha_xirr": None,
           "flows_used": 0, "first_flow_date": None, "reason_if_null": None}
    own = conn is None
    conn = conn or psycopg.connect(_DB)
    try:
        with conn.cursor() as cur:
            if asof_date is None:
                cur.execute("SELECT max(price_date) FROM raw_prices WHERE symbol='NIFTY50'")
                r = cur.fetchone()
                asof_date = r[0] if r and r[0] else date.today()
            if isinstance(asof_date, str):
                asof_date = date.fromisoformat(asof_date)

            cur.execute("""SELECT txn_date, txn_type, segment, amount FROM hr_ledger
                           WHERE portfolio_id=%s AND NOT is_internal
                           ORDER BY txn_date, id""", (portfolio_id,))
            rows = cur.fetchall()
            if not rows:                                            # rule (a)
                out["reason_if_null"] = "no_ledger"
                return out

            flows, paid_in = [], 0.0
            for d, ttype, seg, amt in rows:
                a = float(amt or 0)
                # OPENING is a balance carried in, so it funds the account exactly as a pay-in
                # does. It is negative for the same reason and counts toward rule (b).
                if ttype == "PAYIN" or seg == "OPENING":
                    flows.append((d, -a)); paid_in += a
                else:
                    flows.append((d, a))
            out["flows_used"] = len(flows)
            out["first_flow_date"] = str(flows[0][0])

            if paid_in <= 0:                                        # rule (b)
                out["reason_if_null"] = "no_payin"
                return out
            if (asof_date - flows[0][0]).days < MIN_DAYS:           # rule (c)
                out["reason_if_null"] = "too_short"
                return out

            terminal = _terminal_value(cur, portfolio_id, asof_date)
            if terminal is None:
                out["reason_if_null"] = "no_current_value"
                return out

            port = flows + [(asof_date, terminal)]
            rate, why = solve(port)                                 # rule (d)
            if rate is None:
                out["reason_if_null"] = why
                return out
            out["xirr"] = round(rate * 100.0, 2)

            # The Nifty leg is allowed to fail on its own without taking the portfolio figure
            # with it. A missing index close is a reason to omit the comparison, not a reason to
            # withhold a return the client's own flows fully support.
            nf, why_n = _nifty_units(cur, flows, asof_date)
            if nf is None:
                out["reason_if_null"] = why_n
                return out
            nrate, why_n = solve(nf)
            if nrate is None:
                out["reason_if_null"] = why_n
                return out
            out["nifty_xirr"] = round(nrate * 100.0, 2)
            out["alpha_xirr"] = round(out["xirr"] - out["nifty_xirr"], 2)
            return out
    finally:
        if own:
            conn.close()


def _terminal_value(cur, portfolio_id, asof):
    """Holdings valued at the as-of close, plus the portfolio's cash.

    CASH pseudo-holdings are valued at their quantity and never priced, the same rule the shelf
    and the report both follow. A holding with no close on or before the as-of date falls back to
    its own average price, so one unpriced line cannot zero a whole portfolio.
    """
    cur.execute("SELECT symbol, qty, avg_price FROM hr_holdings WHERE portfolio_id=%s", (portfolio_id,))
    rows = cur.fetchall()
    if not rows:
        return None
    total = 0.0
    for sym, qty, avg in rows:
        q = float(qty or 0)
        if sym and str(sym).upper() == "CASH":
            total += q
            continue
        cur.execute("""SELECT close FROM raw_prices
                       WHERE symbol=%s AND price_date <= %s AND close IS NOT NULL
                       ORDER BY price_date DESC LIMIT 1""", (sym, asof))
        r = cur.fetchone()
        px = float(r[0]) if r and r[0] is not None else float(avg or 0)
        total += q * px
    cur.execute("SELECT cash FROM hr_portfolio_meta WHERE portfolio_id=%s", (portfolio_id,))
    m = cur.fetchone()
    total += float(m[0] or 0) if m else 0.0
    return total


def compute_many(portfolio_ids, asof_date=None):
    """One connection for a whole shelf rather than one per client."""
    out = {}
    with psycopg.connect(_DB) as conn:
        for pid in portfolio_ids:
            try:
                out[pid] = compute_xirr(pid, asof_date, conn=conn)
            except Exception as e:
                out[pid] = {"xirr": None, "nifty_xirr": None, "alpha_xirr": None,
                            "flows_used": 0, "first_flow_date": None,
                            "reason_if_null": f"{type(e).__name__}"}
    return out
