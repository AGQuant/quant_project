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
                entry_ts TIMESTAMPTZ, entry_px NUMERIC, eq_signal_px NUMERIC,
                exit_ts TIMESTAMPTZ, exit_px NUMERIC,
                stop_px NUMERIC, target_px NUMERIC, target_basis TEXT, atr NUMERIC,
                gates_snapshot JSONB, exit_reason TEXT,
                pnl_pts NUMERIC, pnl_pct NUMERIC, net_pnl_pct NUMERIC,
                cost_flat NUMERIC DEFAULT 500, cost_slippage_pct NUMERIC DEFAULT 0.05,
                basis_entry_rs NUMERIC, basis_entry_pct NUMERIC, basis_entry_sign TEXT, basis_dir_entry TEXT,
                basis_exit_rs NUMERIC, basis_exit_pct NUMERIC, basis_exit_sign TEXT, basis_dir_exit TEXT,
                results_date DATE,
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


def _touch_count(bars: List[Tuple], vwap: float) -> int:
    """Number of VWAP touches this session = sign-changes of (close - vwap) + 1 (id=3063: #1-#3 allowed)."""
    signs = []
    for b in bars:
        c = _f(b[4])
        if c is None:
            continue
        signs.append(1 if c >= vwap else -1)
    if not signs:
        return 0
    crosses = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
    return crosses + 1


def _fut_price(cur, sym: str, d: date) -> Optional[float]:
    """cc#444: EXECUTION price = concurrent Fyers FUTURES 5m close (latest on the as-of session).
    Signals compute on equity; entries/exits/MTM price on futures."""
    cur.execute("""SELECT close FROM intraday_prices WHERE symbol=%s AND source='fyers_fut'
                   AND timeframe='5m' AND ts::date=%s ORDER BY ts DESC LIMIT 1""", (sym, d))
    r = cur.fetchone()
    return _f(r[0]) if r else None


def _target_dist(side: str, eq_px: float, atr: Optional[float], piv: Dict) -> Tuple[Optional[float], Optional[str]]:
    """id=3064 min-1% target rule (equity-derived DISTANCE in points, frozen at entry):
    ATR(5m,20) if ATR%>1, else R1 if room>1%, else R2 (shorts ATR->S1->S2)."""
    if not eq_px:
        return None, None
    atr_pct = (atr / eq_px * 100) if atr else 0.0
    if side == "long":
        if atr and atr_pct > 1.0:
            return atr, "ATR"
        r1, r2 = piv.get("r1"), piv.get("r2")
        if r1 and (r1 - eq_px) / eq_px * 100 > 1.0:
            return (r1 - eq_px), "R1"
        if r2 and r2 > eq_px:
            return (r2 - eq_px), "R2"
        return (atr, "ATR") if atr else (None, None)
    if atr and atr_pct > 1.0:
        return atr, "ATR"
    s1, s2 = piv.get("s1"), piv.get("s2")
    if s1 and (eq_px - s1) / eq_px * 100 > 1.0:
        return (eq_px - s1), "S1"
    if s2 and s2 < eq_px:
        return (eq_px - s2), "S2"
    return (atr, "ATR") if atr else (None, None)


def _basis_snapshot(cur, sym: str, latest: Dict) -> Dict:
    """cc#444 basis research field: fut-eq value (Rs + %), premium/discount sign, and 30-min
    direction (widening = |basis| growing, fading = shrinking toward 0, flat)."""
    rs = latest.get("basis"); pct = latest.get("pct")
    sign = None if rs is None else ("premium" if rs >= 0 else "discount")
    direction = "flat"
    cur.execute("""SELECT ts, basis FROM futures_basis WHERE symbol=%s AND basis IS NOT NULL
                   ORDER BY ts DESC LIMIT 12""", (sym,))
    rows = cur.fetchall()
    if len(rows) >= 2 and rs is not None:
        latest_ts = rows[0][0]; ref = None
        for ts, b in rows:
            if (latest_ts - ts).total_seconds() >= 30 * 60:
                ref = _f(b); break
        if ref is None:
            ref = _f(rows[-1][1])
        if ref is not None:
            if abs(rs - ref) < 0.5:
                direction = "flat"
            else:
                direction = "widening" if abs(rs) > abs(ref) else "fading"
    return {"rs": _rnd(rs), "pct": _rnd(pct), "sign": sign, "dir": direction}


def _results_date(cur, sym: str) -> Optional[str]:
    """Next earnings ex_date for the symbol (G5 / id=3062 Results column). None if none upcoming."""
    cur.execute("SELECT MIN(ex_date) FROM earnings_calendar WHERE UPPER(ticker)=%s AND ex_date >= CURRENT_DATE",
                (sym.upper(),))
    r = cur.fetchone()
    return str(r[0]) if r and r[0] else None


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

    # cc#444 (id=3063): gap (from prev close) + VWAP touch counter (#1-#3 allowed)
    today_open = _f(bars[0][1])
    gap_pct = ((today_open - prev) / prev * 100.0) if (today_open and prev) else None
    touch = _touch_count(bars, vwap)
    base = {"cmp": _rnd(cur_c), "vwap": _rnd(vwap), "atr": _rnd(atr), "volx": _rnd(volx),
            "or_high": _rnd(or_hi), "or_low": _rnd(or_lo), "sess_high": _rnd(sess_hi),
            "day_pct": _rnd(day_pct), "gap_pct": _rnd(gap_pct), "vwap_touch": touch,
            "pp": _rnd(pp), "r1": _rnd(r1), "s1": _rnd(s1),
            "basis": _rnd(basis), "oi_chg": _rnd(oi_chg), "sector_day": _rnd(sec_day),
            "turnover_top80": True, "v10_long": ctx.get("v10_long"), "v10_short": ctx.get("v10_short"),
            "segment": seg, "bar_ts": bars[-1][0].isoformat()}
    out: List[Dict] = []

    # liquidity + blackout are hard common gates for every candidate
    if sym not in ctx["top80"] or sym in ctx["blackout"]:
        return []

    # id=3064 common structural gate for the momentum setups (ORB, VWAP-RECLAIM):
    # long only above PP, short only below PP. (R1-REJ / S1-BOUNCE are mean-reversion-to-PP and
    # keep their own room-to-PP rule — the CMP-vs-PP gate would contradict them by construction.)
    def _pp_ok(side):
        if pp is None:
            return False
        return cur_c > pp if side == "long" else cur_c < pp

    # ── ORB (id=3063) ─────────────────────────────────────────────────────────────
    # long: VolX>=1.25, gap-up<=2%. short: NO volume rule, gap-down<=3%.
    if None not in (or_hi, or_lo, day_pct) and gap_pct is not None:
        long_ok = (cur_c > or_hi and cur_c > vwap and (basis is not None and basis >= 0)
                   and 0.3 <= day_pct <= 3.0 and (volx is not None and volx >= 1.25)
                   and gap_pct <= 2.0 and _pp_ok("long") and _regime_ok(ctx, "long"))
        if long_ok:
            out.append({"tag": "ORB", "side": "long", "entry_px": cur_c, "snapshot": {**base}})
        short_ok = (cur_c < or_lo and cur_c < vwap and (basis is not None and basis < 0)
                    and -3.0 <= day_pct <= -0.3 and gap_pct >= -3.0
                    and _pp_ok("short") and _regime_ok(ctx, "short"))
        if short_ok:
            out.append({"tag": "ORB", "side": "short", "entry_px": cur_c, "snapshot": {**base}})

    # ── VWAP-RECLAIM (id=3063) ────────────────────────────────────────────────────
    # touch #1-#3; long relative-strength (sector>0 AND stock>sector, VolX>=1.0);
    # short mirror (sector<0 AND stock<sector, NO volume rule).
    if touch <= 3 and len(bars) >= 2:
        above_frac = sum(1 for bb in bars if (_f(bb[4]) or 0) >= vwap) / max(1, len(bars))
        below_frac = 1.0 - above_frac
        prev_c = _f(bars[-2][4]); prev_low = _f(bars[-2][3]); prev_high = _f(bars[-2][2])
        dip_long = (prev_low is not None and prev_low < vwap and (vwap - prev_low) / vwap * 100 <= 0.6)
        rel_long = (sec_day is not None and day_pct is not None and sec_day > 0 and day_pct > sec_day)
        if (above_frac >= 0.60 and dip_long and prev_c is not None and prev_c < vwap and cur_c > vwap
                and (volx is not None and volx >= 1.0) and rel_long and _pp_ok("long")
                and _regime_ok(ctx, "long")):
            out.append({"tag": "VWAP-RECLAIM", "side": "long", "entry_px": cur_c,
                        "snapshot": {**base, "above_vwap_frac": round(above_frac, 2)}})
        rip_short = (prev_high is not None and prev_high > vwap and (prev_high - vwap) / vwap * 100 <= 0.6)
        rel_short = (sec_day is not None and day_pct is not None and sec_day < 0 and day_pct < sec_day)
        if (below_frac >= 0.60 and rip_short and prev_c is not None and prev_c > vwap and cur_c < vwap
                and rel_short and _pp_ok("short") and _regime_ok(ctx, "short")):
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
def open_trade(conn, ctx: Dict, cand: Dict) -> Optional[int]:
    """cc#444: signal fires on EQUITY; EXECUTION prices on the concurrent FUTURES 5m close.
    Target = equity-derived min-1% distance (id=3064) applied to the futures entry; 1:1 mirror stop.
    Records the entry basis (fut-eq Rs/%/sign/dir) + next results date for evening analysis."""
    sym = cand["symbol"]; side = cand["side"]; tag = cand["tag"]
    eq_px = float(cand["entry_px"]); atr = cand["snapshot"].get("atr")
    piv = ctx["pivots"].get(sym, {})
    today = _now().date()
    with conn.cursor() as cur:
        fut_px = _fut_price(cur, sym, ctx["asof"]) or eq_px
        dist, tbasis = _target_dist(side, eq_px, atr, piv)   # equity-derived distance (points)
        if dist and dist > 0:
            target = fut_px + dist if side == "long" else fut_px - dist
            stop = fut_px - dist if side == "long" else fut_px + dist   # 1:1 mirror, never widened
        else:
            target = stop = None
        bsnap = _basis_snapshot(cur, sym, ctx["basis"].get(sym, {}))
        rdate = _results_date(cur, sym)
        cur.execute("""INSERT INTO v14_trades
            (trade_date, tag, symbol, side, entry_ts, entry_px, eq_signal_px, stop_px, target_px,
             target_basis, atr, gates_snapshot, results_date,
             basis_entry_rs, basis_entry_pct, basis_entry_sign, basis_dir_entry, status)
            VALUES (%s,%s,%s,%s,NOW(),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open') RETURNING id""",
            (today, tag, sym, side, round(fut_px, 2), round(eq_px, 2), _rnd(stop), _rnd(target),
             tbasis, atr, json.dumps(cand["snapshot"]), rdate,
             bsnap["rs"], bsnap["pct"], bsnap["sign"], bsnap["dir"]))
        tid = cur.fetchone()[0]
        cur.execute("""INSERT INTO v14_watchlist (trade_date, tag, symbol, notes)
                       VALUES (%s,%s,%s,'entered') ON CONFLICT (trade_date, tag, symbol)
                       DO UPDATE SET touches=v14_watchlist.touches+1""", (today, tag, sym))
    conn.commit()
    log.info(f"v14 OPEN {tag} {side} {sym} fut@{fut_px} eq@{eq_px} tgt={target}({tbasis}) stop={stop}")
    return tid


def _close_trade(conn, trade: Dict, exit_px: float, reason: str, basis_ctx: Optional[Dict] = None):
    side = trade["side"]; entry = float(trade["entry_px"])
    pnl_pts = (exit_px - entry) if side == "long" else (entry - exit_px)
    pnl_pct = pnl_pts / entry * 100.0 if entry else 0.0
    net_pct = pnl_pct - COST_SLIPPAGE
    with conn.cursor() as cur:
        bs = _basis_snapshot(cur, trade["symbol"], (basis_ctx or {}).get(trade["symbol"], {})) \
            if basis_ctx is not None else {"rs": None, "pct": None, "sign": None, "dir": None}
        cur.execute("""UPDATE v14_trades SET exit_ts=NOW(), exit_px=%s, exit_reason=%s,
                       pnl_pts=%s, pnl_pct=%s, net_pnl_pct=%s,
                       basis_exit_rs=%s, basis_exit_pct=%s, basis_exit_sign=%s, basis_dir_exit=%s,
                       status='closed' WHERE id=%s""",
                    (round(exit_px, 2), reason, round(pnl_pts, 2), round(pnl_pct, 3), round(net_pct, 3),
                     bs["rs"], bs["pct"], bs["sign"], bs["dir"], trade["id"]))
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
        # cc#444: MTM / exits price on the FUTURES feed (the tradeable instrument), fyers_eq fallback.
        with conn.cursor() as cur:
            cmp_v = _fut_price(cur, sym, d)
            if cmp_v is None:
                cur.execute("""SELECT close FROM intraday_prices WHERE symbol=%s AND source='fyers_eq'
                               AND timeframe='5m' AND ts::date=%s ORDER BY ts DESC LIMIT 1""", (sym, d))
                r = cur.fetchone(); cmp_v = _f(r[0]) if r else None
        if cmp_v is None or entry is None:
            if hard and cmp_v is not None:
                _close_trade(conn, t, cmp_v, "squareoff", ctx["basis"]); closed.append(sym)
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
            _close_trade(conn, t, cmp_v, reason, ctx["basis"]); closed.append(sym)
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
        cur.execute("""SELECT id, tag, symbol, side, entry_ts, entry_px, eq_signal_px, stop_px,
                       target_px, target_basis, atr, results_date,
                       basis_entry_rs, basis_entry_sign, basis_dir_entry, gates_snapshot
                       FROM v14_trades WHERE status='open' ORDER BY entry_ts DESC""")
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["entry_ts"] = r["entry_ts"].isoformat() if r["entry_ts"] else None
        r["results_date"] = str(r["results_date"]) if r["results_date"] else None
        for k in ("entry_px", "eq_signal_px", "stop_px", "target_px", "atr", "basis_entry_rs"):
            r[k] = _f(r[k])
    return rows


def get_trades(conn, limit: int = 200) -> List[Dict]:
    with conn.cursor() as cur:
        cur.execute("""SELECT id, trade_date, tag, symbol, side, entry_ts, entry_px, exit_ts, exit_px,
                       exit_reason, pnl_pts, pnl_pct, net_pnl_pct, target_basis,
                       basis_entry_rs, basis_entry_sign, basis_dir_entry,
                       basis_exit_rs, basis_exit_sign, basis_dir_exit, gates_snapshot
                       FROM v14_trades WHERE status='closed' ORDER BY exit_ts DESC LIMIT %s""", (limit,))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["trade_date"] = str(r["trade_date"])
        r["entry_ts"] = r["entry_ts"].isoformat() if r["entry_ts"] else None
        r["exit_ts"] = r["exit_ts"].isoformat() if r["exit_ts"] else None
        for k in ("entry_px", "exit_px", "pnl_pts", "pnl_pct", "net_pnl_pct",
                  "basis_entry_rs", "basis_exit_rs"):
            r[k] = _f(r[k])
    return rows


def get_daily_pnl(conn) -> Dict:
    """cc#444 daily P&L view: OPEN P&L (live futures MTM of open trades) + CLOSED P&L (realized
    today, net of costs) + combined day net. As-of futures price off-market."""
    with conn.cursor() as cur:
        d = _asof_date(cur)
        # closed today
        cur.execute("""SELECT COUNT(*), COUNT(*) FILTER (WHERE net_pnl_pct>0),
                       COALESCE(SUM(pnl_pts),0), COALESCE(SUM(pnl_pct),0), COALESCE(SUM(net_pnl_pct),0)
                       FROM v14_trades WHERE status='closed' AND trade_date=%s""", (d,))
        cn, cw, cpts, cpct, cnet = cur.fetchone()
        # open MTM
        cur.execute("SELECT id, symbol, side, entry_px FROM v14_trades WHERE status='open'")
        opens = cur.fetchall()
        open_pts = 0.0; open_pct = 0.0; nopen = 0
        for _id, sym, side, ep in opens:
            ep = _f(ep)
            cmpv = _fut_price(cur, sym, d)
            if cmpv is None or ep is None:
                continue
            pts = (cmpv - ep) if side == "long" else (ep - cmpv)
            open_pts += pts; open_pct += pts / ep * 100.0 if ep else 0.0; nopen += 1
    return {
        "asof": str(d) if d else None,
        "open": {"positions": nopen, "mtm_pts": round(open_pts, 2), "mtm_pct": round(open_pct, 3)},
        "closed_today": {"trades": int(cn), "wins": int(cw),
                         "win_rate": round(100 * cw / cn, 1) if cn else 0.0,
                         "gross_pts": round(_f(cpts) or 0, 2), "gross_pct": round(_f(cpct) or 0, 3),
                         "net_pct": round(_f(cnet) or 0, 3)},
        "combined_net_pct": round((_f(cnet) or 0) + open_pct, 3),
    }


def get_day_log(conn, limit: int = 30) -> List[Dict]:
    """cc#444 Day-Log history: per trading day — trades, WR, gross pts, cost %, net %."""
    with conn.cursor() as cur:
        cur.execute("""SELECT trade_date, COUNT(*), COUNT(*) FILTER (WHERE net_pnl_pct>0),
                       COALESCE(ROUND(SUM(pnl_pts)::numeric,2),0),
                       COALESCE(ROUND(SUM(pnl_pct)::numeric,3),0),
                       COALESCE(ROUND(SUM(net_pnl_pct)::numeric,3),0)
                       FROM v14_trades WHERE status='closed'
                       GROUP BY trade_date ORDER BY trade_date DESC LIMIT %s""", (limit,))
        out = []
        for d, n, w, gpts, gpct, npct in cur.fetchall():
            out.append({"date": str(d), "trades": int(n),
                        "win_rate": round(100 * w / n, 1) if n else 0.0,
                        "gross_pts": _f(gpts), "cost_pct": round((_f(gpct) or 0) - (_f(npct) or 0), 3),
                        "net_pct": _f(npct)})
    return out


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
