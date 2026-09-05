"""
qb_nav.py — cc#839: daily NAV series per Quant Basket, plus the benchmark leg.

The basket cards already show a headline return. What they could not show is the SHAPE of it — and
a single number cannot tell you whether 5.75% was a steady climb or a spike that has been giving
itself back for three weeks. This builds the series behind the smallcase-style two-line chart.

NAV DEFINITION (one formula, used identically by the nightly append and the historical backfill —
two definitions would eventually disagree and the chart would stop reconciling with the card):

    holdings(d) = SUM over positions entered <= d and not yet exited as of d, of qty * close(sym, d)
    realised(d) = SUM over positions exited <= d, of qty * exit_price
    cash(d)     = capital - SUM(allocation of every position entered <= d) + realised(d)
    nav(d)      = (holdings(d) + cash(d)) / capital        -- 1.0 at inception

Cash is derived, never stored, so it cannot drift from the position rows it is computed from.

NIFTYBEES is the established cash-parking instrument and has NO raw_prices history (0 rows — it is
marked live via Yahoo). A parked-cash position is therefore marked AT COST in the historical series.
That is the honest treatment for a liquid-ETF cash sleeve, and it is stated in the API response
rather than hidden — inventing a price series for it would put fictitious P&L into the chart.

BENCHMARK. Per cap_type: large_cap -> NIFTY50; mid / small / multicap -> NIFTY500 when a daily
index series exists, else NIFTY50. Checked against the live DB on 02-Aug-2026: raw_prices carries
NIFTY50, BANKNIFTY, MIDCAPNIFTY and FINNIFTY only, and nifty500_benchmark is a CONSTITUENT/weights
table (symbol, market_cap, weight, entry_price, base_date, base_index) with no daily series. So
every basket currently benchmarks to NIFTY50 and the response says so explicitly — `benchmark_note`
is surfaced on the chart so the choice is never silent.

Both legs are rebased to 100 at the SAME t0, so the gap between the lines is the alpha.
"""

import os
import logging
from typing import Dict, Any, List

import psycopg2

import qb_config   # cc#1710: capital from quant_basket_config.capital_rs (registry fallback)

log = logging.getLogger("scorr.qb_nav")

NIFTY50 = "NIFTY50"
NIFTY500 = "NIFTY500"
CASH_PARK_SYMBOLS = ("NIFTYBEES", "LIQUIDBEES")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS qb_nav_daily (
    basket_name   TEXT NOT NULL,
    nav_date      DATE NOT NULL,
    nav           NUMERIC,
    benchmark_nav NUMERIC,
    holdings_value NUMERIC,
    cash_value     NUMERIC,
    benchmark_sym  TEXT,
    computed_at   TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (basket_name, nav_date)
);
CREATE INDEX IF NOT EXISTS idx_qb_nav_daily_basket ON qb_nav_daily(basket_name, nav_date);
"""


def _conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def _has_series(cur, symbol: str) -> bool:
    cur.execute("SELECT 1 FROM raw_prices WHERE symbol=%s LIMIT 1", (symbol,))
    return cur.fetchone() is not None


def benchmark_for(cur, cap_type: str):
    """Returns (symbol, note). The note travels to the chart so the fallback is never silent."""
    ct = (cap_type or "").strip().lower()
    if ct.startswith("large"):
        return NIFTY50, None
    if _has_series(cur, NIFTY500):
        return NIFTY500, None
    return NIFTY50, (f"{cap_type or 'multicap'} benchmarked to NIFTY50 — no daily NIFTY500 "
                     f"index series exists in raw_prices")


def _closes(cur, symbols: List[str], since) -> Dict[str, Dict[str, float]]:
    """{symbol: {date_str: close}} for every trading day from `since`. One read for the whole set."""
    if not symbols:
        return {}
    cur.execute("""SELECT symbol, price_date, close FROM raw_prices
                   WHERE symbol = ANY(%s) AND price_date >= %s AND close > 0
                   ORDER BY symbol, price_date""", (list(symbols), since))
    out: Dict[str, Dict[str, float]] = {}
    for sym, d, c in cur.fetchall():
        out.setdefault(sym, {})[str(d)] = float(c)
    return out


def _forward_fill(series: Dict[str, float], dates: List[str]) -> Dict[str, float]:
    """A symbol with no bar on a given trading day keeps its last known close rather than dropping
    out of the basket for a day — a hole would read as a sudden loss and back again."""
    out, last = {}, None
    for d in dates:
        if d in series:
            last = series[d]
        if last is not None:
            out[d] = last
    return out


def compute_series(conn, basket_name: str) -> Dict[str, Any]:
    """Full NAV + benchmark series for one basket, from its inception to the latest trading day."""
    with conn.cursor() as cur:
        cur.execute("SELECT capital, cap_type FROM quant_basket_registry WHERE basket_name=%s",
                    (basket_name,))
        reg = cur.fetchone()
        if not reg:
            return {"basket": basket_name, "error": "not in registry", "points": []}
        # cc#1710: capital from quant_basket_config.capital_rs (registry fallback) — the same
        # Rs 5L today for every basket; one source with the engines from here on.
        try:
            capital = float(qb_config.basket_params(conn, basket_name)["capital"])
        except LookupError:
            capital = float(reg[0] or 0)
        bench_sym, bench_note = benchmark_for(cur, reg[1])

        cur.execute("""SELECT symbol, entry_price, entry_date, qty, allocation,
                              status, exit_price, exit_date
                       FROM quant_paper_positions WHERE basket_name=%s ORDER BY entry_date""",
                    (basket_name,))
        pos = [{"symbol": r[0], "entry_price": float(r[1] or 0), "entry_date": r[2],
                "qty": float(r[3] or 0), "allocation": float(r[4] or 0), "status": r[5],
                "exit_price": (float(r[6]) if r[6] is not None else None), "exit_date": r[7]}
               for r in cur.fetchall()]
        if not pos or capital <= 0:
            return {"basket": basket_name, "points": [], "benchmark_sym": bench_sym,
                    "benchmark_note": bench_note, "capital": capital,
                    "reason": "no positions yet — seed pending"}

        t0 = min(p["entry_date"] for p in pos)
        # The trading calendar is the benchmark's own bar dates: an index bar exists on exactly the
        # days the market traded, so this needs no holiday table.
        cur.execute("""SELECT price_date FROM raw_prices
                       WHERE symbol=%s AND price_date >= %s AND close > 0
                       ORDER BY price_date""", (bench_sym, t0))
        dates = [str(r[0]) for r in cur.fetchall()]
        if not dates:
            return {"basket": basket_name, "points": [], "benchmark_sym": bench_sym,
                    "benchmark_note": bench_note, "capital": capital,
                    "reason": f"no {bench_sym} history from {t0}"}

        syms = sorted({p["symbol"] for p in pos if p["symbol"] not in CASH_PARK_SYMBOLS})
        raw = _closes(cur, syms + [bench_sym], t0)

    px = {s: _forward_fill(raw.get(s, {}), dates) for s in syms}
    bench = _forward_fill(raw.get(bench_sym, {}), dates)
    bench0 = bench.get(dates[0])

    points, marked_at_cost = [], set()
    for d in dates:
        holdings = 0.0
        entered_cost = 0.0
        realised = 0.0
        for p in pos:
            ed = str(p["entry_date"])
            if ed > d:
                continue
            entered_cost += p["allocation"]
            xd = str(p["exit_date"]) if p["exit_date"] else None
            if xd and xd <= d:
                realised += p["qty"] * (p["exit_price"] if p["exit_price"] is not None else p["entry_price"])
                continue
            if p["symbol"] in CASH_PARK_SYMBOLS:
                holdings += p["allocation"]           # parked cash, marked at cost (see docstring)
                marked_at_cost.add(p["symbol"])
                continue
            c = px.get(p["symbol"], {}).get(d)
            holdings += p["qty"] * c if c is not None else p["allocation"]
        cash = capital - entered_cost + realised
        nav = (holdings + cash) / capital
        b = bench.get(d)
        points.append({"d": d, "nav": round(nav * 100.0, 4),
                       "bench": (round(b / bench0 * 100.0, 4) if (b and bench0) else None),
                       "holdings": round(holdings, 2), "cash": round(cash, 2)})

    return {"basket": basket_name, "capital": capital, "t0": dates[0],
            "benchmark_sym": bench_sym, "benchmark_note": bench_note,
            "marked_at_cost": sorted(marked_at_cost), "points": points}


def persist_series(conn, basket_name: str) -> Dict[str, Any]:
    """Recompute and UPSERT the whole series. Idempotent by (basket, date).

    A full recompute rather than an append-only tail: a late exit backdated into
    quant_paper_positions must correct the history it belongs to, not leave a wrong tail behind it.
    The series is a few hundred rows per basket, so recomputing costs nothing."""
    ensure_schema(conn)
    s = compute_series(conn, basket_name)
    pts = s.get("points") or []
    if not pts:
        return {"basket": basket_name, "rows": 0, "reason": s.get("reason") or s.get("error")}
    with conn.cursor() as cur:
        for p in pts:
            cur.execute("""INSERT INTO qb_nav_daily
                             (basket_name, nav_date, nav, benchmark_nav, holdings_value,
                              cash_value, benchmark_sym, computed_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                           ON CONFLICT (basket_name, nav_date) DO UPDATE
                             SET nav=EXCLUDED.nav, benchmark_nav=EXCLUDED.benchmark_nav,
                                 holdings_value=EXCLUDED.holdings_value,
                                 cash_value=EXCLUDED.cash_value,
                                 benchmark_sym=EXCLUDED.benchmark_sym,
                                 computed_at=NOW()""",
                        (basket_name, p["d"], p["nav"], p["bench"], p["holdings"],
                         p["cash"], s["benchmark_sym"]))
    conn.commit()
    return {"basket": basket_name, "rows": len(pts), "t0": s.get("t0"),
            "benchmark_sym": s.get("benchmark_sym"), "marked_at_cost": s.get("marked_at_cost")}


def persist_all(conn) -> Dict[str, Any]:
    """Called from the nightly qb_eod pass — registry-derived, same discipline as cc#838."""
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT basket_name FROM quant_basket_registry WHERE is_active ORDER BY basket_name")
        names = [r[0] for r in cur.fetchall()]
    out = []
    for n in names:
        try:
            out.append(persist_series(conn, n))
        except Exception as e:
            log.exception("qb_nav %s failed", n)
            out.append({"basket": n, "error": str(e)[:200]})
    return {"baskets": len(names), "results": out}


_WINDOW_DAYS = {"1M": 30, "3M": 90, "6M": 180, "MAX": None}
MIN_POINTS_FOR_CHART = 5     # below this the card shows a disabled chart icon, not a two-dot line


def get_series(conn, basket_name: str, window: str = "MAX") -> Dict[str, Any]:
    """Read the stored series for the chart, re-based to 100 at the START OF THE WINDOW.

    Re-basing per window is what makes the toggles mean anything: a 1M view should answer "what did
    the last month do", not "where does the last month sit on an all-time curve"."""
    ensure_schema(conn)
    win = window if window in _WINDOW_DAYS else "MAX"
    days = _WINDOW_DAYS[win]
    with conn.cursor() as cur:
        if days:
            cur.execute("""SELECT nav_date, nav, benchmark_nav, benchmark_sym FROM qb_nav_daily
                           WHERE basket_name=%s
                             AND nav_date >= (SELECT MAX(nav_date) FROM qb_nav_daily WHERE basket_name=%s)
                                              - make_interval(days => %s)
                           ORDER BY nav_date""", (basket_name, basket_name, days))
        else:
            cur.execute("""SELECT nav_date, nav, benchmark_nav, benchmark_sym FROM qb_nav_daily
                           WHERE basket_name=%s ORDER BY nav_date""", (basket_name,))
        rows = cur.fetchall()
    if not rows:
        return {"basket": basket_name, "window": win, "points": [], "enough_history": False,
                "min_points": MIN_POINTS_FOR_CHART}
    n0 = float(rows[0][1] or 0) or None
    b0 = float(rows[0][2]) if rows[0][2] is not None else None
    pts = []
    for d, nav, bn, _ in rows:
        pts.append({"d": str(d),
                    "nav": (round(float(nav) / n0 * 100.0, 2) if (nav is not None and n0) else None),
                    "bench": (round(float(bn) / b0 * 100.0, 2) if (bn is not None and b0) else None)})
    last = pts[-1]
    return {
        "basket": basket_name, "window": win, "points": pts,
        "benchmark_sym": rows[0][3],
        # cc#823 number contract: one decimal on returns.
        "basket_return_pct": (round(last["nav"] - 100.0, 1) if last["nav"] is not None else None),
        "benchmark_return_pct": (round(last["bench"] - 100.0, 1) if last["bench"] is not None else None),
        "alpha_pct": (round(last["nav"] - last["bench"], 1)
                      if (last["nav"] is not None and last["bench"] is not None) else None),
        "enough_history": len(pts) >= MIN_POINTS_FOR_CHART,
        "min_points": MIN_POINTS_FOR_CHART,
        "disclaimer": "Paper/backtest performance. Not verified, not a recommendation.",
    }
