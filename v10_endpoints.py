"""
v10_endpoints.py — V10 ST+EMA intraday strategy routes (Scorr platform).
Mounted in main.py via: app.include_router(v10_router)
Isolated from V8 / live feed writes. Paper + advisory only.
"""
import os
import psycopg
from typing import Optional
from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/api/v10", tags=["v10"])
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


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
    """Max-pain strike for the nearest expiry — the strike that minimises total
    option-writer payout. Uses the LATEST OI snapshot per (strike, option_type)."""
    underlying = (symbol or "NIFTY").upper()
    if underlying in ("NIFTY50", "NIFTY 50"):
        underlying = "NIFTY"
    spot_sym = _MAXPAIN_SPOT.get(underlying, "NIFTY50")
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH exp AS (
                    SELECT MIN(expiry) AS e FROM option_chain
                    WHERE underlying=%s AND expiry >= CURRENT_DATE
                ),
                oc AS (
                    SELECT DISTINCT ON (strike, option_type) strike, option_type, oi
                    FROM option_chain
                    WHERE underlying=%s AND expiry=(SELECT e FROM exp) AND oi IS NOT NULL
                    ORDER BY strike, option_type, ts DESC
                ),
                spot AS (
                    SELECT close AS s FROM raw_prices WHERE symbol=%s
                    ORDER BY price_date DESC LIMIT 1
                ),
                pain AS (
                    SELECT o.strike, SUM(CASE
                        WHEN o.option_type='CE' THEN o.oi*GREATEST(o.strike-(SELECT s FROM spot),0)
                        WHEN o.option_type='PE' THEN o.oi*GREATEST((SELECT s FROM spot)-o.strike,0)
                        ELSE 0 END) AS total_pain
                    FROM oc o GROUP BY o.strike
                )
                SELECT p.strike, (SELECT s FROM spot) AS spot, (SELECT e FROM exp) AS expiry
                FROM pain p WHERE p.total_pain > 0
                ORDER BY p.total_pain ASC LIMIT 1
            """, (underlying, underlying, spot_sym))
            r = cur.fetchone()
        if not r or r[0] is None:
            return {"status": "no_data", "symbol": underlying,
                    "note": "Option chain data pending"}
        strike = float(r[0]); spot = float(r[1]) if r[1] is not None else None
        dist = (strike - spot) if spot is not None else None
        dist_pct = round(dist / spot * 100, 2) if (spot and dist is not None) else None
        return {"status": "ok", "symbol": underlying, "max_pain_strike": strike,
                "spot": spot, "distance": dist, "distance_pct": dist_pct,
                "expiry": str(r[2]) if r[2] is not None else None}
    except Exception as e:
        raise HTTPException(500, f"v10_maxpain failed: {e}")


@router.get("/buildup")
def v10_buildup(limit: int = 15):
    """Futures buildup screen — top long (price up) and short (price down) movers
    across stock futures, with OI/basis when the feed is available.
    NOTE: oi/oi_chg/basis are NULL until the OI feed lands (feed pending)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                WITH sess AS (
                    SELECT MAX(ts::date) AS d FROM futures_basis
                    WHERE ts::time BETWEEN '09:15' AND '15:30'
                ),
                f AS (
                    SELECT DISTINCT ON (symbol) symbol, futures_close AS o
                    FROM futures_basis, sess
                    WHERE ts::date=sess.d AND ts::time BETWEEN '09:15' AND '15:30'
                    ORDER BY symbol, ts ASC
                ),
                l AS (
                    SELECT DISTINCT ON (symbol) symbol, futures_close AS c, oi, oi_chg, basis
                    FROM futures_basis, sess
                    WHERE ts::date=sess.d AND ts::time BETWEEN '09:15' AND '15:30'
                    ORDER BY symbol, ts DESC
                )
                SELECT f.symbol, l.c AS price,
                       ROUND(((l.c-f.o)/NULLIF(f.o,0)*100)::numeric,2) AS day_1d,
                       l.oi, l.oi_chg, l.basis
                FROM f JOIN l USING(symbol)
                WHERE f.o IS NOT NULL AND l.c IS NOT NULL
            """)
            rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(500, f"v10_buildup failed: {e}")

    def _row(r):
        sym, price, day_1d, oi, oi_chg, basis = r
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
                "vol_ratio": None, "signal": sig}

    data = [_row(r) for r in rows if r[2] is not None]
    longs = sorted(data, key=lambda x: x["day_1d"], reverse=True)[:limit]
    shorts = sorted(data, key=lambda x: x["day_1d"])[:limit]
    oi_pending = all(d["oi"] is None for d in data) if data else True
    return {"status": "ok", "long_buildup": longs, "short_buildup": shorts,
            "oi_feed_pending": oi_pending,
            "note": "OI / basis feed pending — classification limited to price move" if oi_pending else None}
