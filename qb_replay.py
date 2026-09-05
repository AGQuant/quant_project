"""
qb_replay.py — cc#1711 QB REPLAY SINCE INCEPTION (Large Cap + Alpha Multicap under V2 + cap 15).

Founder 05-Sep-2026: "yes adjust since inception. its paper trade so adjust rebalancing and
current holdings as well." Spec: session_log 39173 (cap 15, Rs 5L, fill-to-cap) on top of 6097
(large_cap V2) and 6086 (alpha_multicap V2). One consistent rule set from 2026-06-01; V1 rules
(16-name book, HS2) are NOT replayed.

DETERMINISTIC: same inputs -> same rows. Every rule value comes from quant_basket_config via
qb_config (cc#1710) and the stage2_stock keys; nothing here is a basket-name literal.

CONVENTIONS (stated in every replayed row's notes / actions.replay_note):
  D3 UNIVERSE  large_cap = screener_raw mcap rank 1..100 AS OF TODAY (no as-of rank history
               exists; approximation, logged); alpha_multicap = nifty500_universe (current).
  D4 PRICES    signal on the close of day S (gvm_history score_date S — the latest score_date on
               or before the due date); entry/exit EXECUTED at the close of the next trading day
               D > S; entry_date / exit_date = D. Matches the live engine numerically (ADANIPORTS
               1814.50 = close of 02-Jun for the 01-Jun signal) but stamps the execution date.
               HS1 breached on close D -> exit at close D, exit_date D.
  D5 ENTRIES   founder confirmation deemed given: fill to the cap with every gate-passer in rank
               order at every rebalance date.
  SIZING       slot = capital / max_stocks at a full book; N<10 brake -> capital/10 + cash
               (qb_config.size_slots). qty = floor(min(slot, cash / open_slots) / close). Cash can
               never go negative by construction. allocation = qty * price (actual cost).
  EXITS        HS1 nightly from the config exit string (hs1_20 -> -20% from entry, any close);
               monthly rank exit from the same string (monthly_max3_rank_gt25 -> max 3 held names
               ranked worse than 25 on the signal date, worst first). No HS2.

TABLES WRITTEN (staging basket name = <basket><suffix>, e.g. large_cap__r15):
  quant_paper_positions  one row per entry (exit fields set when it exited)
  quant_rebalance_log    one was_due row per rebalance date + one eod_stop_check row per trading
                         day (live shape: actions.type, positions[], hard_stop_1[], nifty_today)
  qb_nav_daily           qb_nav's formula, one row per trading day of the benchmark calendar

USAGE (server, DATABASE_URL set):
  python qb_replay.py --basket large_cap --start 2026-06-01 --end 2026-09-04 --suffix __r15
  python qb_replay.py --basket alpha_multicap --start 2026-06-01 --end 2026-09-04 --suffix __r15
The module also exposes every step as (sql, params) so the identical statements can be executed
through a plain SQL channel when no direct connection exists (how cc#1711 Phase A was run).
"""
import argparse
import json
import math
import os
import re
from datetime import date
from typing import Dict, List, Optional, Tuple

import qb_config

CARD = "cc#1711"
REPLAY_NOTE = ("REPLAY cc#1711 (session_log 39173) V2+cap15 since inception. D4: signal=close(S, "
               "gvm_history score_date) exec=close(next trading day D) date=D; HS1 breached on close D "
               "-> exit close D. D3: universe = current screener_raw mcap rank (approximation, no as-of "
               "history). D5: founder confirmation deemed given, fill to cap in rank order.")

# ── rules from config ──────────────────────────────────────────────────────────────────────────

def basket_rules(conn, basket: str) -> Dict:
    """Everything the replay needs, read from quant_basket_config.stage2_stock (+ qb_config for cap
    and capital). Raises when a needed key is absent — no invented rule."""
    p = qb_config.basket_params(conn, basket)
    with conn.cursor() as cur:
        cur.execute("SELECT stage2_stock, cap_type FROM quant_basket_config WHERE basket_name=%s", (basket,))
        row = cur.fetchone()
    if not row:
        raise LookupError(f"{basket}: no quant_basket_config row")
    s2 = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
    exit_s = str(s2.get("exit") or "")
    m_hs1 = re.search(r"hs1_(\d+)", exit_s)
    m_rank = re.search(r"max(\d+)_rank_gt(\d+)", exit_s)
    if not (m_hs1 and m_rank):
        raise LookupError(f"{basket}: exit string '{exit_s}' has no hs1_NN / maxN_rank_gtK")
    universe = "mcap_top100" if str(s2.get("spec", "")).endswith("6097") else "nifty500"
    return {
        "basket": basket, "cap_type": row[1], "max_stocks": p["max_stocks"], "capital": p["capital"],
        "gvm_min": float(s2["gvm_min"]), "v_min": (float(s2["v_min"]) if s2.get("v_min") is not None else None),
        "m_min_exclusive": (float(s2["m_min_exclusive"]) if s2.get("m_min_exclusive") is not None else None),
        "dgvm_min_exclusive": float(s2.get("dgvm_180d_min_exclusive", 0.5)),
        "hs1_pct": -float(m_hs1.group(1)), "max_exits": int(m_rank.group(1)), "keep_rank": int(m_rank.group(2)),
        "universe": universe, "source": p["source"], "config": s2,
    }


# ── calendar ─────────────────────────────────────────────────────────────────────────────────

def benchmark_symbol(cap_type: Optional[str]) -> str:
    return "NIFTY50" if (cap_type or "").strip().lower().startswith("large") else "NIFTY500"


def trading_days(conn, bench: str, start: date, end: date) -> List[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT price_date FROM raw_prices WHERE symbol=%s AND price_date BETWEEN %s AND %s "
                    "AND close>0 ORDER BY price_date", (bench, start, end))
        return [str(r[0]) for r in cur.fetchall()]


def rebalance_due_dates(start: date, end: date, day: int = 6) -> List[date]:
    """Inception = start; then the 6th of every following month up to end (spec D2)."""
    out = [start]
    y, m = start.year, start.month
    while True:
        m += 1
        if m > 12:
            m, y = 1, y + 1
        d = date(y, m, day)
        if d > end:
            break
        out.append(d)
    return out


def signal_and_exec(conn, due: date, days: List[str]) -> Tuple[str, Optional[str]]:
    """D4: signal = latest gvm_history score_date <= due; exec = first trading day > signal."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(score_date) FROM gvm_history WHERE score_date <= %s", (due,))
        sig = str(cur.fetchone()[0])
    ex = next((d for d in days if d > sig), None)
    return sig, ex


# ── ranking snapshot on a signal date ─────────────────────────────────────────────────────────

SNAPSHOT_SQL = {
    "mcap_top100": """
WITH mcap AS (SELECT nse_code AS symbol, ROW_NUMBER() OVER (ORDER BY market_cap DESC NULLS LAST) AS mrank
              FROM screener_raw WHERE nse_code IS NOT NULL AND market_cap IS NOT NULL),
latest AS (SELECT symbol, gvm_score, g_score, v_score, m_score FROM gvm_history WHERE score_date = %(sig)s::date),
dg AS (SELECT DISTINCT ON (symbol) symbol, gvm_score AS gvm_180 FROM gvm_history
       WHERE score_date BETWEEN %(sig)s::date - 200 AND %(sig)s::date - 180 ORDER BY symbol, score_date DESC),
r AS (SELECT l.symbol, l.gvm_score, l.g_score, l.v_score, l.m_score, (l.gvm_score - d.gvm_180) AS dgvm,
             (0.5*l.gvm_score + 0.5*l.m_score) AS score,
             (l.gvm_score >= %(gvm_min)s AND d.gvm_180 IS NOT NULL AND (l.gvm_score - d.gvm_180) > %(dgvm)s
              AND (%(v_min)s::numeric IS NULL OR l.v_score >= %(v_min)s)
              AND (%(m_min)s::numeric IS NULL OR l.m_score > %(m_min)s)) AS passes
      FROM latest l JOIN mcap m ON m.symbol = l.symbol LEFT JOIN dg d ON d.symbol = l.symbol
      WHERE m.mrank <= 100)
SELECT row_number() OVER (ORDER BY score DESC NULLS LAST, symbol) AS rank, symbol, gvm_score, g_score, v_score,
       m_score, dgvm, score, passes FROM r ORDER BY rank""",
    "nifty500": """
WITH latest AS (SELECT symbol, gvm_score, g_score, v_score, m_score FROM gvm_history WHERE score_date = %(sig)s::date),
dg AS (SELECT DISTINCT ON (symbol) symbol, gvm_score AS gvm_180 FROM gvm_history
       WHERE score_date BETWEEN %(sig)s::date - 200 AND %(sig)s::date - 180 ORDER BY symbol, score_date DESC),
r AS (SELECT l.symbol, l.gvm_score, l.g_score, l.v_score, l.m_score, (l.gvm_score - d.gvm_180) AS dgvm,
             (0.5*l.gvm_score + 0.5*l.m_score) AS score,
             (l.gvm_score >= %(gvm_min)s AND d.gvm_180 IS NOT NULL AND (l.gvm_score - d.gvm_180) > %(dgvm)s
              AND (%(v_min)s::numeric IS NULL OR l.v_score >= %(v_min)s)
              AND (%(m_min)s::numeric IS NULL OR l.m_score > %(m_min)s)) AS passes
      FROM latest l JOIN nifty500_universe u ON u.symbol = l.symbol LEFT JOIN dg d ON d.symbol = l.symbol)
SELECT row_number() OVER (ORDER BY score DESC NULLS LAST, symbol) AS rank, symbol, gvm_score, g_score, v_score,
       m_score, dgvm, score, passes FROM r ORDER BY rank""",
}


def snapshot(conn, rules: Dict, sig: str) -> List[Dict]:
    params = {"sig": sig, "gvm_min": rules["gvm_min"], "dgvm": rules["dgvm_min_exclusive"],
              "v_min": rules["v_min"], "m_min": rules["m_min_exclusive"]}
    with conn.cursor() as cur:
        cur.execute(SNAPSHOT_SQL[rules["universe"]], params)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── prices ────────────────────────────────────────────────────────────────────────────────────

def closes_on(conn, symbols: List[str], day: str) -> Dict[str, float]:
    """Close on `day`, forward-filled from the last bar on or before it (qb_nav convention)."""
    if not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices
                       WHERE symbol = ANY(%s) AND price_date <= %s::date AND close > 0
                       ORDER BY symbol, price_date DESC""", (list(symbols), day))
        return {r[0]: float(r[1]) for r in cur.fetchall()}


FIRST_HS1_SQL = """
SELECT v.symbol, MIN(p.price_date) AS hit_date
FROM (VALUES %s) AS v(symbol, entry_price, after_date, upto_date)
JOIN raw_prices p ON p.symbol = v.symbol AND p.price_date > v.after_date::date AND p.price_date <= v.upto_date::date
WHERE p.close > 0 AND p.close <= v.entry_price::numeric * (1 + %s::numeric / 100.0)
GROUP BY v.symbol"""


def first_hs1(conn, open_pos: List[Dict], after: str, upto: str, hs1_pct: float) -> Dict[str, str]:
    """{symbol: first trading day in (after, upto] whose close breaches HS1}."""
    if not open_pos:
        return {}
    vals = ",".join("(%s,%s,%s,%s)" for _ in open_pos)
    args = []
    for p in open_pos:
        args += [p["symbol"], p["entry_price"], after, upto]
    with conn.cursor() as cur:
        cur.execute(FIRST_HS1_SQL % (vals, "%s"), args + [hs1_pct])
        return {r[0]: str(r[1]) for r in cur.fetchall()}


# ── the replay ────────────────────────────────────────────────────────────────────────────────

def _cash(capital: float, positions: List[Dict]) -> float:
    entered = sum(p["allocation"] for p in positions)
    realised = sum(p["qty"] * p["exit_price"] for p in positions if p.get("exit_price") is not None)
    return round(capital - entered + realised, 2)


def replay(conn, basket: str, start: date, end: date) -> Dict:
    rules = basket_rules(conn, basket)
    cap, capital = rules["max_stocks"], rules["capital"]
    bench = benchmark_symbol(rules["cap_type"])
    days = trading_days(conn, bench, start, end)
    if not days:
        raise RuntimeError(f"no {bench} trading days in window")
    positions: List[Dict] = []       # every entry ever made (exit fields filled when exited)
    events: List[Dict] = []          # rebalance rows
    stops: List[Dict] = []
    schedule = []
    for due in rebalance_due_dates(start, end):
        sig, ex = signal_and_exec(conn, due, days)
        if ex is None or ex > days[-1]:
            continue
        schedule.append((due, sig, ex))

    def open_positions():
        return [p for p in positions if p.get("exit_date") is None]

    def apply_stops(after: str, upto: str):
        hits = first_hs1(conn, open_positions(), after, upto, rules["hs1_pct"])
        if not hits:
            return
        by_day: Dict[str, List[str]] = {}
        for sym, d in hits.items():
            by_day.setdefault(d, []).append(sym)
        for d in sorted(by_day):
            closes = closes_on(conn, by_day[d], d)
            for sym in by_day[d]:
                p = next(x for x in open_positions() if x["symbol"] == sym)
                c = closes[sym]
                ret = (c / p["entry_price"] - 1) * 100
                p.update(exit_date=d, exit_price=c, status="exited_stop",
                         exit_reason=f"HARD_STOP_1: stock {ret:.2f}% from entry (<= {rules['hs1_pct']:.0f}%)")
                stops.append({"date": d, "symbol": sym, "exit_price": c, "entry_price": p["entry_price"],
                              "qty": p["qty"], "pnl": round((c - p["entry_price"]) * p["qty"], 2),
                              "ret_pct": round(ret, 2)})

    prev_exec = None
    for i, (due, sig, ex) in enumerate(schedule):
        # 1) nightly HS1 between the previous execution day and this one (inclusive of D itself)
        if prev_exec is not None:
            apply_stops(prev_exec, ex)
        snap = snapshot(conn, rules, sig)
        rank_of = {r["symbol"]: r["rank"] for r in snap}
        passers = [r for r in snap if r["passes"]]
        held = open_positions()
        # 2) monthly rank exit (not at inception): worst rank first, max N, unranked = worst
        rank_exits = []
        if i > 0:
            def _rk(p):
                return rank_of.get(p["symbol"], 10 ** 9)
            pool = sorted([p for p in held if _rk(p) > rules["keep_rank"]], key=_rk, reverse=True)
            rank_exits = pool[:rules["max_exits"]]
            if rank_exits:
                closes = closes_on(conn, [p["symbol"] for p in rank_exits], ex)
                for p in rank_exits:
                    c = closes[p["symbol"]]
                    p.update(exit_date=ex, exit_price=c, status="exited_rank",
                             exit_reason=(f"RANK_EXIT: rank {rank_of.get(p['symbol'], 'n/a')} > top-{rules['keep_rank']} "
                                          f"on {sig} (monthly max {rules['max_exits']})"))
        held = open_positions()
        held_syms = {p["symbol"] for p in held}
        # 3) fill to the cap in rank order with every gate-passer not held (D5)
        slots = max(0, cap - len(held))
        cands = [r for r in passers if r["symbol"] not in held_syms][:slots]
        n_after = len(held) + len(cands)
        slot, _unused, mode = qb_config.size_slots(capital, cap, n_after) if cands else (0.0, 0.0, "none")
        cash = _cash(capital, positions)
        entries = []
        if cands:
            closes = closes_on(conn, [r["symbol"] for r in cands], ex)
            budget = min(slot, cash / len(cands))
            for r in cands:
                c = closes.get(r["symbol"])
                if not c:
                    continue
                qty = math.floor(budget / c)
                if qty <= 0:
                    continue
                alloc = round(qty * c, 2)
                cash = round(cash - alloc, 2)
                pos = {"symbol": r["symbol"], "entry_date": ex, "entry_price": c, "qty": qty, "allocation": alloc,
                       "signal_date": sig, "rank": r["rank"],
                       "score": (float(r["score"]) if r["score"] is not None else None),
                       "gvm": r["gvm_score"], "g": r["g_score"], "v": r["v_score"], "m": r["m_score"], "dgvm": r["dgvm"],
                       "exit_date": None, "exit_price": None, "status": "open", "exit_reason": None}
                positions.append(pos)
                entries.append({"symbol": r["symbol"], "rank": r["rank"], "score": pos["score"], "price": c,
                                "qty": qty, "amount": alloc})
        held_after = open_positions()
        events.append({
            "due": str(due), "signal_date": sig, "exec_date": ex, "n_passers": len(passers),
            "rank_exits": [{"symbol": p["symbol"], "rank": rank_of.get(p["symbol"]), "price": p["exit_price"],
                            "qty": p["qty"], "amount": round(p["qty"] * p["exit_price"], 2),
                            "pnl": round((p["exit_price"] - p["entry_price"]) * p["qty"], 2)} for p in rank_exits],
            "entries": entries, "slot": slot, "sizing_mode": mode,
            "held_after": len(held_after), "cash_after": _cash(capital, positions), "value_after": None,
        })
        closes = closes_on(conn, [p["symbol"] for p in held_after], ex)
        events[-1]["value_after"] = round(sum(p["qty"] * closes.get(p["symbol"], p["entry_price"]) for p in held_after)
                                          + events[-1]["cash_after"], 2)
        prev_exec = ex
    # 4) nightly HS1 from the last execution to the end of the window
    if prev_exec is not None and prev_exec < days[-1]:
        apply_stops(prev_exec, days[-1])
    return {"basket": basket, "rules": rules, "bench": bench, "days": days, "schedule": schedule,
            "positions": positions, "events": events, "stops": stops, "note": REPLAY_NOTE}


# ── writes (staging name = basket + suffix) ───────────────────────────────────────────────────

def positions_insert(res: Dict, target: str) -> Tuple[str, list]:
    rows, args = [], []
    for p in res["positions"]:
        note = (f"{REPLAY_NOTE} | signal {p['signal_date']} rank {p['rank']} score {p['score']} | "
                f"{p['exit_reason'] or 'open'} | gvm {p['gvm']} g {p['g']} v {p['v']} m {p['m']} dgvm {p['dgvm']}")
        rows.append("(%s,%s,%s,%s::date,%s,%s,%s,%s,%s,%s::date,%s,%s,%s,%s,%s,NOW(),NOW())")
        hs1 = round(p["entry_price"] * (1 + res["rules"]["hs1_pct"] / 100.0), 2)
        args += [target, p["symbol"], p["entry_price"], p["entry_date"], p["qty"], p["allocation"],
                 hs1, p["status"], p["exit_price"], p["exit_date"], p["gvm"], p["g"], p["v"], p["m"], note]
    sql = ("INSERT INTO quant_paper_positions (basket_name, symbol, entry_price, entry_date, qty, allocation, "
           "stop_loss_price, status, exit_price, exit_date, gvm_at_entry, g_at_entry, v_at_entry, m_at_entry, "
           "notes, created_at, updated_at) VALUES " + ",".join(rows))
    return sql, args


def rebalance_rows_insert(res: Dict, target: str) -> Tuple[str, list]:
    rows, args = [], []
    for e in res["events"]:
        actions = {"was_due": e["due"], "signal_date": e["signal_date"], "exec_date": e["exec_date"],
                   "exits_hs1": [], "exits_hs2": [], "exits_rank": [x["symbol"] for x in e["rank_exits"]],
                   "rank_exit_detail": e["rank_exits"], "entries": [x["symbol"] for x in e["entries"]],
                   "entry_detail": e["entries"], "n_gate_passers": e["n_passers"],
                   "held_after_exits": e["held_after"], "cap_max_stocks": res["rules"]["max_stocks"],
                   "slot_rs": e["slot"], "sizing_mode": e["sizing_mode"], "alloc_residual": e["cash_after"],
                   "entry_status": "replayed", "replay_note": REPLAY_NOTE, "card": CARD,
                   "rules_source": res["rules"]["source"]}
        rows.append("(%s,%s::date,%s,%s,%s,%s,%s::jsonb,NOW())")
        args += [target, e["exec_date"], len(e["entries"]), len(e["rank_exits"]), e["held_after"],
                 e["value_after"], json.dumps(actions, default=str)]
    sql = ("INSERT INTO quant_rebalance_log (basket_name, rebalance_date, stocks_in, stocks_out, stocks_held, "
           "total_portfolio_value, actions, computed_at) VALUES " + ",".join(rows))
    return sql, args


# One eod_stop_check row per trading day of the benchmark calendar, in the live shape, computed
# from the staging position rows + raw_prices (forward-filled close). Exits dated d show as
# exited with their reason; hard_stop_1 lists that day's HS1 exits; stocks_out counts every exit
# that day (rank exits included, as the live runner's stocks_out does).
EOD_ROWS_SQL = """
INSERT INTO quant_rebalance_log (basket_name, rebalance_date, stocks_in, stocks_out, stocks_held,
                                 total_portfolio_value, actions, computed_at)
SELECT %(target)s, d.day, 0,
       (SELECT COUNT(*) FROM quant_paper_positions x WHERE x.basket_name=%(target)s AND x.exit_date = d.day),
       (SELECT COUNT(*) FROM quant_paper_positions x WHERE x.basket_name=%(target)s AND x.entry_date <= d.day
                                                       AND (x.exit_date IS NULL OR x.exit_date > d.day)),
       COALESCE((SELECT ROUND(SUM(x.qty * c.close), 2) FROM quant_paper_positions x
                 JOIN LATERAL (SELECT close FROM raw_prices r WHERE r.symbol = x.symbol AND r.price_date <= d.day
                               AND r.close > 0 ORDER BY r.price_date DESC LIMIT 1) c ON TRUE
                 WHERE x.basket_name=%(target)s AND x.entry_date <= d.day
                   AND (x.exit_date IS NULL OR x.exit_date > d.day)), 0),
       jsonb_build_object(
         'type', 'eod_stop_check', 'replay', %(card)s, 'replay_note', %(note)s,
         'nifty_today', (SELECT close FROM raw_prices WHERE symbol='NIFTY50' AND price_date = d.day LIMIT 1),
         'hard_stop_1', COALESCE((SELECT jsonb_agg(x.symbol ORDER BY x.symbol) FROM quant_paper_positions x
                                  WHERE x.basket_name=%(target)s AND x.exit_date = d.day AND x.status='exited_stop'), '[]'::jsonb),
         'hard_stop_2', '[]'::jsonb, 'gvm_exits', '[]'::jsonb, 'm_recovered_exits', '[]'::jsonb,
         'rank_exits', COALESCE((SELECT jsonb_agg(x.symbol ORDER BY x.symbol) FROM quant_paper_positions x
                                 WHERE x.basket_name=%(target)s AND x.exit_date = d.day AND x.status='exited_rank'), '[]'::jsonb),
         'unrealised_pnl', COALESCE((SELECT ROUND(SUM(x.qty * (c.close - x.entry_price)), 2) FROM quant_paper_positions x
                 JOIN LATERAL (SELECT close FROM raw_prices r WHERE r.symbol = x.symbol AND r.price_date <= d.day
                               AND r.close > 0 ORDER BY r.price_date DESC LIMIT 1) c ON TRUE
                 WHERE x.basket_name=%(target)s AND x.entry_date <= d.day
                   AND (x.exit_date IS NULL OR x.exit_date > d.day)), 0),
         'realised_pnl', COALESCE((SELECT ROUND(SUM(x.qty * (x.exit_price - x.entry_price)), 2) FROM quant_paper_positions x
                 WHERE x.basket_name=%(target)s AND x.exit_date <= d.day), 0),
         'positions', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                 'symbol', x.symbol, 'eod_close', c.close,
                 'stock_ret_pct', ROUND((c.close / x.entry_price - 1) * 100, 4),
                 'vs_nifty_pct', NULL, 'pnl', ROUND(x.qty * (c.close - x.entry_price), 2),
                 'status', CASE WHEN x.exit_date = d.day THEN x.status ELSE 'open' END,
                 'exit_reason', CASE WHEN x.exit_date = d.day THEN split_part(x.notes, ' | ', 3) ELSE NULL END)
                 ORDER BY x.symbol)
                 FROM quant_paper_positions x
                 JOIN LATERAL (SELECT close FROM raw_prices r WHERE r.symbol = x.symbol AND r.price_date <= d.day
                               AND r.close > 0 ORDER BY r.price_date DESC LIMIT 1) c ON TRUE
                 WHERE x.basket_name=%(target)s AND x.entry_date <= d.day
                   AND (x.exit_date IS NULL OR x.exit_date >= d.day)), '[]'::jsonb)),
       NOW()
FROM (SELECT price_date AS day FROM raw_prices WHERE symbol=%(bench)s AND price_date BETWEEN %(start)s::date AND %(end)s::date
      AND close > 0) d
WHERE EXISTS (SELECT 1 FROM quant_paper_positions x WHERE x.basket_name=%(target)s AND x.entry_date <= d.day)
ORDER BY d.day"""

# qb_nav's formula (qb_nav.compute_series): holdings + derived cash over capital, both legs *100
# rebased at the first trading day; benchmark forward-filled; no NIFTYBEES in the replay so cash
# is plain cash.
NAV_ROWS_SQL = """
INSERT INTO qb_nav_daily (basket_name, nav_date, nav, benchmark_nav, holdings_value, cash_value, benchmark_sym, computed_at)
SELECT %(target)s, d.day,
       ROUND(((h.holdings + (%(capital)s - e.entered + e.realised)) / %(capital)s) * 100.0, 4),
       ROUND(b.close / b0.close * 100.0, 4),
       ROUND(h.holdings, 2), ROUND(%(capital)s - e.entered + e.realised, 2), %(bench)s, NOW()
FROM (SELECT price_date AS day, close FROM raw_prices WHERE symbol=%(bench)s AND price_date BETWEEN %(start)s::date AND %(end)s::date AND close > 0) d
JOIN LATERAL (SELECT close FROM raw_prices WHERE symbol=%(bench)s AND price_date <= d.day AND close > 0 ORDER BY price_date DESC LIMIT 1) b ON TRUE
CROSS JOIN (SELECT close FROM raw_prices WHERE symbol=%(bench)s AND price_date >= %(t0)s::date AND close > 0 ORDER BY price_date LIMIT 1) b0
JOIN LATERAL (SELECT COALESCE(SUM(x.qty * c.close), 0) AS holdings FROM quant_paper_positions x
              JOIN LATERAL (SELECT close FROM raw_prices r WHERE r.symbol = x.symbol AND r.price_date <= d.day AND r.close > 0
                            ORDER BY r.price_date DESC LIMIT 1) c ON TRUE
              WHERE x.basket_name=%(target)s AND x.entry_date <= d.day AND (x.exit_date IS NULL OR x.exit_date > d.day)) h ON TRUE
JOIN LATERAL (SELECT COALESCE(SUM(x.allocation), 0) AS entered,
                     COALESCE(SUM(CASE WHEN x.exit_date <= d.day THEN x.qty * x.exit_price END), 0) AS realised
              FROM quant_paper_positions x WHERE x.basket_name=%(target)s AND x.entry_date <= d.day) e ON TRUE
WHERE d.day >= %(t0)s::date
ORDER BY d.day"""


def eod_rows_insert(res: Dict, target: str, start: date, end: date) -> Tuple[str, dict]:
    return EOD_ROWS_SQL, {"target": target, "bench": res["bench"], "start": str(start), "end": str(end),
                          "card": CARD, "note": REPLAY_NOTE}


def nav_rows_insert(res: Dict, target: str, start: date, end: date) -> Tuple[str, dict]:
    t0 = min(p["entry_date"] for p in res["positions"]) if res["positions"] else str(start)
    return NAV_ROWS_SQL, {"target": target, "bench": res["bench"], "start": str(start), "end": str(end),
                          "capital": res["rules"]["capital"], "t0": t0}


# A4 self-checks, all SQL over the staging rows so the numbers come from the table, not memory.
SELF_CHECK_SQL = {
    "max_held_any_day": "SELECT MAX(stocks_held) FROM quant_rebalance_log WHERE basket_name=%(target)s",
    "min_cash": "SELECT MIN(cash_value) FROM qb_nav_daily WHERE basket_name=%(target)s",
    "rebalance_value_mismatch": """
        SELECT l.rebalance_date, l.total_portfolio_value, v.recomputed
        FROM quant_rebalance_log l
        JOIN LATERAL (SELECT (SELECT COALESCE(ROUND(SUM(x.qty * c.close), 2), 0) FROM quant_paper_positions x
                JOIN LATERAL (SELECT close FROM raw_prices r WHERE r.symbol=x.symbol AND r.price_date <= l.rebalance_date
                              AND r.close > 0 ORDER BY r.price_date DESC LIMIT 1) c ON TRUE
                WHERE x.basket_name=l.basket_name AND x.entry_date <= l.rebalance_date
                  AND (x.exit_date IS NULL OR x.exit_date > l.rebalance_date))
               + (l.actions->>'alloc_residual')::numeric AS recomputed) v ON TRUE
        WHERE l.basket_name=%(target)s AND l.actions ? 'was_due'
          AND ABS(l.total_portfolio_value - v.recomputed) > 1""",
    "exit_price_not_close": """
        SELECT x.symbol, x.exit_date, x.exit_price, r.close FROM quant_paper_positions x
        JOIN raw_prices r ON r.symbol=x.symbol AND r.price_date=x.exit_date
        WHERE x.basket_name=%(target)s AND x.exit_date IS NOT NULL AND x.exit_price <> r.close""",
}


# ── entry point (server) ──────────────────────────────────────────────────────────────────────

def run(conn, basket: str, start: date, end: date, suffix: str = "__r15", write: bool = True) -> Dict:
    res = replay(conn, basket, start, end)
    target = basket + suffix
    out = {"target": target, "positions": len(res["positions"]), "events": len(res["events"]),
           "stops": len(res["stops"]), "days": len(res["days"])}
    if write:
        with conn.cursor() as cur:
            # staging rows only — idempotent re-run of the same target
            for tbl in ("quant_paper_positions", "quant_rebalance_log", "qb_nav_daily"):
                cur.execute(f"DELETE FROM {tbl} WHERE basket_name=%s", (target,))
            sql, args = positions_insert(res, target)
            cur.execute(sql, args)
            sql, args = rebalance_rows_insert(res, target)
            cur.execute(sql, args)
            sql, prm = eod_rows_insert(res, target, start, end)
            cur.execute(sql, prm)
            sql, prm = nav_rows_insert(res, target, start, end)
            cur.execute(sql, prm)
            checks = {}
            for k, q in SELF_CHECK_SQL.items():
                cur.execute(q, {"target": target})
                checks[k] = cur.fetchall()
        conn.commit()
        out["self_checks"] = checks
    out["report"] = res
    return out


if __name__ == "__main__":
    import psycopg
    ap = argparse.ArgumentParser()
    ap.add_argument("--basket", required=True)
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", required=True)
    ap.add_argument("--suffix", default="__r15")
    ap.add_argument("--dry", action="store_true", help="compute only, write nothing")
    a = ap.parse_args()
    with psycopg.connect(os.getenv("DATABASE_URL")) as conn:
        r = run(conn, a.basket, date.fromisoformat(a.start), date.fromisoformat(a.end), a.suffix, write=not a.dry)
    print(json.dumps({k: v for k, v in r.items() if k != "report"}, default=str, indent=1))
