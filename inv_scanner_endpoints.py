"""cc#1286 · INVESTMENT SCANNER TAB — page route + board feed (spec session_log 30147/30129).

/inv-scanner            the page (template scorr_inv_scanner.html, token-contract styled)
/api/inv-scanner/board  one payload per track: ranked scores joined with universe tags,
                        pillar block, overlay readings, open-state badge and — for the
                        reversal track — the S1 trigger state read from the score row's own
                        components jsonb, so the page can say "touched N sessions ago,
                        reclaim pending" instead of implying it.

Read-only over the cc#1283/1284/1285 tables. TC Scanner surfaces untouched.
"""

import os
import logging
from typing import Optional

import psycopg
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

# cc#1697: the (i) sheet's entry/exit numbers, imported not retyped — inv_scanner_rules.py is
# the engine's own constants module (ENTRY_MOM/ENTRY_REV/EXIT_MOM/EXIT_REV already power run()'s
# actual gate/exit checks there); a read-only import here means the sheet can never silently
# drift from what the engine really enforces. Band thresholds (84/65/50) are NOT imported — they
# are inline literals inside inv_scanner_scoring.py's _band(), which is scanner SCORING and stays
# untouched per this card's own do_not_touch, so they are restated as literals in META below
# (verified against inv_scanner_scoring.py at build time; flagged in cc_task_logs 1697 that this
# is the one place the card's "84/65/50 appear once" verify line does not literally hold).
from inv_scanner_rules import ENTRY_MOM, ENTRY_REV, EXIT_MOM, EXIT_REV
from inv_scanner_rules import HARD_STOP_ABS_PCT, HARD_STOP_ALPHA_PCT, BENCHMARK_SYMBOL   # cc#1768: the hard-stop levels, surfaced (H7)
# cc#1767: the GATES column reads the engine's OWN gate function and gate registry — never a
# second computation of the same four numbers.
from inv_scanner_rules import evaluate_gates, gates_passed, GATE_ORDER, GATE_LABELS, GATE_RULES, GATE_UNITS

log = logging.getLogger("scorr.inv_scanner_page")
router = APIRouter(tags=["investment_scanner"])

META = {
    "what_it_is": "Two tracks, momentum and reversal, each scored 0–100. Runs once a day, "
                  "after the GVM engine writes for the night (EOD).",
    "universe": "Every symbol in the GVM universe, plus the quant basket names, plus two extra "
                "screens (a momentum screen and a quality-improving “gv rising” screen) "
                "— each name carries every source it qualified through.",
    "entry": {
        "rule": "BUY when momentum score > {mom} OR reversal score > {rev}, AND all four price "
                "gates pass.".format(mom=ENTRY_MOM, rev=ENTRY_REV),
        "gates": ["day return is positive", "week gain is between 0% and 5%",
                  "month gain is between 0% and 7%", "the stock's sector averaged a positive month"],
        "thresholds": {"entry_mom": ENTRY_MOM, "entry_rev": ENTRY_REV},
    },
    "exit": {
        # cc#1768 (INVESTMENT_SCANNER_EXIT_V2, 39581): bars 75 / 70 and the hard stop, every number
        # read from inv_scanner_rules — the engine's own constants, never retyped here.
        "rule": "A hard stop is checked first, regardless of score: exit when the stock is {abs:g}% "
                "or worse from entry, or when its return since entry trails {bench} by {alpha:g}% or "
                "more (alpha). Then score decay: a momentum entry exits when its momentum score drops "
                "below {mom}; a reversal entry exits when its reversal score drops below {rev}. The "
                "10-point gap between the entry bar and the exit bar stops churn. There is no target leg."
                .format(abs=HARD_STOP_ABS_PCT, alpha=HARD_STOP_ALPHA_PCT, bench=BENCHMARK_SYMBOL,
                        mom=EXIT_MOM, rev=EXIT_REV),
        "thresholds": {"exit_mom": EXIT_MOM, "exit_rev": EXIT_REV,
                       "hard_stop_abs_pct": HARD_STOP_ABS_PCT, "hard_stop_alpha_pct": HARD_STOP_ALPHA_PCT,
                       "benchmark": BENCHMARK_SYMBOL},
    },
    "bands": {
        "rule": "STRONG BUY ≥ 84 · ACCUMULATE ≥ 65 · WATCH ≥ 50 · AVOID < 50, "
                "on both tracks.",
        "thresholds": {"strong_buy": 84, "accumulate": 65, "watch": 50},
    },
    "honesty": "Research only — this is a scanner, not a trade recommendation. Exits are the "
               "hard stop and score decay; there is no target leg. Every number on this page is as of the run date shown, "
               "not live.",
}

_DIR = os.path.dirname(os.path.abspath(__file__))


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@router.get("/inv-scanner", response_class=HTMLResponse)
def inv_scanner_page():
    with open(os.path.join(_DIR, "scorr_inv_scanner.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/api/inv-scanner/symbol/{symbol}")
def symbol_card(symbol: str):
    """cc#1287: one symbol's scanner standing for the /check INVEST mode section. Honest
    absence when the name is not in the universe — never a fake score."""
    sym = (symbol or "").strip().upper()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(run_date) FROM investment_scanner_scores")
        r = cur.fetchone()
        d = r[0] if r else None
        if d is None:
            return {"symbol": sym, "in_universe": False, "run_date": None}
        cur.execute("""
            SELECT s.mom_score, s.band_mom, s.rev_score, s.band_rev,
                   u.tags, u.insufficient_history, st.status, st.entry_track, st.entered_at
            FROM investment_scanner_scores s
            JOIN investment_scanner_universe u ON u.symbol=s.symbol AND u.run_date=s.run_date
            LEFT JOIN investment_scanner_state st ON st.symbol=s.symbol
            WHERE s.run_date=%s AND s.symbol=%s""", (d, sym))
        row = cur.fetchone()
    if not row:
        return {"symbol": sym, "in_universe": False, "run_date": str(d)}
    mom, bm, rev, br, tags, insuff, st_status, st_track, st_at = row
    return {"symbol": sym, "in_universe": True, "run_date": str(d),
            "mom_score": _f(mom), "band_mom": bm,
            "rev_score": _f(rev), "band_rev": br,
            "tags": tags or [], "insufficient_history": bool(insuff),
            "state": ({"status": st_status, "track": st_track, "entered_at": str(st_at)}
                      if st_status else None)}


@router.get("/api/inv-scanner/board")
def board(track: str = "momentum", limit: int = 100):
    """Ranked board for one track. Every row carries everything the table renders — the page
    computes nothing."""
    col, band_col, comp_col = (("mom_score", "band_mom", "mom_components")
                               if track != "reversal" else
                               ("rev_score", "band_rev", "rev_components"))
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(run_date) FROM investment_scanner_scores")
        r = cur.fetchone()
        d = r[0] if r else None
        if d is None:
            return {"run_date": None, "track": track, "rows": []}
        cur.execute(f"""
            SELECT s.symbol, s.{col}, s.{band_col}, s.mom_score, s.rev_score,
                   u.gvm, u.g, u.v, u.m, u.tags, u.insufficient_history,
                   ut.week_index_52, ut.rsi_month, ut.vol_ratio_21, ut.month_return,
                   st.status, st.entry_track, st.entered_at,
                   s.{comp_col}->'s1_touch_reclaim'->'input' AS s1_input,
                   ut.week_return, gs.segment
            FROM investment_scanner_scores s
            JOIN investment_scanner_universe u ON u.symbol=s.symbol AND u.run_date=s.run_date
            LEFT JOIN universe_technicals ut ON ut.symbol=s.symbol
                 AND ut.score_date=(SELECT MAX(score_date) FROM universe_technicals)
            LEFT JOIN investment_scanner_state st ON st.symbol=s.symbol
            LEFT JOIN (SELECT DISTINCT ON (symbol) symbol, segment FROM gvm_scores
                       ORDER BY symbol, score_date DESC) gs ON gs.symbol=s.symbol
            WHERE s.run_date=%s AND s.{col} IS NOT NULL
            ORDER BY s.{col} DESC, s.symbol
            LIMIT %s""", (d, max(1, min(limit, 250))))
        fetched = cur.fetchall()
        rows = []
        seg_cache = {}
        bar = ENTRY_MOM if track != "reversal" else ENTRY_REV
        for r in fetched:
            (sym, score, band, mom, rev, gvm, g, v, m, tags, insuff,
             wk52, rsi, vol, mret, st_status, st_track, st_at, s1, wk_ret, segment) = r
            # cc#1767: the four gates through the engine's own evaluate_gates (same sources: day
            # from _day_return, week/month from universe_technicals, sector from IC's
            # _segment_month) — the SAME call run() makes before an entry. Never recomputed here.
            try:
                gates = evaluate_gates(cur, sym, _f(wk_ret), _f(mret), segment, seg_cache)
            except Exception as ex:   # the row still renders; every capsule reads "no data"
                log.warning("cc#1767 evaluate_gates failed for %s: %s", sym, ex)
                gates = {k: {"value": None, "pass": False, "error": str(ex)[:80]} for k in GATE_ORDER}
            n_pass = gates_passed(gates)
            mom_f, rev_f, score_f = _f(mom), _f(rev), _f(score)
            # the engine's score condition, both tracks, exactly as run() asks it
            score_ok = (mom_f is not None and mom_f > ENTRY_MOM) or (rev_f is not None and rev_f > ENTRY_REV)
            entered = st_status == "open"
            gates_state = ("ENTERED" if entered
                           else "ENTERS" if (score_ok and n_pass == 4)
                           else "GATED" if score_ok
                           else "BELOW_BAR" if n_pass == 4
                           else "OUT")
            rows.append({
                "symbol": sym, "score": score_f, "band": band,
                "mom_score": mom_f, "rev_score": rev_f,
                "gvm": _f(gvm), "g": _f(g), "v": _f(v), "m": _f(m),
                "tags": tags or [], "insufficient_history": bool(insuff),
                "wk52": _f(wk52), "rsi_month": _f(rsi), "vol_ratio_21": _f(vol),
                "month_return": _f(mret),
                "state": ({"status": st_status, "track": st_track, "entered_at": str(st_at)}
                          if st_status else None),
                "s1": s1 if isinstance(s1, dict) else None,
                # cc#1767: the gates, the count, the state and the score bar — the page renders,
                # it computes nothing. score_bar is SEPARATE from the gate count (A6).
                "gates": gates,
                "gates_pass_n": n_pass,
                "gates_state": gates_state,
                "score_bar": {"value": score_f, "bar": bar, "pass": score_f is not None and score_f > bar,
                              "track": "reversal" if track == "reversal" else "momentum"},
            })
    meta = dict(META)
    meta["gate_order"] = list(GATE_ORDER)
    meta["gate_labels"] = GATE_LABELS
    meta["gate_rules"] = GATE_RULES
    meta["gate_units"] = GATE_UNITS
    meta["gate_function"] = "inv_scanner_rules.evaluate_gates — the entry engine's own call (run()), read per row here"
    meta["gates_state_rule"] = ("ENTERED = open in investment_scanner_state; ENTERS = score bar passes on either track "
                                "AND 4/4 gates (would enter on the next run); GATED = score bar passes, a gate fails; "
                                "BELOW_BAR = 4/4 gates, score under the bar; OUT = neither. A missing input is not a pass.")
    return {"run_date": str(d), "track": track, "count": len(rows), "rows": rows, "meta": meta}
