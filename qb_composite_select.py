"""
qb_composite_select.py — parameterized QB selection/proposal engine (cc#555 + cc#556).
Serves the Large Cap V2 (spec id=6097) and Mid Cap V2 (spec id=6098) dry-run propose endpoints
off ONE config-driven walker (the qb_alpha_select.py pattern generalized). Alpha Multicap
(qb_alpha_select.py) and Small Cap (qb_smallcap_select.py) keep their own verified modules
unchanged — this module only backs large_cap + mid_cap.

DRY-RUN ONLY. Proposes monthly-rebalance entries (and, when keep_rank is set, the max-N rank exit).
Execution stays founder-confirmed — nothing here auto-buys/sells. Reads screener_raw / gvm_history /
quant_paper_positions only.

Config per basket:
  large_cap (id=6097): universe mcap rank<=100; score 0.5*GVM+0.5*M; gates GVM>=7.0 AND dGVM_180d>+0.5;
                       top-12 equal weight 5L/12; monthly max-3 exit outside composite top-20; HS2
                       removed for large_cap in qb_eod_checker.
  mid_cap  (id=6098): universe mcap rank 101-250; rank by M SCORE; gates GVM>=7.5 AND G>=7.0 (no V,
                       no dGVM); top-20 equal weight 5L/20; ENTRY-ONLY (exits UNCHANGED, HS2 kept).
Sizing (both): N (<=top_n) filled slots. N>=brake_n -> slot=capital/top_n (cash for empty slots).
               N<brake_n -> slot=capital/brake_n (50k), remaining CASH (concentration brake).
"""
import os
import logging

import psycopg

log = logging.getLogger("qb_composite_select")

# rank_by expressions are trusted, code-defined literals (never user input) — allowlisted.
_RANK_EXPR = {
    "composite": "(0.5*l.gvm_score + 0.5*l.m_score)",
    "m_score":   "l.m_score",
}

LARGECAP_CFG = {
    "basket": "large_cap", "capital": 500000.0, "rank_min": 1, "rank_max": 100,
    "gvm_min": 7.0, "g_min": None, "v_min": None, "dgvm_min": 0.5,
    "rank_by": "composite", "top_n": 12, "brake_n": 10, "keep_rank": 20, "max_exits": 3,
}
MIDCAP_CFG = {
    "basket": "mid_cap", "capital": 500000.0, "rank_min": 101, "rank_max": 250,
    "gvm_min": 7.5, "g_min": 7.0, "v_min": None, "dgvm_min": None,
    "rank_by": "m_score", "top_n": 20, "brake_n": 10, "keep_rank": None, "max_exits": 0,
}


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    return round(float(v), 3) if v is not None else None


def _build_sql(cfg):
    rank_expr = _RANK_EXPR[cfg["rank_by"]]
    use_dgvm = cfg.get("dgvm_min") is not None
    dgvm_join = "JOIN dgvm d ON d.symbol = l.symbol" if use_dgvm else ""
    dgvm_sel = "(l.gvm_score - d.gvm_180)" if use_dgvm else "NULL"
    gates = ["l.gvm_score >= %(gvm_min)s"]
    if cfg.get("g_min") is not None:
        gates.append("l.g_score >= %(g_min)s")
    if cfg.get("v_min") is not None:
        gates.append("l.v_score >= %(v_min)s")
    if use_dgvm:
        gates.append("(l.gvm_score - d.gvm_180) > %(dgvm_min)s")
    gates_bool = " AND ".join(gates)
    return f"""
    WITH mcap AS (
        SELECT nse_code AS symbol,
               ROW_NUMBER() OVER (ORDER BY market_cap DESC NULLS LAST) AS mrank
        FROM screener_raw WHERE nse_code IS NOT NULL AND market_cap IS NOT NULL
    ),
    latest AS (
        SELECT symbol, gvm_score, g_score, v_score, m_score, segment
        FROM gvm_history WHERE score_date = (SELECT MAX(score_date) FROM gvm_history)
    ),
    dgvm AS (
        SELECT DISTINCT ON (symbol) symbol, gvm_score AS gvm_180
        FROM gvm_history
        WHERE score_date BETWEEN %(asof)s::date - 200 AND %(asof)s::date - 180
        ORDER BY symbol, score_date DESC
    )
    SELECT l.symbol, m.mrank, l.gvm_score, l.g_score, l.v_score, l.m_score, l.segment,
           {dgvm_sel} AS dgvm,
           {rank_expr} AS rank_score,
           ({gates_bool}) AS passes_gates
    FROM latest l
    JOIN mcap m ON m.symbol = l.symbol
    {dgvm_join}
    WHERE m.mrank BETWEEN %(rank_min)s AND %(rank_max)s
    ORDER BY rank_score DESC NULLS LAST, l.symbol
    """


def propose_rebalance(cfg, conn=None, as_of=None):
    """Config-driven dry-run proposal (large_cap / mid_cap). Returns entries + sizing (+ the
    max-N rank exit when cfg['keep_rank'] is set; entry-only otherwise)."""
    own = conn is None
    if own:
        conn = _conn()
    if as_of is None:
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_DATE")
            as_of = cur.fetchone()[0]
    cap, top_n, brake_n = cfg["capital"], cfg["top_n"], cfg["brake_n"]
    try:
        params = {"asof": as_of, "rank_min": cfg["rank_min"], "rank_max": cfg["rank_max"],
                  "gvm_min": cfg["gvm_min"], "g_min": cfg.get("g_min"),
                  "v_min": cfg.get("v_min"), "dgvm_min": cfg.get("dgvm_min")}
        with conn.cursor() as cur:
            cur.execute(_build_sql(cfg), params)
            cols = [c.name for c in cur.description]
            ranked = []
            for i, r in enumerate(cur.fetchall(), start=1):
                d = dict(zip(cols, r))
                d["rank"] = i
                ranked.append(d)
            rank_of = {r["symbol"]: r["rank"] for r in ranked}

            # entries: top-N among gate-qualifiers (already rank_score-desc)
            qualifiers = [r for r in ranked if r["passes_gates"]]
            picked = qualifiers[:top_n]
            n = len(picked)

            # sizing: N>=brake_n -> capital/top_n (cash only for empty slots); N<brake_n -> brake slot + cash
            if n == 0:
                slot, cash, mode = 0.0, cap, "empty"
            elif n < brake_n:
                slot = round(cap / brake_n, 2)
                cash = round(cap - slot * n, 2)
                mode = f"brake_5L_div_{brake_n}"
            else:
                slot = round(cap / top_n, 2)
                cash = round(cap - slot * n, 2)
                mode = f"equal_5L_div_{top_n}"

            entries = [{"symbol": r["symbol"], "mcap_rank": int(r["mrank"]), "rank": r["rank"],
                        "gvm": _f(r["gvm_score"]), "g": _f(r["g_score"]), "v": _f(r["v_score"]),
                        "m": _f(r["m_score"]), "dgvm": _f(r["dgvm"]), "segment": r["segment"],
                        "rank_score": _f(r["rank_score"]), "slot_value": slot} for r in picked]

            # current holdings + their live rank
            cur.execute("SELECT symbol FROM quant_paper_positions "
                        "WHERE basket_name=%s AND status='open'", (cfg["basket"],))
            held = [row[0] for row in cur.fetchall()]
            holdings = [{"symbol": s, "rank": rank_of.get(s)} for s in held]

            # exits: only when this basket uses the rank-exit template (keep_rank set)
            exits, refills = [], []
            keep_rank = cfg.get("keep_rank")
            if keep_rank is not None:
                def _rk(h):
                    return h["rank"] if h["rank"] is not None else 10**9
                pool = sorted([h for h in holdings if _rk(h) > keep_rank], key=_rk, reverse=True)
                exits = pool[:cfg.get("max_exits", 0)]
                held_set = set(held)
                refill_slots = max(len(exits), max(0, top_n - len(held_set)))
                refills = [{"symbol": q["symbol"], "rank": q["rank"],
                            "rank_score": _f(q["rank_score"])}
                           for q in qualifiers if q["symbol"] not in held_set][:refill_slots]

        out = {
            "as_of": str(as_of), "basket": cfg["basket"], "capital": cap,
            "top_n": top_n, "entries": entries, "n_qualified": len(qualifiers), "n_selected": n,
            "slot_value": slot, "cash_value": cash, "sizing_mode": mode,
            "holdings": holdings, "entry_only": keep_rank is None,
            "rules": {
                "universe": f"screener_raw mcap rank {cfg['rank_min']}-{cfg['rank_max']}",
                "rank_by": ("0.5*GVM + 0.5*M" if cfg["rank_by"] == "composite" else "M score"),
                "hard_gates": _gates_text(cfg),
                "selection": f"top-{top_n} equal weight capital/{top_n}; <{brake_n} -> capital/{brake_n} + cash",
                "exit": (f"monthly 6th: max {cfg['max_exits']} held names ranked outside top-{keep_rank}, worst first"
                         if keep_rank is not None else "UNCHANGED (HS1/HS2/monthly re-screen); entry-only engine"),
            },
        }
        if keep_rank is not None:
            out["exits"] = exits
            out["refills"] = refills
        return out
    finally:
        if own:
            conn.close()


def _gates_text(cfg):
    parts = [f"GVM>={cfg['gvm_min']}"]
    if cfg.get("g_min") is not None:
        parts.append(f"G>={cfg['g_min']}")
    if cfg.get("v_min") is not None:
        parts.append(f"V>={cfg['v_min']}")
    if cfg.get("dgvm_min") is not None:
        parts.append(f"dGVM_180d>+{cfg['dgvm_min']}")
    return " AND ".join(parts)


def propose_largecap(conn=None, as_of=None):
    return propose_rebalance(LARGECAP_CFG, conn=conn, as_of=as_of)


def propose_midcap(conn=None, as_of=None):
    return propose_rebalance(MIDCAP_CFG, conn=conn, as_of=as_of)
