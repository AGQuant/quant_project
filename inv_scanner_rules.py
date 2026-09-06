"""cc#1285 · INVESTMENT SCANNER ENGINE 3/3 — ENTRY/EXIT RULES (spec session_log 30147).

ENTRY  BUY when (mom_score > 85 OR rev_score > 80) AND ALL four price gates:
         day_return > 0 · week_return in [0,5] · month_return in [0,7] · segment month avg > 0.
       The gates exist to stop the scanner chasing extended names — a 99-score name up 35% on
       the month is exactly what the month gate is FOR. Every gate's input, source and pass/fail
       is logged in the signal row's gates jsonb; the signal carries its qualifying track(s).

EXIT   cc#1768 (INVESTMENT_SCANNER_EXIT_V2, session_log 39581; founder 06-Sep "Exit Scorr below 75
       Momentum and below 70 Reversal" — supersedes the exit line of 30147 ONLY):
       momentum-entered name: mom_score < 75 → EXIT. reversal-entered: rev_score < 70.
       10-point hysteresis (was 5 with bars 80/75 — three of the four V1 exits, at 79.4, 79.4 and
       74.0, were cutting names on small score dips; those closed rows stand as history with the
       bars that fired them). DUAL-track entries exit when BOTH tracks are below their bars —
       while either track still clears, the name keeps a reason to be held.
       HARD STOP (AMENDMENT_06SEP_HARD_STOP), evaluated FIRST on every open position, regardless
       of score: exit when absolute return from entry <= -10% OR alpha <= -5%, alpha = stock
       return since entry minus NIFTY500 over the same window (raw_prices NIFTY500; base = the
       close on or before entered_at, current = the close on the stock's own latest EOD date —
       both legs on one as-of date, never a live stock price against a stale index). exit_reason
       names the leg and its value ("hard stop -10.4 pct from entry" / "alpha -5.7 pct vs
       NIFTY500"). No entry_price -> neither leg evaluable -> logged and skipped, never a pass or
       a fail. No NIFTY500 base bar -> the absolute leg only, alpha logged as not evaluable.
       A position breaching both the hard stop and score decay records the HARD STOP.

DAY RETURN source resolution (verified on the full 196 before this file was written —
196/196 have two closes in raw_prices, 186/196 have a live cmp_prices row):
  live cmp_prices row stamped TODAY  → day = cmp / latest raw close − 1  (source 'cmp_live')
  otherwise                          → last two raw_prices closes        (source 'raw_eod')
An UNCOMPUTABLE gate blocks the entry and says so in the log — the scanner never assumes a
number it does not have.

Segment month = invest_check_v2._segment_month, the IC source, imported not copied (27979:
shared grammar, one arithmetic). State opens ONLY via BUY events; signals table is append-only.
No v8_paper_* / tc_* / quant_paper_positions writes; tradewall untouched in V1 (founder call).
"""

import os
import logging
from typing import Optional

import psycopg
from psycopg.types.json import Json
from fastapi import APIRouter, Header, HTTPException

from invest_check_v2 import _segment_month   # IC's own segment-month arithmetic, not a copy
import price_resolver   # cc#1297: the ONE canonical price path (cc#343/717/811/1291) — never a
                         # second lookup, the exact mistake cc#1291 fixed elsewhere tonight.

log = logging.getLogger("scorr.inv_scanner_rules")
router = APIRouter(tags=["investment_scanner"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

ENTRY_MOM, ENTRY_REV = 85.0, 80.0
# cc#1768 (session_log 39581): exit bars 80/75 -> 75/70. The ONLY definition — every reason
# string, the (i) sheet (inv_scanner_endpoints META) and the wall's engine-rules row read these.
EXIT_MOM, EXIT_REV = 75.0, 70.0
# cc#1768 AMENDMENT_06SEP_HARD_STOP: the two hard-stop levels and the one benchmark. Read by
# hard_stop_check() below and surfaced wherever the exit rules are shown.
HARD_STOP_ABS_PCT = -10.0        # exit when (close / entry_price - 1) * 100 <= this
HARD_STOP_ALPHA_PCT = -5.0       # exit when stock return since entry - NIFTY500 return <= this
BENCHMARK_SYMBOL = "NIFTY500"    # raw_prices symbol; never another index (H5)

# cc#1767: the four price gates, NAMED ONCE. Fixed order (the page renders a capsule per gate in
# this order, so position carries meaning), the short label each capsule prints, and the rule in
# words the tooltip states. The pass/fail arithmetic itself lives in evaluate_gates() below — the
# same call run() makes at entry time and /api/inv-scanner/board makes per row, so the column and
# the engine cannot disagree.
GATE_ORDER = ("day_return", "week_return", "month_return", "segment_month")
GATE_LABELS = {"day_return": "DAY", "week_return": "WEEK", "month_return": "MO", "segment_month": "SECT"}
GATE_RULES = {"day_return": "> 0", "week_return": "0 to 5", "month_return": "0 to 7", "segment_month": "> 0"}
GATE_UNITS = {"day_return": "%", "week_return": "%", "month_return": "%", "segment_month": "% (sector month avg)"}


def evaluate_gates(cur, sym, wk, mo, segment, seg_cache=None) -> dict:
    """THE four entry gates for one symbol (session_log 30147): day return > 0 · week return in
    [0,5] · month return in [0,7] · the segment's month average > 0. Lifted verbatim out of run()
    by cc#1767 so the board column reads the SAME arithmetic and the SAME sources the entry engine
    does: day from _day_return (cmp vs last close when cmp is newer, else the last two EOD closes),
    week/month from universe_technicals (passed in), segment month from
    invest_check_v2._segment_month (the IC source, cached per segment via seg_cache). A missing
    input is value None and pass False — UNCOMPUTABLE never passes, and the caller can tell
    "failed" from "not measured" by whether value is None."""
    if seg_cache is None:
        seg_cache = {}
    day, day_src = _day_return(cur, sym)
    if segment not in seg_cache:
        seg_cache[segment] = _segment_month(cur, segment) if segment else (None, 0)
    seg_mo, seg_n = seg_cache[segment]
    return {
        "day_return":   {"value": None if day is None else round(day, 3), "source": day_src,
                         "pass": day is not None and day > 0},
        "week_return":  {"value": wk, "pass": wk is not None and 0 <= wk <= 5},
        "month_return": {"value": mo, "pass": mo is not None and 0 <= mo <= 7},
        "segment_month": {"value": None if seg_mo is None else round(seg_mo, 3),
                          "members": seg_n, "segment": segment,
                          "pass": seg_mo is not None and seg_mo > 0},
    }


def gates_passed(gates: dict) -> int:
    """How many of the four gates pass. A missing input counts as NOT passed (cc#1767 scope 6)."""
    return sum(1 for k in GATE_ORDER if (gates.get(k) or {}).get("pass"))


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


DDL = """
CREATE TABLE IF NOT EXISTS investment_scanner_state (
    symbol TEXT PRIMARY KEY,
    entered_at DATE NOT NULL,
    entry_track TEXT NOT NULL,
    entry_score NUMERIC,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','exited')),
    exited_at DATE,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS investment_scanner_signals (
    id BIGSERIAL PRIMARY KEY,
    event TEXT NOT NULL CHECK (event IN ('BUY','EXIT')),
    ts TIMESTAMPTZ DEFAULT NOW(),
    run_date DATE NOT NULL,
    symbol TEXT NOT NULL,
    track TEXT,
    score NUMERIC,
    gates JSONB
);
-- cc#1297: the engine had NO price columns at all (verified against the live schema before
-- writing this card) -- score-decay exits only, deliberately, per this file's own docstring. Day
-- P&L / Net P&L on the page need a captured entry/exit price, so it is added here.
ALTER TABLE investment_scanner_state ADD COLUMN IF NOT EXISTS entry_price NUMERIC;
ALTER TABLE investment_scanner_state ADD COLUMN IF NOT EXISTS exit_price NUMERIC;
"""


def _day_return(cur, sym):
    """(pct, source) — live cmp vs latest EOD close when cmp is stamped today, else the last
    two EOD closes. (None, reason) when uncomputable."""
    cur.execute("""SELECT close, price_date FROM raw_prices
                   WHERE symbol=%s AND close IS NOT NULL ORDER BY price_date DESC LIMIT 2""", (sym,))
    rows = cur.fetchall()
    if not rows:
        return None, "no_eod_closes"
    last_close, last_date = _f(rows[0][0]), rows[0][1]
    # column is `cmp` (checked information_schema before the fix — `price` raised on every
    # evaluation and would have killed each rules run at the first entry candidate)
    cur.execute("""SELECT cmp, updated_at FROM cmp_prices WHERE symbol=%s
                   ORDER BY updated_at DESC LIMIT 1""", (sym,))
    c = cur.fetchone()
    if c and c[0] is not None and c[1] is not None and c[1].date() > last_date:
        cmp_v = _f(c[0])
        if cmp_v and last_close:
            return (cmp_v / last_close - 1) * 100.0, "cmp_live"
    if len(rows) >= 2 and _f(rows[1][0]):
        return (last_close / _f(rows[1][0]) - 1) * 100.0, "raw_eod"
    return None, "single_close_only"


def hard_stop_check(cur, sym, entry_price, entered_at) -> dict:
    """cc#1768 H1-H5: the hard-stop leg for ONE open position, pure read. Returns a dict the
    caller can log verbatim: fired (bool), reason (the exit_reason text, None when not fired),
    abs_pct, alpha_pct, as_of (the stock's latest EOD date — both legs are measured there),
    stock_close, bench_base / bench_base_date / bench_now / bench_now_date, alpha_evaluable,
    skipped (a reason string when NOTHING could be evaluated — no entry_price, no stock close)."""
    out = {"fired": False, "reason": None, "abs_pct": None, "alpha_pct": None, "as_of": None,
           "stock_close": None, "bench_base": None, "bench_base_date": None,
           "bench_now": None, "bench_now_date": None, "alpha_evaluable": False, "skipped": None}
    entry_price = _f(entry_price)
    if not entry_price or entered_at is None:
        out["skipped"] = "no entry_price" if not entry_price else "no entered_at"   # H5: neither leg — never a pass or a fail
        return out
    cur.execute("""SELECT close, price_date FROM raw_prices
                   WHERE symbol=%s AND close IS NOT NULL ORDER BY price_date DESC LIMIT 1""", (sym,))
    r = cur.fetchone()
    if not r or not _f(r[0]):
        out["skipped"] = "no EOD close for the stock"
        return out
    close, as_of = _f(r[0]), r[1]
    out["stock_close"], out["as_of"] = close, str(as_of)
    abs_pct = (close / entry_price - 1.0) * 100.0
    out["abs_pct"] = round(abs_pct, 2)
    # H2: base = the NIFTY500 close on or before the entry date; current = the close on or before
    # the SAME as-of date the stock close carries. Both EOD, one as-of.
    cur.execute("""SELECT close, price_date FROM raw_prices
                   WHERE symbol=%s AND close IS NOT NULL AND price_date <= %s
                   ORDER BY price_date DESC LIMIT 1""", (BENCHMARK_SYMBOL, entered_at))
    b0 = cur.fetchone()
    cur.execute("""SELECT close, price_date FROM raw_prices
                   WHERE symbol=%s AND close IS NOT NULL AND price_date <= %s
                   ORDER BY price_date DESC LIMIT 1""", (BENCHMARK_SYMBOL, as_of))
    b1 = cur.fetchone()
    alpha_pct = None
    if b0 and b1 and _f(b0[0]) and _f(b1[0]):
        out.update({"bench_base": _f(b0[0]), "bench_base_date": str(b0[1]),
                    "bench_now": _f(b1[0]), "bench_now_date": str(b1[1])})
        alpha_pct = abs_pct - (_f(b1[0]) / _f(b0[0]) - 1.0) * 100.0
        out["alpha_pct"], out["alpha_evaluable"] = round(alpha_pct, 2), True
    legs = []
    if abs_pct <= HARD_STOP_ABS_PCT:
        legs.append("hard stop %.1f pct from entry" % abs_pct)
    if alpha_pct is not None and alpha_pct <= HARD_STOP_ALPHA_PCT:
        legs.append("alpha %.1f pct vs %s" % (alpha_pct, BENCHMARK_SYMBOL))
    if legs:
        out["fired"], out["reason"] = True, "; ".join(legs)   # H4: the leg and its measured value, never a bare "hard stop"
    return out


def _close_position(cur, sym, d, reason, track, score, detail):
    """The one exit write, shared by the hard-stop leg and the score-decay leg: exit_price via the
    canonical resolver (cc#1297 — same source every other surface's CMP comes from), the state row
    flipped to exited, and an EXIT signal carrying the reason + the leg's own detail."""
    exit_r = price_resolver.resolve_price(cur, sym)
    exit_px = _f(exit_r.get("price")) if exit_r else None
    cur.execute("""UPDATE investment_scanner_state
                   SET status='exited', exited_at=%s, exit_reason=%s, exit_price=%s
                   WHERE symbol=%s""", (d, reason, exit_px, sym))
    cur.execute("""INSERT INTO investment_scanner_signals
                   (event, run_date, symbol, track, score, gates)
                   VALUES ('EXIT', %s, %s, %s, %s, %s)""",
                (d, sym, track, score, Json(dict(detail, exit_reason=reason))))
    return exit_px


def run(conn=None) -> dict:
    own = conn is None
    if own:
        conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute("SELECT MAX(run_date) FROM investment_scanner_scores")
            d = cur.fetchone()[0]
            if d is None:
                return {"status": "skip", "reason": "no scores run to evaluate"}
            # idempotence: one evaluation per run_date — re-running the same night is a no-op
            cur.execute("SELECT COUNT(*) FROM investment_scanner_signals WHERE run_date=%s", (d,))
            if cur.fetchone()[0] > 0:
                return {"status": "skip", "reason": f"signals already evaluated for {d}"}
            cur.execute("""
                SELECT s.symbol, s.mom_score, s.rev_score,
                       ut.week_return, ut.month_return, g.segment
                FROM investment_scanner_scores s
                LEFT JOIN universe_technicals ut ON ut.symbol=s.symbol
                     AND ut.score_date=(SELECT MAX(score_date) FROM universe_technicals)
                LEFT JOIN (SELECT DISTINCT ON (symbol) symbol, segment FROM gvm_scores
                           ORDER BY symbol, score_date DESC) g ON g.symbol=s.symbol
                WHERE s.run_date=%s""", (d,))
            rows = cur.fetchall()
            cur.execute("""SELECT symbol, entry_track, entry_price, entered_at, entry_score
                           FROM investment_scanner_state WHERE status='open'""")
            open_rows = cur.fetchall()
            open_pos = {r[0]: r[1] for r in open_rows}

            seg_cache = {}
            buys, exits, blocked, hard_stop_log = [], [], [], []
            # ---- cc#1768 HARD STOP FIRST (H3), on EVERY open position — including one that is
            # not in tonight's scores run, since this leg does not read a score (H1). A name that
            # exits here is removed from open_pos so the score-decay pass below never re-judges
            # it; a position breaching both legs therefore records the hard stop. ----
            for sym, track, entry_price, entered_at, entry_score in open_rows:
                hs = hard_stop_check(cur, sym, entry_price, entered_at)
                entry_line = {"symbol": sym, "track": track, **{k: hs[k] for k in
                              ("abs_pct", "alpha_pct", "as_of", "alpha_evaluable", "skipped")}}
                if hs["skipped"]:
                    log.info("inv_scanner_rules hard stop skipped for %s: %s", sym, hs["skipped"])   # H5: logged, never a pass or a fail
                elif not hs["alpha_evaluable"]:
                    log.info("inv_scanner_rules hard stop: alpha not evaluable for %s (no %s base bar); absolute leg only",
                             sym, BENCHMARK_SYMBOL)
                if hs["fired"]:
                    exit_px = _close_position(cur, sym, d, hs["reason"], track, _f(entry_score),
                                              {"leg": "hard_stop", "abs_pct": hs["abs_pct"], "alpha_pct": hs["alpha_pct"],
                                               "as_of": hs["as_of"], "stock_close": hs["stock_close"],
                                               "bench": BENCHMARK_SYMBOL, "bench_base": hs["bench_base"],
                                               "bench_base_date": hs["bench_base_date"], "bench_now": hs["bench_now"],
                                               "bench_now_date": hs["bench_now_date"],
                                               "levels": {"abs_pct": HARD_STOP_ABS_PCT, "alpha_pct": HARD_STOP_ALPHA_PCT}})
                    exits.append({"symbol": sym, "reason": hs["reason"], "leg": "hard_stop", "exit_price": exit_px})
                    open_pos.pop(sym, None)
                    entry_line["fired"] = True
                hard_stop_log.append(entry_line)
            for sym, mom, rev, wk, mo, segment in rows:
                mom, rev, wk, mo = _f(mom), _f(rev), _f(wk), _f(mo)
                # ---- EXIT (score decay): open names judged on tonight's scores, bars EXIT_MOM /
                # EXIT_REV (cc#1768: 75 / 70). The reason string prints the bar that fired. ----
                if sym in open_pos:
                    tracks = open_pos[sym].split("+")
                    dead = []
                    for t in tracks:
                        if t == "momentum" and (mom is None or mom < EXIT_MOM):
                            dead.append(f"momentum {mom} < {EXIT_MOM}")
                        if t == "reversal" and (rev is None or rev < EXIT_REV):
                            dead.append(f"reversal {rev} < {EXIT_REV}")
                    if len(dead) == len(tracks):        # ALL entered tracks decayed
                        reason = "; ".join(dead)
                        _close_position(cur, sym, d, reason, open_pos[sym],
                                        mom if "momentum" in tracks else rev,
                                        {"leg": "score_decay", "mom_score": mom, "rev_score": rev,
                                         "bars": {"momentum": EXIT_MOM, "reversal": EXIT_REV}})
                        exits.append({"symbol": sym, "reason": reason, "leg": "score_decay"})
                    continue                             # an open name is never re-entered
                # ---- ENTRY ----
                tracks = []
                if mom is not None and mom > ENTRY_MOM:
                    tracks.append("momentum")
                if rev is not None and rev > ENTRY_REV:
                    tracks.append("reversal")
                if not tracks:
                    continue
                # cc#1767: the gate arithmetic moved into evaluate_gates() (identical dict, same
                # sources) so the board column calls the very same function this entry check does.
                gates = evaluate_gates(cur, sym, wk, mo, segment, seg_cache)
                all_pass = all(gx["pass"] for gx in gates.values())
                score = mom if "momentum" in tracks else rev
                if all_pass:
                    track = "+".join(tracks)
                    # cc#1297: capture entry_price via the canonical resolver. day_return above
                    # already resolves a price for the gate check but does not persist it — this
                    # is a fresh, current-at-signal-time resolve, not a reuse of a stale gate value.
                    entry_r = price_resolver.resolve_price(cur, sym)
                    entry_px = _f(entry_r.get("price")) if entry_r else None
                    cur.execute("""INSERT INTO investment_scanner_state
                                   (symbol, entered_at, entry_track, entry_score, status, entry_price)
                                   VALUES (%s,%s,%s,%s,'open',%s)
                                   ON CONFLICT (symbol) DO UPDATE SET
                                     entered_at=EXCLUDED.entered_at, entry_track=EXCLUDED.entry_track,
                                     entry_score=EXCLUDED.entry_score, status='open',
                                     exited_at=NULL, exit_reason=NULL,
                                     entry_price=EXCLUDED.entry_price, exit_price=NULL""",
                                (sym, d, track, score, entry_px))
                    cur.execute("""INSERT INTO investment_scanner_signals
                                   (event, run_date, symbol, track, score, gates)
                                   VALUES ('BUY', %s, %s, %s, %s, %s)""",
                                (d, sym, track, score, Json(gates)))
                    buys.append({"symbol": sym, "track": track, "score": score})
                else:
                    failed = [k for k, gx in gates.items() if not gx["pass"]]
                    blocked.append({"symbol": sym, "tracks": tracks, "score": score,
                                    "failed_gates": failed, "gates": gates})
        conn.commit()
        out = {"status": "ok", "run_date": str(d), "buys": buys, "exits": exits,
               "blocked": blocked[:25], "blocked_n": len(blocked),
               "hard_stop": hard_stop_log,   # cc#1768: every open position's two legs, fired or not, skipped when unevaluable
               "exit_bars": {"momentum": EXIT_MOM, "reversal": EXIT_REV,
                             "hard_stop_abs_pct": HARD_STOP_ABS_PCT, "hard_stop_alpha_pct": HARD_STOP_ALPHA_PCT,
                             "benchmark": BENCHMARK_SYMBOL}}
        log.info(f"inv_scanner_rules: buys={len(buys)} exits={len(exits)} blocked={len(blocked)}")
        return out
    finally:
        if own:
            conn.close()


@router.post("/api/admin/run-inv-scanner-rules")
def admin_run(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return run()


@router.get("/api/inv-scanner/signals")
def get_signals(limit: int = 100):
    """Latest signals, newest first, gates included — feed for cc#1286/1287."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT event, ts, run_date, symbol, track, score, gates
                       FROM investment_scanner_signals ORDER BY ts DESC, id DESC LIMIT %s""",
                    (max(1, min(limit, 500)),))
        rows = [{"event": r[0], "ts": str(r[1]), "run_date": str(r[2]), "symbol": r[3],
                 "track": r[4], "score": _f(r[5]), "gates": r[6]} for r in cur.fetchall()]
    return {"count": len(rows), "rows": rows}


@router.get("/api/inv-scanner/state")
def get_state():
    """Open + closed scanner positions (score-decay + cc#1768 hard-stop book). cc#1297: adds entry/exit price and
    P&L, all computed HERE from stored prices + a live resolve — the page computes nothing, same
    discipline as every other surface in this codebase. Long-only (V1 writes BUY only, cc#1295)."""
    with _conn() as conn, conn.cursor() as cur:
        # cc#1297: self-healing schema-ensure, same as run() — this endpoint can be hit by the
        # page before the nightly job (02:40 IST) has run DDL even once post-deploy.
        cur.execute(DDL)
        conn.commit()
        cur.execute("""SELECT symbol, entered_at, entry_track, entry_score, status, exited_at,
                              exit_reason, entry_price, exit_price
                       FROM investment_scanner_state ORDER BY status, entered_at DESC""")
        raw = cur.fetchall()
        rows = []
        for r in raw:
            symbol, entered_at, entry_track, entry_score, status, exited_at, exit_reason, entry_price, exit_price = r
            entry_price, exit_price = _f(entry_price), _f(exit_price)
            row = {"symbol": symbol, "entered_at": str(entered_at), "entry_track": entry_track,
                   "entry_score": _f(entry_score), "status": status,
                   "exited_at": str(exited_at) if exited_at else None, "exit_reason": exit_reason,
                   "entry_price": entry_price, "exit_price": exit_price,
                   "cmp": None, "day_pnl_pct": None, "net_pnl_pct": None}
            if status == "open":
                # cc#1291-style: the ONE resolver, at request time, never stored/stale.
                pr = price_resolver.resolve_price(cur, symbol)
                cmp_v = _f(pr.get("price")) if pr else None
                row["cmp"] = cmp_v
                prev_px, _prev_date = price_resolver.latest_completed_close(cur, symbol)
                if cmp_v is not None and prev_px:
                    row["day_pnl_pct"] = round((cmp_v / prev_px - 1) * 100.0, 2)
                # cc#1297 schema_gap_flagged_first: the one legacy row (LGBBROSLTD, entered before
                # this card) predates price capture and has no entry_price. NOT backfilled with a
                # reconstructed guess — net_pnl_pct stays None (renders em-dash) until it exits.
                if cmp_v is not None and entry_price:
                    row["net_pnl_pct"] = round((cmp_v / entry_price - 1) * 100.0, 2)
            elif status == "exited" and entry_price and exit_price:
                row["net_pnl_pct"] = round((exit_price / entry_price - 1) * 100.0, 2)
            rows.append(row)
    return {"count": len(rows), "rows": rows}
