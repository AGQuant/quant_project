"""
v14_engine.py — V14 INTRADAY ENGINE (P1, PAPER ONLY)
=====================================================
Canonical spec: session_log id=3060 (V14_INTRADAY_ENGINE_SPEC_V1_LOCKED, 12-Jul-2026).
Thresholds below are SPEC-LOCKED — tune only via founder-approved BT7 evidence later.

Three tagged intraday setups, all sides paper-traded within clock windows:
  ORB           — opening-range breakout (both sides)
  VWAP-RECLAIM  — reclaim of VWAP after a shallow dip (both sides)
  R1-REJ        — rejection at R1 (short) / S1-bounce mirror (long)

Common gates G1-G5, uniform bracket exits, full trade log with a gates snapshot at
entry for evening per-tag analysis. Zero writes to V8/V10 — read-only on v10 state.
No capital rules yet; P&L is in points + %, with Rs 500 flat + 0.05% slippage recorded.
"""

import json
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Dict, List, Optional, Tuple

import psycopg

log = logging.getLogger("scorr.v14")
IST = timezone(timedelta(hours=5, minutes=30))

# ── spec-locked constants (id=3060) ──────────────────────────────────────────────
MAX_SLOTS        = 4            # G4: max concurrent
ATR_PERIOD       = 20           # ATR(5m, 20)
ATR_MULT         = 1.0          # target = entry +/- 1.0x ATR, capped at nearest pivot
TIME_STOP_MIN    = 30           # <+0.3% after 30 min -> market exit
TIME_STOP_PCT    = 0.3
TRAIL_TRIGGER    = 0.5          # at +0.5% move stop to breakeven
SQUAREOFF        = (15, 15)     # hard square-off 15:15 IST
COST_FLAT        = 500.0        # Rs per trade
COST_SLIPPAGE    = 0.05         # % round-trip slippage
TOP_N_TURNOVER   = 80           # G2 liquidity: top-80 by turnover
# G3 clock windows (entries only)
CLOCK_WINDOWS    = [((9, 30), (11, 0)), ((13, 45), (14, 45))]
TAGS             = ("ORB", "VWAP-RECLAIM", "R1-REJ")


def _now() -> datetime:
    return datetime.now(IST)


def _f(x) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


# ── schema ───────────────────────────────────────────────────────────────────────
def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS v14_trades (
                id SERIAL PRIMARY KEY,
                trade_date DATE NOT NULL, tag TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                entry_ts TIMESTAMPTZ, entry_px NUMERIC, exit_ts TIMESTAMPTZ, exit_px NUMERIC,
                stop_px NUMERIC, target_px NUMERIC, atr NUMERIC,
                gates_snapshot JSONB, exit_reason TEXT,
                pnl_pts NUMERIC, pnl_pct NUMERIC, net_pnl_pct NUMERIC,
                cost_flat NUMERIC DEFAULT 500, cost_slippage_pct NUMERIC DEFAULT 0.05,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS v14_watchlist (
                id SERIAL PRIMARY KEY,
                trade_date DATE NOT NULL, tag TEXT NOT NULL, symbol TEXT NOT NULL,
                first_seen_ts TIMESTAMPTZ DEFAULT NOW(), touches INTEGER DEFAULT 1,
                notes TEXT,
                UNIQUE (trade_date, tag, symbol)
            );
            CREATE INDEX IF NOT EXISTS ix_v14_trades_date ON v14_trades(trade_date);
            CREATE INDEX IF NOT EXISTS ix_v14_trades_status ON v14_trades(status);
        """)
    conn.commit()


# ── as-of session (last date with fyers_eq 5m bars: today live, else last session) ─
def _asof_date(cur) -> Optional[date]:
    cur.execute("SELECT MAX(ts::date) FROM intraday_prices WHERE source='fyers_eq' AND timeframe='5m'")
    r = cur.fetchone()
    return r[0] if r else None


def _bars(cur, sym: str, d: date) -> List[Tuple]:
    """(ts, open, high, low, close, volume) for a symbol on date d, 09:15+, ascending."""
    cur.execute("""SELECT ts, open, high, low, close, volume FROM intraday_prices
                   WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m'
                     AND ts::date=%s AND ts::time>='09:15:00'
                   ORDER BY ts ASC""", (sym, d))
    return cur.fetchall()


def _prior_dates(cur, sym: str, d: date, n: int = 6) -> List[date]:
    cur.execute("""SELECT DISTINCT ts::date FROM intraday_prices
                   WHERE symbol=%s AND source='fyers_eq' AND timeframe='5m' AND ts::date < %s
                   ORDER BY ts::date DESC LIMIT %s""", (sym, d, n))
    return [r[0] for r in cur.fetchall()]


# ── per-symbol intraday features ──────────────────────────────────────────────────
def _vwap(bars: List[Tuple]) -> Optional[float]:
    num = den = 0.0
    for ts, o, h, l, c, v in bars:
        c = _f(c); vol = _f(v) or 0.0
        if c is None:
            continue
        num += c * vol; den += vol
    return round(num / den, 2) if den else None


def _atr(bars: List[Tuple], period: int = ATR_PERIOD) -> Optional[float]:
    if len(bars) < 2:
        return None
    trs = []
    for i in range(1, len(bars)):
        h = _f(bars[i][2]); l = _f(bars[i][3]); pc = _f(bars[i - 1][4])
        if None in (h, l, pc):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    window = trs[-period:]
    return round(sum(window) / len(window), 2)


def _opening_range(bars: List[Tuple]) -> Tuple[Optional[float], Optional[float]]:
    """09:15-09:30 opening range high/low (the 09:15, 09:20, 09:25 bars)."""
    hi = lo = None
    for ts, o, h, l, c, v in bars:
        t = ts.time()
        if t.hour == 9 and t.minute < 30:
            h = _f(h); l = _f(l)
            if h is not None:
                hi = h if hi is None else max(hi, h)
            if l is not None:
                lo = l if lo is None else min(lo, l)
    return hi, lo


def _volx(cur, sym: str, d: date, bars: List[Tuple], priors: List[date]) -> Optional[float]:
    if not bars:
        return None
    last_t = bars[-1][0].time()
    today_cum = sum((_f(b[5]) or 0.0) for b in bars)
    if today_cum <= 0:
        return None
    pcs = []
    for pd_ in priors[:5]:
        pb = _bars(cur, sym, pd_)
        cum = sum((_f(b[5]) or 0.0) for b in pb if b[0].time() <= last_t)
        if cum > 0:
            pcs.append(cum)
    if not pcs:
        return None
    avg = sum(pcs) / len(pcs)
    return round(today_cum / avg, 2) if avg else None


# ── shared per-cycle context loaded once ──────────────────────────────────────────
def load_context(cur) -> Dict:
    """Everything shared across symbols in one cycle: as-of date, prev closes, pivots, basis,
    v10 NIFTY regime, sector day%, top-80 turnover set, blackout set."""
    d = _asof_date(cur)
    ctx: Dict = {"asof": d, "prev_close": {}, "pivots": {}, "basis": {}, "sector_day": {},
                 "seg_by_sym": {}, "top80": set(), "blackout": set(), "v10_long": None, "v10_short": None}
    if d is None:
        return ctx
    # prev-day closes
    cur.execute("SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices WHERE price_date < %s "
                "ORDER BY symbol, price_date DESC", (d,))
    ctx["prev_close"] = {r[0]: _f(r[1]) for r in cur.fetchall()}
    # pivots (latest)
    cur.execute("SELECT DISTINCT ON (symbol) symbol, pp, r1, s1, r2, s2 FROM v8_paper_pivots "
                "ORDER BY symbol, pivot_date DESC")
    ctx["pivots"] = {r[0]: {"pp": _f(r[1]), "r1": _f(r[2]), "s1": _f(r[3]), "r2": _f(r[4]), "s2": _f(r[5])}
                     for r in cur.fetchall()}
    # basis (latest per symbol)
    cur.execute("SELECT DISTINCT ON (symbol) symbol, basis, basis_pct, oi, oi_prev, oi_chg "
                "FROM futures_basis ORDER BY symbol, ts DESC")
    ctx["basis"] = {r[0]: {"basis": _f(r[1]), "pct": _f(r[2]), "oi": _f(r[3]), "oi_prev": _f(r[4]),
                           "oi_chg": _f(r[5])} for r in cur.fetchall()}
    # segment map + mcap-weighted sector day% (cc#429 convention)
    cur.execute("""WITH mem AS (
                      SELECT g.segment, m.symbol, m.day_1d::numeric d1, g.market_cap::numeric mc
                      FROM v8_metrics m JOIN gvm_scores g ON g.symbol=m.symbol
                      WHERE m.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                        AND g.segment IS NOT NULL AND m.day_1d IS NOT NULL
                        AND g.market_cap IS NOT NULL AND g.market_cap>0)
                   SELECT segment, ROUND(SUM(d1*mc)/NULLIF(SUM(mc),0),2) FROM mem GROUP BY segment""")
    ctx["sector_day"] = {r[0]: _f(r[1]) for r in cur.fetchall()}
    cur.execute("SELECT symbol, segment FROM gvm_scores WHERE segment IS NOT NULL")
    ctx["seg_by_sym"] = {r[0]: r[1] for r in cur.fetchall()}
    # G2: top-80 by today's turnover (as-of session)
    cur.execute("""SELECT symbol FROM (
                     SELECT symbol, SUM(close*volume) turnover FROM intraday_prices
                     WHERE source='fyers_eq' AND timeframe='5m' AND ts::date=%s
                       AND symbol IN (SELECT symbol FROM futures_universe WHERE is_active=TRUE)
                     GROUP BY symbol ORDER BY turnover DESC NULLS LAST LIMIT %s) x""", (d, TOP_N_TURNOVER))
    ctx["top80"] = {r[0] for r in cur.fetchall()}
    # G5: results blackout (T / T+1)
    cur.execute("SELECT UPPER(ticker) FROM earnings_calendar WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + 1)")
    ctx["blackout"] = {r[0] for r in cur.fetchall()}
    # G1: V10 NIFTY regime — long only when V10 NIFTY is long; shorts mirror
    cur.execute("SELECT side FROM v10_positions WHERE symbol='NIFTY50' AND leg='FUT' AND status='OPEN' LIMIT 1")
    vr = cur.fetchone()
    if vr:
        s = (vr[0] or "").upper()
        ctx["v10_long"] = (s == "BUY"); ctx["v10_short"] = (s == "SELL")
    return ctx


# ── common gates ──────────────────────────────────────────────────────────────────
def _in_clock() -> bool:
    n = _now(); hm = (n.hour, n.minute)
    for (a, b) in CLOCK_WINDOWS:
        if a <= hm < b:
            return True
    return False


def _regime_ok(ctx: Dict, side: str) -> bool:
    # G1: long only when V10 NIFTY long; short only when V10 NIFTY short. Unknown -> block (safe).
    if side == "long":
        return bool(ctx.get("v10_long"))
    return bool(ctx.get("v10_short"))


def _rnd(v, d=2):
    return round(v, d) if isinstance(v, (int, float)) else v


# The real per-symbol evaluation (all three setups) lives here so VolX/ATR are computed once.
def evaluate_symbol(cur, sym: str, ctx: Dict) -> List[Dict]:
    """Return a list of candidate entries (dicts with tag, side, entry_px, snapshot) that pass the
    setup rules AND common gates G1/G2/G5 (G3 clock + G4 slots are enforced by the caller)."""
    d = ctx["asof"]
    bars = _bars(cur, sym, d)
    if len(bars) < 4:
        return []
    priors = _prior_dates(cur, sym, d)
    cur_c = _f(bars[-1][4])
    vwap = _vwap(bars)
    atr = _atr(bars)
    volx = _volx(cur, sym, d, bars, priors)
    or_hi, or_lo = _opening_range(bars)
    sess_hi = max((_f(b[2]) for b in bars if _f(b[2]) is not None), default=None)
    prev = ctx["prev_close"].get(sym)
    day_pct = ((cur_c - prev) / prev * 100.0) if (cur_c and prev) else None
    piv = ctx["pivots"].get(sym, {})
    pp, r1, s1 = piv.get("pp"), piv.get("r1"), piv.get("s1")
    b = ctx["basis"].get(sym, {})
    basis = b.get("basis"); oi_chg = b.get("oi_chg")
    seg = ctx["seg_by_sym"].get(sym)
    sec_day = ctx["sector_day"].get(seg) if seg else None
    if None in (cur_c, vwap):
        return []

    base = {"cmp": _rnd(cur_c), "vwap": _rnd(vwap), "atr": _rnd(atr), "volx": _rnd(volx),
            "or_high": _rnd(or_hi), "or_low": _rnd(or_lo), "sess_high": _rnd(sess_hi),
            "day_pct": _rnd(day_pct), "pp": _rnd(pp), "r1": _rnd(r1), "s1": _rnd(s1),
            "basis": _rnd(basis), "oi_chg": _rnd(oi_chg), "sector_day": _rnd(sec_day),
            "segment": seg, "bar_ts": bars[-1][0].isoformat()}
    out: List[Dict] = []

    # liquidity + blackout are hard common gates for every candidate
    if sym not in ctx["top80"] or sym in ctx["blackout"]:
        return []

    # ── ORB ──────────────────────────────────────────────────────────────────────
    if None not in (or_hi, or_lo, volx, day_pct):
        for side, brk, dmin, dmax, basis_ok in (
            ("long",  cur_c > or_hi,  0.3,  3.0, (basis is not None and basis >= 0)),
            ("short", cur_c < or_lo, -3.0, -0.3, (basis is not None and basis < 0)),
        ):
            aligned = (cur_c > vwap) if side == "long" else (cur_c < vwap)
            dp_ok = (dmin <= day_pct <= dmax) if side == "long" else (dmin <= day_pct <= dmax)
            if brk and volx >= 1.5 and aligned and basis_ok and dp_ok and _regime_ok(ctx, side):
                out.append({"tag": "ORB", "side": side, "entry_px": cur_c, "snapshot": {**base}})

    # ── VWAP-RECLAIM ─────────────────────────────────────────────────────────────
    # above-VWAP fraction of the session + shallow dip below then close back above (long); mirror short
    if volx is not None:
        above_frac = sum(1 for bb in bars if (_f(bb[4]) or 0) >= vwap) / max(1, len(bars))
        below_frac = 1.0 - above_frac
        prev_c = _f(bars[-2][4]) if len(bars) >= 2 else None
        prev_low = _f(bars[-2][3]) if len(bars) >= 2 else None
        # long: majority above VWAP, prior bar dipped <=0.6% below, this bar closes back above
        dip_long = (prev_low is not None and prev_low < vwap and (vwap - prev_low) / vwap * 100 <= 0.6)
        if (above_frac >= 0.60 and dip_long and prev_c is not None and prev_c < vwap and cur_c > vwap
                and volx >= 1.0 and (sec_day is not None and sec_day >= 0) and _regime_ok(ctx, "long")):
            out.append({"tag": "VWAP-RECLAIM", "side": "long", "entry_px": cur_c,
                        "snapshot": {**base, "above_vwap_frac": round(above_frac, 2)}})
        prev_high = _f(bars[-2][2]) if len(bars) >= 2 else None
        rip_short = (prev_high is not None and prev_high > vwap and (prev_high - vwap) / vwap * 100 <= 0.6)
        if (below_frac >= 0.60 and rip_short and prev_c is not None and prev_c > vwap and cur_c < vwap
                and volx >= 1.0 and (sec_day is not None and sec_day <= 0) and _regime_ok(ctx, "short")):
            out.append({"tag": "VWAP-RECLAIM", "side": "short", "entry_px": cur_c,
                        "snapshot": {**base, "below_vwap_frac": round(below_frac, 2)}})

    # ── R1-REJ (short) / S1-BOUNCE (long mirror) ─────────────────────────────────
    if None not in (r1, pp, sess_hi):
        touched_r1 = abs(sess_hi - r1) / r1 * 100 <= 0.1
        below_r1 = (r1 - cur_c) / r1 * 100 >= 0.15
        fall = (sess_hi - cur_c) / sess_hi * 100 if sess_hi else None
        room_pp = (cur_c - pp) / cur_c * 100 if pp else None
        oi_up = (oi_chg is not None and oi_chg > 0)
        basis_fade = (basis is not None and basis <= 0)
        if (touched_r1 and below_r1 and fall is not None and 0.3 <= fall <= 1.5
                and cur_c < vwap and (oi_up or basis_fade) and room_pp is not None and room_pp >= 0.7
                and _regime_ok(ctx, "short")):
            out.append({"tag": "R1-REJ", "side": "short", "entry_px": cur_c,
                        "snapshot": {**base, "fall_from_high": _rnd(fall), "room_to_pp": _rnd(room_pp)}})
    if None not in (s1, pp):
        sess_lo = min((_f(b[3]) for b in bars if _f(b[3]) is not None), default=None)
        if sess_lo is not None:
            touched_s1 = abs(sess_lo - s1) / s1 * 100 <= 0.1
            above_s1 = (cur_c - s1) / s1 * 100 >= 0.15
            rise = (cur_c - sess_lo) / sess_lo * 100 if sess_lo else None
            room_pp = (pp - cur_c) / cur_c * 100 if pp else None
            oi_up = (oi_chg is not None and oi_chg > 0)
            basis_prem = (basis is not None and basis >= 0)
            if (touched_s1 and above_s1 and rise is not None and 0.3 <= rise <= 1.5
                    and cur_c > vwap and (oi_up or basis_prem) and room_pp is not None and room_pp >= 0.7
                    and _regime_ok(ctx, "long")):
                out.append({"tag": "R1-REJ", "side": "long", "entry_px": cur_c,
                            "snapshot": {**base, "rise_from_low": _rnd(rise), "room_to_pp": _rnd(room_pp)}})
    return out


# ── entry / exit ───────────────────────────────────────────────────────────────────
def _nearest_pivot_cap(side: str, entry: float, target: float, piv: Dict) -> float:
    """Cap the ATR target at the nearest pivot in the trade direction (R1/R2 for long, S1/S2 for short)."""
    if side == "long":
        levels = [v for v in (piv.get("r1"), piv.get("r2")) if v and v > entry]
        cap = min(levels) if levels else None
        return min(target, cap) if cap else target
    levels = [v for v in (piv.get("s1"), piv.get("s2")) if v and v < entry]
    cap = max(levels) if levels else None
    return max(target, cap) if cap else target


def open_trade(conn, ctx: Dict, cand: Dict) -> Optional[int]:
    sym = cand["snapshot"].get("symbol") or cand.get("symbol")
    side = cand["side"]; entry = float(cand["entry_px"]); tag = cand["tag"]
    atr = cand["snapshot"].get("atr")
    piv = ctx["pivots"].get(cand["symbol"], {})
    if atr:
        raw_target = entry + ATR_MULT * atr if side == "long" else entry - ATR_MULT * atr
        target = _nearest_pivot_cap(side, entry, raw_target, piv)
    else:
        target = None
    # 1:1 mirror stop, set at order time, never widened
    stop = (entry - (target - entry)) if (target and side == "long") else \
           (entry + (entry - target)) if (target and side == "short") else None
    today = _now().date()
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO v14_trades
            (trade_date, tag, symbol, side, entry_ts, entry_px, stop_px, target_px, atr,
             gates_snapshot, status)
            VALUES (%s,%s,%s,%s,NOW(),%s,%s,%s,%s,%s,'open') RETURNING id""",
            (today, tag, cand["symbol"], side, entry, stop, target, atr,
             json.dumps(cand["snapshot"])))
        tid = cur.fetchone()[0]
        cur.execute("""INSERT INTO v14_watchlist (trade_date, tag, symbol, notes)
                       VALUES (%s,%s,%s,'entered') ON CONFLICT (trade_date, tag, symbol)
                       DO UPDATE SET touches=v14_watchlist.touches+1""",
                    (today, tag, cand["symbol"]))
    conn.commit()
    log.info(f"v14 OPEN {tag} {side} {cand['symbol']} @ {entry} tgt={target} stop={stop}")
    return tid


def _close_trade(conn, trade: Dict, exit_px: float, reason: str):
    side = trade["side"]; entry = float(trade["entry_px"])
    pnl_pts = (exit_px - entry) if side == "long" else (entry - exit_px)
    pnl_pct = pnl_pts / entry * 100.0 if entry else 0.0
    net_pct = pnl_pct - COST_SLIPPAGE
    with conn.cursor() as cur:
        cur.execute("""UPDATE v14_trades SET exit_ts=NOW(), exit_px=%s, exit_reason=%s,
                       pnl_pts=%s, pnl_pct=%s, net_pnl_pct=%s, status='closed' WHERE id=%s""",
                    (round(exit_px, 2), reason, round(pnl_pts, 2), round(pnl_pct, 3),
                     round(net_pct, 3), trade["id"]))
    conn.commit()
    log.info(f"v14 CLOSE {trade['tag']} {trade['symbol']} @ {exit_px} ({reason}) {pnl_pts:+.2f}pts")


def manage_open(conn, ctx: Dict) -> Dict:
    """Uniform bracket exits for every open trade: target, 1:1 stop, 30-min time stop, breakeven
    trail at +0.5%, hard square-off 15:15."""
    d = ctx["asof"]; now = _now(); closed = []
    with conn.cursor() as cur:
        cur.execute("SELECT id, tag, symbol, side, entry_ts, entry_px, stop_px, target_px "
                    "FROM v14_trades WHERE status='open'")
        cols = [c[0] for c in cur.description]
        opens = [dict(zip(cols, r)) for r in cur.fetchall()]
    hard = (now.hour, now.minute) >= SQUAREOFF
    for t in opens:
        sym = t["symbol"]; side = t["side"]; entry = _f(t["entry_px"])
        with conn.cursor() as cur:
            cur.execute("""SELECT close FROM intraday_prices WHERE symbol=%s AND source='fyers_eq'
                           AND timeframe='5m' AND ts::date=%s ORDER BY ts DESC LIMIT 1""", (sym, d))
            r = cur.fetchone()
        cmp_v = _f(r[0]) if r else None
        if cmp_v is None or entry is None:
            if hard and cmp_v is not None:
                _close_trade(conn, t, cmp_v, "squareoff"); closed.append(sym)
            continue
        move_pct = ((cmp_v - entry) / entry * 100.0) if side == "long" else ((entry - cmp_v) / entry * 100.0)
        stop = _f(t["stop_px"]); target = _f(t["target_px"])
        # breakeven trail at +0.5%
        if move_pct >= TRAIL_TRIGGER and stop is not None:
            new_stop = entry
            if (side == "long" and new_stop > stop) or (side == "short" and new_stop < stop):
                with conn.cursor() as cur:
                    cur.execute("UPDATE v14_trades SET stop_px=%s WHERE id=%s", (round(entry, 2), t["id"]))
                conn.commit(); stop = new_stop
        reason = None
        if target is not None and ((side == "long" and cmp_v >= target) or (side == "short" and cmp_v <= target)):
            reason = "target"
        elif stop is not None and ((side == "long" and cmp_v <= stop) or (side == "short" and cmp_v >= stop)):
            reason = "stop"
        elif t["entry_ts"] and (now - t["entry_ts"]).total_seconds() >= TIME_STOP_MIN * 60 and move_pct < TIME_STOP_PCT:
            reason = "time"
        elif hard:
            reason = "squareoff"
        if reason:
            _close_trade(conn, t, cmp_v, reason); closed.append(sym)
    return {"open": len(opens), "closed": closed}


# ── main 5-min cycle ───────────────────────────────────────────────────────────────
def run_v14_cycle(conn) -> Dict:
    """One 5-min cycle: manage exits on open trades, then (in a clock window, slots free) evaluate
    every top-80 symbol for the 3 setups and paper-open triggers. Read-only on V8/V10."""
    ensure_tables(conn)
    with conn.cursor() as cur:
        ctx = load_context(cur)
    if ctx["asof"] is None:
        return {"status": "no_data"}

    exits = manage_open(conn, ctx)

    opened = []
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM v14_trades WHERE status='open'")
        n_open = int(cur.fetchone()[0])
        cur.execute("SELECT symbol FROM v14_trades WHERE trade_date=%s", (ctx["asof"],))
        traded_today = {r[0] for r in cur.fetchall()}   # G4: 1 trade/symbol/day, no re-entry

    if _in_clock() and n_open < MAX_SLOTS:
        universe = sorted(ctx["top80"])
        for sym in universe:
            if n_open >= MAX_SLOTS:
                break
            if sym in traded_today:
                continue
            with conn.cursor() as cur:
                cands = evaluate_symbol(cur, sym, ctx)
            for c in cands:
                c["symbol"] = sym
                if n_open >= MAX_SLOTS:
                    break
                tid = open_trade(conn, ctx, c)
                if tid:
                    opened.append({"id": tid, "tag": c["tag"], "side": c["side"], "symbol": sym})
                    traded_today.add(sym); n_open += 1
                    break   # one trade/symbol/day

    return {"status": "ok", "asof": str(ctx["asof"]), "in_clock": _in_clock(),
            "open_slots_used": n_open, "opened": opened, "exits": exits}


# ── read helpers for the /v14 page ──────────────────────────────────────────────────
def get_open(conn) -> List[Dict]:
    with conn.cursor() as cur:
        cur.execute("""SELECT id, tag, symbol, side, entry_ts, entry_px, stop_px, target_px, atr,
                       gates_snapshot FROM v14_trades WHERE status='open' ORDER BY entry_ts DESC""")
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["entry_ts"] = r["entry_ts"].isoformat() if r["entry_ts"] else None
        for k in ("entry_px", "stop_px", "target_px", "atr"):
            r[k] = _f(r[k])
    return rows


def get_trades(conn, limit: int = 200) -> List[Dict]:
    with conn.cursor() as cur:
        cur.execute("""SELECT id, trade_date, tag, symbol, side, entry_ts, entry_px, exit_ts, exit_px,
                       exit_reason, pnl_pts, pnl_pct, net_pnl_pct, gates_snapshot
                       FROM v14_trades WHERE status='closed' ORDER BY exit_ts DESC LIMIT %s""", (limit,))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["trade_date"] = str(r["trade_date"])
        r["entry_ts"] = r["entry_ts"].isoformat() if r["entry_ts"] else None
        r["exit_ts"] = r["exit_ts"].isoformat() if r["exit_ts"] else None
        for k in ("entry_px", "exit_px", "pnl_pts", "pnl_pct", "net_pnl_pct"):
            r[k] = _f(r[k])
    return rows


def get_tag_summary(conn, trade_date: Optional[str] = None) -> List[Dict]:
    """Per-tag day summary: trades / win-rate / net points (after slippage) for closed trades."""
    with conn.cursor() as cur:
        if trade_date:
            cur.execute("""SELECT tag, COUNT(*), COUNT(*) FILTER (WHERE net_pnl_pct>0),
                           COALESCE(ROUND(SUM(pnl_pts)::numeric,2),0),
                           COALESCE(ROUND(AVG(net_pnl_pct)::numeric,3),0)
                           FROM v14_trades WHERE status='closed' AND trade_date=%s GROUP BY tag ORDER BY tag""",
                        (trade_date,))
        else:
            cur.execute("""SELECT tag, COUNT(*), COUNT(*) FILTER (WHERE net_pnl_pct>0),
                           COALESCE(ROUND(SUM(pnl_pts)::numeric,2),0),
                           COALESCE(ROUND(AVG(net_pnl_pct)::numeric,3),0)
                           FROM v14_trades WHERE status='closed' GROUP BY tag ORDER BY tag""")
        out = []
        for tag, n, wins, net_pts, avg_net in cur.fetchall():
            out.append({"tag": tag, "trades": int(n), "wins": int(wins),
                        "win_rate": round(100 * wins / n, 1) if n else 0.0,
                        "net_points": _f(net_pts), "avg_net_pct": _f(avg_net)})
    return out
