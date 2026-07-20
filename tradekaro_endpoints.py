"""
tradekaro_endpoints.py — cc#569 (Trade Karo dabba book surface)
================================================================
Dedicated READ surface for the founder's Trade Karo book — a dabba (TradeKaro platform) book
kept SEPARATE from SmartGain MHK40 and the lot-based client books:

  * ~Rs 60k real capital, 50x limit = Rs 30,00,000 (30L) buying power, Rs 35L ceiling.
  * Goal Rs 6,000 / week. ZERO brokerage + ZERO interest -> Gross = Net (no 3-line split).
  * Positions in client_positions WHERE client='TRADEKARO', is_dabba=true, raw 1-unit qty.

CC owns the READ surface only; position writes stay Claude-web (typed founder updates via SQL),
same division of labour as the other client books (id=2102 / data_note on cc#569).

Endpoints:
  GET /api/tradekaro/book — positions valued LIVE (fut-LTP-first -> synthetic spot+basis ->
      guardrail, IDENTICAL pricing rule to SmartGain /api/smartgain/m2m so MTM never drifts),
      limit-utilisation dial (deployed Rs + % of 30L, per-position % too, return-on-limit),
      weekly P&L vs the Rs 6k goal (realised this week + open MTM, Gross=Net), concentration
      flag (any single position > ~50% of deployed) and earnings-blackout badges (symbol reports
      within IST-today / +1, read from earnings_calendar — the manual book's only blackout guard).
  GET /tradekaro — the HTML surface (scorr_tradekaro.html).

Mounted in main.py via app.include_router(tradekaro_router); page gated + PWA-injected + nav-registered.
"""
import os
from datetime import datetime, timedelta, time as dt_time

import psycopg
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from nse_holidays import is_trading_day

router = APIRouter()
DATABASE_URL = os.getenv("DATABASE_URL")

CLIENT       = "TRADEKARO"
LIMIT        = 3_000_000        # 50x on ~Rs 60k = Rs 30L buying power (the utilisation denominator)
CEILING      = 3_500_000        # Rs 35L hard ceiling
WEEKLY_GOAL  = 6_000            # Rs 6,000 / week target
CONC_FRAC    = 0.50             # single position > 50% of deployed -> concentration flag


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ist_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _market_open_ist() -> bool:
    """True only during a real NSE session — trading day + 09:15-15:30 IST. Off-hours the surface
    serves the last futures session bar, never a stale/phantom tick (same rule as SmartGain)."""
    now = _ist_now()
    return is_trading_day(now.date()) and dt_time(9, 15) <= now.time() <= dt_time(15, 30)


def _monday(d):
    return d - timedelta(days=d.weekday())


# Live-LTP CTE — byte-identical pricing logic to /api/smartgain/m2m (fut-live <=10min -> off-hours
# fut_eod -> synthetic spot+basis -> spot_only -> eod), so the Trade Karo book values on exactly the
# same rule as every other Scorr book. Only the position source (client_positions) differs.
_BOOK_SQL = """
WITH open_book AS (
    SELECT id, symbol, direction, qty, entry_price, source_tag
    FROM client_positions
    WHERE client = %(client)s AND status = 'OPEN'
)
SELECT
    h.id, h.symbol, h.direction, h.qty,
    ROUND(h.entry_price::numeric, 2)                            AS entry_price,
    ROUND(lp.live_ltp::numeric, 2)                             AS ltp,
    ROUND(
        CASE h.direction
            WHEN 'LONG'  THEN (lp.live_ltp - h.entry_price) * h.qty
            WHEN 'SHORT' THEN (h.entry_price - lp.live_ltp) * h.qty
            ELSE 0
        END::numeric, 2)                                        AS mtm,
    lp.pricing_method, lp.is_live,
    ROUND(lp.ltp_age_min::numeric, 1)                          AS ltp_age_min,
    lp.last_tick, lp.fut_ever_existed,
    h.source_tag,
    -- earnings-blackout: does this symbol report within IST-today / +1? Manual book => the UI is the
    -- only guard. Ticker normalised (BAJAJ-AUTO/M&M) against the position symbol (BAJAJAUTO/M&M).
    (SELECT jsonb_build_object('ex_date', ec.ex_date, 'event_type', ec.event_type, 'company', ec.company_name)
       FROM earnings_calendar ec
      WHERE UPPER(REPLACE(REPLACE(ec.ticker, '-', ''), '&', '')) =
            UPPER(REPLACE(REPLACE(h.symbol, '-', ''), '&', ''))
        AND ec.status = 'upcoming'
        AND ec.ex_date BETWEEN (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                           AND (NOW() AT TIME ZONE 'Asia/Kolkata')::date + 1
      ORDER BY ec.ex_date LIMIT 1)                             AS earnings
FROM open_book h
LEFT JOIN LATERAL (
    SELECT
        CASE
            WHEN c.fut_close IS NOT NULL
             AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0 <= 10 THEN 'fut_live'
            WHEN c.fut_close IS NOT NULL AND NOT %(mkt_open)s THEN 'fut_eod'
            WHEN c.spot_ltp IS NOT NULL AND c.basis IS NOT NULL THEN 'synthetic'
            WHEN c.spot_ltp IS NOT NULL THEN 'spot_only'
            WHEN c.eod_close IS NOT NULL THEN 'eod'
            ELSE NULL
        END AS pricing_method,
        CASE
            WHEN c.fut_close IS NOT NULL
             AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0 <= 10 THEN c.fut_close
            WHEN c.fut_close IS NOT NULL AND NOT %(mkt_open)s THEN c.fut_close
            WHEN c.spot_ltp IS NOT NULL AND c.basis IS NOT NULL THEN c.spot_ltp + c.basis
            WHEN c.spot_ltp IS NOT NULL THEN c.spot_ltp
            ELSE c.eod_close
        END AS live_ltp,
        CASE
            WHEN c.fut_close IS NOT NULL
             AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0 <= 10
                THEN EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0
            WHEN c.fut_close IS NOT NULL AND NOT %(mkt_open)s
                THEN EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0
            WHEN c.spot_ltp IS NOT NULL AND c.basis IS NOT NULL
                THEN EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.basis_ts))/60.0
            WHEN c.spot_ltp IS NOT NULL
                THEN EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.spot_ts))/60.0
            ELSE EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.eod_ts))/60.0
        END AS ltp_age_min,
        CASE
            WHEN c.fut_close IS NOT NULL
             AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0 <= 10 THEN true
            WHEN c.fut_close IS NOT NULL AND NOT %(mkt_open)s THEN false
            WHEN c.spot_ltp IS NOT NULL AND c.basis IS NOT NULL
                THEN EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.basis_ts))/60.0 <= 30
            ELSE false
        END AS is_live,
        COALESCE(
            CASE WHEN c.fut_close IS NOT NULL
             AND EXTRACT(EPOCH FROM ((NOW() AT TIME ZONE 'Asia/Kolkata') - c.fut_ts))/60.0 <= 10 THEN c.fut_ts END,
            CASE WHEN c.fut_close IS NOT NULL AND NOT %(mkt_open)s THEN c.fut_ts END,
            CASE WHEN c.spot_ltp IS NOT NULL AND c.basis IS NOT NULL THEN c.basis_ts END,
            CASE WHEN c.spot_ltp IS NOT NULL THEN c.spot_ts END,
            c.eod_ts
        ) AS last_tick,
        c.fut_ever_existed
    FROM (
        SELECT
            (SELECT ip.close FROM intraday_prices ip
              WHERE ip.symbol = h.symbol AND ip.source = 'fyers_fut'
                AND EXTRACT(DOW FROM ip.ts) BETWEEN 1 AND 5
                AND ip.ts::time >= TIME '09:15' AND ip.ts::time < TIME '15:30'
              ORDER BY ip.ts DESC LIMIT 1)                      AS fut_close,
            (SELECT ip.ts FROM intraday_prices ip
              WHERE ip.symbol = h.symbol AND ip.source = 'fyers_fut'
                AND EXTRACT(DOW FROM ip.ts) BETWEEN 1 AND 5
                AND ip.ts::time >= TIME '09:15' AND ip.ts::time < TIME '15:30'
              ORDER BY ip.ts DESC LIMIT 1)                      AS fut_ts,
            (SELECT cp.cmp::numeric FROM cmp_prices cp WHERE cp.symbol = h.symbol) AS spot_ltp,
            (SELECT cp.updated_at FROM cmp_prices cp WHERE cp.symbol = h.symbol)   AS spot_ts,
            (SELECT fb.basis FROM futures_basis fb WHERE fb.symbol = h.symbol
              ORDER BY fb.ts DESC LIMIT 1)                       AS basis,
            (SELECT fb.ts FROM futures_basis fb WHERE fb.symbol = h.symbol
              ORDER BY fb.ts DESC LIMIT 1)                       AS basis_ts,
            (SELECT ip.close FROM intraday_prices ip WHERE ip.symbol = h.symbol
              ORDER BY ip.ts DESC LIMIT 1)                       AS eod_close,
            (SELECT ip.ts FROM intraday_prices ip WHERE ip.symbol = h.symbol
              ORDER BY ip.ts DESC LIMIT 1)                       AS eod_ts,
            EXISTS(SELECT 1 FROM intraday_prices ip4
                    WHERE ip4.symbol = h.symbol AND ip4.source = 'fyers_fut' LIMIT 1) AS fut_ever_existed
    ) c
) lp ON true
ORDER BY h.id
"""


def _week_realised(cur, week_start):
    """Realised Trade Karo P&L booked this week (client_closed, pnl precomputed by Claude web)."""
    cur.execute("""
        SELECT COALESCE(SUM(pnl), 0)::numeric
        FROM client_closed
        WHERE client = %s AND close_date >= %s
    """, (CLIENT, week_start))
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else 0.0


@router.get("/api/tradekaro/book")
def tradekaro_book():
    """Live Trade Karo book: positions + limit utilisation (over 30L) + weekly P&L vs Rs 6k
    (Gross=Net, zero brokerage) + concentration flag + earnings badges. Read-only."""
    try:
        mkt_open = _market_open_ist()
        today = _ist_now().date()
        week_start = _monday(today)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(_BOOK_SQL, {"client": CLIENT, "mkt_open": mkt_open})
            cols = [d[0] for d in cur.description]
            raw = [dict(zip(cols, r)) for r in cur.fetchall()]
            realised_week = _week_realised(cur, week_start)

        positions = []
        for row in raw:
            entry = float(row["entry_price"]) if row["entry_price"] is not None else None
            qty   = int(row["qty"]) if row["qty"] is not None else None
            ltp   = float(row["ltp"]) if row["ltp"] is not None else None
            mtm   = float(row["mtm"]) if row["mtm"] is not None else None
            method = row["pricing_method"]
            age = float(row["ltp_age_min"]) if row["ltp_age_min"] is not None else None
            fut_ever = bool(row.get("fut_ever_existed"))

            # Same guardrail as SmartGain: never present spot_only/eod as real MTM when the symbol
            # has no live futures feed or the tick is >24h stale; null out rather than mislead.
            reason = None
            if method in ("spot_only", "eod"):
                if not fut_ever:
                    reason = "no_live_futures_feed"
                elif age is not None and age > 24 * 60:
                    reason = f"stale_data_{round(age / 1440)}d"
                if reason:
                    method, ltp, mtm = "unavailable", None, None
            elif method is None:
                reason, method = "no_data", "unavailable"

            invested = round(entry * qty, 2) if (entry is not None and qty is not None) else None
            deployed = round(ltp * qty, 2) if (ltp is not None and qty is not None) else None
            mtm_pct  = round(mtm / invested * 100, 2) if (mtm is not None and invested) else None
            pct_limit = round(deployed / LIMIT * 100, 2) if deployed is not None else None

            earnings = row["earnings"]
            earn_badge = None
            if earnings:
                ex = earnings.get("ex_date")
                days_away = None
                if ex:
                    try:
                        exd = ex if hasattr(ex, "toordinal") else datetime.strptime(str(ex), "%Y-%m-%d").date()
                        days_away = (exd - today).days
                    except Exception:
                        days_away = None
                earn_badge = {"ex_date": str(ex) if ex else None,
                              "event_type": earnings.get("event_type"),
                              "days_away": days_away,
                              "label": "Reports today" if days_away == 0 else "Reports tomorrow"}

            positions.append({
                "id": row["id"], "symbol": row["symbol"], "direction": row["direction"],
                "qty": qty, "entry_price": entry, "cmp": ltp,
                "invested_value": invested, "deployed_value": deployed,
                "mtm": mtm, "mtm_pct": mtm_pct, "pct_of_limit": pct_limit,
                "pricing_method": method, "reason": reason,
                "is_live": bool(row["is_live"]) and method not in ("unavailable",),
                "ltp_age_min": age,
                "last_tick": row["last_tick"].isoformat() if row["last_tick"] else None,
                "source_tag": row["source_tag"],
                "earnings": earn_badge,
            })

        # ── limit utilisation dial ──
        deployed_total = round(sum(p["deployed_value"] or 0 for p in positions), 2)
        limit_pct      = round(deployed_total / LIMIT * 100, 2) if LIMIT else None

        # ── weekly P&L (Gross = Net; ZERO brokerage/interest) ──
        open_mtm    = round(sum(p["mtm"] or 0 for p in positions), 2)
        weekly_pnl  = round(realised_week + open_mtm, 2)
        goal_pct    = round(weekly_pnl / WEEKLY_GOAL * 100, 1) if WEEKLY_GOAL else None
        return_on_limit = round(weekly_pnl / LIMIT * 100, 3) if LIMIT else None

        # ── concentration flag (single position > 50% of deployed) ──
        concentration = {"flagged": False, "symbol": None, "pct": None}
        if deployed_total > 0:
            top = max(positions, key=lambda p: p["deployed_value"] or 0)
            top_pct = round((top["deployed_value"] or 0) / deployed_total * 100, 1)
            concentration = {"flagged": top_pct > CONC_FRAC * 100,
                             "symbol": top["symbol"], "pct": top_pct,
                             "threshold_pct": int(CONC_FRAC * 100)}

        any_live = any(p["is_live"] for p in positions)
        last_updated = max((p["last_tick"] for p in positions if p["last_tick"]), default=None)

        return {
            "book": "TRADEKARO", "is_dabba": True, "zero_brokerage": True,
            "positions": positions, "position_count": len(positions),
            "limit": {
                "buying_power": LIMIT, "ceiling": CEILING,
                "deployed": deployed_total, "deployed_pct": limit_pct,
                "headroom": round(LIMIT - deployed_total, 2),
            },
            "weekly": {
                "week_start": str(week_start),
                "realised": realised_week, "open_mtm": open_mtm,
                "gross": weekly_pnl, "net": weekly_pnl,   # Gross = Net (no costs)
                "brokerage": 0.0,
                "goal": WEEKLY_GOAL, "goal_pct": goal_pct,
                "return_on_limit_pct": return_on_limit,
            },
            "concentration": concentration,
            "market_open": mkt_open,
            "last_updated": last_updated,
            "data_source": "live_fyers" if any_live else "last_session",
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/tradekaro", response_class=HTMLResponse)
def tradekaro_page():
    """Trade Karo dabba book — gated (scorr_auth PROTECTED set), isolated from the MHK40 surface."""
    with open("scorr_tradekaro.html", "r", encoding="utf-8") as f:
        return f.read()
