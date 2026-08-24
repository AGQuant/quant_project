"""cc#1285 · INVESTMENT SCANNER ENGINE 3/3 — ENTRY/EXIT RULES (spec session_log 30147).

ENTRY  BUY when (mom_score > 85 OR rev_score > 80) AND ALL four price gates:
         day_return > 0 · week_return in [0,5] · month_return in [0,7] · segment month avg > 0.
       The gates exist to stop the scanner chasing extended names — a 99-score name up 35% on
       the month is exactly what the month gate is FOR. Every gate's input, source and pass/fail
       is logged in the signal row's gates jsonb; the signal carries its qualifying track(s).

EXIT   momentum-entered name: mom_score < 80 → EXIT. reversal-entered: rev_score < 75.
       5-point hysteresis per spec; score-decay exits ONLY in V1 (no SL/target legs).
       DUAL-track entries exit when BOTH tracks are below their thresholds — while either track
       still clears, the name keeps a reason to be held. (Interpretation logged on the card.)

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

log = logging.getLogger("scorr.inv_scanner_rules")
router = APIRouter(tags=["investment_scanner"])

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

ENTRY_MOM, ENTRY_REV = 85.0, 80.0
EXIT_MOM, EXIT_REV = 80.0, 75.0


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
            cur.execute("SELECT symbol, entry_track FROM investment_scanner_state WHERE status='open'")
            open_pos = dict(cur.fetchall())

            seg_cache = {}
            buys, exits, blocked = [], [], []
            for sym, mom, rev, wk, mo, segment in rows:
                mom, rev, wk, mo = _f(mom), _f(rev), _f(wk), _f(mo)
                # ---- EXIT first: open names judged on tonight's scores ----
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
                        cur.execute("""UPDATE investment_scanner_state
                                       SET status='exited', exited_at=%s, exit_reason=%s
                                       WHERE symbol=%s""", (d, reason, sym))
                        cur.execute("""INSERT INTO investment_scanner_signals
                                       (event, run_date, symbol, track, score, gates)
                                       VALUES ('EXIT', %s, %s, %s, %s, %s)""",
                                    (d, sym, open_pos[sym],
                                     mom if "momentum" in tracks else rev,
                                     Json({"exit_reason": reason,
                                           "mom_score": mom, "rev_score": rev})))
                        exits.append({"symbol": sym, "reason": reason})
                    continue                             # an open name is never re-entered
                # ---- ENTRY ----
                tracks = []
                if mom is not None and mom > ENTRY_MOM:
                    tracks.append("momentum")
                if rev is not None and rev > ENTRY_REV:
                    tracks.append("reversal")
                if not tracks:
                    continue
                day, day_src = _day_return(cur, sym)
                if segment not in seg_cache:
                    seg_cache[segment] = _segment_month(cur, segment) if segment else (None, 0)
                seg_mo, seg_n = seg_cache[segment]
                gates = {
                    "day_return":   {"value": None if day is None else round(day, 3), "source": day_src,
                                     "pass": day is not None and day > 0},
                    "week_return":  {"value": wk, "pass": wk is not None and 0 <= wk <= 5},
                    "month_return": {"value": mo, "pass": mo is not None and 0 <= mo <= 7},
                    "segment_month": {"value": None if seg_mo is None else round(seg_mo, 3),
                                      "members": seg_n, "segment": segment,
                                      "pass": seg_mo is not None and seg_mo > 0},
                }
                all_pass = all(gx["pass"] for gx in gates.values())
                score = mom if "momentum" in tracks else rev
                if all_pass:
                    track = "+".join(tracks)
                    cur.execute("""INSERT INTO investment_scanner_state
                                   (symbol, entered_at, entry_track, entry_score, status)
                                   VALUES (%s,%s,%s,%s,'open')
                                   ON CONFLICT (symbol) DO UPDATE SET
                                     entered_at=EXCLUDED.entered_at, entry_track=EXCLUDED.entry_track,
                                     entry_score=EXCLUDED.entry_score, status='open',
                                     exited_at=NULL, exit_reason=NULL""",
                                (sym, d, track, score))
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
               "blocked": blocked[:25], "blocked_n": len(blocked)}
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
    """Open scanner positions (score-decay book, V1)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT symbol, entered_at, entry_track, entry_score, status, exited_at, exit_reason
                       FROM investment_scanner_state ORDER BY status, entered_at DESC""")
        rows = [{"symbol": r[0], "entered_at": str(r[1]), "entry_track": r[2],
                 "entry_score": _f(r[3]), "status": r[4],
                 "exited_at": str(r[5]) if r[5] else None, "exit_reason": r[6]}
                for r in cur.fetchall()]
    return {"count": len(rows), "rows": rows}
