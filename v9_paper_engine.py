"""
v9_paper_engine.py — cc#629 V9 SECTOR PAIRS "BRAHMASTRA" paper engine (Spec V3, session_log 8169).
================================================================================================
PAPER-ONLY, monthly, sector-neutral momentum pairs. STRICTLY separate book (v9_paper_* tables) —
never touches v8_paper or any V8 table (context-isolation rule 7).

SELECTION (monthly, first trading day, run from the nightly chain ~01:50 IST after GVM 01:30):
  1. Universe = futures_universe.is_active (~208). Eligible sectors = gvm_scores/gvm_history segments
     with >4 futures stocks.
  2. Per symbol, momentum M = gvm_history.m_score at: now (<=asof), M10 (<=asof-10), M42 (<=asof-42),
     nearest-prior. LONG candidate: M_now > M10 AND M_now > M42 (rising over BOTH). SHORT: M_now < both.
  3. LIVE dV overlay (live-only condition — untestable historically, backfill V flat): LONG requires
     V_now >= V10 AND V_now >= V42 with >=1 strict >; SHORT mirrored (<=, >=1 strict <). A candidate
     that passes M but FAILS ONLY the dV gate is logged (reason=dv_gate) for evidence, NOT entered.
  4. dm_mag = (M_now-M10)+(M_now-M42). Per sector: steepest improver (max dm_mag) LONG vs steepest
     decliner (min dm_mag) SHORT. RANK-1 ONLY, max 1 pair/sector (rank-2 dilutes 1.83 vs 2.70).
  5. Hard filter: gvm(long)-gvm(short) BETWEEN 1.0 AND 2.0 (below 1 no edge, above 2 inverts).
  6. Skip a sector if either leg is empty after the gates. Zero qualifying pairs = a valid CASH month.

EXECUTION: 1 lot each leg (lot_size from futures_universe). Entry/exit = EOD close (raw_prices).
  HOLD to the next monthly rebalance — NO intra-month exit, NO distortion take-profit, NO stop
  (spec: distortion IS the trend; a z2 exit sells winners). The monthly run first CLOSES the prior
  month's open pairs at EOD close (-> v9_paper_trades), then opens the new selection. Idempotent per
  rebalance_date (rerun-safe: same date re-run is a no-op once positions exist).

Backtest context (session_log 8169, PAPER is the judge): 57mo +2.70%/mo, 69% pos months, Sharpe 1.63.
"""
import logging
import os
from datetime import date, datetime

import psycopg2
import psycopg2.extras

log = logging.getLogger("scorr.v9_paper")

GVM_GAP_MIN, GVM_GAP_MAX = 1.0, 2.0     # hard filter band (spec)
LOOKBACK_SHORT, LOOKBACK_LONG = 10, 42  # champion ridge 10+42
MIN_SECTOR_STOCKS = 4                    # eligible sectors have >4 futures stocks

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS v9_paper_positions (
    id              BIGSERIAL PRIMARY KEY,
    rebalance_date  DATE NOT NULL,
    segment         TEXT NOT NULL,
    long_symbol     TEXT NOT NULL,
    short_symbol    TEXT NOT NULL,
    long_lot        INTEGER,
    short_lot       INTEGER,
    long_entry      NUMERIC,
    short_entry     NUMERIC,
    gvm_long        NUMERIC,
    gvm_short       NUMERIC,
    gvm_gap         NUMERIC,
    dm_mag_long     NUMERIC,
    dm_mag_short    NUMERIC,
    status          TEXT DEFAULT 'OPEN',
    entry_date      DATE,
    exit_date       DATE,
    long_exit       NUMERIC,
    short_exit      NUMERIC,
    spread_pnl      NUMERIC,
    ret_pct         NUMERIC,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (rebalance_date, segment)
);
CREATE INDEX IF NOT EXISTS idx_v9pp_status ON v9_paper_positions(status, rebalance_date DESC);
CREATE TABLE IF NOT EXISTS v9_paper_trades (
    id              BIGSERIAL PRIMARY KEY,
    position_id     BIGINT,
    rebalance_date  DATE,
    exit_date       DATE,
    segment         TEXT,
    long_symbol     TEXT,
    short_symbol    TEXT,
    long_lot        INTEGER,
    short_lot       INTEGER,
    long_entry      NUMERIC, long_exit  NUMERIC,
    short_entry     NUMERIC, short_exit NUMERIC,
    long_pnl        NUMERIC, short_pnl  NUMERIC,
    spread_pnl      NUMERIC, ret_pct    NUMERIC,
    hold_days       INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_v9pt_exit ON v9_paper_trades(exit_date DESC);
"""


def _conn():
    return psycopg2.connect(os.getenv("DATABASE_URL", ""))


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def _f(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _oplog(cur, title, details):
    import json
    cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                   VALUES (CURRENT_DATE, NOW(), 'v9_paper', %s, %s::jsonb)""",
                (title, json.dumps(details, default=str)))


def _closes(cur, symbols, asof):
    """Latest raw_prices close on/before asof per symbol."""
    if not symbols:
        return {}
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices
                   WHERE symbol = ANY(%s) AND price_date <= %s AND close IS NOT NULL
                   ORDER BY symbol, price_date DESC""", (list(symbols), asof))
    return {r[0]: _f(r[1]) for r in cur.fetchall()}


def select_pairs(cur, asof):
    """Return (pairs, rejects). pairs = list of dicts (segment, long/short symbol + gvm + dm_mag +
    lot). rejects = dv_gate candidates (passed M, failed only dV) for evidence logging."""
    # universe with now / -10 / -42 momentum + value, nearest-prior per symbol
    cur.execute("""
        WITH uni AS (SELECT symbol, lot_size FROM futures_universe WHERE is_active = TRUE),
        n AS (SELECT DISTINCT ON (symbol) symbol, segment, m_score, v_score, gvm_score
              FROM gvm_history WHERE score_date <= %(a)s ORDER BY symbol, score_date DESC),
        s10 AS (SELECT DISTINCT ON (symbol) symbol, m_score m10, v_score v10
                FROM gvm_history WHERE score_date <= (%(a)s::date - %(s)s) ORDER BY symbol, score_date DESC),
        s42 AS (SELECT DISTINCT ON (symbol) symbol, m_score m42, v_score v42
                FROM gvm_history WHERE score_date <= (%(a)s::date - %(l)s) ORDER BY symbol, score_date DESC)
        SELECT u.symbol, u.lot_size, n.segment, n.gvm_score, n.m_score, n.v_score,
               s10.m10, s10.v10, s42.m42, s42.v42
        FROM uni u JOIN n ON n.symbol = u.symbol
                   LEFT JOIN s10 ON s10.symbol = u.symbol
                   LEFT JOIN s42 ON s42.symbol = u.symbol
        WHERE n.segment IS NOT NULL
    """, {"a": asof, "s": LOOKBACK_SHORT, "l": LOOKBACK_LONG})
    by_seg = {}
    for sym, lot, seg, gvm, m, v, m10, v10, m42, v42 in cur.fetchall():
        m, v, m10, v10, m42, v42, gvm = map(_f, (m, v, m10, v10, m42, v42, gvm))
        if None in (m, m10, m42) or gvm is None:
            continue
        by_seg.setdefault(seg, []).append({
            "symbol": sym, "lot": int(lot) if lot else 1, "gvm": gvm,
            "m": m, "v": v, "m10": m10, "v10": v10, "m42": m42, "v42": v42,
            "dm_mag": (m - m10) + (m - m42)})

    def _dv_ok(x, is_long):
        v, v10, v42 = x["v"], x["v10"], x["v42"]
        if None in (v, v10, v42):
            return False   # live dV gate needs value; missing => not entered (logged as dv_gate)
        if is_long:
            return v >= v10 and v >= v42 and (v > v10 or v > v42)
        return v <= v10 and v <= v42 and (v < v10 or v < v42)

    pairs, rejects = [], []
    for seg, rows in by_seg.items():
        if len(rows) <= MIN_SECTOR_STOCKS:      # eligible sectors have >4 futures stocks
            continue
        longs_m = [x for x in rows if x["m"] > x["m10"] and x["m"] > x["m42"]]
        shorts_m = [x for x in rows if x["m"] < x["m10"] and x["m"] < x["m42"]]
        longs = [x for x in longs_m if _dv_ok(x, True)]
        shorts = [x for x in shorts_m if _dv_ok(x, False)]
        # dv_gate evidence: passed M but failed ONLY the dV overlay
        for x in longs_m:
            if x not in longs:
                rejects.append({"segment": seg, "symbol": x["symbol"], "side": "long", "reason": "dv_gate"})
        for x in shorts_m:
            if x not in shorts:
                rejects.append({"segment": seg, "symbol": x["symbol"], "side": "short", "reason": "dv_gate"})
        if not longs or not shorts:             # skip sector if either leg empty
            continue
        lg = max(longs, key=lambda x: x["dm_mag"])    # steepest improver
        sh = min(shorts, key=lambda x: x["dm_mag"])   # steepest decliner (most negative)
        gap = round(lg["gvm"] - sh["gvm"], 3)
        if not (GVM_GAP_MIN <= gap <= GVM_GAP_MAX):   # hard filter band
            continue
        pairs.append({"segment": seg, "long": lg, "short": sh, "gvm_gap": gap})
    return pairs, rejects


def _close_open_positions(cur, asof):
    """Close every currently-OPEN pair at the asof EOD close -> v9_paper_trades. Returns count."""
    cur.execute("""SELECT id, rebalance_date, segment, long_symbol, short_symbol, long_lot, short_lot,
                          long_entry, short_entry FROM v9_paper_positions WHERE status='OPEN'""")
    open_rows = cur.fetchall()
    if not open_rows:
        return 0
    syms = {r[3] for r in open_rows} | {r[4] for r in open_rows}
    px = _closes(cur, syms, asof)
    closed = 0
    for pid, rdate, seg, lsym, ssym, llot, slot, lent, sent in open_rows:
        lex, sex = px.get(lsym), px.get(ssym)
        lent, sent, llot, slot = _f(lent), _f(sent), int(llot or 1), int(slot or 1)
        if lex is None or sex is None or lent is None or sent is None:
            continue   # cannot mark without both legs; leave OPEN for the next run
        long_pnl = (lex - lent) * llot
        short_pnl = (sent - sex) * slot
        spread = long_pnl + short_pnl
        notional = lent * llot + sent * slot
        ret = round(spread / notional * 100.0, 4) if notional else 0.0
        hold = (asof - rdate).days if rdate else None
        cur.execute("""UPDATE v9_paper_positions SET status='CLOSED', exit_date=%s, long_exit=%s,
                          short_exit=%s, spread_pnl=%s, ret_pct=%s WHERE id=%s""",
                    (asof, lex, sex, round(spread, 2), ret, pid))
        cur.execute("""INSERT INTO v9_paper_trades
            (position_id, rebalance_date, exit_date, segment, long_symbol, short_symbol, long_lot,
             short_lot, long_entry, long_exit, short_entry, short_exit, long_pnl, short_pnl,
             spread_pnl, ret_pct, hold_days)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (pid, rdate, asof, seg, lsym, ssym, llot, slot, lent, lex, sent, sex,
                     round(long_pnl, 2), round(short_pnl, 2), round(spread, 2), ret, hold))
        closed += 1
    return closed


def run_monthly(conn=None, asof: date = None) -> dict:
    """Monthly rebalance: idempotent per rebalance_date. Close prior open pairs at EOD, select the new
    Brahmastra pairs, open them at EOD close. Zero pairs = a valid cash month (still logged)."""
    own = conn is None
    conn = conn or _conn()
    try:
        ensure_schema(conn)
        asof = asof or date.today()
        with conn.cursor() as cur:
            # idempotent: one rebalance per CALENDAR MONTH (rerun-safe on the same date AND across a
            # scheduler restart within the first week) -> no-op if this month already rebalanced.
            cur.execute("""SELECT COUNT(*) FROM v9_paper_positions
                           WHERE date_trunc('month', rebalance_date) = date_trunc('month', %s::date)""",
                        (asof,))
            if cur.fetchone()[0] > 0:
                conn.rollback()
                return {"asof": str(asof), "status": "already_run_this_month", "opened": 0}
            closed = _close_open_positions(cur, asof)
            pairs, rejects = select_pairs(cur, asof)
            entry_syms = {p["long"]["symbol"] for p in pairs} | {p["short"]["symbol"] for p in pairs}
            px = _closes(cur, entry_syms, asof)
            opened = 0
            skipped_price = []
            for p in pairs:
                lg, sh = p["long"], p["short"]
                lent, sent = px.get(lg["symbol"]), px.get(sh["symbol"])
                if lent is None or sent is None:
                    skipped_price.append(p["segment"])
                    continue
                cur.execute("""INSERT INTO v9_paper_positions
                    (rebalance_date, segment, long_symbol, short_symbol, long_lot, short_lot,
                     long_entry, short_entry, gvm_long, gvm_short, gvm_gap, dm_mag_long, dm_mag_short,
                     status, entry_date)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)
                    ON CONFLICT (rebalance_date, segment) DO NOTHING""",
                            (asof, p["segment"], lg["symbol"], sh["symbol"], lg["lot"], sh["lot"],
                             round(lent, 2), round(sent, 2), round(lg["gvm"], 3), round(sh["gvm"], 3),
                             p["gvm_gap"], round(lg["dm_mag"], 4), round(sh["dm_mag"], 4), asof))
                opened += 1
            summary = {"asof": str(asof), "closed_prior": closed, "opened": opened,
                       "cash_month": opened == 0, "dv_gate_rejects": len(rejects),
                       "skipped_price": skipped_price,
                       "pairs": [{"segment": p["segment"], "long": p["long"]["symbol"],
                                  "short": p["short"]["symbol"], "gvm_gap": p["gvm_gap"]} for p in pairs]}
            _oplog(cur, "V9_PAPER_REBALANCE", summary)
            conn.commit()
            return {"status": "ok", **summary}
    except Exception as e:
        conn.rollback()
        log.error(f"v9 run_monthly: {e}", exc_info=True)
        raise
    finally:
        if own:
            conn.close()
