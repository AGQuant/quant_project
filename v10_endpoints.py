"""
v10_endpoints.py — V10 ST+EMA intraday strategy routes (Scorr platform).
Mounted in main.py via: app.include_router(v10_router)
Isolated from V8 / live feed writes. Paper + advisory only.
"""
import os
import logging
import psycopg
from typing import Optional
from fastapi import APIRouter, Header, HTTPException

import max_pain as max_pain_mod   # cc#1155: the corrected max-pain computation + its guard

router = APIRouter(prefix="/api/v10", tags=["v10"])
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
log = logging.getLogger("v10")    # cc#1155: the guard must be able to say so LOUDLY when it trips


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _check_admin(token: Optional[str]):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


@router.get("/signal")
def v10_signal(symbol: str = "NIFTY50"):
    """Current signal from the latest CLOSED 10m bar. symbol: NIFTY50 | BANKNIFTY."""
    import v10_st_ema
    return v10_st_ema.current_signal(symbol)


@router.post("/append")
def v10_append(x_admin_token: Optional[str] = Header(None)):
    """Build + append latest closed 5m bars (NIFTY + BANKNIFTY) from live 1m feed."""
    _check_admin(x_admin_token)
    import v10_st_ema
    return v10_st_ema.build_and_append_5m()


@router.post("/tick")
def v10_tick(x_admin_token: Optional[str] = Header(None)):
    """Full 5-min cycle: append bars, run paper engine, Telegram alert on new entries.
    Scheduler hits this every 5 min during market hours."""
    _check_admin(x_admin_token)
    import v10_st_ema
    return v10_st_ema.tick()


@router.post("/backfill")
def v10_backfill(days: int = 5, x_admin_token: Optional[str] = Header(None)):
    """Repair the 5m tables from intraday_prices over the last N days (idempotent).
    Use after a tick outage to fill gaps (e.g. the Jun 19-22 gap)."""
    _check_admin(x_admin_token)
    import v10_st_ema
    return v10_st_ema._backfill_5m(days=days)


@router.post("/gap-exit")
def v10_gap_exit(x_admin_token: Optional[str] = Header(None)):
    """Force-close any OPEN position stranded by a tick outage (exits at the first
    bar open after entry date, reason GAP_EXIT). No-op when nothing is stranded."""
    _check_admin(x_admin_token)
    import v10_st_ema
    return v10_st_ema.gap_exit()


# ---- Dashboard reads (no auth — display only) ----
@router.get("/positions")
def v10_positions():
    """Open paper positions (both indices)."""
    import v10_st_ema
    return {"open_positions": v10_st_ema.get_open_positions()}


@router.get("/trades")
def v10_trades(limit: int = 200):
    """Closed paper trade log with P&L."""
    import v10_st_ema
    return {"closed_trades": v10_st_ema.get_closed_trades(limit)}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# cc#941 — PAIRED TRADE LOG. ONE derivation, read by BOTH surfaces.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def paired_trades(symbol=None, limit=200):
    """cc#941 — v10_trades stores ONE trade as TWO rows and every surface showed them raw.

    THE MESS THE FOUNDER SAW. A V10 trade is a futures position plus an option hedge, written as
    two rows sharing (symbol, entry_ts) — e.g. ids 80 and 81. Shown as peers they read as two
    separate trades, and worse, the two rows carry numbers on DIFFERENT SCALES: the FUT row is an
    index level (58,004) and the OPT row is a premium (220). Side is also per-leg, so a LONG trade
    shows a "SELL" row next to its "BUY" row and reads as a reversal. And the P&L that matters —
    what the trade actually made — appeared nowhere, because it is the sum of the two.

    WHAT PAIRING MEANS HERE. Group on (symbol, entry_ts). The FUT leg is the trade: its side is
    the direction, its prices are the headline, its exit_reason is the outcome. The OPT leg is a
    hedge and becomes a sub-line. NET P&L = fut.pnl + opt.pnl, and that is the only number the
    card shows in bold, because it is the only one that is the trade's result.

    VERIFIED SHAPE, not assumed: all 45 groups in the table hold exactly one FUT leg; 36 carry an
    OPT hedge, 9 are unhedged (the early June trades, before hedging started). An unhedged group
    renders as a card with no hedge line, never as a card with a blank one. A group that somehow
    arrives with no FUT leg is SKIPPED and counted in `unpaired` rather than promoting a premium
    to a headline price.

    IST IN SQL, NEVER IN PYTHON. entry_ts/exit_ts are genuine TIMESTAMPTZ, so they convert with
    AT TIME ZONE 'Asia/Kolkata' inside the query (cc#844 class — a Python-side shift on a tz-aware
    value is the phantom-330-minute bug). Duration is computed in SQL from the same two values.

    Display-layer only: nothing here writes, and v10_trades / the tick engine are untouched.
    """
    where, params = "", []
    if symbol:
        where = "WHERE symbol = %s"
        params.append(symbol.upper())
    sql = f"""
        SELECT id, symbol, side, leg, opt_strike, opt_type,
               entry_price, exit_price, exit_reason, points, pnl, lot_size,
               entry_ts,
               to_char(entry_ts AT TIME ZONE 'Asia/Kolkata', 'DD-Mon HH24:MI') AS entry_ist,
               to_char(entry_ts AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD')     AS entry_date,
               to_char(exit_ts  AT TIME ZONE 'Asia/Kolkata', 'DD-Mon HH24:MI') AS exit_ist,
               to_char(exit_ts  AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD')     AS exit_date,
               EXTRACT(EPOCH FROM (exit_ts - entry_ts))                        AS held_s
        FROM v10_trades {where}
        ORDER BY entry_ts DESC, leg ASC
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    def f(x):
        return None if x is None else float(x)

    def dur(sec):
        """'20h 35m'. None when the trade is still open — an unknown duration is not zero."""
        if sec is None:
            return None
        m = int(sec // 60)
        h, m = divmod(m, 60)
        return f"{h}h {m}m" if h else f"{m}m"

    groups, order = {}, []
    for r in rows:
        k = (r["symbol"], r["entry_ts"])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)

    out, unpaired = [], 0
    for k in order:
        legs = groups[k]
        fut = next((x for x in legs if (x["leg"] or "FUT").upper() == "FUT"), None)
        if fut is None:
            unpaired += 1
            continue
        opt = next((x for x in legs if (x["leg"] or "").upper() == "OPT"), None)
        fut_pnl, opt_pnl = f(fut["pnl"]), (f(opt["pnl"]) if opt else None)
        net = None
        if fut_pnl is not None:
            net = fut_pnl + (opt_pnl or 0.0)
        side = (fut["side"] or "").upper()
        t = {
            "id": fut["id"], "symbol": fut["symbol"],
            # DIRECTION COMES FROM THE FUT LEG ONLY. The hedge's own side is the opposite by
            # construction, so reading direction off any other row inverts the trade.
            "direction": "LONG" if side == "BUY" else "SHORT",
            "entry_price": f(fut["entry_price"]), "exit_price": f(fut["exit_price"]),
            "points": f(fut["points"]), "lot_size": fut["lot_size"],
            "reason": (fut["exit_reason"] or "OPEN").upper(),
            "entry_ist": fut["entry_ist"], "exit_ist": fut["exit_ist"],
            "entry_date": fut["entry_date"], "exit_date": fut["exit_date"],
            "held": dur(f(fut["held_s"])),
            "fut_pnl": fut_pnl, "opt_pnl": opt_pnl,
            "net_pnl": net, "net_r": None if net is None else int(round(net)),
            "win": None if net is None else (net >= 0),
            "hedge": None if not opt else {
                "id": opt["id"], "side": (opt["side"] or "").upper(),
                "strike": f(opt["opt_strike"]), "type": opt["opt_type"],
                "entry_price": f(opt["entry_price"]), "exit_price": f(opt["exit_price"]),
                "points": f(opt["points"]), "pnl": opt_pnl,
                "label": ("%s %s" % (int(f(opt["opt_strike"])) if opt["opt_strike"] is not None else "?",
                                     opt["opt_type"] or "")).strip(),
            },
        }
        out.append(t)

    out = out[:max(1, min(int(limit or 200), 1000))]
    closed = [t for t in out if t["net_pnl"] is not None]
    wins = sum(1 for t in closed if t["win"])
    total = round(sum(t["net_pnl"] for t in closed), 2) if closed else None
    return {
        "trades": out, "count": len(out), "unpaired": unpaired,
        "summary": {
            "paired_trades": len(out), "closed": len(closed),
            "wins": wins, "losses": len(closed) - wins,
            # Win rate on NET P&L, which is the point of pairing: a trade whose futures leg lost
            # less than its hedge made is a win, and the per-leg view could never say that.
            "win_rate": (round(100.0 * wins / len(closed), 1) if closed else None),
            "net_pnl": total, "net_r": None if total is None else int(round(total)),
            "hedged": sum(1 for t in out if t["hedge"]),
            "unhedged": sum(1 for t in out if not t["hedge"]),
        },
    }


@router.get("/trades/paired")
def v10_trades_paired(symbol: str = "", limit: int = 200):
    """cc#941: the paired trade log — one row per TRADE, not per leg. Both the mobile card list
    and the desktop table read THIS, so the two can never disagree about a net P&L (16202)."""
    return paired_trades(symbol.strip().upper() or None, limit)


@router.get("/candles")
def v10_candles(symbol: str = "NIFTY50", days: int = 30):
    """cc#537: read-only 5-min OHLC candles for the V10 chart overlay. NIFTY50 -> nifty_5m_test_data,
    BANKNIFTY -> banknifty_5m_test_data (same schema). Both index-SPOT series (the V10 signal is a
    spot event; markers snap to these bars client-side). TIME BASE (verified live): the *_5m_test_data
    bar `ts` is already IST wall-clock tagged +00 (first bar of a day = 09:15 = NSE open), so the raw
    EXTRACT(EPOCH FROM ts) IS the IST wall-clock epoch that TradingView Lightweight Charts -- which
    renders UTCTimestamp in UTC -- shows correctly on the axis. No shift is applied here. The v10_trades
    timestamps, by contrast, are genuine UTC, so the client shifts a trade ts by +5.5h (UTC->IST) before
    snapping it to a bar, so candles + markers align exactly (verified: 16/16 NIFTY FUT entries snap to
    their bar within 51s). Windowed to the last `days` days of AVAILABLE data (test tables are historical,
    so anchored to MAX(ts), not NOW()). DISTINCT ON (ts) guards LWC's strictly-ascending-unique-time
    requirement against any duplicate bar. No auth (display-only)."""
    sym = (symbol or "NIFTY50").upper()
    table = "banknifty_5m_test_data" if sym in ("BANKNIFTY", "BANKNIFTY50", "NIFTYBANK", "BNF") else "nifty_5m_test_data"
    days = max(1, min(int(days or 30), 365))
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"""SELECT EXTRACT(EPOCH FROM ts)::bigint AS t, open, high, low, close
                        FROM (
                          SELECT DISTINCT ON (ts) ts, open, high, low, close FROM {table}
                          WHERE ts >= (SELECT MAX(ts) FROM {table}) - (%s || ' days')::interval
                          ORDER BY ts, close
                        ) x ORDER BY t ASC""", (days,))
        candles = [{"time": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4])} for r in cur.fetchall()]
    return {"symbol": sym, "table": table, "days": days, "count": len(candles), "candles": candles}


@router.get("/summary")
def v10_summary():
    """Running settings + aggregate paper P&L summary."""
    import v10_st_ema
    return v10_st_ema.get_summary()


@router.get("/performance")
def v10_performance():
    """Full live-paper performance stats from v10_trades — total trades, win rate,
    total P&L, avg win/loss pts, profit factor, max drawdown, last 7 days, and
    by-symbol / by-leg breakdowns. Powers the dashboard performance panel."""
    import v10_st_ema
    return v10_st_ema.get_performance()


@router.get("/vix")
def v10_vix():
    """India VIX — live LTP (cmp_prices) + last ~7 trading-day closes from the
    5-min intraday feed (symbol INDIAVIX, fed by Fyers INDEX_LTP_SYMBOLS).
    NOT US VIX — global_indices only carries ^VIX which is the US index."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (ts::date) ts::date AS day, close::numeric AS vix
                FROM intraday_prices
                WHERE symbol='INDIAVIX' AND ts >= NOW() - INTERVAL '8 days'
                ORDER BY ts::date ASC, ts DESC
            """)
            data = [float(r[1]) for r in cur.fetchall() if r[1] is not None]
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol='INDIAVIX'")
            lr = cur.fetchone()
            live = float(lr[0]) if lr and lr[0] is not None else None
        cur_v = live if live is not None else (data[-1] if data else 0.0)
        prev = data[-2] if len(data) >= 2 else cur_v
        return {"label": "India VIX", "cur": cur_v, "chg": round(cur_v - prev, 2),
                "data": data, "source": "intraday INDIAVIX + cmp_prices live"}
    except Exception as e:
        raise HTTPException(500, f"v10_vix failed: {e}")


# underlying tag -> spot symbol in raw_prices
_MAXPAIN_SPOT = {"NIFTY": "NIFTY50", "BANKNIFTY": "BANKNIFTY"}


@router.get("/maxpain")
def v10_maxpain(symbol: str = "NIFTY"):
    """Max-pain strike for the nearest expiry — the expiry price that minimises total option-writer
    payout across the WHOLE chain.

    cc#1155: the computation moved to max_pain.py and was CORRECTED. The old inline SQL had three
    defects — it kept strikes from earlier ticks that have since left the chain, it summed each
    strike's own intrinsic value at the current spot instead of building a payout curve over
    candidate expiry prices, and the spot it used was the previous daily close. That combination is
    what printed NIFTY 22,650, a hundred points below its own chain floor. See max_pain.py for the
    proof of each. The guard lives in max_pain.max_pain(), so no caller can route around it.
    """
    underlying = (symbol or "NIFTY").upper()
    if underlying in ("NIFTY50", "NIFTY 50"):
        underlying = "NIFTY"
    spot_sym = _MAXPAIN_SPOT.get(underlying, "NIFTY50")
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(max_pain_mod.LATEST_CHAIN_SQL, {"u": underlying})
            rows = cur.fetchall()
            cur.execute(
                "SELECT close FROM raw_prices WHERE symbol=%s ORDER BY price_date DESC LIMIT 1",
                (spot_sym,),
            )
            sr = cur.fetchone()
        if not rows:
            return {"status": "no_data", "symbol": underlying,
                    "note": "Option chain data pending"}
        chain = {float(r[0]): (r[1] or 0, r[2] or 0) for r in rows}
        expiry = rows[0][3]
        tick = rows[0][4]
        strike, curve = max_pain_mod.max_pain(chain)
        lo, hi = max_pain_mod.chain_bounds(chain.keys())
        if strike is None:
            # Data honesty: absent beats wrong. The card renders an "MP unavailable" note.
            log.warning(
                "v10_maxpain GUARD TRIPPED underlying=%s expiry=%s tick=%s strikes=%d "
                "chain=[%s, %s] best_candidate=%s — refusing to serve a number",
                underlying, expiry, tick, len(chain), lo, hi,
                curve[0][0] if curve else None,
            )
            return {"status": "unavailable", "symbol": underlying,
                    "max_pain_strike": None, "expiry": str(expiry) if expiry else None,
                    "chain_min": lo, "chain_max": hi, "strikes": len(chain),
                    "note": "Max pain failed its own chain-range guard this tick"}
        spot = float(sr[0]) if (sr and sr[0] is not None) else None
        dist = (strike - spot) if spot is not None else None
        dist_pct = round(dist / spot * 100, 2) if (spot and dist is not None) else None
        return {"status": "ok", "symbol": underlying, "max_pain_strike": strike,
                "spot": spot, "distance": dist, "distance_pct": dist_pct,
                "expiry": str(expiry) if expiry is not None else None,
                "chain_min": lo, "chain_max": hi, "chain_tick": str(tick) if tick else None,
                "top_candidates": [{"strike": k, "payout": float(v)} for k, v in curve]}
    except Exception as e:
        raise HTTPException(500, f"v10_maxpain failed: {e}")


@router.get("/strike_oi")
def v10_strike_oi(symbol: str = "NIFTY", n: int = 7):
    """cc#542: strike-wise Call/Put OI around ATM (+/- n strikes) for the nearest expiry, for
    NIFTY + BANKNIFTY — powers the V8 Index Intel Levels Spine and OI Ladder. Read-only. The
    max-pain marker itself comes from /maxpain (one source of truth); this returns the per-strike
    bars + spot + ATM strike.

    cc#1167 — TWO DEFECTS FIXED, both found from a founder screenshot of the live web dashboard.

    SPOT WAS THE PREVIOUS DAILY CLOSE. It read `SELECT close FROM raw_prices ORDER BY price_date
    DESC LIMIT 1`, which on 20-Aug served 24,078.30 — the 19-Aug close — while cmp_prices held the
    live 24,231.85. The Levels Spine printed that as "SPOT 24,078" and computed every level's
    %-distance from it, on a page showing the live 24,217.9 two cards above. Stale data presented
    as live. Spot now prefers cmp_prices and the payload STATES which basis it used, so a surface
    can never again show a previous close under a live-looking label.

    THE STRIKE SET SPANNED EVERY TICK. `DISTINCT ON (strike, option_type) ... ORDER BY ts DESC`
    ran over the whole expiry, not the latest tick, so a strike that has since dropped out of the
    chain still appeared carrying its last-known OI — the same defect cc#1155 found in max pain.
    It now uses max_pain.LATEST_CHAIN_SQL, so both endpoints read the chain the same way from one
    piece of SQL rather than two that can drift.

    Knock-on worth stating: atm_strike is derived from spot, so a stale spot also mis-centred the
    ATM+/-n window. On 20-Aug it centred on 24,100 instead of 24,250 — a three-strike shift, which
    changes which walls the founder is shown."""
    underlying = (symbol or "NIFTY").upper()
    if underlying in ("NIFTY50", "NIFTY 50"):
        underlying = "NIFTY"
    if underlying in ("BANKNIFTY50", "NIFTYBANK", "BNF"):
        underlying = "BANKNIFTY"
    spot_sym = _MAXPAIN_SPOT.get(underlying, "NIFTY50")
    n = max(1, min(int(n or 7), 25))
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT MIN(expiry) FROM option_chain WHERE underlying=%s AND expiry >= CURRENT_DATE",
                        (underlying,))
            er = cur.fetchone()
            expiry = er[0] if er else None
            # cc#1167: LIVE spot first. The previous close is still a legitimate answer outside
            # market hours, but it is returned LABELLED so the surface can say which it is.
            # NOTE ON updated_at (cc#844 class): it is stored NAIVE IST. Do NOT subtract NOW(),
            # which is UTC — that yields a phantom -330 minutes. The raw stamp is returned and the
            # surface prints it as IST; no age is computed here.
            spot = spot_basis = spot_asof = None
            cur.execute("SELECT cmp, updated_at FROM cmp_prices WHERE symbol=%s", (spot_sym,))
            cr = cur.fetchone()
            if cr and cr[0] is not None:
                spot = float(cr[0]); spot_basis = "live"
                spot_asof = cr[1].isoformat() if cr[1] else None
            else:
                cur.execute("SELECT close, price_date FROM raw_prices WHERE symbol=%s "
                            "ORDER BY price_date DESC LIMIT 1", (spot_sym,))
                sr = cur.fetchone()
                if sr and sr[0] is not None:
                    spot = float(sr[0]); spot_basis = "prev_close"
                    spot_asof = str(sr[1]) if sr[1] else None
            rows = []
            chain_tick = None
            if expiry is not None:
                # cc#1167: ONE latest-chain query, shared with max pain, so the two cannot drift.
                cur.execute(max_pain_mod.LATEST_CHAIN_SQL, {"u": underlying})
                raw = cur.fetchall()
                rows = [(r[0], r[1], r[2]) for r in raw]
                if raw:
                    chain_tick = raw[0][4]
        strikes = [{"strike": float(r[0]), "call_oi": int(r[1] or 0), "put_oi": int(r[2] or 0)} for r in rows]
        atm_strike, window = None, strikes
        if spot is not None and strikes:
            atm_strike = min(strikes, key=lambda x: abs(x["strike"] - spot))["strike"]
            idx = next((i for i, x in enumerate(strikes) if x["strike"] == atm_strike), None)
            if idx is not None:
                window = strikes[max(0, idx - n): idx + n + 1]
        return {"status": "ok" if window else "no_data", "symbol": underlying, "spot": spot,
                # cc#1167: the basis travels WITH the number. A surface that shows spot must be
                # able to say whether it is live or a previous close, without guessing.
                "spot_basis": spot_basis, "spot_asof": spot_asof,
                "chain_tick": str(chain_tick) if chain_tick else None,
                "expiry": str(expiry) if expiry is not None else None, "atm_strike": atm_strike,
                "n": n, "strikes": window}
    except Exception as e:
        raise HTTPException(500, f"v10_strike_oi failed: {e}")


@router.get("/buildup")
def v10_buildup(limit: int = 15):
    """Futures buildup screen — top long (price up) and short (price down) movers
    across stock futures, with OI/basis when the feed is available.
    NOTE: oi/oi_chg/basis are NULL until the OI feed lands (feed pending)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            # cc#819 bug_1 — DAY% was measured from the WRONG ANCHOR. The old `f` CTE took the
            # session's FIRST futures bar (09:15) as the denominator, so the column reported an
            # intraday move from the open, not the day change. DIXON on 31-Jul: last bar 13,581
            # against the 09:15 bar 14,348 = -5.35%, which is exactly the figure the founder saw and
            # which matches no valid day-change basis. Against the previous session's futures close
            # (14,138) it is -3.94%, the audited number.
            #
            # Worth stating precisely, because the fix follows from it: this was NOT a cross-source
            # mix. Numerator and denominator both came from futures_basis. Spot's previous close is
            # 14,338, close to the 14,348 the code used but not equal to it — so the resemblance is
            # a coincidence, and "wrong table" would have been the wrong diagnosis. It is one source
            # with the wrong time anchor.
            #
            # `pf` is the previous SESSION's last futures close, per the universal pricing rule
            # (id=2169): one basis, futures vs futures. A symbol with no prior futures close is
            # dropped rather than silently rebased onto spot — mixing is what caused this.
            cur.execute("""
                WITH sess AS (
                    SELECT MAX(ts::date) AS d FROM futures_basis
                    WHERE ts::time BETWEEN '09:15' AND '15:30'
                ),
                pf AS (
                    SELECT DISTINCT ON (symbol) symbol, futures_close AS pc
                    FROM futures_basis, sess
                    WHERE ts::date < sess.d AND ts::time BETWEEN '09:15' AND '15:30'
                    ORDER BY symbol, ts DESC
                ),
                l AS (
                    SELECT DISTINCT ON (symbol) symbol, futures_close AS c, oi, oi_chg, basis
                    FROM futures_basis, sess
                    WHERE ts::date=sess.d AND ts::time BETWEEN '09:15' AND '15:30'
                    ORDER BY symbol, ts DESC
                ),
                -- cc#819 bug_2: vol_ratio was hardcoded None in _row(), so the Vol column could
                -- never populate — on-market or off. v8_metrics.vol_ratio is the same futures
                -- universe and is already frozen at the last completed session, which is the
                -- cc#719 freeze behaviour the rest of this row follows.
                v AS (
                    SELECT symbol, vol_ratio FROM v8_metrics
                    WHERE score_date = (SELECT MAX(score_date) FROM v8_metrics)
                )
                SELECT l.symbol, l.c AS price,
                       ROUND(((l.c-pf.pc)/NULLIF(pf.pc,0)*100)::numeric,2) AS day_1d,
                       l.oi, l.oi_chg, l.basis, v.vol_ratio
                FROM l JOIN pf USING(symbol)
                LEFT JOIN v ON v.symbol = l.symbol
                WHERE pf.pc IS NOT NULL AND l.c IS NOT NULL
            """)
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(500, f"v10_buildup failed: {e}")

    def _row(r):
        sym, price, day_1d, oi, oi_chg, basis, vol_ratio = r
        day_1d = float(day_1d) if day_1d is not None else None
        oi_chg = int(oi_chg) if oi_chg is not None else None
        # true buildup needs price + OI; classify only when oi_chg is present
        sig = "NEUTRAL"
        if day_1d is not None and oi_chg is not None:
            up = day_1d > 0
            if oi_chg > 0:
                sig = "LONG_BUILD" if up else "SHORT_BUILD"
            elif oi_chg < 0:
                sig = "SHORT_COVER" if up else "LONG_UNWIND"
        return {"symbol": sym, "price": float(price) if price is not None else None,
                "day_1d": day_1d, "oi": int(oi) if oi is not None else None,
                "oi_chg": oi_chg, "basis": float(basis) if basis is not None else None,
                "vol_ratio": float(vol_ratio) if vol_ratio is not None else None,   # cc#819 bug_2
                "signal": sig}

    data = [_row(r) for r in rows if r[2] is not None]
    longs = sorted(data, key=lambda x: x["day_1d"], reverse=True)[:limit]
    shorts = sorted(data, key=lambda x: x["day_1d"])[:limit]
    oi_pending = all(d["oi"] is None for d in data) if data else True
    return {"status": "ok", "long_buildup": longs, "short_buildup": shorts,
            "oi_feed_pending": oi_pending,
            "note": "OI / basis feed pending — classification limited to price move" if oi_pending else None}


@router.get("/divergence")
def v10_divergence(threshold: float = 1.0):
    """cc#124: Futures-vs-Equity divergence scanner across the full F&O universe.
    Flags a symbol when ANY of three live signals exceeds `threshold` (%):
      - basis%        : live futures-vs-spot premium/discount
      - 1hr divergence: futures %move minus spot %move over the last ~60 min
      - 1D divergence : futures %move minus spot %move vs prior session's last bar
    During market hours, symbols whose latest bar is stale (>15 min) or missing
    are skipped. Source: futures_basis (sole feed). OR logic, no token cost."""
    from datetime import datetime, timedelta
    ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    mins = ist_now.hour * 60 + ist_now.minute
    market_hours = (ist_now.weekday() < 5) and (555 <= mins <= 930)
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH sess AS (
                    SELECT MAX(ts::date) AS d FROM futures_basis
                    WHERE ts::time BETWEEN '09:15' AND '15:30'
                ),
                latest AS (
                    SELECT DISTINCT ON (symbol) symbol, ts, spot_close, futures_close, basis_pct
                    FROM futures_basis, sess
                    WHERE ts::date=sess.d
                    ORDER BY symbol, ts DESC
                ),
                hr1 AS (
                    SELECT DISTINCT ON (fb.symbol) fb.symbol,
                           fb.spot_close AS spot_1h, fb.futures_close AS fut_1h
                    FROM futures_basis fb JOIN latest l ON l.symbol=fb.symbol, sess
                    WHERE fb.ts::date=sess.d AND fb.ts <= l.ts - INTERVAL '60 minutes'
                    ORDER BY fb.symbol, fb.ts DESC
                ),
                prevday AS (
                    SELECT DISTINCT ON (symbol) symbol,
                           spot_close AS spot_pd, futures_close AS fut_pd
                    FROM futures_basis, sess
                    WHERE ts::date < sess.d AND ts::time BETWEEN '09:15' AND '15:30'
                    ORDER BY symbol, ts DESC
                )
                SELECT l.symbol, l.basis_pct,
                    ROUND((((l.futures_close-h.fut_1h)/NULLIF(h.fut_1h,0))
                          -((l.spot_close-h.spot_1h)/NULLIF(h.spot_1h,0)))*100,2) AS div_1hr,
                    ROUND((((l.futures_close-p.fut_pd)/NULLIF(p.fut_pd,0))
                          -((l.spot_close-p.spot_pd)/NULLIF(p.spot_pd,0)))*100,2) AS div_1d,
                    l.spot_close, l.futures_close, l.ts,
                    ROUND(EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - l.ts))/60)::int AS age_min
                FROM latest l
                LEFT JOIN hr1 h ON h.symbol=l.symbol
                LEFT JOIN prevday p ON p.symbol=l.symbol
            """)
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(500, f"v10_divergence failed: {e}")

    out, skipped_stale = [], 0
    for r in rows:
        sym, basis, d1h, d1d, spot, fut, ts, age = r
        # NULL-safe staleness skip during market hours
        if market_hours and (age is None or age > 15):
            skipped_stale += 1
            continue
        basis = float(basis) if basis is not None else None
        d1h = float(d1h) if d1h is not None else None
        d1d = float(d1d) if d1d is not None else None
        sigs = []
        if basis is not None and abs(basis) > threshold:
            sigs.append("BASIS")
        if d1h is not None and abs(d1h) > threshold:
            sigs.append("1HR")
        if d1d is not None and abs(d1d) > threshold:
            sigs.append("1D")
        if not sigs:
            continue
        mag = max(abs(basis or 0), abs(d1h or 0), abs(d1d or 0))
        out.append({"symbol": sym, "basis_pct": basis, "div_1hr": d1h,
                    "div_1d": d1d, "spot_ltp": float(spot) if spot is not None else None,
                    "futures_ltp": float(fut) if fut is not None else None,
                    "last_tick": str(ts) if ts is not None else None,
                    "age_min": int(age) if age is not None else None,
                    "signals": sigs, "_mag": mag})
    out.sort(key=lambda x: x["_mag"], reverse=True)
    for d in out:
        d.pop("_mag", None)
    return {"status": "ok", "universe": "F&O", "count": len(out),
            "threshold": threshold, "market_hours": market_hours,
            "skipped_stale": skipped_stale,
            "ts": ist_now.strftime("%Y-%m-%d %H:%M:%S"), "rows": out}
